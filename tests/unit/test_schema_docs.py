"""Unit tests for app.tools.schema_docs — Feature 2.

_fetch_schema_for_connection is monkeypatched; Gemini calls are injected via model_fn=.
No live BQ or Gemini resources are used.
"""

from __future__ import annotations

import json

import pytest

from app.tools.bigquery_query import ColumnRecord
from app.tools.schema_docs import generate_schema_docs


# ── helpers ───────────────────────────────────────────────────────────────


def _make_col(name, dtype, table="orders", schema="public", pos=1):
    return ColumnRecord(
        table_schema=schema, table_name=table,
        column_name=name, data_type=dtype,
        ordinal_position=pos, is_nullable=True,
    )


def _canned_model(table_responses: dict[str, list[dict]]):
    """Return a model_fn that dispatches canned responses per table key."""
    calls = iter(table_responses.values())
    def _fn(prompt: str) -> str:
        return json.dumps(next(calls))
    return _fn


# ── generate_schema_docs ──────────────────────────────────────────────────


def test_generate_schema_docs_structure(monkeypatch):
    schema = {
        "public.orders": [
            _make_col("order_id", "INT64", table="orders", pos=1),
            _make_col("customer_id", "INT64", table="orders", pos=2),
            _make_col("total_amount", "FLOAT64", table="orders", pos=3),
        ]
    }
    monkeypatch.setattr(
        "app.tools.schema_docs._fetch_schema_for_connection", lambda _: schema
    )
    monkeypatch.setattr("app.tools.connection_resolver.resolve_destination_schema", lambda _: "public")

    gemini_response = [
        {"column_name": "order_id", "description": "Unique identifier for each order."},
        {"column_name": "customer_id", "description": "Reference to the customer who placed the order."},
        {"column_name": "total_amount", "description": "Total monetary value of the order."},
    ]

    result = generate_schema_docs("conn_x", model_fn=lambda _: json.dumps(gemini_response))

    assert result["connection_id"] == "conn_x"
    assert result["dataset"] == "public"
    assert "public.orders" in result["tables"]


def test_generate_schema_docs_all_columns_present(monkeypatch):
    schema = {
        "public.customers": [
            _make_col("id", "INT64", table="customers", pos=1),
            _make_col("email", "STRING", table="customers", pos=2),
        ]
    }
    monkeypatch.setattr("app.tools.schema_docs._fetch_schema_for_connection", lambda _: schema)
    monkeypatch.setattr("app.tools.connection_resolver.resolve_destination_schema", lambda _: "public")

    gemini_response = [
        {"column_name": "id", "description": "Primary key for the customer."},
        {"column_name": "email", "description": "Customer email address."},
    ]
    result = generate_schema_docs("conn_x", model_fn=lambda _: json.dumps(gemini_response))

    cols = result["tables"]["public.customers"]
    names = [c["column_name"] for c in cols]
    assert "id" in names
    assert "email" in names


def test_generate_schema_docs_includes_data_type(monkeypatch):
    schema = {
        "public.t": [_make_col("amount", "FLOAT64", table="t")]
    }
    monkeypatch.setattr("app.tools.schema_docs._fetch_schema_for_connection", lambda _: schema)
    monkeypatch.setattr("app.tools.connection_resolver.resolve_destination_schema", lambda _: "ds")

    gemini_response = [{"column_name": "amount", "description": "Transaction amount."}]
    result = generate_schema_docs("c", model_fn=lambda _: json.dumps(gemini_response))

    col = result["tables"]["public.t"][0]
    assert col["data_type"] == "FLOAT64"
    assert col["description"] == "Transaction amount."


def test_generate_schema_docs_multiple_tables(monkeypatch):
    schema = {
        "public.orders": [_make_col("order_id", "INT64", table="orders")],
        "public.customers": [_make_col("customer_id", "INT64", table="customers")],
    }
    monkeypatch.setattr("app.tools.schema_docs._fetch_schema_for_connection", lambda _: schema)
    monkeypatch.setattr("app.tools.connection_resolver.resolve_destination_schema", lambda _: "public")

    responses = iter([
        json.dumps([{"column_name": "order_id", "description": "Order ID."}]),
        json.dumps([{"column_name": "customer_id", "description": "Customer ID."}]),
    ])
    result = generate_schema_docs("c", model_fn=lambda _: next(responses))

    assert len(result["tables"]) == 2
    assert "public.orders" in result["tables"]
    assert "public.customers" in result["tables"]


def test_generate_schema_docs_graceful_on_bad_json(monkeypatch):
    schema = {"public.t": [_make_col("col_a", "STRING", table="t")]}
    monkeypatch.setattr("app.tools.schema_docs._fetch_schema_for_connection", lambda _: schema)
    monkeypatch.setattr("app.tools.connection_resolver.resolve_destination_schema", lambda _: "ds")

    result = generate_schema_docs("c", model_fn=lambda _: "not valid json")

    col = result["tables"]["public.t"][0]
    assert col["column_name"] == "col_a"
    assert col["description"] == ""


def test_generate_schema_docs_empty_schema(monkeypatch):
    monkeypatch.setattr("app.tools.schema_docs._fetch_schema_for_connection", lambda _: {})
    monkeypatch.setattr("app.tools.connection_resolver.resolve_destination_schema", lambda _: "ds")

    result = generate_schema_docs("c", model_fn=lambda _: "[]")

    assert result["tables"] == {}


def test_generate_schema_docs_missing_column_gets_empty_description(monkeypatch):
    schema = {
        "public.t": [
            _make_col("col_a", "STRING", table="t"),
            _make_col("col_b", "INT64", table="t"),
        ]
    }
    monkeypatch.setattr("app.tools.schema_docs._fetch_schema_for_connection", lambda _: schema)
    monkeypatch.setattr("app.tools.connection_resolver.resolve_destination_schema", lambda _: "ds")

    # Gemini only returns one of the two columns
    gemini_response = [{"column_name": "col_a", "description": "First column."}]
    result = generate_schema_docs("c", model_fn=lambda _: json.dumps(gemini_response))

    cols = {c["column_name"]: c for c in result["tables"]["public.t"]}
    assert cols["col_a"]["description"] == "First column."
    assert cols["col_b"]["description"] == ""
