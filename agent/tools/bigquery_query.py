"""BigQuery tool: INFORMATION_SCHEMA reads + state-store read/write.

Skeleton — function signatures and contracts only. Implementations TODO.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnRecord:
    table_schema: str
    table_name: str
    column_name: str
    data_type: str
    ordinal_position: int
    is_nullable: bool


def fetch_landed_columns(connection_id: str, destination_schema: str) -> list[ColumnRecord]:
    """Query the destination INFORMATION_SCHEMA.COLUMNS for one connection's
    landed tables. This is the authoritative truth for what downstream
    consumers actually see.
    """
    # TODO: SELECT table_schema, table_name, column_name, data_type,
    #       ordinal_position, is_nullable
    #       FROM `<dataset>`.INFORMATION_SCHEMA.COLUMNS
    #       WHERE table_schema = @destination_schema
    raise NotImplementedError


def write_snapshot(snapshot_row: dict, column_rows: list[dict]) -> None:
    """Insert one schema_snapshots row and its column_snapshots rows."""
    # TODO: streaming insert / load job into agent_state.*
    raise NotImplementedError


def latest_snapshot(connection_id: str) -> dict | None:
    """Return the most recent schema_snapshots row for a connection, or None
    if this is the first capture (bootstrap case).
    """
    # TODO: SELECT ... ORDER BY captured_at DESC LIMIT 1
    raise NotImplementedError


def load_columns(snapshot_id: str) -> list[ColumnRecord]:
    """Load all column_snapshots rows for a given snapshot."""
    raise NotImplementedError


def write_drift_event(event: dict) -> None:
    """Insert or update a drift_events row (remediation lifecycle)."""
    raise NotImplementedError


# No handler() dispatch shim. Each typed function above is registered
# directly as an ADK FunctionTool in agent.py — ADK generates the tool
# schema from the signature, so keep these signatures typed and discrete.
