"""AI-readiness scoring and drift volatility analysis — v3 Phase 1.

Feature 1 — AI-Readiness Score:
  score_ai_readiness(connection_id) assembles four BQ-derived signals
  (freshness, drift stability, type suitability, naming coherence) then
  calls Gemini once to produce a letter grade A–F with a narrative and
  top remediations. list_readiness_scores() runs the scoring for every
  connection seen in sync_log, returning results worst-first.

Feature 4 — Drift Volatility Analyzer:
  analyze_drift_volatility(days) queries drift_events, computes per-connection
  change-per-week rates, and calls Gemini to classify each connection as
  STABLE / VOLATILE / CRITICAL with a recommendation.

Gemini call pattern follows classify_drift.py exactly: lazy genai client,
CLASSIFIER_MODEL constant, _call_gemini() injectable via model_fn= for tests.
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable

from .bigquery_query import (
    BQ_LOCATION,
    _client,
    _fetch_schema_for_connection,
    _state_table_fqn,
)

CLASSIFIER_MODEL = "gemini-flash-latest"


# ── Gemini call (the side-effect boundary) ────────────────────────────────

def _call_gemini(prompt: str) -> str:
    from google import genai  # noqa: PLC0415 — lazy import
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ["GCP_PROJECT_ID"]
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
    client = genai.Client(vertexai=True, project=project, location=location)
    response = client.models.generate_content(model=CLASSIFIER_MODEL, contents=prompt)
    return response.text


_FENCE_RE = re.compile(r"^```(?:json)?\s*(.+?)\s*```$", re.DOTALL)


def _extract_json(text: str):
    """Strip optional markdown fence then parse as JSON."""
    s = text.strip()
    m = _FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()
    return json.loads(s)


# ── Feature 1 — signal collectors ─────────────────────────────────────────

def _freshness_signal(connection_id: str) -> dict:
    from google.cloud import bigquery  # noqa: PLC0415
    sql = (
        "SELECT MAX(synced_at) AS last_synced_at, "
        "TIMESTAMP_DIFF(CURRENT_TIMESTAMP(), MAX(synced_at), SECOND) / 3600.0 AS hours_since_sync "
        f"FROM {_state_table_fqn('sync_log')} "
        "WHERE connection_id = @connection_id "
        "GROUP BY connection_id"
    )
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("connection_id", "STRING", connection_id),
    ])
    rows = list(_client().query(sql, location=BQ_LOCATION, job_config=cfg).result())
    if not rows:
        return {"status": "NEVER_SYNCED", "hours_since_sync": None}
    hours = float(rows[0]["hours_since_sync"])
    sla = float(os.environ.get("FRESHNESS_SLA_HOURS", "24"))
    return {"status": "OK" if hours <= sla else "STALE", "hours_since_sync": round(hours, 2)}


def _drift_stability_signal(connection_id: str) -> dict:
    from google.cloud import bigquery  # noqa: PLC0415
    sql = (
        "SELECT COUNT(*) AS total_changes, "
        "COUNTIF(change_type IN ('TYPE_PROMOTION','DEPRECATION','RENAME')) AS breaking_changes "
        f"FROM {_state_table_fqn('drift_events')} "
        "WHERE connection_id = @connection_id "
        "AND detected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
    )
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("connection_id", "STRING", connection_id),
    ])
    rows = list(_client().query(sql, location=BQ_LOCATION, job_config=cfg).result())
    if not rows:
        return {"total_changes": 0, "breaking_changes": 0}
    row = rows[0]
    return {
        "total_changes": int(row["total_changes"]),
        "breaking_changes": int(row["breaking_changes"]),
    }


def _type_suitability_signal(connection_id: str) -> dict:
    schema = _fetch_schema_for_connection(connection_id)
    total = sum(len(cols) for cols in schema.values())
    semi_structured = sum(
        1 for cols in schema.values()
        for col in cols
        if col.data_type.upper() in ("JSON", "STRUCT")
    )
    return {
        "total_columns": total,
        "semi_structured_columns": semi_structured,
        "semi_structured_pct": round(semi_structured / total * 100, 1) if total else 0.0,
    }


def _naming_coherence_signal(connection_id: str) -> dict:
    schema = _fetch_schema_for_connection(connection_id)
    _numeric_suffix = re.compile(r"\d+$")
    total = 0
    incoherent = 0
    for cols in schema.values():
        for col in cols:
            total += 1
            if len(col.column_name) <= 3 or _numeric_suffix.search(col.column_name):
                incoherent += 1
    return {
        "total_columns": total,
        "incoherent_names": incoherent,
        "incoherent_pct": round(incoherent / total * 100, 1) if total else 0.0,
    }


_READINESS_PROMPT = """\
You are an AI data-readiness analyst. Given the following signals for a Fivetran connection,
produce a concise AI-readiness assessment.

CONNECTION: {connection_id}
SIGNALS:
{signals_json}

SIGNAL KEY:
  freshness.status          OK/STALE/NEVER_SYNCED — is recent data available?
  drift_stability_30d       how many schema changes in the last 30 days, and how many were breaking?
  type_suitability          percentage of columns that are JSON/STRUCT (harder for LLMs to consume)
  naming_coherence          percentage of columns with very short (≤3 char) or purely numeric-suffix names
  completeness              n/a (requires row-level sampling; deferred to v3.1)

Respond with ONLY a JSON object (no markdown fences) with exactly these keys:
  grade            : one character — A, B, C, D, or F  (A = most AI-ready, F = least)
  narrative        : 2-3 sentence plain-English summary of the readiness state
  top_remediations : list of up to 3 concrete action items to improve AI-readiness (strings)
"""


def score_ai_readiness(
    connection_id: str,
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> dict:
    """Score the AI-readiness of a Fivetran connection on a grade of A–F.

    Assembles four signals from BigQuery (freshness, drift stability,
    type suitability, naming coherence) and calls Gemini once to synthesize
    a letter grade, narrative, and top remediations. Completeness requires
    row-level sampling and is marked 'n/a' for this version.

    Args:
        connection_id: Fivetran connection ID (e.g. 'assimilate_seem').

    Returns a dict with keys: connection_id, grade (A–F), signals (raw
    signal values), narrative (string), top_remediations (list[str]).
    """
    signals = {
        "freshness": _freshness_signal(connection_id),
        "drift_stability_30d": _drift_stability_signal(connection_id),
        "type_suitability": _type_suitability_signal(connection_id),
        "naming_coherence": _naming_coherence_signal(connection_id),
        "completeness": "n/a",
    }

    prompt = _READINESS_PROMPT.format(
        connection_id=connection_id,
        signals_json=json.dumps(signals, indent=2),
    )
    raw = model_fn(prompt)
    try:
        parsed = _extract_json(raw)
        grade = str(parsed.get("grade", "?")).strip().upper()
        narrative = str(parsed.get("narrative", ""))
        remediations = list(parsed.get("top_remediations", []))
    except (json.JSONDecodeError, AttributeError):
        grade = "?"
        narrative = raw.strip()
        remediations = []

    return {
        "connection_id": connection_id,
        "grade": grade,
        "signals": signals,
        "narrative": narrative,
        "top_remediations": remediations,
    }


_GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4, "?": 5}


def list_readiness_scores(
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> list[dict]:
    """Score AI-readiness for every connection that has ever synced.

    Queries sync_log for distinct connection_ids, runs score_ai_readiness for
    each, and returns results sorted by grade descending (F before A — worst
    first, so the most urgent issues surface at the top).

    Returns a list of score dicts (same shape as score_ai_readiness).
    """
    sql = (
        f"SELECT DISTINCT connection_id FROM {_state_table_fqn('sync_log')} "
        "ORDER BY connection_id"
    )
    rows = _client().query(sql, location=BQ_LOCATION).result()
    connection_ids = [r["connection_id"] for r in rows]
    scores = [score_ai_readiness(cid, model_fn=model_fn) for cid in connection_ids]
    scores.sort(key=lambda s: _GRADE_ORDER.get(s["grade"], 5), reverse=True)
    return scores


# ── Feature 4 — Drift Volatility Analyzer ─────────────────────────────────

_VOLATILITY_PROMPT = """\
You are a data pipeline reliability analyst. Below is a summary of schema drift \
events across Fivetran connections over the past {days} days.

DRIFT SUMMARY (one row per connection):
{rows_json}

For each connection, classify its stability as one of:
  STABLE   — fewer than 2 changes/week and no breaking changes
  VOLATILE — 2+ changes/week OR any breaking changes in the period
  CRITICAL — 5+ breaking changes OR fundamentally unstable schema

Return ONLY a JSON object (no markdown fences) with exactly these keys:
  connections  : list of objects, one per connection, each with keys:
                   connection_id     (string)
                   stability_class   (STABLE | VOLATILE | CRITICAL)
                   narrative         (one sentence describing what's happening)
                   recommendation    (one concrete action item)
  fleet_summary: one sentence describing the overall fleet health
"""


def analyze_drift_volatility(
    days: int = 30,
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> dict:
    """Analyze schema-drift volatility across all connections over a time window.

    Queries drift_events for change counts per connection, computes a
    changes-per-week rate, then calls Gemini to classify each connection as
    STABLE, VOLATILE, or CRITICAL and provide a recommendation.

    Args:
        days: look-back window in days (default 30).

    Returns a dict with keys: period_days (int), connections (list of
    per-connection dicts with connection_id, total_changes, breaking_changes,
    changes_per_week, stability_class, narrative, recommendation),
    fleet_summary (string).
    """
    from google.cloud import bigquery  # noqa: PLC0415
    sql = (
        "SELECT connection_id, "
        "COUNT(*) AS total_changes, "
        "COUNTIF(change_type IN ('TYPE_PROMOTION','DEPRECATION','RENAME')) AS breaking_changes, "
        "ROUND(COUNT(*) / (@days / 7.0), 2) AS changes_per_week "
        f"FROM {_state_table_fqn('drift_events')} "
        "WHERE detected_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY) "
        "GROUP BY connection_id "
        "ORDER BY changes_per_week DESC"
    )
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("days", "INT64", days),
    ])
    rows = list(_client().query(sql, location=BQ_LOCATION, job_config=cfg).result())

    if not rows:
        return {
            "period_days": days,
            "connections": [],
            "fleet_summary": "No drift events recorded in the specified window.",
        }

    row_dicts = [
        {
            "connection_id": r["connection_id"],
            "total_changes": int(r["total_changes"]),
            "breaking_changes": int(r["breaking_changes"]),
            "changes_per_week": float(r["changes_per_week"]),
        }
        for r in rows
    ]

    prompt = _VOLATILITY_PROMPT.format(days=days, rows_json=json.dumps(row_dicts, indent=2))
    raw = model_fn(prompt)
    try:
        parsed = _extract_json(raw)
        gemini_by_cid = {c["connection_id"]: c for c in parsed.get("connections", [])}
        fleet_summary = str(parsed.get("fleet_summary", ""))
    except (json.JSONDecodeError, KeyError):
        gemini_by_cid = {}
        fleet_summary = raw.strip()

    result_connections = []
    for rd in row_dicts:
        cid = rd["connection_id"]
        g = gemini_by_cid.get(cid, {})
        result_connections.append({
            "connection_id": cid,
            "total_changes": rd["total_changes"],
            "breaking_changes": rd["breaking_changes"],
            "changes_per_week": rd["changes_per_week"],
            "stability_class": g.get("stability_class", "UNKNOWN"),
            "narrative": g.get("narrative", ""),
            "recommendation": g.get("recommendation", ""),
        })

    return {
        "period_days": days,
        "connections": result_connections,
        "fleet_summary": fleet_summary,
    }
