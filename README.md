# Fivetran AI-Readiness Agent

> Fivetran lands the data. We make sure it stays AI-ready — detecting schema drift,
> classifying blast radius, gating every fix behind human approval, and surfacing
> freshness SLA status across your entire connection fleet.

**v1 capability:** Schema-Drift Downstream Impact Resolver — detects every schema
change the moment Fivetran lands it, classifies blast radius with Gemini, and gates
every remediation behind explicit human approval via Fivetran's `transformations` API.

**v2 capabilities (2026-05-31):**
- **Multi-connection support** — each Fivetran connection resolves to its own BQ dataset
  via the Fivetran REST API (no more single hardcoded `BQ_DESTINATION_DATASET`).
- **Freshness SLA monitor** — `check_freshness_sla` and `list_freshness_status` tools
  let you ask "is my data fresh enough?" for a single connection or your whole fleet.
  Every successful `sync_end` is recorded in `sync_log`; threshold via `FRESHNESS_SLA_HOURS`.

Built with **Google ADK 1.x** · **Gemini Flash** · **Gemini Enterprise Agent Platform** ·
**BigQuery** · the official [Fivetran MCP server](https://github.com/fivetran/fivetran-mcp).

See [`DESIGN.md`](DESIGN.md) for architecture decisions and design rationale.
See [`TEST.md`](TEST.md) for live test results and empirical findings.

## Status — v2 Complete (2026-05-31)

| Component | State |
|---|---|
| Detection pipeline (webhook → sync_log → snapshot → diff → classify → drift_events) | ✅ Live |
| Multi-connection resolver (`connection_id → BQ dataset` via Fivetran REST API) | ✅ Live (v2) |
| Freshness SLA monitor (`sync_log` + `check_freshness_sla` / `list_freshness_status`) | ✅ Live (v2) |
| HITL agent flow (PROPOSED → APPROVED → APPLIED → VERIFIED) | ✅ Live — both events driven to VERIFIED 2026-05-26 |
| Write-tool confirmation gate (`require_confirmation=True`) | ✅ Verified — `adk_request_confirmation` event fires on Agent Runtime |
| Agent Runtime deployment | ✅ Live at `reasoningEngines/2248457298336808960` (us-east1) |
| Webhook receiver | ✅ Live at Cloud Run `fivetran-sync-end-receiver` (us-east1), min-instances=1 |
| Eval suite | ✅ 7/7 cases passing `tool_trajectory_avg_score=1.0` |
| Unit tests | ✅ 104/104 passing |

## Layout

Canonical `agents-cli create --prototype --adk` structure. `[gen]` = template-generated,
`[port]` = project code, `[infra]` = non-template infrastructure.

```
pyproject.toml              [gen+port] deps (google-adk 1.x, bigquery, fivetran-mcp,
                                            secret-manager, functions-framework)
agents-cli-manifest.yaml    [gen+port] deployment target = agent_runtime (us-east1);
                                       migrated from [tool.agents-cli] by scaffold upgrade
uv.lock                     [gen] committed for reproducibility
CLAUDE.md                   [gen] coding-agent guidance
app/
  __init__.py               [gen] exports `app`
  agent.py                  [port] root_agent + App; 7 FunctionTools (drift lifecycle +
                                   check_freshness_sla + list_freshness_status);
                                   McpToolset split (read/write);
                                   _secret_or_env() for Agent Runtime credential fallback
  agent_runtime_app.py      [gen] Agent Runtime entrypoint
  system_instructions.md    [port] loaded as instruction=; schema_file patterns +
                                   freshness SLA guidance
  app_utils/                [gen] telemetry.py, typing.py
  tools/
    bigquery_query.py       [port] state-table CRUD; list_proposed_drift_events;
                                   write_sync_log, check_freshness_sla,
                                   list_freshness_status (v2)
    snapshot_diff.py        [port] capture_and_gate, content_hash, diff_columns
    classify_drift.py       [port] Gemini classifier + remediation SQL generator
tests/
  eval/
    eval_config.json        [port] tool_trajectory_avg_score=1.0 (response_match_score
                                   excluded — no expected final_response in cases)
    evalsets/
      drift_trajectories.evalset.json  [port] 7 HITL trajectory cases (5 drift
                                              lifecycle + 2 freshness SLA)
      basic.evalset.json               [gen] template default (reference)
      README.md                        [port] eval schema + usage notes
  unit/                     [port] 104 unit tests across 5 modules
    test_bigquery_query.py
    test_classify_drift.py
    test_snapshot_diff.py
    test_webhook_receiver.py
    test_connection_resolver.py        [port] v2 — 15 cases for resolver
ingest/
  webhook_receiver/
    main.py                 [port] Cloud Run handler: HMAC verify → dispatch →
                                   _run_detection_pipeline (write_sync_log + drift pipeline)
    connection_resolver.py  [port] v2 — connection_id → BQ dataset via Fivetran REST API
    requirements.txt        [infra] fallback deps (canonical source is pyproject.toml)
state/ddl/                  [infra] BigQuery DDL for 4 state tables
  01_schema_snapshots.sql
  02_column_snapshots.sql
  03_drift_events.sql
  04_sync_log.sql                      [infra] v2 — one row per successful sync_end
scripts/                    [infra] Fivetran connector setup + key/tier probes
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
      resolve_destination_schema(connection_id)        [v2]
        Fivetran REST API lookup + in-process cache
        fallback → BQ_DESTINATION_DATASET env var
      write_sync_log → sync_log [v2]                  ← every sync, before hash gate
      capture_and_gate: INFORMATION_SCHEMA → content_hash
        unchanged → exit cheap (hash gate)
        bootstrap → write baseline snapshot, no diff
        drift     → continue
      write schema_snapshots + column_snapshots (BigQuery)
      diff_columns → ColumnChange events (RENAME / TYPE_PROMOTION /
                     REORDER / NEW_FIELD / DEPRECATION)
      classify_drift (Gemini Flash) × N → VIEW shim SQL + confidence
      insert_drift_event × N → drift_events [PROPOSED]
  → Agent Runtime playground (human review)
      list_proposed_drift_events  → surface PROPOSED queue
      check_freshness_sla         → single-connection freshness check [v2]
      list_freshness_status       → fleet-wide freshness, stalest first [v2]
      approve_drift / reject_drift → APPROVED / REJECTED
      Fivetran MCP write tools (confirmation-gated) → create_transformation / run
      mark_drift_applied → APPLIED
      mark_drift_verified → VERIFIED
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

## Known Limitations

- Agent Runtime playground does not render an Approve/Reject widget — confirmation
  fires at the ADK protocol layer (`adk_request_confirmation` event) but the visual
  widget is a hosting-layer feature not yet surfaced in the playground UI. Text reply
  serves as the approval signal.
- `list_freshness_status` only surfaces connections that have fired at least one
  successful `sync_end` webhook. Connections that are paused or newly added will not
  appear until their first sync lands.
