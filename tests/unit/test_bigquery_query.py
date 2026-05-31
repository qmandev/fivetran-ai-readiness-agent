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
"""Unit tests for app.tools.bigquery_query — the pure helpers only.

The live-BQ functions (`fetch_landed_columns`, `latest_snapshot`,
`load_columns`, `write_snapshot`, `insert_drift_event`, `update_drift_event`)
are not exercised here — those need a real BigQuery client and are covered
by integration tests against the live state-store dataset.

Pure helpers under test:
  - _columns_query           : SQL string for INFORMATION_SCHEMA.COLUMNS
  - _state_table_fqn         : fully-qualified state-store table reference
  - _row_to_column_record    : BQ row dict -> ColumnRecord
  - _as_json_string          : JSON-serialization helper for drift_events
  - _drift_event_placeholder : placeholder selection for INSERT (JSON vs scalar)
"""

import os

import pytest

from app.tools.bigquery_query import (
    BQ_LOCATION,
    ColumnRecord,
    _as_json_string,
    _columns_query,
    _default_sla_hours,
    _drift_event_placeholder,
    _row_to_column_record,
    _state_table_fqn,
)


# --- region pinning constant ------------------------------------------------

def test_bq_location_pinned_to_us_east1():
    """Decision #4: us-east1 colocates Cloud SQL + BigQuery + agent runtime.
    The F finding (2026-05-19) made the implicit pin a hard requirement —
    every BQ command must run with location='us-east1' or the query lands
    in the US multi-region and returns 'Dataset not found'."""
    assert BQ_LOCATION == "us-east1"


# --- _columns_query ---------------------------------------------------------

def test_columns_query_targets_information_schema():
    sql = _columns_query("api-project-910787152095", "public")
    # Must reference INFORMATION_SCHEMA.COLUMNS in the FROM clause
    assert "INFORMATION_SCHEMA.COLUMNS" in sql
    # Must use a backticked fully-qualified table reference
    assert "`api-project-910787152095.public.INFORMATION_SCHEMA.COLUMNS`" in sql


def test_columns_query_selects_required_fields():
    sql = _columns_query("p", "ds")
    for field in (
        "table_schema",
        "table_name",
        "column_name",
        "data_type",
        "ordinal_position",
        "is_nullable",
    ):
        assert field in sql, f"missing field in SELECT: {field}"


def test_columns_query_orders_for_deterministic_output():
    """Ordering matters because content_hash() consumes the result; even
    though content_hash sorts internally, an unstable scan order would
    make debugging the diff confusing. Explicit ORDER BY is the safer
    contract."""
    sql = _columns_query("p", "ds")
    assert "ORDER BY" in sql


# --- _state_table_fqn -------------------------------------------------------

def test_state_table_fqn_uses_configured_dataset(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.setenv("BQ_STATE_DATASET", "agent_state")
    assert _state_table_fqn("schema_snapshots") == (
        "`my-proj.agent_state.schema_snapshots`"
    )


def test_state_table_fqn_default_state_dataset(monkeypatch):
    """If BQ_STATE_DATASET is unset, default to 'agent_state' (matches env.example)."""
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-proj")
    monkeypatch.delenv("BQ_STATE_DATASET", raising=False)
    assert _state_table_fqn("drift_events") == "`my-proj.agent_state.drift_events`"


def test_state_table_fqn_falls_back_to_gcp_project_id(monkeypatch):
    """deploy/env.example documents GCP_PROJECT_ID; the canonical
    GOOGLE_CLOUD_PROJECT is preferred, but if only GCP_PROJECT_ID is set
    we still resolve."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "fallback-proj")
    assert _state_table_fqn("column_snapshots") == (
        "`fallback-proj.agent_state.column_snapshots`"
    )


# --- _row_to_column_record --------------------------------------------------

def test_row_to_column_record_information_schema_shape():
    """INFORMATION_SCHEMA.COLUMNS represents nullability as 'YES'/'NO'
    strings; the converter must translate to bool."""
    row = {
        "table_schema": "public",
        "table_name": "customers",
        "column_name": "customer_id",
        "data_type": "INT64",
        "ordinal_position": 3,
        "is_nullable": "NO",
    }
    rec = _row_to_column_record(row)
    assert rec == ColumnRecord(
        table_schema="public",
        table_name="customers",
        column_name="customer_id",
        data_type="INT64",
        ordinal_position=3,
        is_nullable=False,
    )


def test_row_to_column_record_yes_translates_to_true():
    row = {
        "table_schema": "public", "table_name": "customers",
        "column_name": "email", "data_type": "STRING",
        "ordinal_position": 5, "is_nullable": "YES",
    }
    assert _row_to_column_record(row).is_nullable is True


def test_row_to_column_record_state_store_shape_bool_passthrough():
    """When reading from our column_snapshots table, is_nullable is already
    a BOOL — passthrough must work without coercing 'true'/'false' strings."""
    row = {
        "table_schema": "public", "table_name": "customers",
        "column_name": "email", "data_type": "STRING",
        "ordinal_position": 5, "is_nullable": True,
    }
    assert _row_to_column_record(row).is_nullable is True


def test_row_to_column_record_coerces_ordinal_to_int():
    """Some BQ row representations return numeric fields as strings; the
    converter must coerce to int."""
    row = {
        "table_schema": "s", "table_name": "t", "column_name": "c",
        "data_type": "INT64", "ordinal_position": "7", "is_nullable": "YES",
    }
    assert _row_to_column_record(row).ordinal_position == 7


# --- _as_json_string --------------------------------------------------------

def test_as_json_string_dict_serializes():
    out = _as_json_string({"a": 1, "b": "x"})
    assert isinstance(out, str)
    assert '"a": 1' in out and '"b": "x"' in out


def test_as_json_string_none_passes_through():
    assert _as_json_string(None) is None


def test_as_json_string_existing_string_unchanged():
    """If a caller has already serialized, don't double-encode."""
    assert _as_json_string('{"already":"json"}') == '{"already":"json"}'


# --- _drift_event_placeholder ----------------------------------------------

def test_drift_event_placeholder_wraps_json_columns_in_parse_json():
    """column_before and column_after are JSON columns in the drift_events
    schema; INSERT must wrap their parameters in PARSE_JSON()."""
    assert _drift_event_placeholder("column_before") == "PARSE_JSON(@column_before)"
    assert _drift_event_placeholder("column_after") == "PARSE_JSON(@column_after)"


def test_drift_event_placeholder_plain_for_scalar_columns():
    assert _drift_event_placeholder("drift_id") == "@drift_id"
    assert _drift_event_placeholder("classification_conf") == "@classification_conf"
    assert _drift_event_placeholder("remediation_status") == "@remediation_status"


# --- update_drift_event input validation -----------------------------------

def test_update_drift_event_rejects_unknown_fields(monkeypatch):
    """A typo in a field name should fail loudly, not silently no-op.
    `update_drift_event` validates each kwarg against the known fields
    before issuing the UPDATE.

    Uses monkeypatch to provide minimal env so the function reaches the
    validation check before client construction. We expect the ValueError
    to fire BEFORE any BigQuery client is touched, so this test does not
    need a live BQ environment.
    """
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-proj")
    from app.tools.bigquery_query import update_drift_event
    with pytest.raises(ValueError, match="unknown drift_events field"):
        update_drift_event("d1", remediation_status="APPROVED", typo="oops")


# --- _default_sla_hours --------------------------------------------------------

def test_default_sla_hours_returns_24_when_unset(monkeypatch):
    """24 hours is the documented default when FRESHNESS_SLA_HOURS is absent."""
    monkeypatch.delenv("FRESHNESS_SLA_HOURS", raising=False)
    assert _default_sla_hours() == 24.0


def test_default_sla_hours_reads_env_var(monkeypatch):
    monkeypatch.setenv("FRESHNESS_SLA_HOURS", "6")
    assert _default_sla_hours() == 6.0


def test_default_sla_hours_accepts_fractional(monkeypatch):
    monkeypatch.setenv("FRESHNESS_SLA_HOURS", "0.5")
    assert _default_sla_hours() == 0.5
