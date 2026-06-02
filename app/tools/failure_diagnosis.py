"""Pipeline failure diagnosis — v3 Phase 4, Feature 6.

diagnose_sync_failures(connection_id, days) queries the `sync_failure_log` table
(populated by Fivetran's external-logging API via scripts/setup_external_logging.sh),
groups errors by error_code, and calls Gemini to identify root causes and recommended
fixes for recurring failure patterns.

Graceful degradation: when sync_failure_log is empty (external-logging not yet
configured, or the connection has no failures) the tool returns immediately with
status="no_failures" without calling Gemini — zero credits consumed.

Gemini call pattern: reuses CLASSIFIER_MODEL + _call_gemini from readiness_score.py.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from urllib.request import Request, urlopen
from typing import Callable

log = logging.getLogger(__name__)

from .bigquery_query import (
    BQ_LOCATION,
    _client,
    _state_table_fqn,
)
from .readiness_score import _call_gemini, _extract_json

_FIVETRAN_API_BASE = "https://api.fivetran.com/v1"
_ERROR_SYNC_STATES = {"error", "broken"}


def _fetch_connector_status(connection_id: str) -> dict | None:
    """Call GET /v1/connectors/{connection_id} and return the status sub-dict.

    Returns None on missing credentials or any network/HTTP error.
    Same Basic auth pattern as connection_resolver._fetch_schema().
    """
    api_key = os.environ.get("FIVETRAN_API_KEY", "")
    api_secret = os.environ.get("FIVETRAN_API_SECRET", "")
    if not api_key or not api_secret:
        return None

    token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
    req = Request(
        f"{_FIVETRAN_API_BASE}/connectors/{connection_id}",
        headers={"Authorization": f"Basic {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
    except Exception as exc:
        log.warning("Fivetran API status check failed for connection=%s: %s", connection_id, exc)
        return None

    return body.get("data", {}).get("status")


_SEVERITY_THRESHOLDS = {
    "CRITICAL": 10,
    "HIGH": 5,
    "MEDIUM": 2,
    "LOW": 1,
}


def _severity_from_count(failure_count: int) -> str:
    for level, threshold in _SEVERITY_THRESHOLDS.items():
        if failure_count >= threshold:
            return level
    return "LOW"


_DIAGNOSIS_PROMPT = """\
You are a data pipeline reliability engineer. Analyze the following Fivetran sync \
failure log for connection `{connection_id}` over the past {days} days.

FAILURE SUMMARY ({failure_count} total failures):
{errors_json}

For each error pattern, identify:
1. The most likely root cause (be specific — e.g. "replication slot lag", \
   "schema mismatch after column rename", "destination quota exceeded")
2. A concrete remediation step the operator can take

Then provide an overall severity assessment and recommended priority actions.

Return ONLY a JSON object (no markdown fences):
{{
  "diagnosis": "2-3 sentence overall assessment of what is happening and why",
  "recommended_actions": ["action 1", "action 2", "action 3"],
  "severity": "LOW | MEDIUM | HIGH | CRITICAL"
}}
"""


def diagnose_sync_failures(
    connection_id: str,
    days: int = 7,
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> dict:
    """Diagnose recurring Fivetran sync failures for a connection.

    Queries `sync_failure_log` (populated by Fivetran's external-logging API)
    for the specified connection over the last `days` days, groups failures by
    error code, and calls Gemini to identify root causes and recommend fixes.

    Returns immediately with status='no_failures' when the table has no rows for
    the connection in the window — no Gemini call is made, no credits consumed.

    Args:
        connection_id: Fivetran connection ID (e.g. 'assimilate_seem').
        days: look-back window in days (default 7).

    Returns a dict with keys: connection_id, period_days, failure_count,
    top_errors (list of {error_code, count, sample_message}), diagnosis (string),
    recommended_actions (list[str]), severity (LOW | MEDIUM | HIGH | CRITICAL).
    When there are no failures: {status: 'no_failures', message: str}.
    """
    from google.cloud import bigquery  # noqa: PLC0415

    sql = (
        "SELECT error_code, error_message, table_name, failed_at "
        f"FROM {_state_table_fqn('sync_failure_log')} "
        "WHERE connection_id = @connection_id "
        "AND failed_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY) "
        "ORDER BY failed_at DESC"
    )
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("connection_id", "STRING", connection_id),
        bigquery.ScalarQueryParameter("days", "INT64", days),
    ])
    rows = list(_client().query(sql, location=BQ_LOCATION, job_config=cfg).result())

    if not rows:
        # No log data — fall back to the Fivetran REST API for live connector status.
        api_status = _fetch_connector_status(connection_id)

        if api_status is None:
            return {
                "status": "no_failures",
                "connection_id": connection_id,
                "period_days": days,
                "message": (
                    f"No sync failures recorded for connection '{connection_id}' in the "
                    f"last {days} days. External-logging may not be configured "
                    "(run scripts/setup_external_logging.sh). "
                    "Could not reach the Fivetran API to check live connector status."
                ),
            }

        sync_state = api_status.get("sync_state", "unknown")
        tasks = api_status.get("tasks", [])

        if sync_state not in _ERROR_SYNC_STATES and not tasks:
            return {
                "status": "no_failures",
                "connection_id": connection_id,
                "period_days": days,
                "message": (
                    f"Connection '{connection_id}' is currently {sync_state!r} with no "
                    f"active errors. No historical failure log data is available for the "
                    f"last {days} days (run scripts/setup_external_logging.sh to enable "
                    "historical logging)."
                ),
            }

        # Live errors present — synthesise top_errors from the API response and diagnose.
        if tasks:
            error_items = tasks[:5]
        else:
            error_items = [{"code": sync_state.upper(), "message": "Connection is in error state"}]

        top_errors = [
            {
                "error_code": item.get("code", "UNKNOWN"),
                "count": 1,
                "sample_message": item.get("message", ""),
            }
            for item in error_items
        ]
        failure_count = len(top_errors)
        prompt = _DIAGNOSIS_PROMPT.format(
            connection_id=connection_id,
            days=days,
            failure_count=failure_count,
            errors_json=json.dumps(top_errors, indent=2),
        )
        raw = model_fn(prompt)
        try:
            parsed = _extract_json(raw)
            diagnosis = str(parsed.get("diagnosis", ""))
            recommended_actions = list(parsed.get("recommended_actions", []))
            gemini_severity = parsed.get("severity", "").upper()
            severity = gemini_severity if gemini_severity in _SEVERITY_THRESHOLDS else _severity_from_count(failure_count)
        except (json.JSONDecodeError, AttributeError):
            diagnosis = raw.strip()
            recommended_actions = []
            severity = _severity_from_count(failure_count)

        return {
            "connection_id": connection_id,
            "period_days": days,
            "failure_count": failure_count,
            "top_errors": top_errors,
            "diagnosis": diagnosis,
            "recommended_actions": recommended_actions,
            "severity": severity,
            "source": "fivetran_api",
        }

    # Group by error_code, rank by frequency
    error_counts: dict[str, dict] = {}
    for row in rows:
        code = row["error_code"] or "UNKNOWN"
        if code not in error_counts:
            error_counts[code] = {"count": 0, "sample_message": row["error_message"] or ""}
        error_counts[code]["count"] += 1

    top_errors = sorted(
        [{"error_code": code, **meta} for code, meta in error_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:5]

    failure_count = len(rows)
    prompt = _DIAGNOSIS_PROMPT.format(
        connection_id=connection_id,
        days=days,
        failure_count=failure_count,
        errors_json=json.dumps(top_errors, indent=2),
    )
    raw = model_fn(prompt)
    try:
        parsed = _extract_json(raw)
        diagnosis = str(parsed.get("diagnosis", ""))
        recommended_actions = list(parsed.get("recommended_actions", []))
        # Trust Gemini's severity if valid; fall back to count-based heuristic
        gemini_severity = parsed.get("severity", "").upper()
        severity = gemini_severity if gemini_severity in _SEVERITY_THRESHOLDS else _severity_from_count(failure_count)
    except (json.JSONDecodeError, AttributeError):
        diagnosis = raw.strip()
        recommended_actions = []
        severity = _severity_from_count(failure_count)

    return {
        "connection_id": connection_id,
        "period_days": days,
        "failure_count": failure_count,
        "top_errors": top_errors,
        "diagnosis": diagnosis,
        "recommended_actions": recommended_actions,
        "severity": severity,
        "source": "sync_failure_log",
    }
