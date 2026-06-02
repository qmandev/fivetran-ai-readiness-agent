"""AI use-case completeness auditor — v3 Phase 2, Feature 8.

audit_use_case_coverage(use_case_description) takes a natural-language
description of an intended AI use case and:
  Phase A — calls Gemini to extract required data entities and fields
  Phase B — cross-references available schemas from all synced connections,
             then calls Gemini to assess coverage and suggest connectors for gaps

Returns coverage_pct, covered fields, missing fields (with suggested connector
types), and a narrative summary.

Gemini call pattern: reuses CLASSIFIER_MODEL + _call_gemini from readiness_score.py.
"""

from __future__ import annotations

import json
from typing import Callable

from .bigquery_query import (
    BQ_LOCATION,
    _client,
    _fetch_schema_for_connection,
    _state_table_fqn,
)
from .readiness_score import _call_gemini, _extract_json

_EXTRACT_REQUIREMENTS_PROMPT = """\
You are a data architect. Given the following AI use-case description, extract a \
structured list of required data entities and fields.

USE CASE: {use_case}

Return ONLY a JSON array (no markdown fences). Each element:
  {{"entity": "descriptive entity name", "required_fields": ["field1", "field2", ...], "why": "one-sentence reason"}}

Be specific about field names (e.g. "email_address", not "email info"). \
Aim for 3–8 entities covering the core data requirements.
"""

_COVERAGE_PROMPT = """\
You are a data architect assessing whether available data sources cover an AI use case.

USE CASE: {use_case}

REQUIRED FIELDS (extracted above):
{required_json}

AVAILABLE SCHEMAS (from active Fivetran connections):
{schemas_json}

PRE-COMPUTED COVERAGE MAP (matched by fuzzy field name):
{coverage_map_json}

Task:
1. Confirm or refine the coverage map — a required field is "covered" if a column in \
any connection plausibly contains that data (name similarity or type match).
2. For each missing field, suggest the most likely Fivetran connector type that would \
supply it (e.g. "Salesforce", "Stripe", "Mixpanel", "Postgres").

Return ONLY a JSON object (no markdown fences):
{{
  "covered": [{{"entity": "...", "field": "...", "connection_id": "...", "table": "..."}}],
  "missing": [{{"entity": "...", "field": "...", "suggested_connector_type": "...", "why": "..."}}],
  "narrative": "2-3 sentence coverage summary"
}}
"""


def _fuzzy_match(required_field: str, available_columns: list[dict]) -> dict | None:
    """Return the first column whose name contains the required field token (case-insensitive)."""
    token = required_field.lower().replace("_", "").replace(" ", "")
    for col in available_columns:
        name = col.get("column", "").lower().replace("_", "")
        if token in name or name in token:
            return col
    return None


def audit_use_case_coverage(
    use_case_description: str,
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> dict:
    """Audit whether available Fivetran connections cover an AI use case.

    Two-phase Gemini workflow:
      Phase A — extract required data entities and fields from the use-case description.
      Phase B — cross-reference required fields against all schemas landed by active
                 Fivetran connections; return coverage percentage and connector suggestions
                 for missing fields.

    Args:
        use_case_description: natural-language description of the AI use case,
            e.g. "I want to predict customer churn using support tickets,
            subscription history, and login activity."

    Returns a dict with keys: use_case, required_entities, coverage_pct,
    covered (list), missing (list with suggested_connector_type), narrative.
    """
    # ── Phase A — extract requirements ────────────────────────────────────
    prompt_a = _EXTRACT_REQUIREMENTS_PROMPT.format(use_case=use_case_description)
    raw_a = model_fn(prompt_a)
    try:
        required_entities = _extract_json(raw_a)
        if not isinstance(required_entities, list):
            required_entities = []
    except (json.JSONDecodeError, AttributeError):
        required_entities = []

    all_required_fields: list[dict] = [
        {"entity": entity.get("entity", ""), "field": field}
        for entity in required_entities
        if isinstance(entity, dict)
        for field in entity.get("required_fields", [])
    ]
    total_fields = len(all_required_fields)

    # ── Phase B — cross-reference schemas ─────────────────────────────────
    sql = (
        f"SELECT DISTINCT connection_id FROM {_state_table_fqn('sync_log')} "
        "ORDER BY connection_id"
    )
    rows = _client().query(sql, location=BQ_LOCATION).result()
    connection_ids = [r["connection_id"] for r in rows]

    # Build flat column list annotated with connection_id + table
    available_columns: list[dict] = []
    schemas_summary: dict[str, list[str]] = {}
    for cid in connection_ids:
        schema = _fetch_schema_for_connection(cid)
        for table_key, cols in schema.items():
            table_cols = [c.column_name for c in cols]
            schemas_summary[f"{cid}/{table_key}"] = table_cols
            for col in cols:
                available_columns.append({
                    "connection_id": cid,
                    "table": table_key,
                    "column": col.column_name,
                    "data_type": col.data_type,
                })

    # Pre-compute a simple fuzzy coverage map for Gemini context
    pre_coverage: dict[str, dict | None] = {}
    for rf in all_required_fields:
        field_key = f"{rf['entity']}/{rf['field']}"
        pre_coverage[field_key] = _fuzzy_match(rf["field"], available_columns)

    covered_count = sum(1 for v in pre_coverage.values() if v is not None)

    if total_fields == 0:
        return {
            "use_case": use_case_description,
            "required_entities": required_entities,
            "coverage_pct": 0.0,
            "covered": [],
            "missing": [],
            "narrative": "No required fields could be extracted from the use-case description.",
        }

    coverage_map_list = [
        {
            "entity": rf["entity"],
            "field": rf["field"],
            "matched_column": pre_coverage.get(f"{rf['entity']}/{rf['field']}"),
        }
        for rf in all_required_fields
    ]

    prompt_b = _COVERAGE_PROMPT.format(
        use_case=use_case_description,
        required_json=json.dumps(all_required_fields, indent=2),
        schemas_json=json.dumps(schemas_summary, indent=2),
        coverage_map_json=json.dumps(coverage_map_list, indent=2),
    )
    raw_b = model_fn(prompt_b)
    try:
        result_b = _extract_json(raw_b)
        covered = result_b.get("covered", [])
        missing = result_b.get("missing", [])
        narrative = str(result_b.get("narrative", ""))
    except (json.JSONDecodeError, AttributeError):
        covered = [
            {
                "entity": pre_coverage[f"{rf['entity']}/{rf['field']}"]["connection_id"]
                if pre_coverage.get(f"{rf['entity']}/{rf['field']}")
                else "",
                "field": rf["field"],
                "connection_id": (pre_coverage.get(f"{rf['entity']}/{rf['field']}") or {}).get("connection_id", ""),
                "table": (pre_coverage.get(f"{rf['entity']}/{rf['field']}") or {}).get("table", ""),
            }
            for rf in all_required_fields
            if pre_coverage.get(f"{rf['entity']}/{rf['field']}")
        ]
        missing = [
            {"entity": rf["entity"], "field": rf["field"], "suggested_connector_type": "", "why": ""}
            for rf in all_required_fields
            if not pre_coverage.get(f"{rf['entity']}/{rf['field']}")
        ]
        narrative = raw_b.strip()

    coverage_pct = round(len(covered) / total_fields * 100, 1) if total_fields else 0.0

    return {
        "use_case": use_case_description,
        "required_entities": required_entities,
        "coverage_pct": coverage_pct,
        "covered": covered,
        "missing": missing,
        "narrative": narrative,
    }
