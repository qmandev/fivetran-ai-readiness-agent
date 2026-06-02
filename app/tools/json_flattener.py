"""JSON / semi-structured column flattener — v3 Phase 3, Feature 5.

detect_json_columns(connection_id) scans INFORMATION_SCHEMA for columns whose
type is JSON or whose name matches a structured-payload pattern (metadata,
properties, payload, …). Returns a list of candidates with the detection reason.

generate_json_flattener(connection_id, table, column) samples 5 live BQ rows to
infer the JSON structure, calls Gemini to generate a CREATE OR REPLACE VIEW DDL
that flattens the column into typed columns, writes an audit row to
`json_flattener_log`, and returns the view name + SQL for the agent to deploy
via the Fivetran MCP `create_transformation` tool in a subsequent turn.

Gemini call pattern: reuses CLASSIFIER_MODEL + _call_gemini from readiness_score.py.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Callable

from .bigquery_query import (
    BQ_LOCATION,
    _client,
    _fetch_schema_for_connection,
    _project,
    _state_dataset,
    _state_table_fqn,
)
from .readiness_score import _call_gemini, _extract_json

# Regex matching column names that likely hold structured JSON payloads.
# No \b anchors — underscores are word chars so "user_metadata" would not match
# \bmetadata\b. Plain case-insensitive substring search is correct here.
_STRUCTURED_NAME_RE = re.compile(
    r"(metadata|properties|attributes|payload|details|extras|config|context)",
    re.IGNORECASE,
)


def _detect_reason(col_name: str, col_type: str) -> str | None:
    """Return a detection reason string, or None if the column is not a candidate."""
    if col_type.upper() == "JSON":
        return "data_type=JSON"
    if col_type.upper() == "STRING" and _STRUCTURED_NAME_RE.search(col_name):
        return f"STRING column name matches structured-payload pattern ({col_name})"
    return None


def detect_json_columns(connection_id: str) -> list[dict]:
    """Detect JSON and semi-structured STRING columns in a connection's schema.

    Scans INFORMATION_SCHEMA for columns whose BigQuery type is JSON, or whose
    name matches a structured-payload naming convention (metadata, properties,
    attributes, payload, details, extras, config, context).

    Args:
        connection_id: Fivetran connection ID (e.g. 'assimilate_seem').

    Returns a list of dicts: connection_id, table (schema.table_name), column,
    data_type, reason (why it was flagged).
    """
    schema = _fetch_schema_for_connection(connection_id)
    results = []
    for table_key, cols in schema.items():
        for col in cols:
            reason = _detect_reason(col.column_name, col.data_type)
            if reason:
                results.append({
                    "connection_id": connection_id,
                    "table": table_key,
                    "column": col.column_name,
                    "data_type": col.data_type,
                    "reason": reason,
                })
    return results


_FLATTENER_PROMPT = """\
You are a BigQuery SQL expert. Given the following JSON structure sampled from a \
BigQuery column, generate a CREATE OR REPLACE VIEW DDL that flattens the JSON column \
into individual typed columns so downstream LLMs and BI tools can query it directly.

SOURCE: `{dataset}.{table}` — column `{column}` (type: {data_type})

INFERRED JSON STRUCTURE (from 5 sampled rows):
{structure_json}

Requirements:
- View name: `{dataset}.{table}_flat`
- Include all existing non-JSON columns from the source table using `* EXCEPT({column})`
- Add one BQ column per top-level JSON key using JSON_VALUE / JSON_QUERY as appropriate
- Use SAFE_CAST where type conversion might fail
- Wrap string values with JSON_VALUE, nested objects/arrays with JSON_QUERY
- Return ONLY the SQL DDL string — no markdown fences, no explanation
"""


def generate_json_flattener(
    connection_id: str,
    table: str,
    column: str,
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> dict:
    """Generate a BigQuery VIEW that flattens a JSON/semi-structured column.

    Samples 5 live rows to infer the JSON structure, calls Gemini to generate
    a CREATE OR REPLACE VIEW DDL, writes an audit row to json_flattener_log,
    and returns the view name + SQL. The returned view_sql can be passed to the
    Fivetran MCP create_transformation tool in a subsequent agent turn.

    Args:
        connection_id: Fivetran connection ID.
        table: fully-qualified table key as returned by detect_json_columns
            (e.g. 'public.events').
        column: column name to flatten.

    Returns a dict: view_name, view_sql, estimated_columns (int),
    deploy_via_mcp (True — reminder to the agent to use create_transformation).
    """
    from google.cloud import bigquery  # noqa: PLC0415
    from ingest.webhook_receiver.connection_resolver import resolve_destination_schema  # noqa: PLC0415
    dataset = resolve_destination_schema(connection_id)

    # Determine the actual BQ table path from the "schema.table" key
    parts = table.split(".", 1)
    bq_table = f"{_project()}.{dataset}.{parts[1] if len(parts) > 1 else table}"

    # Sample up to 5 non-NULL rows to infer JSON structure
    sample_sql = (
        f"SELECT `{column}` FROM `{bq_table}` "
        f"WHERE `{column}` IS NOT NULL LIMIT 5"
    )
    try:
        rows = list(_client().query(sample_sql, location=BQ_LOCATION).result())
        samples = [row[column] for row in rows]
    except Exception:
        samples = []

    # Infer structure from samples
    structure: dict[str, str] = {}
    for s in samples:
        try:
            parsed = json.loads(s) if isinstance(s, str) else s
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    if k not in structure:
                        structure[k] = type(v).__name__
        except (json.JSONDecodeError, TypeError):
            continue

    # Fall back to minimal structure if sampling produced nothing
    if not structure:
        structure = {"_unknown_key": "str"}

    # Determine data_type from schema
    schema = _fetch_schema_for_connection(connection_id)
    col_type = "STRING"
    for table_key, cols in schema.items():
        if table_key == table:
            for col in cols:
                if col.column_name == column:
                    col_type = col.data_type
                    break

    prompt = _FLATTENER_PROMPT.format(
        dataset=dataset,
        table=parts[1] if len(parts) > 1 else table,
        column=column,
        data_type=col_type,
        structure_json=json.dumps(structure, indent=2),
    )
    view_sql = model_fn(prompt).strip()
    view_name = f"{dataset}.{parts[1] if len(parts) > 1 else table}_flat"

    # Write audit row (streaming insert — append-only, no lifecycle updates)
    log_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    log_row = {
        "log_id": log_id,
        "connection_id": connection_id,
        "table_name": table,
        "column_name": column,
        "view_name": view_name,
        "view_sql": view_sql,
        "generated_at": now,
        "deployed": False,
    }
    try:
        ref = f"{_project()}.{_state_dataset()}.json_flattener_log"
        errors = _client().insert_rows_json(ref, [log_row])
        if errors:
            import logging
            logging.getLogger(__name__).warning(
                "json_flattener_log insert failed: %s", errors
            )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning(
            "json_flattener_log insert error (non-fatal): %s", exc
        )

    return {
        "view_name": view_name,
        "view_sql": view_sql,
        "estimated_columns": len(structure),
        "deploy_via_mcp": True,
    }
