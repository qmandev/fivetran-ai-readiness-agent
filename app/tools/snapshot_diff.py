"""Snapshot capture, hash-gate, and column diff (detection algorithm steps 1-5).

Composes the detection pipeline end-to-end via `capture_and_gate`:
    fetch_landed_columns -> exclude_system_columns -> content_hash
                         -> compare against latest_snapshot
The function is read-only and returns a `GateResult` the caller branches on
to decide whether to write a new snapshot, run a diff, or exit cheaply.

`diff_columns` produces *candidate* ColumnChange events per the recall-
favoring rename heuristic; the classifier (classify_drift.py) makes the
final type call.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass

from .bigquery_query import ColumnRecord, fetch_landed_columns, latest_snapshot

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
    """Column-level diff. Produces *candidate* change types; the classifier
    (classify_drift.py) makes the final call (especially RENAME vs
    DEPRECATION+NEW_FIELD, and attributes collateral REORDERs to a
    co-occurring TYPE_PROMOTION on the same table — G2 finding).

    Per-table logic:
      kept (same name on both sides):
        data_type differs            -> TYPE_PROMOTION
        ordinal_position differs only -> REORDER

      cross-section (removed × added):
        same data_type, same table   -> RENAME candidate (recall-favoring per
                                        Resolved Decision #3: one candidate
                                        per same-type pair, regardless of
                                        ordinal proximity; classifier
                                        disambiguates via name semantics +
                                        ordinal_delta as an advisory feature)

      unpaired (no same-type counterpart on the other side):
        added                        -> NEW_FIELD
        removed                      -> DEPRECATION

    Callers MUST filter both inputs through exclude_system_columns() first.
    """
    def _index_by_table(cols: list[ColumnRecord]) -> dict:
        idx: dict = defaultdict(dict)
        for c in cols:
            idx[(c.table_schema, c.table_name)][c.column_name] = c
        return idx

    prior_idx = _index_by_table(prior)
    current_idx = _index_by_table(current)
    all_tables = sorted(set(prior_idx) | set(current_idx))

    changes: list[ColumnChange] = []
    for schema, table in all_tables:
        p = prior_idx.get((schema, table), {})
        c = current_idx.get((schema, table), {})
        p_names, c_names = set(p), set(c)
        removed = p_names - c_names
        added = c_names - p_names
        kept = p_names & c_names

        # Kept columns: TYPE_PROMOTION (type changed) or REORDER (ordinal only).
        # Same name + same type + same ordinal -> no event.
        for name in sorted(kept):
            pc, cc = p[name], c[name]
            if pc.data_type != cc.data_type:
                changes.append(ColumnChange(schema, table, "TYPE_PROMOTION", pc, cc))
            elif pc.ordinal_position != cc.ordinal_position:
                changes.append(ColumnChange(schema, table, "REORDER", pc, cc))

        # RENAME candidates: every removed × every added with matching type.
        # Track participation so unpaired columns become NEW_FIELD/DEPRECATION.
        removed_paired: set[str] = set()
        added_paired: set[str] = set()
        for r_name in sorted(removed):
            r = p[r_name]
            for a_name in sorted(added):
                a = c[a_name]
                if r.data_type == a.data_type:
                    changes.append(ColumnChange(schema, table, "RENAME", r, a))
                    removed_paired.add(r_name)
                    added_paired.add(a_name)

        # Unpaired added -> NEW_FIELD; unpaired removed -> DEPRECATION.
        for a_name in sorted(added - added_paired):
            changes.append(ColumnChange(schema, table, "NEW_FIELD", None, c[a_name]))
        for r_name in sorted(removed - removed_paired):
            changes.append(ColumnChange(schema, table, "DEPRECATION", p[r_name], None))

    return changes


@dataclass(frozen=True)
class GateResult:
    """Output of `capture_and_gate`. The caller branches on these fields:

      - prior_snapshot is None
          BOOTSTRAP. Write the initial baseline snapshot, do NOT run a diff
          (there's no prior to compare against — diffing would emit a
          NEW_FIELD event for every existing column).

      - prior_snapshot is not None AND changed is True
          Real drift. Write the new snapshot; load prior columns via
          `load_columns(prior_snapshot['snapshot_id'])`; run `diff_columns`;
          classify each ColumnChange.

      - changed is False (implies prior_snapshot is not None)
          Hash gate hit — schema unchanged since last capture. Exit cheaply
          without writing.
    """
    changed: bool
    current_columns: list[ColumnRecord]
    current_hash: str
    prior_snapshot: dict | None


def capture_and_gate(
    connection_id: str, destination_schema: str
) -> GateResult:
    """Detection algorithm steps 1-3: fetch landed columns, filter out
    Fivetran-injected system columns, hash, and compare against the latest
    persisted snapshot for this connection.

    Read-only — does not write any state. The caller composes the snapshot
    row (uses `current_columns` and `current_hash`) and calls
    `write_snapshot` after deciding to proceed.

    On bootstrap (no prior snapshot exists), returns `changed=True` and
    `prior_snapshot=None` so the caller writes the initial baseline; the
    caller MUST check `prior_snapshot is None` and skip the diff in that
    case.
    """
    raw = fetch_landed_columns(connection_id, destination_schema)
    current = exclude_system_columns(raw)
    current_hash = content_hash(current)
    prior = latest_snapshot(connection_id)
    if prior is None:
        return GateResult(
            changed=True,
            current_columns=current,
            current_hash=current_hash,
            prior_snapshot=None,
        )
    changed = current_hash != prior["content_hash"]
    return GateResult(
        changed=changed,
        current_columns=current,
        current_hash=current_hash,
        prior_snapshot=prior,
    )
