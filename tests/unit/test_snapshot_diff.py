# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for app.tools.snapshot_diff.

Pure-Python coverage of the detection core: system-column exclusion, the
content_hash gate, and diff_columns' candidate-emission logic. Each behavior
that this exercises traces back to a design decision or an empirical finding
from checklist G:

  - `_fivetran_*` prefix + `_fivetran_id` suffix exclusion  -> F finding
    (Google Cloud PG connector ships `ctid_fivetran_id`, not `_fivetran_id`)
  - content_hash order-independence                          -> hash-gate design
  - Recall-favoring cross-pair rename                        -> Resolved Decision #3
  - TYPE_PROMOTION + collateral REORDER on same table        -> G2 finding
"""

from app.tools import snapshot_diff as sd
from app.tools.bigquery_query import ColumnRecord
from app.tools.snapshot_diff import (
    GateResult,
    capture_and_gate,
    content_hash,
    diff_columns,
    exclude_system_columns,
)


def _col(
    name: str,
    t: str = "STRING",
    ordinal: int = 1,
    schema: str = "public",
    table: str = "customers",
    nullable: bool = True,
) -> ColumnRecord:
    return ColumnRecord(
        table_schema=schema,
        table_name=table,
        column_name=name,
        data_type=t,
        ordinal_position=ordinal,
        is_nullable=nullable,
    )


# --- exclude_system_columns -------------------------------------------------

def test_exclude_drops_fivetran_prefix_columns():
    cols = [_col("_fivetran_synced"), _col("_fivetran_deleted"), _col("customer_id")]
    kept = exclude_system_columns(cols)
    assert [c.column_name for c in kept] == ["customer_id"]


def test_exclude_drops_ctid_fivetran_id_connector_specific():
    """F finding (2026-05-20): Google Cloud PostgreSQL connector ships
    `ctid_fivetran_id` instead of the docs' `_fivetran_id`. The suffix
    branch of the exclusion rule must catch it."""
    cols = [_col("ctid_fivetran_id"), _col("customer_id")]
    kept = exclude_system_columns(cols)
    assert [c.column_name for c in kept] == ["customer_id"]


def test_exclude_drops_documented_fivetran_id_variant():
    """The docs-named `_fivetran_id` should also be excluded (matched by the
    suffix branch, not just the prefix)."""
    cols = [_col("_fivetran_id"), _col("customer_id")]
    kept = exclude_system_columns(cols)
    assert [c.column_name for c in kept] == ["customer_id"]


def test_exclude_keeps_user_columns_with_fivetran_substring():
    """Defensive: a user column merely containing 'fivetran' must NOT be
    dropped. Only the prefix `_fivetran_` and the suffix `_fivetran_id`
    trigger exclusion."""
    cols = [_col("my_fivetran_metric"), _col("fivetran_score"), _col("customer_id")]
    kept = exclude_system_columns(cols)
    assert {c.column_name for c in kept} == {
        "my_fivetran_metric",
        "fivetran_score",
        "customer_id",
    }


# --- content_hash -----------------------------------------------------------

def test_hash_order_independent():
    a = [_col("x", "INT64", 1), _col("y", "STRING", 2)]
    b = [_col("y", "STRING", 2), _col("x", "INT64", 1)]
    assert content_hash(a) == content_hash(b)


def test_hash_changes_when_type_changes():
    a = [_col("x", "INT64", 1)]
    b = [_col("x", "STRING", 1)]
    assert content_hash(a) != content_hash(b)


def test_hash_changes_when_ordinal_changes():
    a = [_col("x", "INT64", 1), _col("y", "INT64", 2)]
    b = [_col("x", "INT64", 2), _col("y", "INT64", 1)]
    assert content_hash(a) != content_hash(b)


def test_hash_deterministic_on_repeat_call():
    a = [_col("x", "INT64", 1)]
    assert content_hash(a) == content_hash(a)


def test_hash_empty_input_is_stable():
    assert content_hash([]) == content_hash([])


# --- diff_columns: no-op / additive / subtractive ---------------------------

def test_diff_no_changes_yields_empty():
    a = [_col("x", "INT64", 1), _col("y", "STRING", 2)]
    b = [_col("x", "INT64", 1), _col("y", "STRING", 2)]
    assert diff_columns(a, b) == []


def test_diff_added_column_yields_new_field():
    a = [_col("x", "INT64", 1)]
    b = [_col("x", "INT64", 1), _col("y", "STRING", 2)]
    diff = diff_columns(a, b)
    assert len(diff) == 1
    assert diff[0].change_type == "NEW_FIELD"
    assert diff[0].before is None
    assert diff[0].after is not None
    assert diff[0].after.column_name == "y"


def test_diff_removed_column_yields_deprecation():
    a = [_col("x", "INT64", 1), _col("y", "STRING", 2)]
    b = [_col("x", "INT64", 1)]
    diff = diff_columns(a, b)
    assert len(diff) == 1
    assert diff[0].change_type == "DEPRECATION"
    assert diff[0].before is not None
    assert diff[0].before.column_name == "y"
    assert diff[0].after is None


# --- diff_columns: kept-column events ---------------------------------------

def test_diff_type_promotion_same_name_same_ordinal():
    """G2 base case: column kept, type changes -> TYPE_PROMOTION."""
    a = [_col("amount", "BIGNUMERIC", 1)]
    b = [_col("amount", "STRING", 1)]
    diff = diff_columns(a, b)
    assert len(diff) == 1
    assert diff[0].change_type == "TYPE_PROMOTION"
    assert diff[0].before.data_type == "BIGNUMERIC"
    assert diff[0].after.data_type == "STRING"


def test_diff_type_promotion_takes_precedence_over_reorder():
    """If the same column has BOTH a type change and an ordinal change, the
    diff emits a single TYPE_PROMOTION (not also a REORDER). Co-occurring
    same-column reorder is collateral, attributed to the type promotion."""
    a = [_col("amount", "BIGNUMERIC", 2)]
    b = [_col("amount", "STRING", 1)]
    diff = diff_columns(a, b)
    assert len(diff) == 1
    assert diff[0].change_type == "TYPE_PROMOTION"


def test_diff_reorder_only_same_name_same_type():
    a = [_col("x", "INT64", 1), _col("y", "STRING", 2)]
    b = [_col("x", "INT64", 2), _col("y", "STRING", 1)]
    diff = diff_columns(a, b)
    assert {c.change_type for c in diff} == {"REORDER"}
    assert len(diff) == 2


# --- diff_columns: rename heuristic (Decision #3) ---------------------------

def test_diff_rename_emits_candidate_not_new_plus_deprecation():
    """Recall-favoring: a single removed/added pair of matching type yields
    one RENAME candidate, NOT a (NEW_FIELD + DEPRECATION) pair."""
    a = [_col("customer_id", "INT64", 1)]
    b = [_col("cust_id", "INT64", 1)]
    diff = diff_columns(a, b)
    assert len(diff) == 1
    assert diff[0].change_type == "RENAME"
    assert diff[0].before.column_name == "customer_id"
    assert diff[0].after.column_name == "cust_id"


def test_diff_multiple_renames_cross_pair_all_candidates():
    """Decision #3 (recall-favoring): emit a RENAME candidate for EVERY
    (removed × added) pair with matching data_type. The classifier ranks them
    using name semantics + ordinal_delta. Here 2 removed × 2 added of the
    same type -> 4 candidates."""
    a = [
        _col("customer_id", "INT64", 1),
        _col("user_id", "INT64", 2),
    ]
    b = [
        _col("cust_id", "INT64", 1),
        _col("u_id", "INT64", 2),
    ]
    diff = diff_columns(a, b)
    renames = [c for c in diff if c.change_type == "RENAME"]
    assert len(renames) == 4
    # And no NEW_FIELD/DEPRECATION since every removed and added is paired.
    assert not any(c.change_type in ("NEW_FIELD", "DEPRECATION") for c in diff)


def test_diff_rename_only_pairs_same_data_type():
    """A removed column with no same-type added column becomes DEPRECATION;
    likewise the added of a different type becomes NEW_FIELD. They are NOT
    paired as a rename candidate."""
    a = [_col("customer_id", "INT64", 1)]
    b = [_col("cust_id", "STRING", 1)]
    diff = diff_columns(a, b)
    assert {c.change_type for c in diff} == {"NEW_FIELD", "DEPRECATION"}


def test_diff_ordinal_distance_does_not_filter_rename_candidates():
    """Decision #3: ordinal_delta is advisory metadata for the classifier,
    NEVER a cutoff. A rename across distant ordinals must still emit."""
    a = [_col("customer_id", "INT64", 1)]
    b = [_col("cust_id", "INT64", 99)]
    diff = diff_columns(a, b)
    renames = [c for c in diff if c.change_type == "RENAME"]
    assert len(renames) == 1


# --- diff_columns: isolation across tables ----------------------------------

def test_diff_changes_isolated_per_table():
    """Per-table rename pairing must not cross table boundaries: a removed
    column in customers and an added of the same type in orders is NOT a
    rename candidate — they're DEPRECATION and NEW_FIELD respectively."""
    a = [
        _col("removed_from_customers", "INT64", 1, table="customers"),
        _col("kept_in_orders", "STRING", 1, table="orders"),
    ]
    b = [
        _col("added_to_orders", "INT64", 2, table="orders"),
        _col("kept_in_orders", "STRING", 1, table="orders"),
    ]
    diff = diff_columns(a, b)
    types = sorted(c.change_type for c in diff)
    assert types == ["DEPRECATION", "NEW_FIELD"]
    # And the tables on the events are distinct:
    by_table = {c.table_name for c in diff}
    assert by_table == {"customers", "orders"}


# --- diff_columns: G2 full-table reorder under type promotion ---------------

# --- capture_and_gate (orchestrator) ---------------------------------------
#
# The orchestrator calls fetch_landed_columns + latest_snapshot from
# bigquery_query — both live-BQ. We monkeypatch them in the snapshot_diff
# module namespace (where they're imported) to isolate the orchestration
# logic from BigQuery.

def _patch_bq(monkeypatch, raw_columns, prior_snapshot):
    monkeypatch.setattr(sd, "fetch_landed_columns",
                        lambda cid, ds: list(raw_columns))
    monkeypatch.setattr(sd, "latest_snapshot",
                        lambda cid: prior_snapshot)


def test_capture_and_gate_bootstrap_returns_changed_with_no_prior(monkeypatch):
    """No prior snapshot exists -> bootstrap. changed=True forces the caller
    to write the initial baseline; prior_snapshot=None signals 'skip diff'."""
    raw = [_col("customer_id", "INT64", 1)]
    _patch_bq(monkeypatch, raw, prior_snapshot=None)
    result = capture_and_gate("conn-1", "public")
    assert isinstance(result, GateResult)
    assert result.changed is True
    assert result.prior_snapshot is None
    assert [c.column_name for c in result.current_columns] == ["customer_id"]
    assert result.current_hash != ""


def test_capture_and_gate_hash_match_returns_unchanged(monkeypatch):
    """Hash matches prior -> changed=False (the cheap-exit path)."""
    raw = [_col("customer_id", "INT64", 1)]
    # Pre-compute what the hash will be (post-exclude_system_columns is no-op
    # here since no system columns) so we can plant it in the prior snapshot.
    expected_hash = content_hash(raw)
    prior = {
        "snapshot_id": "snap-prev",
        "connection_id": "conn-1",
        "content_hash": expected_hash,
        "captured_at": "2026-05-21T00:00:00Z",
    }
    _patch_bq(monkeypatch, raw, prior_snapshot=prior)
    result = capture_and_gate("conn-1", "public")
    assert result.changed is False
    assert result.prior_snapshot == prior


def test_capture_and_gate_hash_differs_returns_changed(monkeypatch):
    raw = [_col("customer_id", "INT64", 1), _col("email", "STRING", 2)]
    prior = {
        "snapshot_id": "snap-prev",
        "connection_id": "conn-1",
        "content_hash": "definitely-not-the-current-hash",
        "captured_at": "2026-05-21T00:00:00Z",
    }
    _patch_bq(monkeypatch, raw, prior_snapshot=prior)
    result = capture_and_gate("conn-1", "public")
    assert result.changed is True
    assert result.prior_snapshot == prior
    assert len(result.current_columns) == 2


def test_capture_and_gate_filters_system_columns_before_hashing(monkeypatch):
    """The hash gate MUST see only post-filter columns, otherwise
    `_fivetran_synced` (changes every sync) would defeat it. Exercise:
    raw rows include system columns; result.current_columns excludes them
    AND result.current_hash equals the hash of the filtered set."""
    raw_with_system = [
        _col("customer_id", "INT64", 1),
        _col("_fivetran_synced", "TIMESTAMP", 2),
        _col("_fivetran_deleted", "BOOL", 3),
        _col("ctid_fivetran_id", "STRING", 4),
    ]
    expected_filtered = [_col("customer_id", "INT64", 1)]
    expected_hash = content_hash(expected_filtered)
    _patch_bq(monkeypatch, raw_with_system, prior_snapshot=None)
    result = capture_and_gate("conn-1", "public")
    assert [c.column_name for c in result.current_columns] == ["customer_id"]
    assert result.current_hash == expected_hash


def test_diff_g2_type_promotion_with_collateral_reorder():
    """G2 finding (2026-05-21): a single source-side type promotion rewrites
    the entire BQ table layout — the promoted column gets TYPE_PROMOTION, and
    OTHER columns on the same table get collateral REORDER events. The
    classifier later attributes those collaterals to the type promotion."""
    a = [
        _col("amount",   "BIGNUMERIC", 1, table="orders"),
        _col("status",   "STRING",     2, table="orders"),
        _col("order_id", "INT64",      3, table="orders"),
    ]
    b = [
        _col("amount",   "STRING",     1, table="orders"),   # type changes; ordinal unchanged
        _col("status",   "STRING",     3, table="orders"),   # reorder 2 -> 3
        _col("order_id", "INT64",      2, table="orders"),   # reorder 3 -> 2
    ]
    diff = diff_columns(a, b)
    types = sorted(c.change_type for c in diff)
    assert types == ["REORDER", "REORDER", "TYPE_PROMOTION"]
    promotion = next(c for c in diff if c.change_type == "TYPE_PROMOTION")
    assert promotion.before.column_name == "amount"
    assert promotion.after.data_type == "STRING"
