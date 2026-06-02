"""Unit tests for app.tools.entity_detector — Feature 7.

_client, _fetch_schema_for_connection, and model_fn are all monkeypatched.
No live BQ or Gemini resources are used.
"""

from __future__ import annotations

import json

import pytest

from app.tools.bigquery_query import ColumnRecord
from app.tools.entity_detector import detect_entity_overlaps


# ── helpers ───────────────────────────────────────────────────────────────


def _make_col(name, dtype, table="users", schema="public", pos=1):
    return ColumnRecord(
        table_schema=schema, table_name=table,
        column_name=name, data_type=dtype,
        ordinal_position=pos, is_nullable=True,
    )


def _bq_row(**kwargs):
    class Row(dict):
        pass
    return Row(kwargs)


def _mock_client(query_rows, insert_errors=None):
    insert_calls = []

    class FakeResult:
        def result(self):
            return iter(query_rows)

    class FakeClient:
        def query(self, *a, **kw):
            return FakeResult()

        def insert_rows_json(self, ref, rows):
            insert_calls.append((ref, rows))
            return insert_errors or []

    fc = FakeClient()
    fc._insert_calls = insert_calls
    return fc


_SCHEMA_A = {
    "public.users": [
        _make_col("user_id", "INT64", table="users", pos=1),
        _make_col("email", "STRING", table="users", pos=2),
        _make_col("created_at", "TIMESTAMP", table="users", pos=3),
    ]
}

_SCHEMA_B = {
    "salesforce.accounts": [
        _make_col("account_id", "INT64", table="accounts", schema="salesforce", pos=1),
        _make_col("email_address", "STRING", table="accounts", schema="salesforce", pos=2),
        _make_col("signup_date", "DATE", table="accounts", schema="salesforce", pos=3),
    ]
}

_CANNED_GEMINI_RESPONSE = json.dumps([
    {
        "entity_name": "Customer",
        "confidence": 0.9,
        "occurrences": [
            {"connection_id": "conn_pg", "table": "public.users", "join_key": "user_id"},
            {"connection_id": "conn_sf", "table": "salesforce.accounts", "join_key": "account_id"},
        ],
        "split_truth_conflicts": [
            "email is NOT NULL in conn_pg but NULLABLE in conn_sf"
        ],
    }
])


def _setup(monkeypatch, schema_map=None, insert_errors=None):
    """Wire up monkeypatches for a two-connection test."""
    if schema_map is None:
        schema_map = {"conn_pg": _SCHEMA_A, "conn_sf": _SCHEMA_B}

    conn_ids = list(schema_map.keys())
    monkeypatch.setattr(
        "app.tools.entity_detector._fetch_schema_for_connection",
        lambda cid: schema_map.get(cid, {}),
    )
    monkeypatch.setattr(
        "app.tools.entity_detector._client",
        lambda: _mock_client(
            [_bq_row(connection_id=c) for c in conn_ids],
            insert_errors=insert_errors,
        ),
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_STATE_DATASET", "agent_state")


# ── detect_entity_overlaps — return shape ─────────────────────────────────


def test_detect_entity_overlaps_returns_list(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert isinstance(result, list)
    assert len(result) == 1


def test_detect_entity_overlaps_entity_name(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result[0]["entity_name"] == "Customer"


def test_detect_entity_overlaps_confidence(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result[0]["confidence"] == pytest.approx(0.9)


def test_detect_entity_overlaps_occurrences(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    occs = result[0]["occurrences"]
    assert len(occs) == 2
    tables = {o["table"] for o in occs}
    assert "public.users" in tables
    assert "salesforce.accounts" in tables


def test_detect_entity_overlaps_join_keys(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    occs = {o["table"]: o for o in result[0]["occurrences"]}
    assert occs["public.users"]["join_key"] == "user_id"
    assert occs["salesforce.accounts"]["join_key"] == "account_id"


def test_detect_entity_overlaps_conflicts(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    conflicts = result[0]["split_truth_conflicts"]
    assert len(conflicts) == 1
    assert "email" in conflicts[0]


# ── BQ write ──────────────────────────────────────────────────────────────


def test_detect_entity_overlaps_writes_entity_map(monkeypatch):
    conn_ids = ["conn_pg", "conn_sf"]
    schema_map = {"conn_pg": _SCHEMA_A, "conn_sf": _SCHEMA_B}
    monkeypatch.setattr(
        "app.tools.entity_detector._fetch_schema_for_connection",
        lambda cid: schema_map.get(cid, {}),
    )
    fake_client = _mock_client([_bq_row(connection_id=c) for c in conn_ids])
    monkeypatch.setattr("app.tools.entity_detector._client", lambda: fake_client)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_STATE_DATASET", "agent_state")

    detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)

    assert len(fake_client._insert_calls) == 1
    ref, rows = fake_client._insert_calls[0]
    assert "entity_map" in ref
    assert len(rows) == 2  # one row per occurrence


def test_detect_entity_overlaps_entity_map_row_fields(monkeypatch):
    conn_ids = ["conn_pg", "conn_sf"]
    schema_map = {"conn_pg": _SCHEMA_A, "conn_sf": _SCHEMA_B}
    monkeypatch.setattr(
        "app.tools.entity_detector._fetch_schema_for_connection",
        lambda cid: schema_map.get(cid, {}),
    )
    fake_client = _mock_client([_bq_row(connection_id=c) for c in conn_ids])
    monkeypatch.setattr("app.tools.entity_detector._client", lambda: fake_client)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_STATE_DATASET", "agent_state")

    detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)

    _, rows = fake_client._insert_calls[0]
    row = next(r for r in rows if r["connection_id"] == "conn_pg")
    assert row["entity_name"] == "Customer"
    assert row["join_key_col"] == "user_id"
    assert row["confidence"] == pytest.approx(0.9)
    assert "detection_id" in row
    assert "detected_at" in row


def test_detect_entity_overlaps_insert_failure_non_fatal(monkeypatch):
    _setup(monkeypatch, insert_errors=[{"error": "simulated"}])
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert len(result) == 1  # result still returned despite insert failure


# ── edge cases ────────────────────────────────────────────────────────────


def test_detect_entity_overlaps_empty_sync_log(monkeypatch):
    monkeypatch.setattr(
        "app.tools.entity_detector._fetch_schema_for_connection", lambda _: {}
    )
    monkeypatch.setattr(
        "app.tools.entity_detector._client",
        lambda: _mock_client([]),
    )
    result = detect_entity_overlaps(model_fn=lambda _: "[]")
    assert result == []


def test_detect_entity_overlaps_single_connection_uses_catalog_prompt(monkeypatch):
    """Single connection now uses the catalog prompt, not the overlap prompt."""
    monkeypatch.setattr(
        "app.tools.entity_detector._fetch_schema_for_connection",
        lambda _: _SCHEMA_A,
    )
    monkeypatch.setattr(
        "app.tools.entity_detector._client",
        lambda: _mock_client([_bq_row(connection_id="conn_only")]),
    )
    calls = []
    def _tracking_fn(prompt):
        calls.append(prompt)
        return "[]"

    detect_entity_overlaps(model_fn=_tracking_fn)
    # Gemini IS called (catalog mode), and the prompt uses the single-connection framing
    assert len(calls) == 1
    assert "single Fivetran connection" in calls[0]


def test_detect_entity_overlaps_graceful_on_bad_gemini_json(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: "not json at all")
    assert result == []


def test_detect_entity_overlaps_empty_gemini_list(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: "[]")
    assert result == []


def test_detect_entity_overlaps_all_required_keys_present(monkeypatch):
    _setup(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    for entity in result:
        for key in ("entity_name", "confidence", "occurrences", "split_truth_conflicts"):
            assert key in entity


# ── single-connection mode ────────────────────────────────────────────────

_SINGLE_CONN_GEMINI_RESPONSE = json.dumps([
    {
        "entity_name": "Customer",
        "confidence": 0.85,
        "occurrences": [{"connection_id": "conn_pg", "table": "public.users", "join_key": "user_id"}],
        "split_truth_conflicts": ["email is nullable but should be NOT NULL"],
    }
])


def _setup_single(monkeypatch):
    monkeypatch.setattr(
        "app.tools.entity_detector._fetch_schema_for_connection",
        lambda _: _SCHEMA_A,
    )
    monkeypatch.setattr(
        "app.tools.entity_detector._client",
        lambda: _mock_client([_bq_row(connection_id="conn_pg")]),
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("BQ_STATE_DATASET", "agent_state")


def test_single_connection_calls_gemini(monkeypatch):
    _setup_single(monkeypatch)
    calls = []
    def _tracking_fn(prompt):
        calls.append(prompt)
        return _SINGLE_CONN_GEMINI_RESPONSE
    detect_entity_overlaps(model_fn=_tracking_fn)
    assert len(calls) == 1  # Gemini IS called for single connection


def test_single_connection_analysis_mode(monkeypatch):
    _setup_single(monkeypatch)
    result = detect_entity_overlaps(model_fn=lambda _: _SINGLE_CONN_GEMINI_RESPONSE)
    assert len(result) == 1
    assert result[0]["analysis_mode"] == "single_connection"


def test_cross_connection_analysis_mode(monkeypatch):
    _setup(monkeypatch)  # two connections
    result = detect_entity_overlaps(model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert len(result) == 1
    assert result[0]["analysis_mode"] == "cross_connection"


def test_zero_connections_still_returns_empty(monkeypatch):
    monkeypatch.setattr(
        "app.tools.entity_detector._fetch_schema_for_connection", lambda _: {}
    )
    monkeypatch.setattr(
        "app.tools.entity_detector._client",
        lambda: _mock_client([]),
    )
    result = detect_entity_overlaps(model_fn=lambda _: _SINGLE_CONN_GEMINI_RESPONSE)
    assert result == []
