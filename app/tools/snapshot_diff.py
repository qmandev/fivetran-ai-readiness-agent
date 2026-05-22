"""Snapshot capture, hash-gate, and column diff (detection algorithm steps 1-5).

Skeleton — signatures and the rename heuristic contract. Implementations TODO.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .bigquery_query import ColumnRecord

# Resolved (see design doc): ordinal_position is an advisory FEATURE passed to
# Gemini, not a gate. The diff favors recall — it pairs any removed+added
# column with matching data_type in the same table as a rename candidate, and
# hands Gemini the ordinal delta as one input signal. This value is metadata
# for prompt context only; it does NOT filter candidates out.
RENAME_ORDINAL_TOLERANCE = None  # advisory-only; not a cutoff

# Fivetran-injected system columns observed in BigQuery for the Google Cloud
# PostgreSQL connector (verified live, 2026-05-20):
#   _fivetran_synced   TIMESTAMP — changes every sync; breaks the hash gate if not filtered
#   _fivetran_deleted  BOOL      — Soft delete mode marker
#   ctid_fivetran_id   STRING    — combined ctid + row-hash tracking. NOTE: this
#                                  connector ships `ctid_fivetran_id` (NOT the
#                                  docs-documented `_fivetran_id`), so a
#                                  prefix-only check misses it.
#
# Rule: a column is a Fivetran system column iff it STARTS with `_fivetran_`
# OR ENDS with `_fivetran_id`. The suffix branch catches `ctid_fivetran_id`
# while staying defensive against unrelated source columns that merely
# contain "fivetran" elsewhere in the name.
FIVETRAN_SYSTEM_PREFIX = "_fivetran_"
FIVETRAN_SYSTEM_ID_SUFFIX = "_fivetran_id"


def exclude_system_columns(columns: list[ColumnRecord]) -> list[ColumnRecord]:
    """Drop Fivetran-injected system columns. Apply at the boundary, before
    content_hash() and diff_columns(), so system columns never enter a
    snapshot, the hash gate, or a drift event.
    """
    return [
        c for c in columns
        if not (
            c.column_name.startswith(FIVETRAN_SYSTEM_PREFIX)
            or c.column_name.endswith(FIVETRAN_SYSTEM_ID_SUFFIX)
        )
    ]


def content_hash(columns: list[ColumnRecord]) -> str:
    """sha256 over sorted (schema, table, column, type, ordinal) tuples.
    Cheap gate: if this equals the prior snapshot's hash, nothing changed.

    Caller contract: pass columns already filtered via
    exclude_system_columns(). _fivetran_synced changes every sync, so an
    unfiltered list defeats the gate entirely.
    """
    tuples = sorted(
        (c.table_schema, c.table_name, c.column_name, c.data_type, c.ordinal_position)
        for c in columns
    )
    return hashlib.sha256(repr(tuples).encode()).hexdigest()


@dataclass(frozen=True)
class ColumnChange:
    table_schema: str
    table_name: str
    change_type: str           # RENAME|TYPE_PROMOTION|REORDER|NEW_FIELD|DEPRECATION (candidate)
    before: ColumnRecord | None
    after: ColumnRecord | None


def diff_columns(
    prior: list[ColumnRecord], current: list[ColumnRecord]
) -> list[ColumnChange]:
    """Column-level diff. Produces *candidate* change types; Gemini makes the
    final classification (especially RENAME vs DEPRECATION+NEW_FIELD).

    Rename handling (recall-favoring): within a table, pair EVERY removed
    column with EVERY added column that has a matching data_type as a RENAME
    candidate. Do not filter on ordinal proximity — Fivetran's drop-then-add
    type-promotion procedure can reorder columns arbitrarily. The ordinal
    delta is attached to each candidate as a feature for Gemini, not used to
    discard candidates here.
    """
    # TODO: index by (schema, table, column); compute added / removed / changed
    # TODO: for each table, cross-pair removed x added on matching data_type
    #       -> RENAME candidates; attach ordinal_delta as advisory metadata
    # TODO: unpaired added -> NEW_FIELD; unpaired removed -> DEPRECATION;
    #       same name + changed type -> TYPE_PROMOTION;
    #       same name + changed ordinal only -> REORDER
    raise NotImplementedError


def capture_and_gate(connection_id: str, destination_schema: str, trigger: str):
    """Steps 1-3: fetch landed columns, hash, compare to latest snapshot.
    Returns (changed: bool, current_columns, prior_snapshot). If unchanged,
    the caller should exit cheaply without writing column rows.

    MUST call exclude_system_columns() on the fetched INFORMATION_SCHEMA
    columns before hashing/storing — _fivetran_* columns are not drift.
    """
    # TODO: fetch_landed_columns -> exclude_system_columns -> content_hash
    #       -> compare to latest_snapshot.content_hash
    raise NotImplementedError
