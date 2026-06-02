"""LLM-friendly schema documentation generator — v3 Phase 2, Feature 2.

generate_schema_docs(connection_id) reads every column in the connection's
BigQuery dataset via _fetch_schema_for_connection, then calls Gemini once per
table to produce a one-sentence plain-English description for each column.
The output is structured so a downstream LLM (or the agent itself) can include
it as context when generating SQL queries against the schema.

Gemini call pattern: same lazy client + CLASSIFIER_MODEL constant as every
other v3 tool. model_fn= injectable for unit tests.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from .bigquery_query import (
    BQ_LOCATION,
    _client,
    _fetch_schema_for_connection,
    _state_table_fqn,
)
from .readiness_score import CLASSIFIER_MODEL, _call_gemini, _extract_json

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.+?)\s*```$", re.DOTALL)

_DOCS_PROMPT = """\
You are a data dictionary author. Given the following BigQuery column names and types \
for table `{table}`, write a concise one-sentence plain-English description for each \
column that a downstream LLM can use as context when generating SQL queries.

COLUMNS:
{columns_json}

Return ONLY a JSON array (no markdown fences) with exactly one object per column:
  [{{"column_name": "...", "description": "..."}}]

Keep each description under 20 words. Focus on what the column contains, not its type.
"""


def generate_schema_docs(
    connection_id: str,
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> dict:
    """Generate plain-English column descriptions for every table in a connection.

    Reads the connection's BigQuery schema via INFORMATION_SCHEMA, then calls
    Gemini once per table to produce a one-sentence description per column.
    The result is structured so a downstream LLM can include it as context
    when generating SQL queries.

    Args:
        connection_id: Fivetran connection ID (e.g. 'assimilate_seem').

    Returns a dict with keys: connection_id, dataset (BQ dataset name),
    tables (dict mapping "schema.table_name" to a list of
    {"column_name": str, "data_type": str, "description": str}).
    """
    from ingest.webhook_receiver.connection_resolver import resolve_destination_schema  # noqa: PLC0415
    dataset = resolve_destination_schema(connection_id)
    schema = _fetch_schema_for_connection(connection_id)

    tables: dict[str, list[dict]] = {}
    for table_key, cols in schema.items():
        columns_input = [
            {"column_name": c.column_name, "data_type": c.data_type} for c in cols
        ]
        prompt = _DOCS_PROMPT.format(
            table=table_key,
            columns_json=json.dumps(columns_input, indent=2),
        )
        raw = model_fn(prompt)
        try:
            descriptions = _extract_json(raw)
            desc_by_name = {
                d["column_name"]: d.get("description", "")
                for d in descriptions
                if isinstance(d, dict)
            }
        except (json.JSONDecodeError, KeyError):
            desc_by_name = {}

        table_result = []
        for col in cols:
            table_result.append({
                "column_name": col.column_name,
                "data_type": col.data_type,
                "description": desc_by_name.get(col.column_name, ""),
            })
        tables[table_key] = table_result

    return {
        "connection_id": connection_id,
        "dataset": dataset,
        "tables": tables,
    }
