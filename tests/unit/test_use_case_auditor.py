"""Unit tests for app.tools.use_case_auditor — Feature 8.

_fetch_schema_for_connection and _client are monkeypatched; both Gemini
calls (phase A + phase B) are injected via model_fn=. No live BQ or Gemini
resources are used.
"""

from __future__ import annotations

import json
import types as _types

import pytest

from app.tools.bigquery_query import ColumnRecord
from app.tools.use_case_auditor import _fuzzy_match, audit_use_case_coverage


# ── helpers ───────────────────────────────────────────────────────────────


def _make_col(name, dtype, table="customers", schema="public", pos=1):
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


# ── _fuzzy_match ──────────────────────────────────────────────────────────


def test_fuzzy_match_exact():
    cols = [{"column": "email", "table": "users", "connection_id": "c"}]
    result = _fuzzy_match("email", cols)
    assert result is not None
    assert result["column"] == "email"


def test_fuzzy_match_partial():
    cols = [{"column": "email_address", "table": "users", "connection_id": "c"}]
    result = _fuzzy_match("email", cols)
    assert result is not None


def test_fuzzy_match_underscore_stripped():
    cols = [{"column": "phone_number", "table": "users", "connection_id": "c"}]
    result = _fuzzy_match("phonenumber", cols)
    assert result is not None


def test_fuzzy_match_no_match():
    cols = [{"column": "created_at", "table": "orders", "connection_id": "c"}]
    result = _fuzzy_match("email", cols)
    assert result is None


def test_fuzzy_match_empty_columns():
    assert _fuzzy_match("email", []) is None


# ── audit_use_case_coverage ───────────────────────────────────────────────

_PHASE_A_RESPONSE = json.dumps([
    {"entity": "Customer", "required_fields": ["email", "subscription_plan", "churn_date"], "why": "Core customer identity and subscription state."},
    {"entity": "Support Ticket", "required_fields": ["ticket_id", "user_id", "resolution_time"], "why": "Support history for churn signal."},
])

_PHASE_B_RESPONSE = json.dumps({
    "covered": [
        {"entity": "Customer", "field": "email", "connection_id": "conn_x", "table": "public.customers"},
        {"entity": "Customer", "field": "subscription_plan", "connection_id": "conn_x", "table": "public.customers"},
        {"entity": "Support Ticket", "field": "user_id", "connection_id": "conn_x", "table": "public.tickets"},
        {"entity": "Support Ticket", "field": "ticket_id", "connection_id": "conn_x", "table": "public.tickets"},
    ],
    "missing": [
        {"entity": "Customer", "field": "churn_date", "suggested_connector_type": "Salesforce", "why": "Churn date is typically in CRM."},
        {"entity": "Support Ticket", "field": "resolution_time", "suggested_connector_type": "Zendesk", "why": "Resolution time lives in the support system."},
    ],
    "narrative": "4 of 6 required fields are available across the current connections. Two gaps require additional connectors.",
})

_SCHEMA = {
    "public.customers": [
        _make_col("id", "INT64", table="customers", pos=1),
        _make_col("email", "STRING", table="customers", pos=2),
        _make_col("subscription_plan", "STRING", table="customers", pos=3),
    ],
    "public.tickets": [
        _make_col("ticket_id", "INT64", table="tickets", pos=1),
        _make_col("user_id", "INT64", table="tickets", pos=2),
    ],
}


def _setup(monkeypatch):
    monkeypatch.setattr(
        "app.tools.use_case_auditor._fetch_schema_for_connection",
        lambda _: _SCHEMA,
    )
    monkeypatch.setattr(
        "app.tools.use_case_auditor._client",
        lambda: _mock_client([_bq_row(connection_id="conn_x")]),
    )


def _two_phase_fn(phase_a, phase_b):
    phases = iter([phase_a, phase_b])
    return lambda _: next(phases)


def test_audit_returns_use_case_echo(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict customer churn",
        model_fn=_two_phase_fn(_PHASE_A_RESPONSE, _PHASE_B_RESPONSE),
    )
    assert result["use_case"] == "Predict customer churn"


def test_audit_coverage_pct(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn(_PHASE_A_RESPONSE, _PHASE_B_RESPONSE),
    )
    # 4 covered out of 6 total fields → 66.7%
    assert result["coverage_pct"] == pytest.approx(66.7, abs=0.1)


def test_audit_covered_and_missing_fields(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn(_PHASE_A_RESPONSE, _PHASE_B_RESPONSE),
    )
    covered_fields = {c["field"] for c in result["covered"]}
    missing_fields = {m["field"] for m in result["missing"]}
    assert "email" in covered_fields
    assert "churn_date" in missing_fields
    assert "resolution_time" in missing_fields


def test_audit_connector_suggestions_in_missing(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn(_PHASE_A_RESPONSE, _PHASE_B_RESPONSE),
    )
    missing_by_field = {m["field"]: m for m in result["missing"]}
    assert missing_by_field["churn_date"]["suggested_connector_type"] == "Salesforce"
    assert missing_by_field["resolution_time"]["suggested_connector_type"] == "Zendesk"


def test_audit_required_entities_returned(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn(_PHASE_A_RESPONSE, _PHASE_B_RESPONSE),
    )
    assert len(result["required_entities"]) == 2
    entity_names = {e["entity"] for e in result["required_entities"]}
    assert "Customer" in entity_names
    assert "Support Ticket" in entity_names


def test_audit_narrative_present(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn(_PHASE_A_RESPONSE, _PHASE_B_RESPONSE),
    )
    assert len(result["narrative"]) > 0


def test_audit_graceful_on_bad_phase_a_json(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn("not json", "[]"),
    )
    assert result["required_entities"] == []
    assert result["coverage_pct"] == 0.0
    assert "No required fields" in result["narrative"]


def test_audit_graceful_on_bad_phase_b_json(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn(_PHASE_A_RESPONSE, "not json"),
    )
    # Falls back to fuzzy pre-coverage map
    assert "coverage_pct" in result
    assert isinstance(result["covered"], list)
    assert isinstance(result["missing"], list)


def test_audit_empty_sync_log(monkeypatch):
    monkeypatch.setattr(
        "app.tools.use_case_auditor._fetch_schema_for_connection",
        lambda _: _SCHEMA,
    )
    monkeypatch.setattr(
        "app.tools.use_case_auditor._client",
        lambda: _mock_client([]),
    )
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn(_PHASE_A_RESPONSE, _PHASE_B_RESPONSE),
    )
    # No connections → Phase B gets empty schemas; coverage comes from Gemini response
    assert "coverage_pct" in result


def test_audit_zero_coverage_when_no_required_fields(monkeypatch):
    _setup(monkeypatch)
    result = audit_use_case_coverage(
        "Predict churn",
        model_fn=_two_phase_fn("[]", "{}"),
    )
    assert result["coverage_pct"] == 0.0
    assert "No required fields" in result["narrative"]
