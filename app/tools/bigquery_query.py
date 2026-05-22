"""BigQuery tool: INFORMATION_SCHEMA reads + state-store read/write.

Two responsibilities, separated so the pure SQL/parsing helpers stay
unit-testable without a live BigQuery client:

  1. Read the LANDING dataset's INFORMATION_SCHEMA.COLUMNS — the source of
     truth on what columns Fivetran has actually landed.
  2. Read/write the three state-store tables in `agent_state`:
     schema_snapshots, column_snapshots, drift_events.

Region-pinning (F finding 2026-05-19): every BigQuery query MUST run with
location='us-east1'. Omitting it sends the job to the US multi-region and
yields the misleading 'Dataset not found in location US' error even when
the dataset exists. The constant BQ_LOCATION enforces this.

Caller-side write_drift_event split (G3 finding 2026-05-21): drift_events
rows have a lifecycle (PROPOSED -> APPROVED -> APPLIED -> VERIFIED), so the
original single-function 'write_drift_event' is split into:
  - insert_drift_event() for the initial PROPOSED write
  - update_drift_event() for status transitions
Splitting avoids streaming-buffer issues (rows inserted via streaming are
not immediately UPDATE-able for up to 90 min) — the initial insert uses a
parameterized INSERT query, so subsequent UPDATEs work right away.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ColumnRecord:
    table_schema: str
    table_name: str
    column_name: str
    data_type: str
    ordinal_position: int
    is_nullable: bool


# ── Region — pinned in design Decision #4; do not change without revisiting.
BQ_LOCATION = "us-east1"


# ── Config accessors (read at call time so tests can monkeypatch env) ──────

def _project() -> str:
    """GCP project ID. Prefer the canonical GOOGLE_CLOUD_PROJECT env var
    (set by the agents-cli template + the ADK auth bootstrap); fall back to
    GCP_PROJECT_ID for parity with deploy/env.example."""
    return os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ["GCP_PROJECT_ID"]


def _state_dataset() -> str:
    return os.environ.get("BQ_STATE_DATASET", "agent_state")


# ── Pure helpers (no BQ client; unit-testable) ─────────────────────────────

def _columns_query(project: str, dataset: str) -> str:
    """SQL string for INFORMATION_SCHEMA.COLUMNS on the given dataset.
    Pure function — kept separate from execution for unit testing.
    """
    return (
        "SELECT table_schema, table_name, column_name, data_type, "
        "ordinal_position, is_nullable "
        f"FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS` "
        "ORDER BY table_schema, table_name, ordinal_position"
    )


def _state_table_fqn(table: str) -> str:
    """Fully-qualified state-store table reference, backtick-quoted for SQL."""
    return f"`{_project()}.{_state_dataset()}.{table}`"


def _row_to_column_record(row: Any) -> ColumnRecord:
    """Convert a BigQuery row (or dict) into a ColumnRecord. Handles the
    INFORMATION_SCHEMA.COLUMNS shape — `is_nullable` is the string 'YES'/'NO'
    there, not a bool; in our state-store column_snapshots table it's stored
    as a real BOOL.
    """
    nullable = row["is_nullable"]
    if isinstance(nullable, str):
        nullable = nullable.upper() == "YES"
    return ColumnRecord(
        table_schema=row["table_schema"],
        table_name=row["table_name"],
        column_name=row["column_name"],
        data_type=row["data_type"],
        ordinal_position=int(row["ordinal_position"]),
        is_nullable=bool(nullable),
    )


# ── BigQuery client (lazy import + lazy construction) ──────────────────────

def _client():
    """Lazy bigquery.Client. Lazy import lets the module load (and unit
    tests covering pure helpers run) without google-cloud-bigquery being
    installed, although it is a declared project dependency.
    """
    from google.cloud import bigquery  # noqa: PLC0415 — lazy on purpose
    return bigquery.Client(project=_project(), location=BQ_LOCATION)


# ── Reads ──────────────────────────────────────────────────────────────────

def fetch_landed_columns(
    connection_id: str, destination_schema: str
) -> list[ColumnRecord]:
    """Query INFORMATION_SCHEMA.COLUMNS for the landed dataset.

    `destination_schema` is the BigQuery dataset name (e.g. 'public' — the
    Google Cloud PostgreSQL connector ignores the Fivetran schema prefix,
    F finding 2026-05-20). `connection_id` is accepted for future per-
    connection scoping but currently the dataset-name parameter is what
    actually selects the columns.

    Returns columns INCLUDING Fivetran system columns. Filter via
    `snapshot_diff.exclude_system_columns()` before hashing/diffing.
    """
    sql = _columns_query(_project(), destination_schema)
    rows = _client().query(sql, location=BQ_LOCATION).result()
    return [_row_to_column_record(r) for r in rows]


def latest_snapshot(connection_id: str) -> dict | None:
    """Most-recent schema_snapshots row for a connection, or None on
    bootstrap (no prior snapshots exist).
    """
    from google.cloud import bigquery  # noqa: PLC0415
    sql = (
        "SELECT snapshot_id, connection_id, connection_name, "
        "destination_schema, captured_at, trigger_event, sync_id, "
        "column_count, content_hash "
        f"FROM {_state_table_fqn('schema_snapshots')} "
        "WHERE connection_id = @connection_id "
        "ORDER BY captured_at DESC "
        "LIMIT 1"
    )
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("connection_id", "STRING", connection_id),
    ])
    rows = _client().query(sql, location=BQ_LOCATION, job_config=cfg).result()
    for r in rows:
        return dict(r)
    return None


def load_columns(snapshot_id: str) -> list[ColumnRecord]:
    """Load all column_snapshots rows for a given snapshot."""
    from google.cloud import bigquery  # noqa: PLC0415
    sql = (
        "SELECT table_schema, table_name, column_name, data_type, "
        "ordinal_position, is_nullable "
        f"FROM {_state_table_fqn('column_snapshots')} "
        "WHERE snapshot_id = @snapshot_id "
        "ORDER BY table_schema, table_name, ordinal_position"
    )
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("snapshot_id", "STRING", snapshot_id),
    ])
    rows = _client().query(sql, location=BQ_LOCATION, job_config=cfg).result()
    return [_row_to_column_record(r) for r in rows]


# ── Writes ─────────────────────────────────────────────────────────────────

def write_snapshot(snapshot_row: dict, column_rows: list[dict]) -> None:
    """Insert one `schema_snapshots` row plus its `column_snapshots` rows
    via streaming insert (`insert_rows_json`).

    Snapshots are append-only — we never UPDATE them — so the streaming-
    buffer's eventual-consistency-to-DML doesn't affect us. Both inserts
    are issued; any row-level errors are surfaced as a RuntimeError so the
    caller can decide (typically: bubble to the agent + drift_events record).
    """
    client = _client()
    project, dataset = _project(), _state_dataset()
    snap_ref = f"{project}.{dataset}.schema_snapshots"
    errors = client.insert_rows_json(snap_ref, [snapshot_row])
    if errors:
        raise RuntimeError(f"insert into schema_snapshots failed: {errors}")
    if column_rows:
        col_ref = f"{project}.{dataset}.column_snapshots"
        errors = client.insert_rows_json(col_ref, column_rows)
        if errors:
            raise RuntimeError(f"insert into column_snapshots failed: {errors}")


# drift_events parameter shape ------------------------------------------------
# Required keys for insert_drift_event(event):
#   drift_id, connection_id, detected_at, from_snapshot_id, to_snapshot_id,
#   table_schema, table_name, change_type, column_before, column_after,
#   classification_conf, gemini_rationale, remediation_sql,
#   transformation_id, remediation_status, approved_by, updated_at
# column_before/column_after may be passed as dict (will be JSON-serialized).
_DRIFT_EVENT_FIELDS = (
    "drift_id", "connection_id", "detected_at", "from_snapshot_id",
    "to_snapshot_id", "table_schema", "table_name", "change_type",
    "column_before", "column_after", "classification_conf",
    "gemini_rationale", "remediation_sql", "transformation_id",
    "remediation_status", "approved_by", "updated_at",
)


def _as_json_string(v: Any) -> str | None:
    """Serialize a dict to a JSON string for PARSE_JSON. None passes through."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    return json.dumps(v)


def insert_drift_event(event: dict) -> None:
    """Initial PROPOSED-state row for a detected drift. Uses a parameterized
    INSERT query (NOT streaming insert) so a subsequent update_drift_event
    can UPDATE the row immediately — streaming-inserted rows are pinned in
    the buffer for up to ~90 min during which DML can't see them.

    Caller must supply the full event dict; see _DRIFT_EVENT_FIELDS.
    """
    from google.cloud import bigquery  # noqa: PLC0415
    cols = ", ".join(_DRIFT_EVENT_FIELDS)
    placeholders = ", ".join(_drift_event_placeholder(f) for f in _DRIFT_EVENT_FIELDS)
    sql = (
        f"INSERT INTO {_state_table_fqn('drift_events')} ({cols}) "
        f"VALUES ({placeholders})"
    )
    params = _drift_event_params(event, bigquery)
    cfg = bigquery.QueryJobConfig(query_parameters=params)
    _client().query(sql, location=BQ_LOCATION, job_config=cfg).result()


def update_drift_event(drift_id: str, **updates: Any) -> None:
    """Update a drift_events row's lifecycle fields. Common transitions:
        remediation_status: PROPOSED -> APPROVED -> APPLIED -> VERIFIED
        transformation_id: set when APPLIED (Fivetran transformation ID)
        approved_by: set when APPROVED (user identity from the approval step)
    `updated_at` is set automatically to CURRENT_TIMESTAMP().
    """
    from google.cloud import bigquery  # noqa: PLC0415
    if not updates:
        return
    set_clauses = []
    params = [bigquery.ScalarQueryParameter("drift_id", "STRING", drift_id)]
    for key, val in updates.items():
        if key not in _DRIFT_EVENT_FIELDS:
            raise ValueError(f"unknown drift_events field: {key}")
        set_clauses.append(f"{key} = @{key}")
        params.append(_scalar_param(key, val, bigquery))
    # Auto-set updated_at unless the caller explicitly passed it. Avoids the
    # duplicate-SET-target SQL error that would result from appending both
    # the caller's value and CURRENT_TIMESTAMP().
    if "updated_at" not in updates:
        set_clauses.append("updated_at = CURRENT_TIMESTAMP()")
    sql = (
        f"UPDATE {_state_table_fqn('drift_events')} "
        f"SET {', '.join(set_clauses)} "
        "WHERE drift_id = @drift_id"
    )
    cfg = bigquery.QueryJobConfig(query_parameters=params)
    _client().query(sql, location=BQ_LOCATION, job_config=cfg).result()


def _drift_event_placeholder(field: str) -> str:
    """JSON columns need PARSE_JSON in the VALUES clause; other types
    use a plain parameter reference."""
    if field in ("column_before", "column_after"):
        return f"PARSE_JSON(@{field})"
    return f"@{field}"


def _drift_event_params(event: dict, bigquery) -> list:
    """Build BigQuery query parameters for an insert_drift_event call."""
    params = []
    for field in _DRIFT_EVENT_FIELDS:
        val = event.get(field)
        if field in ("column_before", "column_after"):
            params.append(
                bigquery.ScalarQueryParameter(field, "STRING", _as_json_string(val))
            )
        else:
            params.append(_scalar_param(field, val, bigquery))
    return params


def _scalar_param(name: str, value: Any, bigquery):
    """Map a Python value to a BigQuery ScalarQueryParameter with an
    appropriate type. Keep narrow — we only need the few types the schema
    actually uses (STRING, FLOAT64, TIMESTAMP, JSON-as-STRING)."""
    if name == "detected_at" or name == "updated_at":
        bq_type = "TIMESTAMP"
    elif name == "classification_conf":
        bq_type = "FLOAT64"
    elif name in ("column_before", "column_after"):
        bq_type = "STRING"   # JSON serialized to string; PARSE_JSON applied in SQL
        value = _as_json_string(value)
    else:
        bq_type = "STRING"
    return bigquery.ScalarQueryParameter(name, bq_type, value)


# No handler() dispatch shim. Each typed function above is registered
# directly as an ADK FunctionTool in agent.py — ADK generates the tool
# schema from the signature, so keep these signatures typed and discrete.
