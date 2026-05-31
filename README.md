# Fivetran AI-Readiness Agent

> Fivetran lands the data. We make sure it stays AI-ready — detecting schema drift,
> classifying blast radius, and gating every fix behind human approval.

**v1 capability:** Schema-Drift Downstream Impact Resolver. When a Fivetran sync
changes the landed schema (rename, type promotion, column reorder, new field,
deprecation), the agent detects it, classifies it with Gemini, proposes a
VIEW-based remediation, and — on your explicit approval — deploys it via
Fivetran's own `transformations` API.

Built with **Google ADK 1.x** · **Gemini Flash** · **Gemini Enterprise Agent Platform** ·
**BigQuery** · the official [Fivetran MCP server](https://github.com/fivetran/fivetran-mcp).

See `../fivetranAgentDesign.md` for the full design rationale, resolved decisions,
and live test results.

## Status — v1 Complete (2026-05-29)

| Component | State |
|---|---|
| Detection pipeline (webhook → snapshot → diff → classify → drift_events) | ✅ Live — two real events captured 2026-05-25 |
| HITL agent flow (PROPOSED → APPROVED → APPLIED → VERIFIED) | ✅ Live — both events driven to VERIFIED 2026-05-26 |
| Write-tool confirmation gate (`require_confirmation=True`) | ✅ Verified — `adk_request_confirmation` event fires on Agent Runtime 2026-05-29 |
| Agent Runtime deployment | ✅ Live at `reasoningEngines/2248457298336808960` (us-east1) |
| Webhook receiver | ✅ Live at Cloud Run `fivetran-sync-end-receiver` (us-east1), min-instances=1 |
| Unit tests | ✅ 84/84 passing |

## Layout

Canonical `agents-cli create --prototype --adk` structure. `[gen]` = template-generated,
`[port]` = project code, `[infra]` = non-template infrastructure.

```
pyproject.toml            [gen+port] deps (google-adk 1.x, bigquery, fivetran-mcp,
                                          secret-manager) + [tool.agents-cli]
uv.lock                   [gen] committed for reproducibility
deployment_metadata.json  [gen] deploy target = agent_runtime (us-east1)
CLAUDE.md                 [gen] coding-agent guidance
app/
  __init__.py             [gen] exports `app`
  agent.py                [port] root_agent + App; McpToolset split (read/write);
                                 _secret_or_env() for Agent Runtime credential fallback
  agent_runtime_app.py    [gen] Agent Runtime entrypoint
  system_instructions.md  [port] loaded as instruction=; includes schema_file patterns
  app_utils/              [gen] telemetry.py, typing.py
  tools/
    bigquery_query.py     [port] state-table CRUD + list_proposed_drift_events()
    snapshot_diff.py      [port] capture_and_gate, content_hash, diff_columns
    classify_drift.py     [port] Gemini classifier + remediation SQL generator
tests/
  eval/eval_config.json           [port] LLM-as-judge criteria
  eval/evalsets/
    drift_trajectories.evalset.json [port] 5 HITL trajectory cases
    basic.evalset.json            [gen] template default (reference)
    README.md                     [gen] eval schema reference
  unit/                           [port] 84 unit tests across all 4 modules
ingest/webhook_receiver/  [infra] Cloud Run sync_end receiver (separate service)
state/ddl/                [infra] BigQuery DDL for 3 state tables
scripts/                  [infra] Fivetran connector setup + key/tier probes
deploy/env.example        [infra] env var template (Fivetran/BQ/GCP)
```

## Data Flow

```
Fivetran sync_end (HMAC-SHA-256 signed POST)
  → Cloud Run webhook_receiver
      verify_signature → 401 on mismatch
      ack 200 within 10s → fire-and-forget daemon thread
  → _run_detection_pipeline
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
      list_proposed_drift_events → surface pending findings
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
#                   BQ_DESTINATION_DATASET, BQ_LOCATION

# 3. Apply BigQuery state-table DDL (one-time)
bq query --location=us-east1 --use_legacy_sql=false < state/ddl/schema_snapshots.sql
bq query --location=us-east1 --use_legacy_sql=false < state/ddl/drift_events.sql

# 4. Run unit tests
uv run pytest tests/unit/ -v

# 5. Local agent (CLI)
set -a && source deploy/.env && set +a
uv run adk run app

# 6. Local agent (web UI — pass parent dir, not app/)
set -a && source deploy/.env && set +a
uv run adk web .
```

### Agent Runtime deployment

Credentials are read from GCP Secret Manager at runtime — no `.env` needed in the container.

```bash
# Store credentials (one-time)
echo -n "$FIVETRAN_API_KEY" | gcloud secrets create fivetran-api-key \
  --data-file=- --project api-project-910787152095
echo -n "$FIVETRAN_API_SECRET" | gcloud secrets create fivetran-api-secret \
  --data-file=- --project api-project-910787152095

# Grant Agent Runtime SA access
gcloud secrets add-iam-policy-binding fivetran-api-key \
  --member="serviceAccount:service-910787152095@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding fivetran-api-secret \
  --member="serviceAccount:service-910787152095@gcp-sa-aiplatform-re.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

# Deploy (5-10 min)
uvx google-agents-cli deploy --project api-project-910787152095
```

### Webhook receiver deployment

See `ingest/webhook_receiver/` — deployed separately to Cloud Run. Register the
`sync_end` webhook via the Fivetran MCP or dashboard after deployment.

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

## Known Limitations (v1)

- Webhook receiver reads `BQ_DESTINATION_DATASET=public` regardless of connection ID.
  Multi-connection deployments need a `connection_id → destination_schema` resolver.
- `agents-cli eval run` skipped — version mismatch between scaffold (0.1.3) and
  current CLI (0.2.1). Run `agents-cli scaffold upgrade` before evaluating.
- Agent Runtime playground does not render an Approve/Reject widget — confirmation
  fires at the ADK protocol layer (`adk_request_confirmation` event) but the visual
  widget is a hosting-layer feature not yet surfaced in the playground UI.
