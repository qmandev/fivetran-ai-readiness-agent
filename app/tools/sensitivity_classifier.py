"""PII / column sensitivity classifier — v3 Phase 2, Feature 3.

classify_column_sensitivity(connection_id) batches all column names + types
for the connection into a single Gemini prompt and classifies each as
PII / FINANCIAL / HEALTH / SAFE with a recommended masking strategy.

list_sensitive_columns(min_sensitivity) runs classify_column_sensitivity for
every connection in sync_log, combines results, and returns columns at or above
the requested sensitivity tier (PII > FINANCIAL > HEALTH > SAFE), sorted by
sensitivity tier (highest risk first).

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

_SENSITIVITY_CLASSES = ("PII", "FINANCIAL", "HEALTH", "SAFE")
_SENSITIVITY_RANK = {cls: i for i, cls in enumerate(_SENSITIVITY_CLASSES)}

_CLASSIFIER_PROMPT = """\
You are a data privacy analyst. Classify each column below as one of:
  PII       — personal identifiers: email, phone, SSN, name, date of birth, address, IP address, user_id, device_id
  FINANCIAL — financial data: amount, revenue, balance, account_number, credit_card, price, salary
  HEALTH    — health/medical: diagnosis, medication, condition, lab_result, patient_id
  SAFE      — no sensitivity: timestamps, product_ids, boolean flags, generic counts, system columns

For each non-SAFE column, also suggest a masking strategy:
  HASH       — one-way hash (for IDs used in joins)
  REDACT     — replace with NULL or fixed string (for display fields)
  TOKENIZE   — reversible token (for fields that need round-tripping)
  GENERALIZE — reduce precision (e.g. exact age → age range, exact location → city)

CONNECTION: {connection_id}
COLUMNS:
{columns_json}

Return ONLY a JSON array (no markdown fences). Each element:
  {{"table": "schema.table_name", "column": "column_name", "sensitivity_class": "...", "masking_strategy": "..." or null}}

Include every column. SAFE columns may have masking_strategy null.
"""


def classify_column_sensitivity(
    connection_id: str,
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> list[dict]:
    """Classify every column in a connection by sensitivity tier.

    Fetches the connection's full schema from INFORMATION_SCHEMA and sends
    all column names + types to Gemini in a single prompt. Returns one entry
    per column with sensitivity_class (PII / FINANCIAL / HEALTH / SAFE) and
    masking_strategy (HASH / REDACT / TOKENIZE / GENERALIZE / null).

    Args:
        connection_id: Fivetran connection ID (e.g. 'assimilate_seem').

    Returns a list of dicts: table, column, sensitivity_class, masking_strategy.
    """
    schema = _fetch_schema_for_connection(connection_id)

    columns_input = [
        {"table": table_key, "column": col.column_name, "data_type": col.data_type}
        for table_key, cols in schema.items()
        for col in cols
    ]

    if not columns_input:
        return []

    prompt = _CLASSIFIER_PROMPT.format(
        connection_id=connection_id,
        columns_json=json.dumps(columns_input, indent=2),
    )
    raw = model_fn(prompt)
    try:
        results = _extract_json(raw)
        if not isinstance(results, list):
            return []
        return [
            {
                "connection_id": connection_id,
                "table": r.get("table", ""),
                "column": r.get("column", ""),
                "sensitivity_class": r.get("sensitivity_class", "SAFE").upper(),
                "masking_strategy": r.get("masking_strategy"),
            }
            for r in results
            if isinstance(r, dict)
        ]
    except (json.JSONDecodeError, AttributeError):
        return []


def list_sensitive_columns(
    min_sensitivity: str = "PII",
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> list[dict]:
    """Return sensitive columns across all connections, filtered by tier.

    Queries sync_log for distinct connection IDs, classifies each, combines
    results, and filters to columns at or above min_sensitivity. Sorted by
    sensitivity tier descending (PII first), then connection_id, then table,
    then column.

    Args:
        min_sensitivity: minimum tier to include — one of 'PII', 'FINANCIAL',
            'HEALTH', 'SAFE'. Default 'PII' returns only PII columns. Pass
            'HEALTH' to include HEALTH + FINANCIAL + PII. Pass 'SAFE' for all.

    Returns a list of dicts: connection_id, table, column, sensitivity_class,
    masking_strategy.
    """
    min_rank = _SENSITIVITY_RANK.get(min_sensitivity.upper(), 0)

    sql = (
        f"SELECT DISTINCT connection_id FROM {_state_table_fqn('sync_log')} "
        "ORDER BY connection_id"
    )
    rows = _client().query(sql, location=BQ_LOCATION).result()
    connection_ids = [r["connection_id"] for r in rows]

    combined: list[dict] = []
    for cid in connection_ids:
        results = classify_column_sensitivity(cid, model_fn=model_fn)
        for r in results:
            cls = r.get("sensitivity_class", "SAFE").upper()
            rank = _SENSITIVITY_RANK.get(cls, len(_SENSITIVITY_CLASSES))
            if rank <= min_rank:
                combined.append(r)

    combined.sort(key=lambda r: (
        _SENSITIVITY_RANK.get(r.get("sensitivity_class", "SAFE").upper(), 99),
        r.get("connection_id", ""),
        r.get("table", ""),
        r.get("column", ""),
    ))
    return combined
