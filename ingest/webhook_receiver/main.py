"""Cloud Run webhook receiver for Fivetran `sync_end` events.

Verifies the HMAC signature (G4-verified format: SHA-256 hex digest, no
prefix, no timestamp header), then dispatches the detection pipeline as
fire-and-forget background work and acks HTTP 200 within Fivetran's 10s
webhook timeout.

Invocation model — Resolved Decision #1 (DIRECT invoke, no task queue):
  Detection is convergent (rebuilds truth from BigQuery INFORMATION_SCHEMA
  each run), so a dropped event self-heals on the next sync_end. A durable
  queue would add infrastructure to defend a failure mode that doesn't
  warrant it. `dispatch()` is kept as the single seam so a future swap to
  Pub/Sub stays localized.

Deployment caveat (Cloud Run):
  Fire-and-forget on Cloud Run requires either (a) "CPU always allocated"
  on the service config so background threads keep running after the HTTP
  response, or (b) blocking until the work completes (turns this into a
  near-synchronous handler — fine if pipeline runtime stays < ~5s, which
  G1/G2 measurements suggest at small schemas). v1 picks (a) — see
  deploy/cloudbuild.yaml; if you switch to (b), remove the threading layer.

Detection pipeline orchestrated here (composition only — every primitive
is in app/tools/):

    capture_and_gate  -> if unchanged, exit cheap
                      -> if bootstrap, write baseline only (no diff)
                      -> if drift, continue
    write_snapshot    -> new schema_snapshots + column_snapshots rows
    load_columns      -> prior columns from prior snapshot
    diff_columns      -> candidate ColumnChange events
    classify          -> Gemini ranks each candidate
    insert_drift_event -> PROPOSED rows for agent review
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import threading
import uuid
from datetime import datetime, timezone

from app.tools import bigquery_query, classify_drift, snapshot_diff
from ingest.webhook_receiver.connection_resolver import resolve_destination_schema

SUBSCRIBED_EVENTS = {"sync_end"}

log = logging.getLogger(__name__)


# ── Signature verification (G4-verified: SHA-256 hex, no prefix) ─────────────

def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """Fivetran signs the raw wire body with HMAC-SHA-256 and sends the hex
    digest in `X-Fivetran-Signature-256` (no `sha256=` prefix, no timestamp
    header — verified live 2026-05-21 against payload bytes that exactly
    match Fivetran's reported `content-length`).
    """
    secret = os.environ["FIVETRAN_WEBHOOK_SECRET"].encode()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


# ── Dispatch: fire-and-forget thread spawn ───────────────────────────────────

def dispatch(payload: dict) -> None:
    """Spawn the detection pipeline on a background daemon thread and
    return immediately. The HTTP handler can ack 200 well within Fivetran's
    10s webhook timeout regardless of pipeline runtime (typical 1-5s on
    small schemas per G1/G2; can be longer on bigger fleets).
    """
    thread = threading.Thread(
        target=_run_detection_pipeline,
        args=(payload,),
        name=f"detection-{payload.get('connector_id', 'unknown')}",
        daemon=True,
    )
    thread.start()


# ── Detection pipeline (runs in background thread) ───────────────────────────

def _run_detection_pipeline(payload: dict) -> None:
    """Composes the detection algorithm end-to-end. Exception-safe: any
    failure is logged so the convergent property holds (the next sync_end
    will retry the full pipeline from scratch).
    """
    try:
        connection_id = payload["connector_id"]
        sync_id = payload.get("sync_id", "")
        connection_name = payload.get("connector_name", "")
        destination_schema = resolve_destination_schema(connection_id)

        # Steps 1-3: fetch landed columns, exclude Fivetran system columns,
        # hash, compare against the latest persisted snapshot.
        gate = snapshot_diff.capture_and_gate(connection_id, destination_schema)
        if not gate.changed:
            log.info(
                "sync_end %s: no schema change (hash gate hit); cheap exit.",
                connection_id,
            )
            return

        # Step 4: write the new snapshot (schema_snapshots + column_snapshots).
        snapshot_id = str(uuid.uuid4())
        captured_at = datetime.now(timezone.utc).isoformat()
        snapshot_row = {
            "snapshot_id": snapshot_id,
            "connection_id": connection_id,
            "connection_name": connection_name,
            "destination_schema": destination_schema,
            "captured_at": captured_at,
            "trigger_event": "sync_end",
            "sync_id": sync_id,
            "column_count": len(gate.current_columns),
            "content_hash": gate.current_hash,
        }
        column_rows = [
            {
                "snapshot_id": snapshot_id,
                "connection_id": connection_id,
                "table_schema": c.table_schema,
                "table_name": c.table_name,
                "column_name": c.column_name,
                "data_type": c.data_type,
                "ordinal_position": c.ordinal_position,
                "is_nullable": c.is_nullable,
                "captured_at": captured_at,
            }
            for c in gate.current_columns
        ]
        bigquery_query.write_snapshot(snapshot_row, column_rows)

        # Step 5: on bootstrap (no prior snapshot for this connection), stop
        # here — diff_columns(empty, current) would emit a NEW_FIELD for every
        # existing column, which is noise, not drift.
        if gate.prior_snapshot is None:
            log.info(
                "sync_end %s: bootstrap baseline snapshot %s written (%d columns); no diff.",
                connection_id, snapshot_id, len(gate.current_columns),
            )
            return

        # Step 6: load prior columns and run the diff.
        prior_snapshot_id = gate.prior_snapshot["snapshot_id"]
        prior_columns = bigquery_query.load_columns(prior_snapshot_id)
        changes = snapshot_diff.diff_columns(prior_columns, gate.current_columns)

        if not changes:
            # Hash differed but diff is empty — shouldn't happen normally, but
            # is benign (snapshot recorded, no drift events).
            log.warning(
                "sync_end %s: hash differed (%s -> %s) but diff_columns produced no changes.",
                connection_id, gate.prior_snapshot["content_hash"], gate.current_hash,
            )
            return

        # Steps 7-8: classify each candidate change and write drift_events
        # in PROPOSED state for the agent's review queue.
        for change in changes:
            try:
                # downstream_refs left empty for v1; v1.1 may discover dbt /
                # Looker / BI consumers and pass them in for context-aware
                # remediation SQL generation.
                classification = classify_drift.classify(change, downstream_refs=[])
            except Exception:
                log.exception(
                    "classify failed for change in %s.%s (table=%s); skipping",
                    change.table_schema, change.table_name, change.change_type,
                )
                continue

            drift_id = str(uuid.uuid4())
            event = {
                "drift_id": drift_id,
                "connection_id": connection_id,
                "detected_at": captured_at,
                "from_snapshot_id": prior_snapshot_id,
                "to_snapshot_id": snapshot_id,
                "table_schema": change.table_schema,
                "table_name": change.table_name,
                "change_type": classification.change_type,
                "column_before": _column_to_dict(change.before),
                "column_after": _column_to_dict(change.after),
                "classification_conf": classification.confidence,
                "gemini_rationale": classification.rationale,
                "remediation_sql": classification.remediation_sql,
                "transformation_id": None,
                "remediation_status": "PROPOSED",
                "approved_by": None,
                "updated_at": captured_at,
            }
            bigquery_query.insert_drift_event(event)
            log.info(
                "drift_event %s written: %s on %s.%s (conf=%.2f)",
                drift_id, classification.change_type,
                change.table_schema, change.table_name, classification.confidence,
            )

    except Exception:
        # Catch-all so background-thread death is visible. Detection is
        # convergent: the next sync_end retries the full pipeline.
        log.exception("detection pipeline failed for payload: %s", payload)


def _column_to_dict(col) -> dict | None:
    """ColumnRecord -> dict for the drift_events JSON columns (column_before,
    column_after). None passes through (added or dropped column)."""
    if col is None:
        return None
    return {
        "table_schema": col.table_schema,
        "table_name": col.table_name,
        "column_name": col.column_name,
        "data_type": col.data_type,
        "ordinal_position": col.ordinal_position,
        "is_nullable": col.is_nullable,
    }


# ── HTTP entrypoint (functions-framework / Cloud Run) ────────────────────────

def handle_request(request):
    """Cloud Run / Functions HTTP entrypoint.

    Must return HTTP 200 within 10s (Fivetran webhook timeout) or Fivetran
    retries on its exponential schedule. Do the heavy work async — ack fast.
    """
    raw = request.get_data()
    if not verify_signature(raw, request.headers.get("X-Fivetran-Signature-256")):
        return ("invalid signature", 401)

    payload = request.get_json(silent=True) or {}
    if payload.get("event") not in SUBSCRIBED_EVENTS:
        return ("ignored", 200)            # not an event we act on
    if payload.get("data", {}).get("status") != "SUCCESSFUL":
        # Load-bearing filter — DO NOT remove. Fivetran emits a FAILED
        # sync_end followed by a SUCCESSFUL one when a type-promotion drift
        # event causes a Teleport Sync Error + auto-retry (observed live
        # during G2 type_promotion_propagation, 2026-05-21). Acting on the
        # FAILED event would trigger a spurious agent run before the actual
        # schema change has landed in BigQuery.
        return ("ignored: non-successful sync", 200)

    dispatch(payload)                       # fire-and-forget; returns immediately
    return ("ok", 200)                      # ack within Fivetran's 10s timeout
