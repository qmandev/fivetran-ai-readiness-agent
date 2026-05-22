"""Gemini classification + remediation SQL generation (algorithm steps 6-7).

The classifier takes a *candidate* ColumnChange (emitted by
`snapshot_diff.diff_columns`) and produces a final Classification — the
locked change type, a 0..1 confidence, a rationale, and the BigQuery VIEW-
shim SQL that preserves the downstream contract.

Architecture:
  - _build_prompt        : pure; builds the structured Gemini prompt
  - _extract_json        : pure; strips markdown fences, parses JSON
  - _parse_response      : pure; validates fields, constructs Classification
  - _call_gemini         : the only side-effect; Vertex AI Gemini SDK call
  - classify             : composes the four (the public entry point)

Pure helpers are unit-tested offline. The Gemini call uses Vertex AI credits
when invoked, so unit tests avoid it via dependency injection (callers can
pass a custom `model_fn` for testing, defaulting to `_call_gemini`).

NAME-MAPPING CAVEAT (G3 finding, 2026-05-21):
  Diff input comes from BigQuery INFORMATION_SCHEMA — i.e., DESTINATION-side
  column names. Fivetran's column-config APIs (`modify_connection_column_config`,
  `delete_connection_column_config`) take SOURCE-side names. For ordinary user
  columns these match (`customer_id` = `customer_id`). For Fivetran-synthetic
  columns they diverge — observed: source `ctid` lands in BQ as
  `ctid_fivetran_id`. When this module produces a remediation that calls
  Fivetran APIs, it MUST pass source-side names, not the BQ names from the
  diff. `exclude_system_columns` in snapshot_diff.py currently filters all
  observed Fivetran synthetics, so the agent never targets them for
  remediation — but treat that as a load-bearing assumption: if the
  exclusion rules ever loosen, name-mapping logic must be added here.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from .bigquery_query import ColumnRecord
from .snapshot_diff import ColumnChange

CHANGE_TYPES = ("RENAME", "TYPE_PROMOTION", "REORDER", "NEW_FIELD", "DEPRECATION")

# Model used for classification. Matches `app/agent.py`'s default per
# CLAUDE.md "NEVER change the model." Design-doc note: gemini-3.1-pro-preview
# is the considered upgrade for semantic RENAME-vs-DEPRECATION + SQL gen;
# left as a tuning decision for the eval loop, not changed here.
CLASSIFIER_MODEL = "gemini-flash-latest"


@dataclass(frozen=True)
class Classification:
    change_type: str           # final type from CHANGE_TYPES
    confidence: float          # 0..1 — Gemini's self-estimated confidence
    rationale: str             # short prose justification
    remediation_sql: str       # BQ VIEW shim; empty when no shim is needed


# ── Pure helpers (testable; no LLM call) ────────────────────────────────────

def _column_summary(col: ColumnRecord | None) -> dict[str, Any] | None:
    """Compact dict representation of a column for the prompt JSON block.
    None passes through (represents "absent" — added vs not-yet-present, or
    removed vs no-longer-present)."""
    if col is None:
        return None
    return {
        "schema": col.table_schema,
        "table": col.table_name,
        "name": col.column_name,
        "type": col.data_type,
        "ordinal": col.ordinal_position,
        "nullable": col.is_nullable,
    }


def _ordinal_delta(change: ColumnChange) -> int | None:
    """Ordinal_position delta — advisory feature only (Decision #3); do NOT
    gate classification on it."""
    if change.before is None or change.after is None:
        return None
    return change.after.ordinal_position - change.before.ordinal_position


_INSTRUCTION = """\
You are a Fivetran schema-drift remediation classifier.

INPUT — a candidate ColumnChange detected by the snapshot diff:
{change_block}

ADVISORY context (do NOT gate on these — they are signals, not rules):
  ordinal_delta            : {ordinal_delta} (per Decision #3, ordinal change
                             alone is NOT evidence; co-occurring REORDERs on
                             the same table accompany every TYPE_PROMOTION
                             per G2 finding — Fivetran rewrites the whole
                             table layout on a type change.)
  downstream_consumers     : {downstream_refs}

TASK
1. Decide the FINAL change_type from this fixed set:
       RENAME, TYPE_PROMOTION, REORDER, NEW_FIELD, DEPRECATION
   - candidate_change_type=RENAME: confirm via name semantics. A plausible
     rename has obviously-related names (customer_id -> cust_id;
     created_at -> created_timestamp). If names are unrelated, fall back to
     a (NEW_FIELD + DEPRECATION) interpretation by returning whichever side
     this change row represents.
   - candidate_change_type=TYPE_PROMOTION: verify the type widening fits
     Fivetran's documented type hierarchy (NUMERIC -> STRING is common).
   - candidate_change_type=REORDER: if this REORDER is part of a co-occurring
     TYPE_PROMOTION on the same table, mark confidence low (the classifier
     workflow may filter it out as collateral).
2. Estimate confidence on a 0..1 scale. Use <0.6 for ambiguous cases — the
   agent treats low-confidence events as needing human input.
3. Provide a rationale (<=200 words).
4. Generate a BigQuery VIEW-shim SQL that preserves the downstream
   contract. The shim is deployed via Fivetran's `transformations` API
   (project.dataset.view).
   - RENAME: `CREATE OR REPLACE VIEW {{dataset}}.{{table}}_shim AS
              SELECT *, {{new_col}} AS {{old_col}} FROM {{dataset}}.{{table}}`
   - TYPE_PROMOTION: `CREATE OR REPLACE VIEW {{dataset}}.{{table}}_shim AS
              SELECT *, CAST({{col}} AS {{OLD_TYPE}}) AS {{col}}_legacy
              FROM {{dataset}}.{{table}}` — preserves access to the pre-
              promotion type for downstream consumers that assumed the
              narrower type.
   - REORDER: return "" (BigQuery SELECTs reference by name; no shim
              needed unless a consumer relies on column position, which is
              not standard practice).
   - NEW_FIELD: return "" (additive; no breakage).
   - DEPRECATION: `CREATE OR REPLACE VIEW {{dataset}}.{{table}}_shim AS
              SELECT *, CAST(NULL AS {{OLD_TYPE}}) AS {{dropped_col}}
              FROM {{dataset}}.{{table}}` — keeps the column reference
              alive (with NULL) until consumers are updated.

OUTPUT — return ONLY a JSON object with EXACTLY these keys:
  change_type      : one of the five strings above
  confidence       : float in [0, 1]
  rationale        : string
  remediation_sql  : string (may be empty)

Do NOT wrap the JSON in markdown fences. Do NOT include any prose outside
the JSON object.
"""


def _build_prompt(change: ColumnChange, downstream_refs: list[str]) -> str:
    """Construct the classifier prompt. Pure function — composes the
    structured input from the ColumnChange and the advisory context."""
    change_block = json.dumps({
        "table": f"{change.table_schema}.{change.table_name}",
        "candidate_change_type": change.change_type,
        "before": _column_summary(change.before),
        "after": _column_summary(change.after),
    }, indent=2)
    return _INSTRUCTION.format(
        change_block=change_block,
        ordinal_delta=_ordinal_delta(change),
        downstream_refs=downstream_refs or "[]",
    )


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.+?)\s*```$", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any]:
    """Strip a leading/trailing markdown code fence (some Gemini outputs add
    one even when instructed not to), then parse as JSON. Raises ValueError
    on parse failure."""
    s = text.strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(s)
    except json.JSONDecodeError as e:
        raise ValueError(f"classifier response is not JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError(f"classifier response is not a JSON object: {type(obj).__name__}")
    return obj


def _parse_response(text: str) -> Classification:
    """Validate and convert a raw Gemini response to a Classification.
    Raises ValueError on any contract violation — caller decides whether to
    retry, surface to human, or treat as low-confidence."""
    payload = _extract_json(text)
    for required in ("change_type", "confidence", "rationale", "remediation_sql"):
        if required not in payload:
            raise ValueError(f"classifier response missing field: {required}")
    ct = payload["change_type"]
    if ct not in CHANGE_TYPES:
        raise ValueError(
            f"classifier returned unknown change_type {ct!r}; "
            f"must be one of {CHANGE_TYPES}"
        )
    try:
        conf = float(payload["confidence"])
    except (TypeError, ValueError) as e:
        raise ValueError(f"classifier confidence not a number: {payload['confidence']!r}") from e
    if not 0.0 <= conf <= 1.0:
        raise ValueError(f"classifier confidence out of [0,1]: {conf}")
    return Classification(
        change_type=ct,
        confidence=conf,
        rationale=str(payload["rationale"]),
        remediation_sql=str(payload["remediation_sql"]),
    )


# ── Gemini call (the side-effect boundary) ──────────────────────────────────

def _call_gemini(prompt: str) -> str:
    """Vertex AI Gemini call. Uses the project/location from env vars set
    by app/agent.py's auth bootstrap. Returns the response text verbatim."""
    from google import genai  # noqa: PLC0415 — lazy import
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ["GCP_PROJECT_ID"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    client = genai.Client(vertexai=True, project=project, location=location)
    response = client.models.generate_content(
        model=CLASSIFIER_MODEL,
        contents=prompt,
    )
    return response.text


# ── Public entry point ─────────────────────────────────────────────────────

def classify(
    change: ColumnChange,
    downstream_refs: list[str] | None = None,
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> Classification:
    """Run a candidate ColumnChange through Gemini and return a finalized
    Classification. The optional `model_fn` lets tests inject a stub
    response without making a real LLM call; production callers leave it
    at the default `_call_gemini`.

    Errors from the model call or response parsing are raised verbatim —
    caller (the workflow) decides on retry vs surface to human.
    """
    prompt = _build_prompt(change, downstream_refs or [])
    response_text = model_fn(prompt)
    return _parse_response(response_text)


# No handler() shim — `classify` is registered directly as an ADK
# FunctionTool on the classifier LlmAgent in agent.py.
