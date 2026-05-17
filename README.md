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

```
agent/                  ADK agent + Gemini tools
  __init__.py            ADK entrypoint discovery (from . import agent)
  agent.py               Agent (LlmAgent) + Sequential workflow + MCPToolset
  system_instructions.md loaded as the agent `instruction` string
  requirements.txt       google-adk 1.x (>=1.15,<2) + bigquery
  tools/
    __init__.py
    bigquery_query.py    FunctionTools: INFORMATION_SCHEMA + state-store
    snapshot_diff.py     capture, hash-gate, column diff, rename heuristic
    classify_drift.py    Gemini classification + remediation SQL
tests/eval/              sanctioned ADK eval location
  evalsets/
    drift_trajectories.evalset.json
  eval_config.json       LLM-as-judge criteria
ingest/
  webhook_receiver/      Cloud Run: receives Fivetran sync_end
state/ddl/               BigQuery DDL for the 3 state tables
scripts/                 Fivetran key/tier verification probes
deploy/                  cloudbuild + env template
```

> **Layout note:** this tree is a *staging area*. The deployment container is
> regenerated via `agents-cli create --prototype` on the live environment
> (per the onboarding guide); portable assets (DDL, `snapshot_diff` logic,
> MCP tool-filter, eval trajectories, instructions) port into it. See design
> doc "Onboarding-Guide Alignment".

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

1. `cp deploy/env.example deploy/.env` and fill credentials
2. Verify the Fivetran key + tier:
   - `bash scripts/check_api_access.sh` — READ auth + reversible WRITE-role probe
   - `bash scripts/check_capabilities.sh` — Transformations API availability (read-only) + connection/destination inventory
3. Apply DDL: `bq query --use_legacy_sql=false < state/ddl/*.sql`
4. `pip install -r agent/requirements.txt` (pin the ADK version)
5. Regenerate the deploy container: `agents-cli create --prototype`; port
   assets from this staging tree into it
6. Deploy receiver: `gcloud builds submit --config deploy/cloudbuild.yaml`
7. `agents-cli deploy` the agent to Cloud Run / Agent Runtime
8. Register the `sync_end` webhook via the Fivetran MCP
9. `agents-cli eval run` against `tests/eval/evalsets/drift_trajectories.evalset.json`

## Status

Skeleton only. Tool bodies are `NotImplementedError` stubs; `agent.py`
composition is commented contract. DDL is complete; `exclude_system_columns`
is implemented. Framework: ADK (code-first). Five design decisions resolved —
see design doc "Resolved Decisions".
