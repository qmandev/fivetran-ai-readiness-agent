"""Unit tests for app.tools.failure_diagnosis — Feature 6.

BQ calls are monkeypatched; Gemini calls are injected via model_fn=.
No live BQ or Gemini resources are used.
"""

from __future__ import annotations

import json

import pytest

from app.tools.failure_diagnosis import (
    _severity_from_count,
    diagnose_sync_failures,
)


# ── helpers ───────────────────────────────────────────────────────────────


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


_FAILURE_ROWS = [
    _bq_row(error_code="SCHEMA_CHANGE_REQUIRED", error_message="Column type mismatch", table_name="orders", failed_at="2026-05-30T10:00:00Z"),
    _bq_row(error_code="SCHEMA_CHANGE_REQUIRED", error_message="Column type mismatch", table_name="orders", failed_at="2026-05-29T10:00:00Z"),
    _bq_row(error_code="CONNECTION_FAILED",       error_message="TCP connection refused", table_name=None,   failed_at="2026-05-28T10:00:00Z"),
]

_CANNED_GEMINI_RESPONSE = json.dumps({
    "diagnosis": "The connection has recurring schema type mismatches on the orders table, likely caused by a recent source ALTER. One connection failure suggests transient network issues.",
    "recommended_actions": [
        "Check for recent ALTER TABLE on the orders table in the source database.",
        "Review Fivetran schema change handling settings.",
        "Verify network connectivity to the source database.",
    ],
    "severity": "HIGH",
})


# ── _severity_from_count ──────────────────────────────────────────────────


def test_severity_critical():
    assert _severity_from_count(10) == "CRITICAL"


def test_severity_high():
    assert _severity_from_count(5) == "HIGH"


def test_severity_medium():
    assert _severity_from_count(2) == "MEDIUM"


def test_severity_low():
    assert _severity_from_count(1) == "LOW"


def test_severity_zero():
    assert _severity_from_count(0) == "LOW"


# ── diagnose_sync_failures — no_failures path ─────────────────────────────


def test_diagnose_no_failures_returns_status(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client([]),
    )
    result = diagnose_sync_failures("conn_x", days=7, model_fn=lambda _: "{}")
    assert result["status"] == "no_failures"


def test_diagnose_no_failures_does_not_call_gemini(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client([]),
    )
    calls = []
    diagnose_sync_failures("conn_x", days=7, model_fn=lambda p: calls.append(p) or "{}")
    assert calls == []  # Gemini must NOT be called when table is empty


def test_diagnose_no_failures_message_mentions_connection(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client([]),
    )
    result = diagnose_sync_failures("my_conn", model_fn=lambda _: "{}")
    assert "my_conn" in result["message"]


def test_diagnose_no_failures_includes_days(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client([]),
    )
    result = diagnose_sync_failures("c", days=14, model_fn=lambda _: "{}")
    assert result["period_days"] == 14


# ── diagnose_sync_failures — with failures ────────────────────────────────


def test_diagnose_failure_count(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result["failure_count"] == 3


def test_diagnose_top_errors_ranked_by_frequency(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result["top_errors"][0]["error_code"] == "SCHEMA_CHANGE_REQUIRED"
    assert result["top_errors"][0]["count"] == 2


def test_diagnose_top_errors_capped_at_five(monkeypatch):
    many_rows = [
        _bq_row(error_code=f"ERR_{i}", error_message="msg", table_name=None, failed_at="2026-05-30T00:00:00Z")
        for i in range(8)
    ]
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(many_rows),
    )
    result = diagnose_sync_failures("c", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert len(result["top_errors"]) <= 5


def test_diagnose_severity_from_gemini(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result["severity"] == "HIGH"


def test_diagnose_severity_fallback_on_bad_gemini(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: "not json")
    # 3 failures → MEDIUM by count heuristic
    assert result["severity"] == "MEDIUM"


def test_diagnose_recommended_actions(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert isinstance(result["recommended_actions"], list)
    assert len(result["recommended_actions"]) == 3


def test_diagnose_diagnosis_string(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert "schema" in result["diagnosis"].lower()


def test_diagnose_connection_id_in_result(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("assimilate_seem", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result["connection_id"] == "assimilate_seem"


def test_diagnose_period_days_in_result(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("c", days=14, model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result["period_days"] == 14


def test_diagnose_null_error_code_grouped_as_unknown(monkeypatch):
    rows = [
        _bq_row(error_code=None, error_message="unknown failure", table_name=None, failed_at="2026-05-30T00:00:00Z"),
        _bq_row(error_code=None, error_message="unknown failure", table_name=None, failed_at="2026-05-29T00:00:00Z"),
    ]
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(rows),
    )
    result = diagnose_sync_failures("c", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    error_codes = {e["error_code"] for e in result["top_errors"]}
    assert "UNKNOWN" in error_codes


def test_diagnose_graceful_on_invalid_gemini_severity(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    bad_resp = json.dumps({"diagnosis": "x", "recommended_actions": [], "severity": "BANANA"})
    result = diagnose_sync_failures("c", model_fn=lambda _: bad_resp)
    # Falls back to count-based: 3 failures → MEDIUM
    assert result["severity"] == "MEDIUM"


def test_diagnose_sample_message_captured(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("c", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    schema_err = next(e for e in result["top_errors"] if e["error_code"] == "SCHEMA_CHANGE_REQUIRED")
    assert schema_err["sample_message"] == "Column type mismatch"


def test_diagnose_log_path_source_field(monkeypatch):
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._client",
        lambda: _mock_client(_FAILURE_ROWS),
    )
    result = diagnose_sync_failures("c", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result["source"] == "sync_failure_log"


# ── API fallback tests (log table empty) ──────────────────────────────────


def _empty_client():
    return _mock_client([])


def test_api_fallback_called_when_log_empty(monkeypatch):
    monkeypatch.setattr("app.tools.failure_diagnosis._client", lambda: _empty_client())
    calls = []
    def _fake_status(conn_id):
        calls.append(conn_id)
        return {"sync_state": "scheduled", "tasks": []}
    monkeypatch.setattr("app.tools.failure_diagnosis._fetch_connector_status", _fake_status)
    diagnose_sync_failures("conn_x", model_fn=lambda _: "{}")
    assert calls == ["conn_x"]


def test_api_healthy_returns_no_failures(monkeypatch):
    monkeypatch.setattr("app.tools.failure_diagnosis._client", lambda: _empty_client())
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._fetch_connector_status",
        lambda _: {"sync_state": "scheduled", "tasks": []},
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: "{}")
    assert result["status"] == "no_failures"
    assert "scheduled" in result["message"]


def test_api_error_state_calls_gemini(monkeypatch):
    monkeypatch.setattr("app.tools.failure_diagnosis._client", lambda: _empty_client())
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._fetch_connector_status",
        lambda _: {"sync_state": "error", "tasks": []},
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert "status" not in result or result.get("status") != "no_failures"
    assert result["source"] == "fivetran_api"
    assert result["failure_count"] >= 1


def test_api_tasks_present_calls_gemini(monkeypatch):
    monkeypatch.setattr("app.tools.failure_diagnosis._client", lambda: _empty_client())
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._fetch_connector_status",
        lambda _: {
            "sync_state": "scheduled",
            "tasks": [{"code": "RECONNECT_REQUIRED", "message": "OAuth token expired"}],
        },
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: _CANNED_GEMINI_RESPONSE)
    assert result["source"] == "fivetran_api"
    assert result["top_errors"][0]["error_code"] == "RECONNECT_REQUIRED"


def test_api_unreachable_falls_back_gracefully(monkeypatch):
    monkeypatch.setattr("app.tools.failure_diagnosis._client", lambda: _empty_client())
    monkeypatch.setattr(
        "app.tools.failure_diagnosis._fetch_connector_status",
        lambda _: None,
    )
    result = diagnose_sync_failures("conn_x", model_fn=lambda _: "{}")
    assert result["status"] == "no_failures"
    assert "Could not reach" in result["message"]
