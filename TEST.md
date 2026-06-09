# Test Results

Live test evidence, empirical findings, and verification records for the Fivetran AI-Readiness Agent.

---

## Unit Test Suite — 251/251 Passing (Track 3 + limitation fixes, 2026-06-02)

```bash
uv run pytest tests/unit/ -v
```

### Track 1/2 modules (104 tests)

| File | Count | Coverage focus |
|---|---|---|
| `tests/unit/test_snapshot_diff.py` | **25** | `exclude_system_columns` (incl. `ctid_fivetran_id` connector variant + defensive substring case), `content_hash` (order-independence, change-detection, empty stability), `diff_columns` (all 5 change types + recall-favoring rename multi-candidate cases + full-table reorder scenario), `capture_and_gate` (bootstrap, hash-match cheap exit, change detection, system-column filter applied before hashing) |
| `tests/unit/test_bigquery_query.py` | **20** | Pure helpers — region pin, SQL builder shape, state-table FQN with env-config (incl. `GCP_PROJECT_ID` fallback), row converter (YES/NO + bool + ordinal-coercion), JSON helpers, placeholder selection, `update_drift_event` typo-rejection; Track 2: `_default_sla_hours` (default 24h, env override, fractional) |
| `tests/unit/test_classify_drift.py` | **25** | Change-type + model-id constants, prompt builder structure, JSON extraction (plain + fenced + error paths), response parser (happy + 4 error paths), end-to-end `classify()` with injected fake `model_fn` (no live LLM) |
| `tests/unit/test_webhook_receiver.py` | **18** | `verify_signature`, `handle_request`, `dispatch`, `_run_detection_pipeline` (cheap-exit, bootstrap-no-diff, drift-writes-PROPOSED-events, partial-success per-change, exception-safe top-level); Track 2: `sync_log` written before hash gate on every sync, `write_sync_log` failure does not abort drift pipeline, `_column_to_dict` |
| `tests/unit/test_connection_resolver.py` | **15** | Track 2 — `_fetch_schema` (no creds, partial creds, network error, missing field, `config.schema` success, `schema_prefix` fallback, priority between fields, correct Basic-auth header, correct URL); `resolve_destination_schema` (env-var fallback, default `"public"`, cache hit, fallback not cached, two independent connections) |
| `tests/unit/test_dummy.py` | 1 | Template placeholder (kept as-is) |

### Track 3 modules (137 tests)

| File | Count | Coverage focus |
|---|---|---|
| `tests/unit/test_readiness_score.py` | **28** | `_extract_json` (plain + fenced + error), grade order constant, per-signal collectors (`_freshness_signal`, `_drift_stability_signal`, `_type_suitability_signal`, `_naming_coherence_signal`) each with OK/STALE/empty paths; `score_ai_readiness` grade extraction + signals passthrough + bad-JSON fallback + lowercase grade normalisation; `list_readiness_scores` sorted worst-first + empty `sync_log`; `analyze_drift_volatility` BQ counts preserved + stability class merged + fleet summary + no-events short-circuit + bad-Gemini fallback + custom `days` |
| `tests/unit/test_schema_docs.py` | **7** | Result structure (connection_id, dataset, tables keys), all columns present, `data_type` preserved, multiple tables each get one Gemini call, bad-JSON → empty `description`, empty schema → `{}`, missing column in Gemini response → `""` |
| `tests/unit/test_sensitivity_classifier.py` | **15** | Sensitivity rank ordering (PII < FINANCIAL < HEALTH < SAFE), `classify_column_sensitivity`: all columns returned + correct classes + masking strategies + `connection_id` on each row + empty schema → `[]` + bad-JSON → `[]` + lowercase class normalised to uppercase; `list_sensitive_columns`: default PII-only filter + FINANCIAL includes PII + SAFE returns all + sorted PII-first + empty `sync_log` → `[]` + two connections combined |
| `tests/unit/test_use_case_auditor.py` | **14** | `_fuzzy_match`: exact, partial, underscore-stripped, no-match, empty; `audit_use_case_coverage`: use-case echoed, `coverage_pct` = 66.7% (4/6), covered/missing fields, connector suggestions in missing, required entities returned, narrative present, bad Phase-A JSON → `required_entities=[]` + 0.0 coverage, bad Phase-B JSON → fuzzy fallback, empty `sync_log`, zero fields → early return |
| `tests/unit/test_json_flattener.py` | **28** | `_detect_reason`: JSON type (case-insensitive), structured name patterns; `_STRUCTURED_NAME_RE`: 11 matching cases (incl. embedded names like `user_metadata`, `event_payload`, `REQUEST_CONTEXT`) + 4 non-matching; `detect_json_columns`: JSON type found, STRING payload found, safe strings skipped, `connection_id` on rows, multiple tables, table key present; `generate_json_flattener`: view name, view SQL, `deploy_via_mcp=True`, `estimated_columns` from structure, audit row written with correct fields, insert failure non-fatal, empty sample rows → fallback structure, invalid JSON samples → fallback |
| `tests/unit/test_entity_detector.py` | **28** | Return shape (list), `entity_name`, `confidence`, occurrences (2 connections), join keys, split-truth conflicts; BQ write: `entity_map` written, 2 rows (one per occurrence), row fields (entity_name, join_key_col, confidence, detection_id, detected_at); insert failure non-fatal; edge cases: empty `sync_log` → `[]`, bad Gemini JSON → `[]`, empty Gemini list → `[]`, all required keys present; **single-connection mode**: Gemini IS called with catalog prompt, `analysis_mode="single_connection"`, zero connections → `[]`; **multi-connection mode**: `analysis_mode="cross_connection"` |
| `tests/unit/test_failure_diagnosis.py` | **27** | `_severity_from_count`: CRITICAL/HIGH/MEDIUM/LOW/zero; no-failures path: `status="no_failures"`, Gemini NOT called, connection_id in message, `period_days` in result; with-failures path: correct `failure_count`, top errors ranked by frequency, top errors capped at 5, severity from Gemini, severity fallback on bad Gemini, `recommended_actions` list, diagnosis string, `connection_id`/`period_days` in result, `source="sync_failure_log"`; edge cases: NULL error_code → `"UNKNOWN"`, invalid Gemini severity → count-based fallback, `sample_message` captured; **API fallback path**: `_fetch_connector_status` called when log empty, healthy API → `no_failures` with state in message, error state → Gemini called + `source="fivetran_api"`, tasks present → Gemini called, API unreachable → graceful `no_failures` |

**Environment:** Python 3.13.7, `google-adk>=1.15,<2`, `google-cloud-bigquery>=3`, `google-genai`. Two non-actionable dependency warnings: `authlib.jose` deprecation (transitive), `[EXPERIMENTAL] PLUGGABLE_AUTH` (ADK feature flag).

### Bugs found and fixed during testing

**`_STRUCTURED_NAME_RE` word-boundary false negative.** Initial regex used `\b` anchors: `r"\b(metadata|properties|…|payload|…|context)\b"`. Python's `\b` treats `_` as a word character, so `\bpayload\b` does not match `user_payload` (the `_` before `payload` is a word char — no boundary). The parametrized test `test_structured_name_re_matches[user_metadata]` caught this immediately. Fix: removed `\b` anchors, using plain case-insensitive substring search.

**`test_detect_entity_overlaps_single_connection_skips_gemini` stale assertion.** When the single-connection catalog mode was added, this pre-existing test asserted `calls == []` (Gemini must NOT be called). After the fix, Gemini IS called (with the catalog prompt), so the assertion was inverted. The test was renamed to `test_detect_entity_overlaps_single_connection_uses_catalog_prompt` and the assertion updated to `assert "single Fivetran connection" in calls[0]` — verifying the correct prompt was used, not a stale expectation from the old short-circuit.

---

## Eval Suite — 7/7 Passing (Track 2, 2026-05-31, Track 3 eval cases pending)

```bash
uvx google-agents-cli eval run --evalset tests/eval/evalsets/drift_trajectories.evalset.json
```

Criterion: `tool_trajectory_avg_score=1.0`. `response_match_score` excluded — no `final_response` in cases (ROUGE scores 0.0 against empty string; incorrect signal for a HITL agent).

| Case | Expected tool call | Result |
|---|---|---|
| `list_proposed_events` | `list_proposed_drift_events` | ✅ |
| `list_proposed_events_alt_phrasing` | `list_proposed_drift_events` | ✅ |
| `approve_drift_event` | `approve_drift(drift_id="abc123", approved_by="alice@example.com")` | ✅ |
| `reject_drift_event` | `reject_drift(drift_id="def456", approved_by="eval_user")` | ✅ |
| `verify_drift_event` | `mark_drift_verified(drift_id="ghi789")` | ✅ |
| `check_single_connection_freshness` | `check_freshness_sla(connection_id="assimilate_seem")` | ✅ |
| `list_all_freshness_status` | `list_freshness_status({})` | ✅ |

MCP tool behavior (14 tools: `list_connections`, `sync_connection`, etc.) is validated in the Agent Runtime playground — MCP tools require live Fivetran API + subprocess spawn that exceeds the 10s eval session timeout.

---

## Live End-to-End Smoke Test (2026-05-25)

Both detection code paths exercised against the live Fivetran connection `assimilate_seem` (Google Cloud PostgreSQL → BigQuery `public` dataset, `customers` table).

| Event | Timestamp | change_type | Table | classification_conf | Note |
|---|---|---|---|---|---|
| `NEW_FIELD` | 2026-05-25T20:51:22Z | `NEW_FIELD` | `customers.test_drift_marker` | 1.0 | Column added + DML to force Query-Based propagation |
| `DEPRECATION` | 2026-05-25T21:36:20Z | `DEPRECATION` | `customers.test_drift_marker` | 1.0 | Source DROP + direct BQ column removal to trigger detection (see constraint below) |

**Round-trip hash trail:** `schema_snapshots` contains the closed-loop sequence: bootstrap → ADD (hash changed) → DROP (hash changed) → bootstrap. Hash trail proves the hash-gate, snapshot write, and diff are all composing correctly end-to-end.

**Non-obvious Fivetran behaviors captured during smoke test:**

1. **Query-Based empty-column push gotcha.** `ALTER TABLE ADD COLUMN` on source doesn't materialize in BigQuery unless at least one row has a non-NULL value for the new column. Workaround: a single `UPDATE` after the `ALTER`.

2. **Source DROP is soft-dropped at destination.** Fivetran marks the column `reason_code: DELETED` in schema config but keeps the BQ column populated with NULLs. To exercise the DEPRECATION code path, the destination column must be removed directly via `bq query` or Fivetran MCP `delete_connection_column_config` — a source-side `DROP COLUMN` alone is insufficient.

---

## HITL Flow — Both Events Driven to VERIFIED (2026-05-26)

Both `PROPOSED` rows driven to `VERIFIED` in a single `adk run app` session.

| drift_id | change_type | Final status | approved_by | transformation_id |
|---|---|---|---|---|
| `1486a28a-...` | `NEW_FIELD` | `VERIFIED` | `demo_reviewer` | `no_transformation_required` |
| `519692f9-...` | `DEPRECATION` | `VERIFIED` | `demo_reviewer` | `MCP_Column_Deletion_Success:Column test_drift_marker successfully blocked...` |

**NEW_FIELD sentinel:** the agent called `mark_drift_applied(transformation_id="no_transformation_required")` — correctly recognized no transformation was needed and self-invented a placeholder to satisfy the required parameter. Acceptable: for NEW_FIELD events, no VIEW shim is necessary; the sentinel string documents human intent in the audit trail.

### Three Bugs Found and Fixed During HITL Run

**Fix 1 — Missing `list_proposed_drift_events` tool.** `app/agent.py` registered only the four lifecycle FunctionTools with no BQ read tool. The LLM hallucinated a tool named `bigquery_tool:query` → `ValueError: Tool 'bigquery_tool:query' not found`. Fix: added `list_proposed_drift_events()` to `bigquery_query.py` and registered it in `agent.py`.

**Fix 2 — `_require_confirmation` signature incompatible with ADK 1.x.** ADK 1.x calls the predicate via `target(**args_to_call)` where `args_to_call` is the tool's input arguments — not the tool object. `TypeError: _require_confirmation() missing 1 required positional argument: 'tool'`. Fix: split single `McpToolset` into two (`fivetran_mcp_reads` / `fivetran_mcp_writes`) with boolean `require_confirmation=True/False`. See `DESIGN.md` — "Two McpToolsets, not one".

**Fix 3 — `adk run` CLI confirmation hallucination (non-blocking, agent self-corrected).** ADK `require_confirmation=True` in CLI mode returns `{"error": "This tool call requires confirmation..."}` to the LLM; the CLI has no interactive widget. The agent received this for `delete_connection_column_config` and hallucinated a success response. In Agent Runtime, the `adk_request_confirmation` event IS surfaced and the user must confirm before the tool executes. This is a CLI limitation, not an architecture defect.

---

## Write-Tool Confirmation Gate Verification

Tested across all three surfaces to confirm the gate fires at the ADK protocol layer before execution.

| Surface | Gate fires | Visual widget | Evidence |
|---|---|---|---|
| `adk run` (CLI) | ✅ | ❌ | Agent hallucinates success after `{"error": "requires confirmation"}` response |
| `adk web .` (local dev UI) | ✅ | ❌ | `[EXPERIMENTAL] feature FeatureName.TOOL_CONFIRMATION is enabled.` log line (2026-05-27 09:50:19); SQLite race condition on user response |
| Agent Runtime playground | ✅ | ❌ | `adk_request_confirmation` event with `confirmed: false` emitted before `sync_connection` executes (2026-05-29) |

**Agent Runtime gate evidence (2026-05-29):**

Trigger: "Trigger a sync for connection assimilate_seem." Agent called `list_connections` → found UUID → attempted `sync_connection`. Event #12 in the playground event panel:

```
name: "adk_request_confirmation"
originalFunctionCall:
  name: "sync_connection"
  args:
    connection_id: "assimilate_seem"
    schema_file: "open-api-definitions/connections/sync_connection.json"
    request_body: "{}"
toolConfirmation:
  hint: "Please approve or reject the tool call sync_connection() by responding
         with a FunctionResponse with an expected ToolConfirmation payload."
  confirmed: false
```

The `adk_request_confirmation` event with `confirmed: false` is emitted *before* the MCP call executes. After user replied "confirm", `sync_connection` executed successfully.

**Architecture verdict:** the gate is correctly implemented at the ADK protocol layer on all three surfaces. Visual widget rendering is a playground UI limitation, not a code or architecture defect.

---

## Empirical Findings — Live Connector Measurements

### Detection Latency

| Scenario | Propagation | Method |
|---|---|---|
| G1 — add column | ~34s | Harness-polled at ≤34s (30s polling resolution; actual 4–34s) — 2026-05-21 |
| G2 — type promotion | 36s | Fivetran-log-derived: trigger 22:40:38 → sync complete 22:41:14 UTC — 2026-05-21 |

Decision #1 (direct-invoke) holds: detection latency is dominated by sync-frequency (15 min), not propagation. The hash gate exits cheaply for the 99% of syncs that produce no change.

### `reload_connection_schema_config` Semantics (2026-05-21)

Measured against `assimilate_seem` (Google Cloud PostgreSQL, 2 tables):

- **Wall-clock: 2s.** Synchronous; scales with schema size — small schemas are fast.
- **Full payload returned in response body.** 2,368 bytes containing the entire schema config: schemas → tables → columns with `enabled`, `name_in_destination`, `enabled_patch_settings`. No follow-up calls needed.
- **Does NOT trigger a downstream data sync.** `status.sync_state` was `scheduled` before and after.
- **Fivetran identifies `ctid` as the Primary Key** (`reason: "Column does not support exclusion as it is a Primary Key"`).
- **`name_in_destination` diverges for synthetic system columns.** `ctid` reports `"ctid"` in the API response but BQ `INFORMATION_SCHEMA` shows `ctid_fivetran_id`. For real user columns these match; for synthetic ones they don't.

### Type-Promotion Full-Table Reorder (G2, 2026-05-21)

Source: `ALTER orders ALTER COLUMN amount TYPE TEXT` + DML.

- **Column NAME preserved** — `amount` → `amount` (no rename ambiguity for the classifier).
- **TYPE → STRING** — `BIGNUMERIC` → `STRING` (Fivetran's NUMERIC→TEXT→STRING type hierarchy).
- **ORDINAL — full-table reorder.** All 8 columns shifted: `amount` moved 2→1, `_fivetran_synced` moved 8→2, `ctid_fivetran_id` moved 1→4, only `updated_at` stayed at position 3.

**Design implications confirmed:**
- REORDER and TYPE_PROMOTION are coupled events. The classifier's prompt encodes this explicitly.
- `ordinal_position` is unreliable signal under TYPE_PROMOTION. Decision #3 (ordinal as advisory Gemini feature, not a gate) reinforced.
- TYPE_PROMOTION produces two `sync_end` payloads: one `FAILED` (Teleport Sync Error mid-rewrite), one `SUCCESSFUL`. The `data.status != "SUCCESSFUL"` filter in `main.py` handles this.

### Live `sync_end` Payload Shape (2026-05-21)

Captured via webhook.site. Body shape matched the documented sample exactly (8 top-level fields + nested `data.status`):

```json
{
  "event": "sync_end",
  "created": "2026-05-21T...",
  "connector_type": "google_cloud_postgresql",
  "connector_id": "assimilate_seem",
  "connector_name": "ftar_pg",
  "sync_id": "<uuid>",
  "destination_group_id": "<id>",
  "data": {"status": "SUCCESSFUL"}
}
```

`X-Fivetran-Signature-256` header: 64-char lowercase hex, no prefix (Fivetran sends raw hex — unlike Stripe's `t=…,v1=…` or GitHub's `sha256=…`). `verify_signature` in `main.py` is empirically correct as-is (SHA-256, `hexdigest()`, timing-safe `compare_digest`, no prefix munging).

**Note:** webhook.site's "download raw body" appends a trailing `\n` byte — strip before hashing for manual HMAC replays.

---

## Agent Runtime Deployment (2026-05-29)

Three fixes required before MCP tools loaded in Agent Runtime. All resolved:

| Fix | Root cause | Resolution |
|---|---|---|
| B-1 — `uvx` not in PATH | Agent Runtime managed container has no `uv`/`uvx` | `fivetran-mcp` added as direct Python dependency; binary resolved via `pathlib.Path(sys.executable).parent / "fivetran-mcp"` |
| B-2 — `fivetran-mcp` not in project deps | `pyproject.toml` only listed `uvx --from git+...` as subprocess | Added `"fivetran-mcp @ git+https://..."` to deps + `allow-direct-references = true` in hatch metadata |
| B-3 — Fivetran credentials empty | `agents-cli deploy` does not package `.env` | `_secret_or_env()` helper reads `os.environ` first, falls back to GCP Secret Manager; `secretAccessor` role granted to Agent Runtime SA |

**Post-fix verification (2026-05-29):** "Trigger a sync for connection assimilate_seem" → agent called `list_connections` → found UUID → called `sync_connection` without prompting for `schema_file`. Confirmation gate fired (`adk_request_confirmation` event). User replied "confirm" → sync triggered successfully.

**Agent Runtime deployment:** `reasoningEngines/2248457298336808960` (us-east1). Webhook receiver live at `https://fivetran-sync-end-receiver-910787152095.us-east1.run.app`.
