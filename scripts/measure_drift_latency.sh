#!/usr/bin/env bash
# Drift-latency measurement harness. Codifies the manual sequence used for
# checklist G1 (detection latency) and reusable for G2 (type promotion) and
# any other scenario. Serves checklist H (repeatable test harness).
#
# Each scenario lives in tests/drift_scenarios/<name>/ with:
#   apply.sql    PG-side DDL + DML (must end in `SELECT now() AS source_change_complete;`)
#   detect.sql   BQ predicate returning an integer count; >0 = detected
#   cleanup.sql  optional reset — NOT invoked by this script; run manually (see below)
#
# ── Cleanup behavior (applies to every scenario) ────────────────────────────
# Cleanup is OPTIONAL and source-side only. Skip it if the next scenario
# targets a different table/column — `cleanup.sql` exists for tidiness, not
# correctness.
#
# CRITICAL: Fivetran soft-drops at the destination. When source `ALTER TABLE
# ... DROP COLUMN` runs, Fivetran does NOT physically remove the column from
# BigQuery. New rows get NULL for it; the column persists in BQ
# `INFORMATION_SCHEMA`. The cleanup.sql only resets the SOURCE side.
#
# To fully remove a column from BigQuery, you need one of:
#   (a) Fivetran MCP `delete_connection_column_config` — surgical, agent-grade
#   (b) Reload connection schema config + force a sync — heavier
#   (c) Drop the BQ column directly via `bq` — bypasses Fivetran (not recommended)
# The agent's v1 remediation path will use (a) when applying drops via MCP.
#
# Safe run pattern (password never lands in shell history):
#   read -rsp "PG root password: " PGPASSWORD; export PGPASSWORD; echo
#   cat tests/drift_scenarios/<name>/cleanup.sql \
#     | gcloud sql connect ftar-pg --user=postgres --database=appdb
#   unset PGPASSWORD
# No need to click `Sync Now` for cleanup unless you want to observe the
# next sync reflect the source-side change — BQ will still show the column.
#
# Run as:
#   bash scripts/measure_drift_latency.sh \
#     add_column_propagation \
#     tests/drift_scenarios/add_column_propagation/apply.sql \
#     tests/drift_scenarios/add_column_propagation/detect.sql
#
# Knobs (env vars):
#   POLL_SECS     polling interval, default 30
#   TIMEOUT_SECS  give-up bound, default 1200 (20 min — covers Mode A worst case)
#   PG_INSTANCE   Cloud SQL instance name, default ftar-pg
#   PG_DATABASE   target database, default appdb
#
# The script prompts for the Postgres root password with `read -rsp`
# (no echo, never lands in shell history). It does NOT trigger the Fivetran
# sync — that's a manual click in the dashboard's Status tab. The script
# pauses until you confirm the click, so the recorded T_sync_trigger is the
# exact moment polling begins.
#
# ── Known caveat: missing DETECTED block ────────────────────────────────────
# Observed during the G2 (type_promotion_propagation) run on 2026-05-21: the
# `=== Polling BQ every 30s ===` header printed but no T+Xs poll lines or
# DETECTED block followed in the captured terminal output (root cause
# undetermined — possible stdout buffering, premature scroll-loss, or
# Ctrl+C). The Fivetran Status-tab sync log (`Successful sync ends`
# timestamp) is an authoritative fallback for T_detected and is actually
# more precise than this script's 30s polling resolution. If you can't find
# a DETECTED block in your scrollback after a run, use Fivetran's sync
# completion time minus T_sync_trigger as the latency.

set -euo pipefail

SCENARIO="${1:-}"
APPLY_SQL="${2:-}"
DETECT_SQL="${3:-}"
POLL_SECS="${POLL_SECS:-30}"
TIMEOUT_SECS="${TIMEOUT_SECS:-1200}"
PG_INSTANCE="${PG_INSTANCE:-ftar-pg}"
PG_DATABASE="${PG_DATABASE:-appdb}"

if [ -z "$SCENARIO" ] || [ -z "$APPLY_SQL" ] || [ -z "$DETECT_SQL" ]; then
  echo "Usage: bash $0 <scenario-name> <apply.sql> <detect.sql>"
  echo "       (env: POLL_SECS, TIMEOUT_SECS, PG_INSTANCE, PG_DATABASE)"
  exit 2
fi
[ -f "$APPLY_SQL" ]  || { echo "apply.sql not found: $APPLY_SQL"; exit 2; }
[ -f "$DETECT_SQL" ] || { echo "detect.sql not found: $DETECT_SQL"; exit 2; }

DETECT_BODY="$(cat "$DETECT_SQL")"

# --- Step 1: Apply source change ---------------------------------------------
echo "=== Scenario: $SCENARIO ==="
echo "Applying source change from $APPLY_SQL ..."
read -rsp "Postgres root password: " PGPASSWORD
export PGPASSWORD
echo
cat "$APPLY_SQL" | gcloud sql connect "$PG_INSTANCE" --user=postgres --database="$PG_DATABASE"
unset PGPASSWORD
echo

# --- Step 2: Pause for manual Sync Now ---------------------------------------
echo "=== Trigger Fivetran sync ==="
echo "Go to Fivetran dashboard → connector → click 'Sync Now'."
read -rp "Press ENTER the instant you click Sync Now... " _
SYNC_START_EPOCH=$(date +%s)
T_SYNC_TRIGGER_UTC=$(date -u +"%Y-%m-%d %H:%M:%S")
echo "T_sync_trigger = ${T_SYNC_TRIGGER_UTC} UTC"
echo

# --- Step 3: Poll BQ until detection or timeout ------------------------------
echo "=== Polling BQ every ${POLL_SECS}s (timeout ${TIMEOUT_SECS}s) ==="
while true; do
  ELAPSED=$(( $(date +%s) - SYNC_START_EPOCH ))
  if [ "$ELAPSED" -ge "$TIMEOUT_SECS" ]; then
    echo "TIMEOUT at T+${ELAPSED}s — no detection within ${TIMEOUT_SECS}s."
    exit 1
  fi
  RESULT=$(bq query --location=us-east1 --use_legacy_sql=false \
                    --format=csv --quiet "$DETECT_BODY" 2>/dev/null | tail -1 | tr -d '[:space:]')
  echo "T+${ELAPSED}s  detect=${RESULT}"
  if [[ "$RESULT" =~ ^[0-9]+$ ]] && [ "$RESULT" -gt 0 ]; then
    T_DETECTED_UTC=$(date -u +"%Y-%m-%d %H:%M:%S")
    echo
    echo "=== DETECTED ==="
    echo "  Scenario           : $SCENARIO"
    echo "  T_sync_trigger     : $T_SYNC_TRIGGER_UTC UTC"
    echo "  T_detected         : $T_DETECTED_UTC UTC"
    echo "  Propagation latency: ${ELAPSED}s  (poll resolution ${POLL_SECS}s)"
    echo
    echo "Cleanup: run the scenario's cleanup.sql manually if/when desired."
    exit 0
  fi
  sleep "$POLL_SECS"
done
