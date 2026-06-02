"""Unit tests for app.tools.json_flattener — Feature 5.

BQ calls and _fetch_schema_for_connection are monkeypatched; Gemini calls are
injected via model_fn=. No live BQ or Gemini resources are used.
"""

from __future__ import annotations

import json
import types as _types

import pytest

from app.tools.bigquery_query import ColumnRecord
from app.tools.json_flattener import (
    _STRUCTURED_NAME_RE,
    _detect_reason,
    detect_json_columns,
    generate_json_flattener,
)


# ── helpers ───────────────────────────────────────────────────────────────


def _make_col(name, dtype, table="events", schema="public", pos=1):
    return ColumnRecord(
        table_schema=schema, table_name=table,
        column_name=name, data_type=dtype,
        ordinal_position=pos, is_nullable=True,
    )


def _bq_row(**kwargs):
    class Row(dict):
        pass
    return Row(kwargs)


def _mock_client(query_rows=None, insert_errors=None):
    """Return a fake client supporting query() and insert_rows_json()."""
    class FakeResult:
        def __init__(self, rows):
            self._rows = rows
        def result(self):
            return iter(self._rows)

    insert_calls = []

    class FakeClient:
        def query(self, sql, location=None, job_config=None):
            return FakeResult(query_rows or [])

        def insert_rows_json(self, ref, rows):
            insert_calls.append((ref, rows))
            return insert_errors or []

    fc = FakeClient()
    fc._insert_calls = insert_calls
    return fc


# ── _detect_reason ────────────────────────────────────────────────────────


def test_detect_reason_json_type():
    assert _detect_reason("some_col", "JSON") == "data_type=JSON"


def test_detect_reason_json_type_case_insensitive():
    assert _detect_reason("some_col", "json") == "data_type=JSON"


def test_detect_reason_string_metadata():
    reason = _detect_reason("metadata", "STRING")
    assert reason is not None
    assert "metadata" in reason


def test_detect_reason_string_properties():
    assert _detect_reason("properties", "STRING") is not None


def test_detect_reason_string_payload():
    assert _detect_reason("user_payload", "STRING") is not None


def test_detect_reason_string_context():
    assert _detect_reason("request_context", "STRING") is not None


def test_detect_reason_safe_string_name():
    assert _detect_reason("email_address", "STRING") is None


def test_detect_reason_int_json_name():
    assert _detect_reason("metadata", "INT64") is None


def test_detect_reason_none_for_ordinary_column():
    assert _detect_reason("customer_id", "INT64") is None


# ── _STRUCTURED_NAME_RE ───────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    "metadata", "properties", "attributes", "payload",
    "details", "extras", "config", "context",
    "user_metadata", "event_payload", "REQUEST_CONTEXT",
])
def test_structured_name_re_matches(name):
    assert _STRUCTURED_NAME_RE.search(name)


@pytest.mark.parametrize("name", ["email", "customer_id", "amount", "created_at"])
def test_structured_name_re_no_match(name):
    assert not _STRUCTURED_NAME_RE.search(name)


# ── detect_json_columns ───────────────────────────────────────────────────


def test_detect_json_columns_finds_json_type(monkeypatch):
    schema = {
        "public.events": [
            _make_col("id", "INT64", table="events", pos=1),
            _make_col("event_data", "JSON", table="events", pos=2),
            _make_col("created_at", "TIMESTAMP", table="events", pos=3),
        ]
    }
    monkeypatch.setattr("app.tools.json_flattener._fetch_schema_for_connection", lambda _: schema)
    result = detect_json_columns("conn_x")
    assert len(result) == 1
    assert result[0]["column"] == "event_data"
    assert result[0]["data_type"] == "JSON"
    assert "data_type=JSON" in result[0]["reason"]


def test_detect_json_columns_finds_string_payload(monkeypatch):
    schema = {
        "public.requests": [
            _make_col("request_id", "INT64", table="requests", pos=1),
            _make_col("payload", "STRING", table="requests", pos=2),
        ]
    }
    monkeypatch.setattr("app.tools.json_flattener._fetch_schema_for_connection", lambda _: schema)
    result = detect_json_columns("conn_x")
    assert len(result) == 1
    assert result[0]["column"] == "payload"
    assert result[0]["reason"] is not None


def test_detect_json_columns_skips_safe_strings(monkeypatch):
    schema = {
        "public.users": [
            _make_col("email", "STRING", table="users", pos=1),
            _make_col("name", "STRING", table="users", pos=2),
        ]
    }
    monkeypatch.setattr("app.tools.json_flattener._fetch_schema_for_connection", lambda _: schema)
    result = detect_json_columns("conn_x")
    assert result == []


def test_detect_json_columns_connection_id_on_rows(monkeypatch):
    schema = {"public.t": [_make_col("metadata", "STRING", table="t")]}
    monkeypatch.setattr("app.tools.json_flattener._fetch_schema_for_connection", lambda _: schema)
    result = detect_json_columns("my_conn")
    assert result[0]["connection_id"] == "my_conn"


def test_detect_json_columns_multiple_tables(monkeypatch):
    schema = {
        "public.events": [_make_col("data", "JSON", table="events")],
        "public.logs": [_make_col("context", "STRING", table="logs")],
        "public.users": [_make_col("email", "STRING", table="users")],
    }
    monkeypatch.setattr("app.tools.json_flattener._fetch_schema_for_connection", lambda _: schema)
    result = detect_json_columns("c")
    assert len(result) == 2
    columns = {r["column"] for r in result}
    assert "data" in columns
    assert "context" in columns


def test_detect_json_columns_includes_table_key(monkeypatch):
    schema = {"public.events": [_make_col("metadata", "STRING", table="events")]}
    monkeypatch.setattr("app.tools.json_flattener._fetch_schema_for_connection", lambda _: schema)
    result = detect_json_columns("c")
    assert result[0]["table"] == "public.events"


# ── generate_json_flattener ───────────────────────────────────────────────

_SAMPLE_ROW_JSON = json.dumps({"user_id": 1, "action": "click", "page": "/home"})
_CANNED_VIEW_SQL = "CREATE OR REPLACE VIEW `public.events_flat` AS SELECT * EXCEPT(data), JSON_VALUE(data, '$.user_id') AS user_id FROM `proj.public.events`"


def _setup_generate(monkeypatch, sample_rows=None, insert_errors=None):
    schema = {
        "public.events": [
            _make_col("id", "INT64", table="events", pos=1),
            _make_col("data", "JSON", table="events", pos=2),
        ]
    }
    monkeypatch.setattr("app.tools.json_flattener._fetch_schema_for_connection", lambda _: schema)
    monkeypatch.setattr("ingest.webhook_receiver.connection_resolver.resolve_destination_schema", lambda _: "public")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_STATE_DATASET", "agent_state")

    fake_client = _mock_client(
        query_rows=sample_rows if sample_rows is not None else [_bq_row(data=_SAMPLE_ROW_JSON)],
        insert_errors=insert_errors,
    )
    monkeypatch.setattr("app.tools.json_flattener._client", lambda: fake_client)
    return fake_client


def test_generate_json_flattener_returns_view_name(monkeypatch):
    fc = _setup_generate(monkeypatch)
    result = generate_json_flattener("c", "public.events", "data", model_fn=lambda _: _CANNED_VIEW_SQL)
    assert result["view_name"] == "public.events_flat"


def test_generate_json_flattener_returns_view_sql(monkeypatch):
    fc = _setup_generate(monkeypatch)
    result = generate_json_flattener("c", "public.events", "data", model_fn=lambda _: _CANNED_VIEW_SQL)
    assert "CREATE OR REPLACE VIEW" in result["view_sql"]


def test_generate_json_flattener_deploy_via_mcp_true(monkeypatch):
    fc = _setup_generate(monkeypatch)
    result = generate_json_flattener("c", "public.events", "data", model_fn=lambda _: _CANNED_VIEW_SQL)
    assert result["deploy_via_mcp"] is True


def test_generate_json_flattener_estimated_columns(monkeypatch):
    fc = _setup_generate(monkeypatch)
    # Sample row has 3 top-level keys → estimated_columns = 3
    result = generate_json_flattener("c", "public.events", "data", model_fn=lambda _: _CANNED_VIEW_SQL)
    assert result["estimated_columns"] == 3


def test_generate_json_flattener_writes_audit_row(monkeypatch):
    fc = _setup_generate(monkeypatch)
    generate_json_flattener("c", "public.events", "data", model_fn=lambda _: _CANNED_VIEW_SQL)
    assert len(fc._insert_calls) == 1
    ref, rows = fc._insert_calls[0]
    assert "json_flattener_log" in ref
    assert len(rows) == 1
    row = rows[0]
    assert row["connection_id"] == "c"
    assert row["table_name"] == "public.events"
    assert row["column_name"] == "data"
    assert row["view_name"] == "public.events_flat"


def test_generate_json_flattener_insert_failure_non_fatal(monkeypatch):
    fc = _setup_generate(monkeypatch, insert_errors=[{"error": "simulated"}])
    # Should not raise — insert failure is logged and swallowed
    result = generate_json_flattener("c", "public.events", "data", model_fn=lambda _: _CANNED_VIEW_SQL)
    assert "view_name" in result


def test_generate_json_flattener_empty_sample_rows(monkeypatch):
    fc = _setup_generate(monkeypatch, sample_rows=[])
    result = generate_json_flattener("c", "public.events", "data", model_fn=lambda _: _CANNED_VIEW_SQL)
    # Falls back to {"_unknown_key": "str"} → estimated_columns = 1
    assert result["estimated_columns"] == 1


def test_generate_json_flattener_invalid_json_samples(monkeypatch):
    fc = _setup_generate(monkeypatch, sample_rows=[_bq_row(data="not-json")])
    result = generate_json_flattener("c", "public.events", "data", model_fn=lambda _: _CANNED_VIEW_SQL)
    # Unparseable sample → fallback structure
    assert result["estimated_columns"] == 1
