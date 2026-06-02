"""Unit tests for app.tools.readiness_score — Features 1 and 4.

BQ calls are isolated by monkeypatching _client() and _fetch_schema_for_connection().
Gemini calls are isolated by injecting a stub via model_fn=.
No live BQ or Gemini resources are used.
"""

from __future__ import annotations

import json
import types as _types

import pytest

from app.tools.readiness_score import (
    _GRADE_ORDER,
    _extract_json,
    _freshness_signal,
    _drift_stability_signal,
    _type_suitability_signal,
    _naming_coherence_signal,
    _FENCE_RE,
    score_ai_readiness,
    list_readiness_scores,
    analyze_drift_volatility,
)
from app.tools.bigquery_query import ColumnRecord


# ── helpers ──────────────────────────────────────────────────────────────


def _make_row(**kwargs):
    """Build a lightweight object that supports dict-style item access."""
    return _types.SimpleNamespace(**{k: v for k, v in kwargs.items()},
                                  **{"__getitem__": lambda s, k: getattr(s, k)})


def _bq_row(**kwargs):
    """Dict-like row compatible with row["key"] access used in production code."""
    class Row(dict):
        pass
    return Row(kwargs)


def _mock_client(rows_per_call):
    """Return a fake _client() that yields successive canned row lists."""
    call_idx = [0]

    def query(sql, location=None, job_config=None):
        idx = call_idx[0]
        call_idx[0] += 1
        result_rows = rows_per_call[idx] if idx < len(rows_per_call) else []

        class FakeResult:
            def result(self):
                return iter(result_rows)

        return FakeResult()

    class FakeClient:
        pass

    fc = FakeClient()
    fc.query = query
    return fc


# ── _extract_json ─────────────────────────────────────────────────────────


def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_markdown_fence():
    text = "```json\n{\"grade\": \"A\"}\n```"
    assert _extract_json(text) == {"grade": "A"}


def test_extract_json_strips_fence_no_language():
    text = "```\n{\"x\": 2}\n```"
    assert _extract_json(text) == {"x": 2}


def test_extract_json_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        _extract_json("not json")


# ── _grade_order completeness ─────────────────────────────────────────────


def test_grade_order_covers_all_grades():
    for g in ("A", "B", "C", "D", "F"):
        assert g in _GRADE_ORDER
    assert _GRADE_ORDER["A"] < _GRADE_ORDER["F"]


# ── _freshness_signal ─────────────────────────────────────────────────────


def test_freshness_signal_ok(monkeypatch):
    row = _bq_row(last_synced_at="2026-06-02T00:00:00", hours_since_sync=2.0)
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([[row]]),
    )
    monkeypatch.setenv("FRESHNESS_SLA_HOURS", "24")
    result = _freshness_signal("conn_1")
    assert result["status"] == "OK"
    assert result["hours_since_sync"] == 2.0


def test_freshness_signal_stale(monkeypatch):
    row = _bq_row(last_synced_at="2026-05-25T00:00:00", hours_since_sync=168.0)
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([[row]]),
    )
    monkeypatch.setenv("FRESHNESS_SLA_HOURS", "24")
    result = _freshness_signal("conn_1")
    assert result["status"] == "STALE"


def test_freshness_signal_never_synced(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([[]]),
    )
    result = _freshness_signal("never_conn")
    assert result["status"] == "NEVER_SYNCED"
    assert result["hours_since_sync"] is None


# ── _drift_stability_signal ───────────────────────────────────────────────


def test_drift_stability_returns_counts(monkeypatch):
    row = _bq_row(total_changes=5, breaking_changes=2)
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([[row]]),
    )
    result = _drift_stability_signal("conn_1")
    assert result["total_changes"] == 5
    assert result["breaking_changes"] == 2


def test_drift_stability_empty_returns_zeros(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([[]]),
    )
    result = _drift_stability_signal("conn_1")
    assert result == {"total_changes": 0, "breaking_changes": 0}


# ── _type_suitability_signal ──────────────────────────────────────────────


def _make_col(name, dtype, table="customers", schema="public", pos=1):
    return ColumnRecord(
        table_schema=schema, table_name=table,
        column_name=name, data_type=dtype,
        ordinal_position=pos, is_nullable=True,
    )


def test_type_suitability_counts_json_struct(monkeypatch):
    schema = {
        "public.orders": [
            _make_col("id", "INT64"),
            _make_col("meta", "JSON"),
            _make_col("attrs", "STRUCT"),
            _make_col("name", "STRING"),
        ]
    }
    monkeypatch.setattr("app.tools.readiness_score._fetch_schema_for_connection", lambda _: schema)
    result = _type_suitability_signal("c")
    assert result["total_columns"] == 4
    assert result["semi_structured_columns"] == 2
    assert result["semi_structured_pct"] == 50.0


def test_type_suitability_no_semi_structured(monkeypatch):
    schema = {"public.t": [_make_col("a", "STRING"), _make_col("b", "INT64")]}
    monkeypatch.setattr("app.tools.readiness_score._fetch_schema_for_connection", lambda _: schema)
    result = _type_suitability_signal("c")
    assert result["semi_structured_columns"] == 0
    assert result["semi_structured_pct"] == 0.0


def test_type_suitability_empty_schema(monkeypatch):
    monkeypatch.setattr("app.tools.readiness_score._fetch_schema_for_connection", lambda _: {})
    result = _type_suitability_signal("c")
    assert result["total_columns"] == 0
    assert result["semi_structured_pct"] == 0.0


# ── _naming_coherence_signal ──────────────────────────────────────────────


def test_naming_coherence_flags_short_and_numeric_suffix(monkeypatch):
    schema = {
        "public.t": [
            _make_col("id", "INT64"),        # ≤3 chars → incoherent
            _make_col("ab", "STRING"),        # ≤3 chars → incoherent
            _make_col("col1", "STRING"),      # numeric suffix → incoherent
            _make_col("customer_name", "STRING"),  # coherent
            _make_col("email_address", "STRING"),  # coherent
        ]
    }
    monkeypatch.setattr("app.tools.readiness_score._fetch_schema_for_connection", lambda _: schema)
    result = _naming_coherence_signal("c")
    assert result["total_columns"] == 5
    assert result["incoherent_names"] == 3
    assert result["incoherent_pct"] == 60.0


def test_naming_coherence_all_coherent(monkeypatch):
    schema = {"public.t": [_make_col("customer_id", "INT64"), _make_col("created_at", "TIMESTAMP")]}
    monkeypatch.setattr("app.tools.readiness_score._fetch_schema_for_connection", lambda _: schema)
    result = _naming_coherence_signal("c")
    assert result["incoherent_names"] == 0
    assert result["incoherent_pct"] == 0.0


# ── score_ai_readiness ────────────────────────────────────────────────────

_CANNED_SCORE_RESPONSE = json.dumps({
    "grade": "B",
    "narrative": "The connection has good freshness but moderate drift.",
    "top_remediations": ["Reduce breaking changes", "Rename short columns"],
})


def _stub_signals(monkeypatch, connection_id="conn_x"):
    monkeypatch.setattr(
        "app.tools.readiness_score._freshness_signal",
        lambda _: {"status": "OK", "hours_since_sync": 3.0},
    )
    monkeypatch.setattr(
        "app.tools.readiness_score._drift_stability_signal",
        lambda _: {"total_changes": 3, "breaking_changes": 1},
    )
    monkeypatch.setattr(
        "app.tools.readiness_score._type_suitability_signal",
        lambda _: {"total_columns": 10, "semi_structured_columns": 1, "semi_structured_pct": 10.0},
    )
    monkeypatch.setattr(
        "app.tools.readiness_score._naming_coherence_signal",
        lambda _: {"total_columns": 10, "incoherent_names": 1, "incoherent_pct": 10.0},
    )


def test_score_ai_readiness_grade_extraction(monkeypatch):
    _stub_signals(monkeypatch)
    result = score_ai_readiness("conn_x", model_fn=lambda _: _CANNED_SCORE_RESPONSE)
    assert result["connection_id"] == "conn_x"
    assert result["grade"] == "B"
    assert "narrative" in result
    assert isinstance(result["top_remediations"], list)
    assert len(result["top_remediations"]) == 2


def test_score_ai_readiness_signals_passthrough(monkeypatch):
    _stub_signals(monkeypatch)
    result = score_ai_readiness("conn_x", model_fn=lambda _: _CANNED_SCORE_RESPONSE)
    assert result["signals"]["freshness"]["status"] == "OK"
    assert result["signals"]["drift_stability_30d"]["breaking_changes"] == 1
    assert result["signals"]["completeness"] == "n/a"


def test_score_ai_readiness_graceful_on_bad_json(monkeypatch):
    _stub_signals(monkeypatch)
    result = score_ai_readiness("conn_x", model_fn=lambda _: "not json at all")
    assert result["grade"] == "?"
    assert result["top_remediations"] == []


def test_score_ai_readiness_grade_uppercased(monkeypatch):
    _stub_signals(monkeypatch)
    resp = json.dumps({"grade": "a", "narrative": "ok", "top_remediations": []})
    result = score_ai_readiness("conn_x", model_fn=lambda _: resp)
    assert result["grade"] == "A"


# ── list_readiness_scores ─────────────────────────────────────────────────


def test_list_readiness_scores_sorted_worst_first(monkeypatch):
    _stub_signals(monkeypatch)
    calls = iter([
        json.dumps({"grade": "A", "narrative": "great", "top_remediations": []}),
        json.dumps({"grade": "F", "narrative": "bad", "top_remediations": ["fix"]}),
        json.dumps({"grade": "C", "narrative": "ok", "top_remediations": []}),
    ])

    cid_rows = [_bq_row(connection_id="c_a"), _bq_row(connection_id="c_b"), _bq_row(connection_id="c_c")]

    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([cid_rows]),
    )
    results = list_readiness_scores(model_fn=lambda _: next(calls))
    assert results[0]["grade"] == "F"
    assert results[-1]["grade"] == "A"


def test_list_readiness_scores_empty_sync_log(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([[]]),
    )
    results = list_readiness_scores(model_fn=lambda _: "{}")
    assert results == []


# ── analyze_drift_volatility ──────────────────────────────────────────────

_CANNED_VOLATILITY_RESPONSE = json.dumps({
    "connections": [
        {
            "connection_id": "conn_a",
            "stability_class": "VOLATILE",
            "narrative": "Two breaking changes in 30 days.",
            "recommendation": "Introduce a schema change review process.",
        },
        {
            "connection_id": "conn_b",
            "stability_class": "STABLE",
            "narrative": "Only additive changes observed.",
            "recommendation": "No action required.",
        },
    ],
    "fleet_summary": "One volatile connection requires attention.",
})

_VOLATILITY_ROWS = [
    _bq_row(connection_id="conn_a", total_changes=8, breaking_changes=2, changes_per_week=1.87),
    _bq_row(connection_id="conn_b", total_changes=1, breaking_changes=0, changes_per_week=0.23),
]


def test_analyze_drift_volatility_returns_all_connections(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([_VOLATILITY_ROWS]),
    )
    result = analyze_drift_volatility(days=30, model_fn=lambda _: _CANNED_VOLATILITY_RESPONSE)
    assert result["period_days"] == 30
    assert len(result["connections"]) == 2
    cids = {c["connection_id"] for c in result["connections"]}
    assert cids == {"conn_a", "conn_b"}


def test_analyze_drift_volatility_stability_class_merged(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([_VOLATILITY_ROWS]),
    )
    result = analyze_drift_volatility(days=30, model_fn=lambda _: _CANNED_VOLATILITY_RESPONSE)
    by_cid = {c["connection_id"]: c for c in result["connections"]}
    assert by_cid["conn_a"]["stability_class"] == "VOLATILE"
    assert by_cid["conn_b"]["stability_class"] == "STABLE"


def test_analyze_drift_volatility_preserves_bq_counts(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([_VOLATILITY_ROWS]),
    )
    result = analyze_drift_volatility(days=30, model_fn=lambda _: _CANNED_VOLATILITY_RESPONSE)
    conn_a = next(c for c in result["connections"] if c["connection_id"] == "conn_a")
    assert conn_a["total_changes"] == 8
    assert conn_a["breaking_changes"] == 2
    assert conn_a["changes_per_week"] == 1.87


def test_analyze_drift_volatility_fleet_summary(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([_VOLATILITY_ROWS]),
    )
    result = analyze_drift_volatility(days=30, model_fn=lambda _: _CANNED_VOLATILITY_RESPONSE)
    assert "volatile" in result["fleet_summary"].lower()


def test_analyze_drift_volatility_no_events(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([[]]),
    )
    result = analyze_drift_volatility(days=30, model_fn=lambda _: "{}")
    assert result["connections"] == []
    assert "No drift events" in result["fleet_summary"]


def test_analyze_drift_volatility_graceful_on_bad_gemini_json(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([_VOLATILITY_ROWS]),
    )
    result = analyze_drift_volatility(days=30, model_fn=lambda _: "not json")
    assert len(result["connections"]) == 2
    for c in result["connections"]:
        assert c["stability_class"] == "UNKNOWN"


def test_analyze_drift_volatility_custom_days(monkeypatch):
    monkeypatch.setattr(
        "app.tools.readiness_score._client",
        lambda: _mock_client([[_bq_row(connection_id="c", total_changes=1, breaking_changes=0, changes_per_week=0.5)]]),
    )
    resp = json.dumps({
        "connections": [{"connection_id": "c", "stability_class": "STABLE", "narrative": "x", "recommendation": "y"}],
        "fleet_summary": "all good",
    })
    result = analyze_drift_volatility(days=14, model_fn=lambda _: resp)
    assert result["period_days"] == 14
