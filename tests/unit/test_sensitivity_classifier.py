"""Unit tests for app.tools.sensitivity_classifier — Feature 3.

_fetch_schema_for_connection and _client are monkeypatched; Gemini calls
are injected via model_fn=. No live BQ or Gemini resources are used.
"""

from __future__ import annotations

import json
import types as _types

import pytest

from app.tools.bigquery_query import ColumnRecord
from app.tools.sensitivity_classifier import (
    _SENSITIVITY_RANK,
    classify_column_sensitivity,
    list_sensitive_columns,
)


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


def _mock_client(rows):
    class FakeResult:
        def result(self):
            return iter(rows)
    class FakeClient:
        def query(self, *a, **kw):
            return FakeResult()
    return FakeClient()


_MIXED_SCHEMA = {
    "public.users": [
        _make_col("id", "INT64", table="users", pos=1),
        _make_col("email", "STRING", table="users", pos=2),
        _make_col("phone_number", "STRING", table="users", pos=3),
        _make_col("account_balance", "FLOAT64", table="users", pos=4),
        _make_col("created_at", "TIMESTAMP", table="users", pos=5),
    ]
}

_MIXED_GEMINI_RESPONSE = json.dumps([
    {"table": "public.users", "column": "id",              "sensitivity_class": "SAFE",      "masking_strategy": None},
    {"table": "public.users", "column": "email",           "sensitivity_class": "PII",       "masking_strategy": "HASH"},
    {"table": "public.users", "column": "phone_number",    "sensitivity_class": "PII",       "masking_strategy": "REDACT"},
    {"table": "public.users", "column": "account_balance", "sensitivity_class": "FINANCIAL", "masking_strategy": "GENERALIZE"},
    {"table": "public.users", "column": "created_at",      "sensitivity_class": "SAFE",      "masking_strategy": None},
])


# ── sensitivity rank ordering ─────────────────────────────────────────────


def test_sensitivity_rank_pii_highest():
    assert _SENSITIVITY_RANK["PII"] < _SENSITIVITY_RANK["FINANCIAL"]
    assert _SENSITIVITY_RANK["FINANCIAL"] < _SENSITIVITY_RANK["HEALTH"]
    assert _SENSITIVITY_RANK["HEALTH"] < _SENSITIVITY_RANK["SAFE"]


# ── classify_column_sensitivity ───────────────────────────────────────────


def test_classify_column_sensitivity_returns_all_columns(monkeypatch):
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: _MIXED_SCHEMA,
    )
    result = classify_column_sensitivity("conn_x", model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    assert len(result) == 5


def test_classify_column_sensitivity_correct_classes(monkeypatch):
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: _MIXED_SCHEMA,
    )
    result = classify_column_sensitivity("conn_x", model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    by_col = {r["column"]: r for r in result}
    assert by_col["email"]["sensitivity_class"] == "PII"
    assert by_col["account_balance"]["sensitivity_class"] == "FINANCIAL"
    assert by_col["created_at"]["sensitivity_class"] == "SAFE"


def test_classify_column_sensitivity_masking_strategies(monkeypatch):
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: _MIXED_SCHEMA,
    )
    result = classify_column_sensitivity("conn_x", model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    by_col = {r["column"]: r for r in result}
    assert by_col["email"]["masking_strategy"] == "HASH"
    assert by_col["created_at"]["masking_strategy"] is None


def test_classify_column_sensitivity_connection_id_on_each_row(monkeypatch):
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: _MIXED_SCHEMA,
    )
    result = classify_column_sensitivity("conn_x", model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    for row in result:
        assert row["connection_id"] == "conn_x"


def test_classify_column_sensitivity_empty_schema(monkeypatch):
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: {},
    )
    result = classify_column_sensitivity("conn_x", model_fn=lambda _: "[]")
    assert result == []


def test_classify_column_sensitivity_graceful_on_bad_json(monkeypatch):
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: _MIXED_SCHEMA,
    )
    result = classify_column_sensitivity("conn_x", model_fn=lambda _: "not json")
    assert result == []


def test_classify_column_sensitivity_class_uppercased(monkeypatch):
    schema = {"public.t": [_make_col("email", "STRING", table="t")]}
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: schema,
    )
    resp = json.dumps([{"table": "public.t", "column": "email", "sensitivity_class": "pii", "masking_strategy": "hash"}])
    result = classify_column_sensitivity("c", model_fn=lambda _: resp)
    assert result[0]["sensitivity_class"] == "PII"


# ── list_sensitive_columns ────────────────────────────────────────────────


def _make_list_fixture(monkeypatch, conn_id="conn_x", schema=None):
    if schema is None:
        schema = _MIXED_SCHEMA
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: schema,
    )
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._client",
        lambda: _mock_client([_bq_row(connection_id=conn_id)]),
    )


def test_list_sensitive_columns_default_pii_only(monkeypatch):
    _make_list_fixture(monkeypatch)
    result = list_sensitive_columns(min_sensitivity="PII", model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    classes = {r["sensitivity_class"] for r in result}
    assert "SAFE" not in classes
    assert "FINANCIAL" not in classes
    assert "PII" in classes


def test_list_sensitive_columns_financial_includes_pii(monkeypatch):
    _make_list_fixture(monkeypatch)
    result = list_sensitive_columns(min_sensitivity="FINANCIAL", model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    classes = {r["sensitivity_class"] for r in result}
    assert "PII" in classes
    assert "FINANCIAL" in classes
    assert "SAFE" not in classes


def test_list_sensitive_columns_safe_returns_all(monkeypatch):
    _make_list_fixture(monkeypatch)
    result = list_sensitive_columns(min_sensitivity="SAFE", model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    assert len(result) == 5


def test_list_sensitive_columns_sorted_pii_first(monkeypatch):
    _make_list_fixture(monkeypatch)
    result = list_sensitive_columns(min_sensitivity="FINANCIAL", model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    classes = [r["sensitivity_class"] for r in result]
    first_non_pii = next((i for i, c in enumerate(classes) if c != "PII"), len(classes))
    for c in classes[:first_non_pii]:
        assert c == "PII"


def test_list_sensitive_columns_empty_sync_log(monkeypatch):
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: _MIXED_SCHEMA,
    )
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._client",
        lambda: _mock_client([]),
    )
    result = list_sensitive_columns(model_fn=lambda _: _MIXED_GEMINI_RESPONSE)
    assert result == []


def test_list_sensitive_columns_multiple_connections(monkeypatch):
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._fetch_schema_for_connection",
        lambda _: _MIXED_SCHEMA,
    )
    monkeypatch.setattr(
        "app.tools.sensitivity_classifier._client",
        lambda: _mock_client([
            _bq_row(connection_id="conn_a"),
            _bq_row(connection_id="conn_b"),
        ]),
    )
    calls = iter([_MIXED_GEMINI_RESPONSE, _MIXED_GEMINI_RESPONSE])
    result = list_sensitive_columns(min_sensitivity="PII", model_fn=lambda _: next(calls))
    cids = {r["connection_id"] for r in result}
    assert "conn_a" in cids
    assert "conn_b" in cids
