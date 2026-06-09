# Design Rationale

Design decisions, architectural constraints, and the reasoning behind key implementation choices in the Fivetran AI-Readiness Agent.

---

## Why This Exists

Fivetran markets "automatically adapts to schema changes" as a feature — but this creates downstream pain. When Salesforce adds a field, Stripe deprecates an endpoint, or a column gets renamed at the source:

- Fivetran quietly updates the landed schema in the warehouse
- `dbt` models referencing `select column_x` break
- BI dashboards silently lose data
- Data engineers spend half a day diagnosing and patching downstream

Fivetran explicitly places downstream schema-drift remediation on the customer (their Shared Responsibility Model):

> "Regularly maintain the transformation and modeling as the canonical schema evolves."
> "Respond to a known operational breaking change and follow the instructions to restore service."

This agent automates exactly the responsibility line Fivetran draws — it is not solving a Fivetran bug; it is filling Fivetran's stated customer-side gap.

The longer arc: schema integrity is the wedge into a broader **AI-Readiness Auditor** — freshness SLAs, completeness, LLM-friendly metadata, embedding-readiness for RAG. Same agent shell; expanding scope.

---

## Resolved Decisions

### 1. Invocation model — Direct invoke (no task queue)

The `sync_end` webhook is fixed (Fivetran's only push mechanism). The receiver verifies HMAC, returns HTTP 200, then invokes the detection flow directly in a daemon thread — no durable queue.

**Why:** Detection is convergent. Every run reconstructs truth from `INFORMATION_SCHEMA` vs the last stored snapshot. A dropped `sync_end` is recovered by the next sync's snapshot+diff — a missed event delays detection by one sync cycle, never loses data. Fivetran retries non-200 receipts on an exponential schedule (6 min → 21 min → … up to 24h), covering receipt-layer failures for free. A queue adds a component to build, deploy, monitor, and explain, with no correctness benefit.

**Cheap to reverse:** `dispatch()` in `main.py` is the single seam — swapping to Pub/Sub later is localized, not a rearchitecture.

### 2. Notification channel — Chat-only

The Agent Builder conversational surface is the demo. Email adds infra + config and broadens scope past the core.

### 3. Rename heuristic — Ordinal is a Gemini feature, not a gate

The heuristic favors **recall**: pair *any* removed+added column with matching `data_type` in the same table as a rename candidate, and pass the ordinal delta to Gemini as an input signal — not a hard filter. This avoids missing renames when Fivetran's drop-then-add reorders columns significantly.

**Why:** Live observation (G2) showed that a single type-promotion causes a complete table column reorder (all columns shift, not just the promoted one). `ordinal_position` carries no semantic content under TYPE_PROMOTION — it ranges -6 to +3 with no predictable pattern. Using it as a gate would produce false negatives. Gemini confirms rename via name semantics; the ordinal is advisory context.

### 4. Test source — Cloud SQL (Google Cloud PostgreSQL)

Single-cloud (Cloud SQL → BigQuery → Agent Builder) is a cleaner demo narrative. A local Postgres would require an always-up tunnel that injects an uncontrolled failure variable into the exact thing the sandbox measures (detection latency across scheduled syncs). Cloud SQL is a first-class Fivetran-supported service with a dedicated setup guide.

**Sync method:** Query-Based incremental sync (no replication slot, fastest to stand up). Logical replication noted as the realistic-mode follow-up.

### 5. Agent framework — ADK (code-first)

The agent's core logic — the `content_hash` gate, the recall-favoring rename cross-pairing, Gemini-driven classification + VIEW-shim SQL generation — *cannot* be expressed in the no-code path, so code-first is required regardless of preference.

**ADK and Gemini Enterprise are complementary layers:** ADK is the SDK (build/debug); the Gemini Enterprise Agent Platform is the runtime/hosting (managed Agent Runtime). Satisfies the hackathon's Agent Builder requirement.

---

## Key Implementation Choices

### Single `LlmAgent`, not `SequentialAgent`

Per-webhook detection is deterministic Python — running it through ADK LLM-driven sub-agents would burn Gemini calls on mechanical steps and risk non-deterministic skips. The Sequential-style flow is realized in Python composition (`_run_detection_pipeline` in `ingest/webhook_receiver/main.py`); the LlmAgent handles only the human-facing review/approval surface.

### Two `McpToolset`s, not one

ADK 1.x calls `require_confirmation` callable predicates with `**tool_input_args` (the LLM's input args), not the tool object — so a callable predicate cannot inspect the tool name at call time.

**Fix:** split into `fivetran_mcp_reads` (`require_confirmation=False`, 8 tools) and `fivetran_mcp_writes` (`require_confirmation=True`, 6 tools). Both point at the same `fivetran-mcp` command. `require_confirmation=True` (bool) causes ADK to gate every call to the write toolset unconditionally.

### Parameterized `INSERT` for `drift_events`, not streaming insert

Avoids BigQuery's 90-minute streaming-buffer DML lag so subsequent `UPDATE` calls work immediately. `write_snapshot` uses streaming (append-only, never updated, so buffer lag is harmless).

### `capture_and_gate` returns a `GateResult` dataclass

Three branchable fields: `changed`, `current_columns`, `current_hash`, `prior_snapshot`. Bootstrap is signaled by `prior_snapshot is None`; cheap-exit by `changed=False`. Caller branches cleanly without positional unpacking.

### `classify()` accepts `model_fn=` for dependency injection

Production callers leave it at the default `_call_gemini`; tests inject a fake to exercise the prompt-build/response-parse pipeline without burning Gemini credits. End-to-end testable offline.

### `_run_detection_pipeline` is exception-safe at two levels

Per-classification (one failed `classify` doesn't abort the others) AND top-level (any thrown exception is logged but does not propagate out of the thread). The convergent design depends on partial-success semantics: better one drift_event than none, and a fully-failed run self-heals on the next sync_end.

### `_DRIFT_EVENT_FIELDS` is a single source of truth

Used by both `insert_drift_event`'s placeholder construction and `update_drift_event`'s field-name validation. `update_drift_event` validates field names against this constant AND auto-sets `updated_at` (unless the caller passes it — avoids the duplicate-SET-target SQL error).

### `fivetran-mcp` as a direct dependency

Agent Runtime containers do not have `uvx` in PATH. The package is installed as a Python dependency and the binary resolved via `pathlib.Path(sys.executable).parent / "fivetran-mcp"`.

### `_secret_or_env()` helper for Agent Runtime credentials

`agents-cli deploy` does not package `.env`. The helper reads `os.environ` first, falls back to GCP Secret Manager via the Agent Runtime SA. Fivetran credentials are stored as Secret Manager secrets with `roles/secretmanager.secretAccessor` granted to the Agent Runtime SA.

### Track 2: Connection resolver with in-process cache

`connection_resolver.py` calls `GET /v1/connectors/{connection_id}` (Basic auth) on first encounter and caches the result in-process. Falls back to `BQ_DESTINATION_DATASET` on any error so single-connection setups continue to work with zero config change. Fallback is NOT cached — the next `sync_end` retries the lookup (important: once credentials are populated, resolution recovers automatically).

### Track 2: `sync_log` written before the hash gate

`write_sync_log` is Step 0 of `_run_detection_pipeline`, before `capture_and_gate`. This makes `sync_log` the authoritative freshness source regardless of whether the schema changed — `schema_snapshots` is only written when the content hash changes.

---

## Source-Derived Constraints

These facts were verified empirically against the live Postgres connector and change the detection design:

**No native schema-change event.** Fivetran supports `sync_end`, `sync_start`, `status`, `transformation_*`, connection lifecycle events — but no `schema_changed`. Detection must be warehouse-side snapshot diff. The hash gate is critical for cost — 99% of syncs produce no change and exit in milliseconds.

**Pure DDL doesn't propagate (Query-Based mode).** `ALTER TABLE ADD COLUMN` on the source doesn't materialize in BigQuery unless at least one row has a non-NULL value for the new column. Test scenarios must `ALTER TABLE` then `INSERT/UPDATE` to force propagation.

**Fivetran injects system columns.** The Google Cloud PostgreSQL connector ships `ctid_fivetran_id` (not the docs' `_fivetran_id`). The diff logic filters columns starting with `_fivetran_` OR ending with `_fivetran_id` to catch both forms.

**Fivetran does NOT preserve source column order at the destination.** REORDER drift must compare successive destination snapshots, never assume source ordinal == destination ordinal.

**TYPE_PROMOTION causes a full-table reorder at the destination.** A single type change rewrites ALL columns' ordinals (observed: all 8 columns shifted). REORDER and TYPE_PROMOTION are coupled events — the classifier must attribute collateral ordinal changes on a table to a co-occurring TYPE_PROMOTION rather than emit them as standalone REORDER events.

**Source DROP is soft-dropped at destination.** Fivetran marks the column `reason_code: DELETED` but keeps the BQ column populated with NULLs. The agent's DEPRECATION remediation must affirmatively remove BQ columns via `delete_connection_column_config` (MCP) — a source-side drop alone leaves a ghost column that persists in `INFORMATION_SCHEMA` indefinitely.

**Type-promotion is supertype-monotonic at the destination.** Reverting source `amount` from `TEXT` back to `NUMERIC(10,2)` will NOT narrow BQ back to `BIGNUMERIC`. To restore the narrower type requires a schema reload + sync, or `delete_connection_column_config` + recreate via MCP.

**Teleport Sync Error + auto-retry pattern.** TYPE_PROMOTION produces two `sync_end` payloads — one `status=FAILED` (Fivetran's internal rewrite attempt), one `status=SUCCESSFUL`. The `data.status != "SUCCESSFUL"` filter in `main.py` handles this correctly.

**`reload_connection_schema_config` is synchronous and fast at small scale.** Measured 2s wall-clock against a 2-table connection. Returns the full schema config (schemas → tables → columns with `enabled`, `name_in_destination`, etc.) in the response body. Does NOT trigger a downstream data sync.

**`name_in_destination` diverges for synthetic system columns.** Fivetran's column-config API reports `ctid` as `name_in_destination`, but BQ `INFORMATION_SCHEMA` shows it as `ctid_fivetran_id`. When calling Fivetran APIs that take column names, use the source-side name — not the BQ `INFORMATION_SCHEMA` name. `exclude_system_columns` already keeps the agent from targeting synthetics, so this is a latent risk, not an active bug.

---

## Why `transformations` API (not dbt or GitHub)

The original design contemplated a dbt repo + GitHub MCP path for remediation. The Fivetran `transformations` API replaces this entirely — VIEW shims deploy as Fivetran-managed transformations. Tighter integration, fewer moving parts, no external repo required. This is also Fivetran's own documented remediation pattern: *"We encourage customers to use VIEWs to enforce data type coercions."*

---

## Fivetran MCP: `schema_file` Parameter

Every Fivetran MCP tool call requires passing the exact OpenAPI schema file path as a parameter (e.g., `open-api-definitions/connections/sync_connection.json`). The server validates it matches the expected path — this is a built-in acknowledgement gate in the MCP design.

Fixed in `app/system_instructions.md`: the exact paths for all 13 registered tools are listed and the agent is instructed to fill in `schema_file` itself without asking the user.

---

## `agents-cli eval run` — Decision (2026-05-29 → reversed 2026-05-31)

Initially skipped for the Track 1 hackathon submission due to `agents-cli 0.1.3 → 0.2.1` version mismatch risk. After completing the scaffold upgrade (`agents-cli scaffold upgrade` → `agents-cli-manifest.yaml`), the eval suite runs cleanly:

- `eval_config.json` criterion changed from list → dict (ADK 1.15+ requirement); `response_match_score` dropped (no `final_response` in cases; ROUGE would score 0.0 against empty string); `tool_trajectory_avg_score=1.0` is the correct signal for a HITL agent.
- MCP-dependent cases excluded — MCP tools require live Fivetran API + subprocess spawn that exceeds the 10s eval session timeout. MCP tool behavior is validated in the playground instead.

---

## Track 3 AI-Readiness Tools — Design Notes (2026-06-02)

Eight Gemini-powered tools added across four phases. All follow the exact patterns
established in Track 1/2 — no new dependencies, no new ADK patterns.

### Shared helper `_fetch_schema_for_connection` (bigquery_query.py)

Seven of the eight Track 3 tools need `INFORMATION_SCHEMA.COLUMNS` for a given connection.
Rather than each tool re-implementing the query, a single helper resolves the dataset
via `connection_resolver`, queries INFORMATION_SCHEMA, and returns
`{schema.table_name: [ColumnRecord]}`. This avoids seven copies of the same BQ round-trip
and makes the query logic testable in one place.

### `model_fn=` dependency injection on all Track 3 tools

All Track 3 Gemini call sites accept `model_fn=_call_gemini` as a keyword-only argument.
Tests inject a lambda stub — the entire prompt-build / JSON-parse / return-shape pipeline
is exercised offline without burning Gemini credits. This pattern was established in
`classify_drift.py` and extended uniformly across Track 3.

### `_extract_json` / `_call_gemini` — defined once, imported everywhere

`readiness_score.py` is the canonical home for `_call_gemini`, `_extract_json`, and
`CLASSIFIER_MODEL`. All other Track 3 tool files import from there. Single source of truth
for the Gemini client config and fence-stripping logic.

### Graceful degradation on bad Gemini JSON (all Track 3 tools)

Every Track 3 tool wraps its `_extract_json` call in `try/except (json.JSONDecodeError, AttributeError)`:
- `score_ai_readiness` → `grade="?"`, raw response as narrative
- `analyze_drift_volatility` → `stability_class="UNKNOWN"` per connection
- `generate_schema_docs` → empty `description=""` per column
- `classify_column_sensitivity` → returns `[]`
- `audit_use_case_coverage` → falls back to pre-computed fuzzy coverage map
- `detect_entity_overlaps` → returns `[]`
- `diagnose_sync_failures` → `severity` from count heuristic, empty `recommended_actions`

### `_STRUCTURED_NAME_RE` — no `\b` word-boundary anchors

The regex for detecting structured-payload column names (`metadata`, `payload`, etc.) uses
plain case-insensitive substring matching, not `\b` word boundaries. Python's `\b` treats
`_` as a word character, so `\bpayload\b` would not match `user_payload` or `event_payload`.
This was caught by unit tests (`test_json_flattener.py::test_structured_name_re_matches`).

### `diagnose_sync_failures` — live API fallback when the log is empty

The tool queries `sync_failure_log` first (populated by Fivetran's external-logging API).
When the log is empty — the default state until `scripts/setup_external_logging.sh` is run —
it does not dead-end: `_fetch_connector_status` calls `GET /v1/connectors/{connection_id}`
for live connector status. A healthy `sync_state` returns `status="no_failures"` with a
clear message; an `error`/`broken` state (or active `tasks`) is formatted into synthetic
error records and sent to Gemini for diagnosis, returning the full result shape with
`source="fivetran_api"`. Once external-logging is configured, the historical-log path takes
priority and escalates to Gemini root-cause analysis automatically — same tool, richer data.
No credentials or an unreachable API degrades gracefully to `status="no_failures"`.

### `detect_entity_overlaps` — single-connection catalog mode

Cross-connection overlap detection needs two sources, but the original `[]` short-circuit
for a single connection left single-connection users with no output. The tool now branches
on connection count: **1 connection** → Gemini analyzes the entities *within* that connection
(naming each entity, its primary table, and the best join key, plus intra-schema quality
observations) with `analysis_mode="single_connection"`; **2+ connections** → cross-connection
overlap detection with `analysis_mode="cross_connection"`. The return shape is identical
across both paths. Zero connections still returns `[]`.

### Streaming insert for append-only Track 3 state tables

`json_flattener_log` and `entity_map` use streaming insert (`insert_rows_json`), not
parameterized INSERT. These tables are append-only — rows are never updated after insert —
so the streaming buffer's 90-minute DML lag is irrelevant. `drift_events` still uses
parameterized INSERT because its lifecycle updates (`UPDATE … SET remediation_status=…`)
must be available immediately.

### `resolve_destination_schema` — lazy import only (Agent Runtime packaging constraint)

`ingest` is NOT in `pyproject.toml`'s `packages = ["app", "frontend"]` list and has no
`__init__.py`. The `agents-cli deploy` command packages only the declared packages —
`ingest.webhook_receiver.connection_resolver` does not exist in the agent container.

**Any module-level `from ingest…` import in an `app/tools/` file causes an `ImportError`
at agent startup, silently failing the Reasoning Engine update on Vertex AI.**

The correct pattern (used everywhere) is a lazy import inside the function body:

```python
def my_tool(connection_id: str) -> dict:
    from ingest.webhook_receiver.connection_resolver import resolve_destination_schema  # noqa: PLC0415
    dataset = resolve_destination_schema(connection_id)
```

**Test patch target:** with a lazy import, monkeypatch must target the source module, not a
re-export that no longer exists at the tool module level:

```python
# Correct — patches the source
monkeypatch.setattr("ingest.webhook_receiver.connection_resolver.resolve_destination_schema", lambda _: "ds")

# Wrong — name doesn't exist at module level with lazy import
monkeypatch.setattr("app.tools.schema_docs.resolve_destination_schema", ...)
```

This was discovered when `schema_docs.py` and `json_flattener.py` both had module-level
imports that caused the Track 3 Agent Runtime deploy to fail with `"The Reasoning Engine failed
to be updated."` The operation was created on Vertex AI (manifest accepted) but the
container crashed at startup — distinguishing an import error from a manifest version
incompatibility. Fixed 2026-06-02: both imports moved to function body; test patches
updated to the `ingest.webhook_receiver.connection_resolver` path.

### `sensitivity_classifier.py` — one Gemini call per connection, not per table

All columns from all tables in a connection are batched into a single Gemini prompt
regardless of how many tables exist. This keeps the tool's Gemini cost O(connections),
not O(tables). The prompt includes `{table: ..., column: ..., data_type: ...}` tuples so
Gemini can apply table-context when distinguishing `orders.amount` (FINANCIAL) from
`blog_posts.amount` (SAFE).

### `use_case_auditor.py` — fuzzy pre-coverage as Phase B fallback

Before calling Gemini Phase B, the auditor pre-computes a fuzzy coverage map using
token-based substring matching. If Phase B's JSON is malformed, the fallback uses the
fuzzy map to populate `covered` and `missing`, ensuring the function always returns a
structurally valid response even when the LLM fails.

---

## `agents-cli scaffold upgrade` Notes (2026-05-31)

- `agents-cli scaffold upgrade` partially succeeded — manifest migrated but template comparison failed with "Project name exceeds 26 characters". Functional result is correct; the 26-char limit only affects the cosmetic diff-generation step.
- `app/.adk/` added to `.gitignore` — runtime artifacts (timestamped eval JSONs + `session.db`).
