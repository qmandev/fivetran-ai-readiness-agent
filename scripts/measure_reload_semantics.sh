#!/usr/bin/env bash
# Probe semantics of POST /v1/connections/{id}/schemas/reload — checklist G3.
#
# Unknowns being measured:
#   • Sync vs async — does the HTTP call block until the reload completes?
#   • Wall-clock duration of the call.
#   • Does reload trigger a downstream DATA sync, or just refresh metadata?
#   • Response shape (full schema config? Just status?)
#
# We test with `exclude_mode=PRESERVE` (the default — non-destructive; keeps
# selected schemas/tables enabled as they are). EXCLUDE mode would disable
# previously-unselected columns and is destructive — not what we want to
# verify here.
#
# Reads FIVETRAN_API_KEY/SECRET from deploy/.env. Takes optional connection
# ID as $1; otherwise auto-picks the first connection in the account.

set -uo pipefail

API="https://api.fivetran.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT/deploy/.env"

if [ -z "${FIVETRAN_API_KEY:-}" ] || [ -z "${FIVETRAN_API_SECRET:-}" ]; then
  if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
fi
: "${FIVETRAN_API_KEY:?ERROR: FIVETRAN_API_KEY not set}"
: "${FIVETRAN_API_SECRET:?ERROR: FIVETRAN_API_SECRET not set}"

AUTH="$FIVETRAN_API_KEY:$FIVETRAN_API_SECRET"

# ── 1. Resolve connection ID ────────────────────────────────────────────────
# Auto-pick filters out `fivetran_log` (Fivetran's auto-injected internal
# metadata connector — present in every account, never the user-relevant
# one). Pass the connection_id explicitly as $1 to override.
CONNECTION_ID="${1:-}"
if [ -z "$CONNECTION_ID" ]; then
  echo "Listing connections to auto-pick (skipping fivetran_log)..."
  curl -s -u "$AUTH" "$API/v1/connections" \
    | python3 -c '
import json, sys
items = json.load(sys.stdin).get("data", {}).get("items", [])
for i in items:
    print(f"  - {i.get(\"id\",\"?\"):28s} service={i.get(\"service\",\"?\")}")' >&2
  CONNECTION_ID=$(curl -s -u "$AUTH" "$API/v1/connections" \
    | python3 -c '
import json, sys
items = json.load(sys.stdin).get("data", {}).get("items", [])
user_items = [i for i in items if i.get("service") != "fivetran_log"]
if not user_items:
    sys.exit("No user connections found (only fivetran_log present?)")
print(user_items[0]["id"])')
  echo "Auto-picked connection_id: $CONNECTION_ID"
fi

# ── 2. Pre-state ────────────────────────────────────────────────────────────
echo
echo "=== Pre-state ==="
echo "--- connection details ---"
curl -s -u "$AUTH" "$API/v1/connections/$CONNECTION_ID" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin).get("data", {})
for k in ("id", "service", "schema", "paused", "sync_frequency",
         "status", "succeeded_at", "failed_at", "data_delay_sensitivity"):
    if k in d: print(f"  {k}: {d[k]}")
status = d.get("status", {})
if isinstance(status, dict):
    for k in ("setup_state", "sync_state", "update_state", "is_historical_sync"):
        if k in status: print(f"  status.{k}: {status[k]}")'

# ── 3. Time the reload ──────────────────────────────────────────────────────
echo
echo "=== POST /v1/connections/$CONNECTION_ID/schemas/reload (exclude_mode=PRESERVE) ==="
START=$(date +%s)
START_UTC=$(date -u +"%Y-%m-%d %H:%M:%S")
echo "Started:   $START_UTC UTC"

RESP_FILE="$(mktemp)"
HTTP_CODE=$(curl -s -o "$RESP_FILE" -w '%{http_code}' \
  -u "$AUTH" \
  -X POST "$API/v1/connections/$CONNECTION_ID/schemas/reload" \
  -H 'Content-Type: application/json' \
  -d '{"exclude_mode":"PRESERVE"}')

END=$(date +%s)
END_UTC=$(date -u +"%Y-%m-%d %H:%M:%S")
ELAPSED=$((END - START))
echo "Completed: $END_UTC UTC"
echo "HTTP status: $HTTP_CODE"
echo "Wall-clock:  ${ELAPSED}s"

# ── 4. Response shape ───────────────────────────────────────────────────────
echo
echo "=== Response shape ==="
echo "Full response saved to: $RESP_FILE"
echo "--- head ---"
python3 -m json.tool < "$RESP_FILE" | head -40 || cat "$RESP_FILE"
echo "..."
RESP_BYTES=$(wc -c < "$RESP_FILE")
TABLE_COUNT=$(python3 -c '
import json
try:
    d = json.load(open("'"$RESP_FILE"'"))
    schemas = d.get("data", {}).get("schemas", {})
    total = sum(len(s.get("tables", {})) for s in schemas.values())
    print(total)
except Exception:
    print("?")')
echo "--- summary ---"
echo "  response size: ${RESP_BYTES} bytes"
echo "  tables in response: ${TABLE_COUNT}"

# ── 5. Post-state ───────────────────────────────────────────────────────────
echo
echo "=== Post-state (immediately after reload returned) ==="
echo "--- connection details ---"
curl -s -u "$AUTH" "$API/v1/connections/$CONNECTION_ID" \
  | python3 -c '
import json, sys
d = json.load(sys.stdin).get("data", {})
for k in ("id", "succeeded_at", "failed_at"):
    if k in d: print(f"  {k}: {d[k]}")
status = d.get("status", {})
if isinstance(status, dict):
    for k in ("setup_state", "sync_state", "update_state", "is_historical_sync"):
        if k in status: print(f"  status.{k}: {status[k]}")'

# ── 6. Findings prompt ──────────────────────────────────────────────────────
echo
echo "=== Findings to record ==="
echo "  Wall-clock duration of POST .../schemas/reload : ${ELAPSED}s"
echo "  HTTP status returned                            : $HTTP_CODE"
echo "  Sync-state post-reload (from above)             : check status.sync_state"
echo "    - if 'syncing' → reload TRIGGERS a downstream sync (async)"
echo "    - if 'scheduled' → reload only refreshes metadata; next sync runs on schedule"
echo "  Response payload                                : ${RESP_BYTES} bytes, ${TABLE_COUNT} tables described"
echo "    - large payload → reload returns full schema config synchronously (one-shot)"
echo "    - small payload (just status) → reload is async; full schema fetched later"

rm -f "$RESP_FILE.tmp" 2>/dev/null || true
