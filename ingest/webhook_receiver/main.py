"""Cloud Run webhook receiver for Fivetran `sync_end` events.

Verifies the HMAC signature, then kicks off the detection flow.

Skeleton — signature verification + dispatch contract. Implementation TODO.
Invocation model resolved: DIRECT invoke (no task queue). `dispatch()` is kept
as the single seam so a future swap to Pub/Sub stays localized.
"""

from __future__ import annotations

import hashlib
import hmac
import os

SUBSCRIBED_EVENTS = {"sync_end"}


def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    """Fivetran signs the payload with SHA-256 HMAC using the webhook secret."""
    secret = os.environ["FIVETRAN_WEBHOOK_SECRET"].encode()
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header or "")


def dispatch(payload: dict) -> None:
    """Kick off the detection flow for one connection.

    Resolved: DIRECT invoke of the Agent Builder agent. No task queue.
    Kept as the single seam so a future swap to Pub/Sub stays localized.

    Must be fire-and-forget: start the agent invocation on a background
    thread/task and return immediately so the HTTP handler can ack within
    Fivetran's 10s webhook timeout. Detection is convergent, so a failure
    here self-heals on the next sync_end (no durable queue needed).
    """
    # TODO: spawn background task -> invoke Agent Builder agent with
    #       {connection_id, sync_id, trigger_event="sync_end"}; return now
    raise NotImplementedError


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
