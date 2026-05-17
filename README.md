# Fivetran AI-Readiness Agent

An agent that keeps your data ready for your agents.

**v1 capability:** Schema-Drift Downstream Impact Resolver. When a Fivetran
sync changes the landed schema (rename, type promotion, column reorder, new
field, deprecation), the agent detects it, classifies it with Gemini, proposes
a VIEW-based remediation, and — on your approval — deploys it via Fivetran's
own `transformations` API.

Built with the **Agent Development Kit (ADK)** on the Gemini Enterprise Agent
Platform + Gemini + the official
[Fivetran MCP server](https://github.com/fivetran/fivetran-mcp).
See `../fivetranAgentDesign.md` for the full design rationale.

## Layout

Canonical `agents-cli create --prototype --adk` structure with our assets
ported in. `[gen]` = template-generated, `[port]` = ours, `[infra]` = our
non-template infrastructure.

```
pyproject.toml            [gen] deps (google-adk 1.x + bigquery) + [tool.agents-cli]
uv.lock                   [gen] committed for reproducibility
deployment_metadata.json  [gen] deploy target = agent_runtime
CLAUDE.md                 [gen] coding-agent guidance
app/
  __init__.py             [gen] exports `app`
  agent.py                [port] root_agent + App; composition contract
  agent_runtime_app.py    [gen] Agent Runtime entrypoint
  system_instructions.md  [port] loaded as instruction=
  app_utils/              [gen] telemetry.py, typing.py
  tools/                  [port] bigquery_query / snapshot_diff / classify_drift
tests/
  eval/eval_config.json           [port] LLM-as-judge criteria
  eval/evalsets/
    drift_trajectories.evalset.json [port]
    basic.evalset.json            [gen] template default (reference)
    README.md                     [gen] eval schema reference
  integration/, unit/             [gen]
ingest/webhook_receiver/  [infra] separate Cloud Run sync_end receiver
state/ddl/                [infra] BigQuery DDL for the 3 state tables
scripts/                  [infra] Fivetran key/tier verification probes
deploy/env.example        [infra] env var template (Fivetran/BQ)
docs/                     [infra] placeholder for summarized rationale
```

## Data Flow

```
Fivetran sync_end (HMAC-signed)
  -> Cloud Run webhook_receiver (verify, ack <10s)
  -> snapshot_diff: INFORMATION_SCHEMA -> content_hash gate
       (unchanged -> exit cheap)
  -> write schema_snapshots + column_snapshots
  -> classify_drift (Gemini) -> drift_events [PROPOSED]
  -> notify user -> approve
  -> Fivetran transformations API (via MCP) -> run -> verify
  -> drift_events [VERIFIED]
```

## Setup (skeleton — not yet runnable)

1. `cp deploy/env.example deploy/.env` and fill credentials (`.env` is gitignored)
2. Verify the Fivetran key + tier:
   - `bash scripts/check_api_access.sh` — READ auth + reversible WRITE-role probe
   - `bash scripts/check_capabilities.sh` — Transformations API availability + inventory
3. `agents-cli install` then `uv run pytest tests/unit tests/integration` (sanity)
4. Apply DDL: `bq query --use_legacy_sql=false < state/ddl/*.sql`
5. Implement tool bodies in `app/tools/*`; assemble the workflow in `app/agent.py`
6. `agents-cli playground` for interactive local testing
7. `agents-cli eval run` against `tests/eval/evalsets/drift_trajectories.evalset.json`
8. Deploy the `ingest/webhook_receiver` Cloud Run service separately; register
   the `sync_end` webhook via the Fivetran MCP
9. `agents-cli deploy` (after explicit approval — see CLAUDE.md)

## Status

Migrated to the canonical `agents-cli` (ADK 1.x) layout. Tool bodies are
`NotImplementedError` stubs; `app/agent.py` is a composition contract
(`root_agent` temporarily = classifier so `agents-cli playground` runs).
DDL complete; `exclude_system_columns` implemented. Design decisions resolved —
see `../fivetranAgentDesign.md` "Resolved Decisions".
