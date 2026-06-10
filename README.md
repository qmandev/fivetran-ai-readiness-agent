# Fivetran AI-Readiness Agent

> Fivetran lands the data. We make sure it stays AI-ready — detecting schema drift,
> classifying blast radius, gating every fix behind human approval, surfacing freshness
> SLA status, and grading every connection on its readiness for downstream AI.

📐 [`DESIGN.md`](DESIGN.md) — architecture decisions and design rationale ·
🧪 [`TEST.md`](TEST.md) — live test results and empirical findings

An AI agent that keeps Fivetran-loaded warehouse data fit for downstream AI consumers.

## What It Does

The agent delivers three tracks of capability, all live:

### Track 1 — Schema-Drift Detection & HITL Remediation

A Cloud Run webhook receiver listens for Fivetran `sync_end` events. On every sync it
snapshots the landed BigQuery schema, diffs it against the prior baseline, and hash-gates
on no-change (cheap exit — 99% of syncs produce nothing). When drift is detected:

1. **Classify** — each change (RENAME / TYPE_PROMOTION / REORDER / NEW_FIELD / DEPRECATION)
   goes to Gemini Flash, which assigns a change type, confidence score, blast-radius
   rationale, and a BigQuery VIEW-shim SQL that preserves the downstream contract.
2. **Propose** — the finding is written to `drift_events` as a `PROPOSED` row with the
   full audit trail.
3. **Review** — the ADK agent surfaces PROPOSED rows in the Agent Runtime playground. The
   reviewer approves or rejects in natural language.
4. **Remediate** — on approval, the agent calls the Fivetran MCP to register the VIEW shim
   as a Fivetran-managed transformation. Every write tool is gated by
   `require_confirmation=True` — the agent cannot touch production without an explicit
   human signal.
5. **Verify** — the reviewer marks the event VERIFIED. `drift_events` becomes a complete
   audit trail from detection to resolution.

### Track 2 — Multi-Connection Support & Freshness SLA Monitor

Each Fivetran connection resolves to its own BigQuery dataset via the Fivetran REST API
(`GET /v1/connectors/{id}`), cached in-process. No redeployment needed when new connections
are added.

Every successful `sync_end` is recorded in `sync_log` before the hash gate, so freshness
data is captured even when the schema is unchanged. `check_freshness_sla` and
`list_freshness_status` let you ask "is my data fresh enough?" for a single connection or
the entire fleet, with a per-call SLA threshold override for downstream consumers with
tighter latency requirements.

### Track 3 — Gemini-Powered AI-Readiness Analysis

Eight new FunctionTools, each addressing a specific AI-readiness gap from Fivetran's research:

| Tool | What it answers |
|---|---|
| `score_ai_readiness` | What is this connection's AI-readiness grade (A–F)? |
| `list_readiness_scores` | Which connections are the least AI-ready, and why? |
| `analyze_drift_volatility` | Which pipelines are STABLE vs VOLATILE vs CRITICAL? |
| `generate_schema_docs` | What does each column mean, in plain English? |
| `classify_column_sensitivity` | Which columns are PII / FINANCIAL / HEALTH, and how should they be masked? |
| `list_sensitive_columns` | What sensitive data is exposed across my entire fleet? |
| `audit_use_case_coverage` | Do I have the data I need to build this AI use case? What's missing? |
| `detect_json_columns` | Which columns hold semi-structured data that should be flattened? |
| `generate_json_flattener` | Generate a BigQuery VIEW to flatten this JSON column into typed columns. |
| `detect_entity_overlaps` | Are the same real-world entities siloed across multiple connections? |
| `diagnose_sync_failures` | Why is this pipeline failing, and how do I fix it? |

Built with **Google ADK 1.x** · **Gemini Flash** · **Gemini Enterprise Agent Platform** ·
**BigQuery** · the official [Fivetran MCP server](https://github.com/fivetran/fivetran-mcp).

## Status — Track 3 Complete (2026-06-08)

| Component | State |
|---|---|
| Detection pipeline (webhook → sync_log → snapshot → diff → classify → drift_events) | ✅ Live |
| Multi-connection resolver (`connection_id → BQ dataset` via Fivetran REST API) | ✅ Live (Track 2) |
| Freshness SLA monitor (`sync_log` + `check_freshness_sla` / `list_freshness_status`) | ✅ Live (Track 2) |
| HITL agent flow (PROPOSED → APPROVED → APPLIED → VERIFIED) | ✅ Live — both events driven to VERIFIED 2026-05-26 |
| Write-tool confirmation gate (`require_confirmation=True`) | ✅ Verified — `adk_request_confirmation` event fires on Agent Runtime |
| Agent Runtime deployment | ✅ Live at `reasoningEngines/2248457298336808960` (us-east1) |
| Webhook receiver | ✅ Live at Cloud Run `fivetran-sync-end-receiver` (us-east1), min-instances=1 |
| Eval suite | ✅ 7/7 cases passing `tool_trajectory_avg_score=1.0` |
| Unit tests | ✅ 251/251 passing |
| Track 3 AI-readiness scoring (`score_ai_readiness`, `list_readiness_scores`, `analyze_drift_volatility`) | ✅ (Track 3 Phase 1) |
| Track 3 Schema intelligence (`generate_schema_docs`, `classify_column_sensitivity`, `list_sensitive_columns`) | ✅ (Track 3 Phase 2) |
| Track 3 Use-case auditor (`audit_use_case_coverage`) | ✅ (Track 3 Phase 2) |
| Track 3 JSON flattener (`detect_json_columns`, `generate_json_flattener`) | ✅ (Track 3 Phase 3) |
| Track 3 Entity overlap detector (`detect_entity_overlaps`, single-connection catalog + cross-connection modes) | ✅ (Track 3 Phase 3) |
| Track 3 Failure diagnosis (`diagnose_sync_failures`, live API fallback) | ✅ (Track 3 Phase 4) |

## Layout

Canonical `agents-cli create --prototype --adk` structure. `[gen]` = template-generated,
`[port]` = project code, `[infra]` = non-template infrastructure.

```
pyproject.toml              [gen+port] deps (google-adk 1.x, bigquery, fivetran-mcp,
                                            secret-manager, functions-framework)
agents-cli-manifest.yaml    [gen+port] deployment target = agent_runtime (us-east1)
uv.lock                     [gen] committed for reproducibility
CLAUDE.md                   [gen] coding-agent guidance
app/
  __init__.py               [gen] exports `app`
  agent.py                  [port] root_agent + App; 17 FunctionTools (Track 2 drift lifecycle +
                                   freshness + Track 3 readiness/scoring/schema/sensitivity/
                                   auditor/flattener/entity/failure tools);
                                   McpToolset split (read/write);
                                   _secret_or_env() for Agent Runtime credential fallback
  agent_runtime_app.py      [gen] Agent Runtime entrypoint
  system_instructions.md    [port] loaded as instruction=; schema_file patterns +
                                   freshness SLA + Track 3 tool guidance
  app_utils/                [gen] telemetry.py, typing.py
  tools/
    bigquery_query.py       [port] state-table CRUD; list_proposed_drift_events;
                                   write_sync_log, check_freshness_sla,
                                   list_freshness_status (Track 2);
                                   _fetch_schema_for_connection shared helper (Track 3)
    snapshot_diff.py        [port] capture_and_gate, content_hash, diff_columns
    classify_drift.py       [port] Gemini classifier + remediation SQL generator
    readiness_score.py      [port] Track 3 — score_ai_readiness, list_readiness_scores,
                                   analyze_drift_volatility; shared _call_gemini +
                                   _extract_json helpers reused by all Track 3 tools
    schema_docs.py          [port] Track 3 — generate_schema_docs
    sensitivity_classifier.py [port] Track 3 — classify_column_sensitivity, list_sensitive_columns
    use_case_auditor.py     [port] Track 3 — audit_use_case_coverage (2-phase Gemini)
    json_flattener.py       [port] Track 3 — detect_json_columns, generate_json_flattener;
                                   writes audit rows to json_flattener_log
    entity_detector.py      [port] Track 3 — detect_entity_overlaps; writes to entity_map
    failure_diagnosis.py    [port] Track 3 — diagnose_sync_failures; queries sync_failure_log
tests/
  eval/
    eval_config.json        [port] tool_trajectory_avg_score=1.0 (response_match_score
                                   excluded — no expected final_response in cases)
    evalsets/
      drift_trajectories.evalset.json  [port] 7 HITL trajectory cases (5 drift
                                              lifecycle + 2 freshness SLA)
      basic.evalset.json               [gen] template default (reference)
      README.md                        [port] eval schema + usage notes
  unit/                     [port] 241 unit tests across 11 modules
    test_bigquery_query.py
    test_classify_drift.py
    test_snapshot_diff.py
    test_webhook_receiver.py
    test_connection_resolver.py        [port] Track 2
    test_readiness_score.py            [port] Track 3 Phase 1 — 28 tests
    test_schema_docs.py                [port] Track 3 Phase 2 — 7 tests
    test_sensitivity_classifier.py     [port] Track 3 Phase 2 — 15 tests
    test_use_case_auditor.py           [port] Track 3 Phase 2 — 14 tests
    test_json_flattener.py             [port] Track 3 Phase 3 — 28 tests
    test_entity_detector.py            [port] Track 3 Phase 3 — 24 tests
    test_failure_diagnosis.py          [port] Track 3 Phase 4 — 21 tests
ingest/
  webhook_receiver/
    main.py                 [port] Cloud Run handler: HMAC verify → dispatch →
                                   _run_detection_pipeline (write_sync_log + drift pipeline)
    connection_resolver.py  [port] Track 2 — connection_id → BQ dataset via Fivetran REST API
    requirements.txt        [infra] fallback deps (canonical source is pyproject.toml)
state/ddl/                  [infra] BigQuery DDL for 7 state tables
  01_schema_snapshots.sql
  02_column_snapshots.sql
  03_drift_events.sql
  04_sync_log.sql                      [infra] Track 2 — one row per successful sync_end
  05_json_flattener_log.sql            [infra] Track 3 — VIEW generation audit trail
  06_entity_map.sql                    [infra] Track 3 — cross-connection entity overlaps
  07_sync_failure_log.sql              [infra] Track 3 — Fivetran external-logging failures
scripts/                    [infra] Fivetran connector setup + key/tier probes +
                                    setup_external_logging.sh (Track 3 Phase 4)
deploy/
  cloudbuild.yaml           [infra] Cloud Build: DDL apply (all state/ddl/*.sql) +
                                    receiver redeploy from project root
  env.example               [infra] env var template
```

## Data Flow

```
Fivetran sync_end (HMAC-SHA-256 signed POST)
  → Cloud Run webhook_receiver
      verify_signature → 401 on mismatch; ignore FAILED syncs (Teleport-retry guard)
      ack 200 within 10s → fire-and-forget daemon thread
  → _run_detection_pipeline
      resolve_destination_schema(connection_id)        [Track 2]
        Fivetran REST API lookup + in-process cache
        fallback → BQ_DESTINATION_DATASET env var
      write_sync_log → sync_log [Track 2]                  ← every sync, before hash gate
      capture_and_gate: INFORMATION_SCHEMA → content_hash
        unchanged → exit cheap (hash gate)
        bootstrap → write baseline snapshot, no diff
        drift     → continue
      write schema_snapshots + column_snapshots (BigQuery)
      diff_columns → ColumnChange events (RENAME / TYPE_PROMOTION /
                     REORDER / NEW_FIELD / DEPRECATION)
      classify_drift (Gemini Flash) × N → VIEW shim SQL + confidence
      insert_drift_event × N → drift_events [PROPOSED]
  → Agent Runtime playground (human review + Track 3 AI-readiness tools)
      list_proposed_drift_events  → surface PROPOSED queue
      check_freshness_sla         → single-connection freshness check [Track 2]
      list_freshness_status       → fleet-wide freshness, stalest first [Track 2]
      approve_drift / reject_drift → APPROVED / REJECTED
      Fivetran MCP write tools (confirmation-gated) → create_transformation / run
      mark_drift_applied → APPLIED
      mark_drift_verified → VERIFIED
      score_ai_readiness          → A–F grade + signals + remediations [Track 3]
      list_readiness_scores       → fleet-wide scores, worst-first [Track 3]
      analyze_drift_volatility    → STABLE/VOLATILE/CRITICAL per connection [Track 3]
      generate_schema_docs        → plain-English column descriptions [Track 3]
      classify_column_sensitivity → PII/FINANCIAL/HEALTH/SAFE per column [Track 3]
      list_sensitive_columns      → fleet-wide sensitive columns [Track 3]
      audit_use_case_coverage     → coverage % + connector gap suggestions [Track 3]
      detect_json_columns         → JSON/semi-structured column candidates [Track 3]
      generate_json_flattener     → BQ VIEW DDL to flatten JSON column [Track 3]
      detect_entity_overlaps      → cross-connection entity matches [Track 3]
      diagnose_sync_failures      → Gemini root-cause from failure log [Track 3]
```

## Setup

### Prerequisites

- GCP project with Gemini Enterprise Agent Platform, BigQuery, Secret Manager, Cloud Run APIs enabled
- Fivetran account with a Google Cloud PostgreSQL → BigQuery connection
- `uv` installed (`brew install uv` or `pip install uv`)
- `gcloud` authenticated (`gcloud auth application-default login`)

### Local development

```bash
# 1. Clone and install deps
uv sync

# 2. Copy env template and fill credentials
cp deploy/env.example deploy/.env
# Edit deploy/.env: FIVETRAN_API_KEY, FIVETRAN_API_SECRET, GCP_PROJECT_ID,
#                   BQ_DESTINATION_DATASET (fallback dataset for resolver),
#                   FRESHNESS_SLA_HOURS (optional; default 24)

# 3. Apply BigQuery state-table DDL (one-time; re-run is safe — IF NOT EXISTS)
for f in state/ddl/*.sql; do
  bq query --location=us-east1 --use_legacy_sql=false < "$f"
done

# 4. Run unit tests
uv run pytest tests/unit/ -v

# 5. Run eval suite
uvx google-agents-cli eval run --evalset tests/eval/evalsets/drift_trajectories.evalset.json

# 6. Local agent (CLI)
set -a && source deploy/.env && set +a
uv run adk run app

# 7. Local agent (web UI — pass parent dir, not app/)
set -a && source deploy/.env && set +a
uv run adk web .
```

### Webhook receiver + DDL deployment

```bash
# Applies state/ddl/*.sql (incl. 04_sync_log.sql) and redeploys the receiver.
gcloud builds submit --config=deploy/cloudbuild.yaml
```

### Agent Runtime deployment

Credentials are read from GCP Secret Manager at runtime — no `.env` needed in the container.

```bash
# Store credentials (one-time)
printf "%s" "$FIVETRAN_API_KEY" | gcloud secrets create fivetran-api-key \
  --data-file=- --project YOUR_GCP_PROJECT_ID
printf "%s" "$FIVETRAN_API_SECRET" | gcloud secrets create fivetran-api-secret \
  --data-file=- --project YOUR_GCP_PROJECT_ID

# Grant Agent Runtime SA access
gcloud secrets add-iam-policy-binding fivetran-api-key \
  --member="serviceAccount:service-YOUR_PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding fivetran-api-secret \
  --member="serviceAccount:service-YOUR_PROJECT_NUMBER@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Deploy agent (5-10 min)
uvx google-agents-cli deploy --project YOUR_GCP_PROJECT_ID
```

## Key Implementation Notes

**Two `McpToolset`s, not one.** ADK 1.x calls `require_confirmation` callable
predicates with `**tool_input_args` (the LLM's input args), not the tool object —
so a predicate cannot inspect the tool name. Solution: split into
`fivetran_mcp_reads` (`require_confirmation=False`, 8 tools) and
`fivetran_mcp_writes` (`require_confirmation=True`, 6 tools).

**`fivetran-mcp` as a direct dependency.** Agent Runtime containers do not have
`uvx` in PATH. The package is installed as a Python dependency and the binary
resolved via `pathlib.Path(sys.executable).parent / "fivetran-mcp"`.

**Fivetran MCP `schema_file` parameter.** Every MCP tool call requires passing the
exact OpenAPI schema path (e.g., `open-api-definitions/connections/sync_connection.json`).
The system instructions enumerate all registered tool paths so the LLM fills this in
automatically without asking the user.

**Parameterized INSERT for `drift_events`.** Uses `INSERT` (not streaming insert)
to avoid BigQuery's 90-minute streaming-buffer DML lag — subsequent `UPDATE` calls
work immediately.

**Multi-connection resolver.** `ingest/webhook_receiver/connection_resolver.py` calls
`GET /v1/connectors/{connection_id}` (Basic auth) on first encounter and caches the
result in-process. Falls back to `BQ_DESTINATION_DATASET` on any error so single-
connection setups continue to work with zero config change.

**Freshness SLA monitor.** `sync_log` is written as Step 0 of the detection pipeline,
before the hash gate — so every successful sync is recorded even when the schema is
unchanged. `check_freshness_sla` / `list_freshness_status` query `sync_log` and return
`OK`, `STALE`, or `NEVER_SYNCED`. SLA threshold defaults to `FRESHNESS_SLA_HOURS=24`;
overridable per-call for connections with tighter SLAs.

**Track 3 shared helper `_fetch_schema_for_connection`.** All Track 3 tools that read
`INFORMATION_SCHEMA` call this single helper in `bigquery_query.py` — one query per
connection, result grouped as `{schema.table: [ColumnRecord]}`. Avoids duplicating the
INFORMATION_SCHEMA query across the seven tools that need it.

**Track 3 `model_fn=` dependency injection.** All Track 3 Gemini calls accept an optional
`model_fn=` parameter (default: `_call_gemini`). Tests inject stubs — zero Gemini
credits consumed by the unit test suite.

**Track 3 failure diagnosis — live API fallback.** `diagnose_sync_failures` queries
`sync_failure_log` first (populated by Fivetran's external-logging API); when the log is
empty it falls back to `GET /v1/connectors/{connection_id}` for live connector status.
Run `scripts/setup_external_logging.sh` once for richer historical failure data.

## Known Limitations

- Agent Runtime playground does not render an Approve/Reject widget — confirmation
  fires at the ADK protocol layer (`adk_request_confirmation` event) but the visual
  widget is a hosting-layer feature not yet surfaced in the playground UI. Text reply
  serves as the approval signal.
- `list_freshness_status` and Track 3 fleet-wide tools only surface connections that have
  fired at least one successful `sync_end` webhook. Connections that are paused or
  newly added will not appear until their first sync lands.
