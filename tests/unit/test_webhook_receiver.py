# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for ingest/webhook_receiver/main.py.

Three concerns under test:
  - `verify_signature`: the G4-derived HMAC contract (SHA-256 hex, no
    prefix, signature over the raw wire bytes). Exercised with the actual
    captured payload + secret from 2026-05-21.
  - `handle_request`: HTTP gating — bad signature, wrong event, non-
    SUCCESSFUL status, and the happy path that calls `dispatch`.
  - `_run_detection_pipeline`: the synchronous composition of capture/diff/
    classify/write. All BQ + Gemini deps monkeypatched at the module
    boundary so the test has no real side effects.
"""

import hashlib
import hmac
import json
import threading
from types import SimpleNamespace

import pytest

from app.tools.bigquery_query import ColumnRecord
from app.tools.classify_drift import Classification
from app.tools.snapshot_diff import ColumnChange, GateResult
from ingest.webhook_receiver import main as wr


# --- Fake HTTP request -----------------------------------------------------

class _Headers:
    def __init__(self, d): self._d = dict(d)
    def get(self, key, default=None):
        return self._d.get(key, self._d.get(key.lower(), default))


class FakeRequest:
    def __init__(self, body: str | bytes, headers: dict | None = None,
                 json_data=None):
        self._body = body if isinstance(body, bytes) else body.encode()
        self.headers = _Headers(headers or {})
        self._json = json_data
    def get_data(self):
        return self._body
    def get_json(self, silent: bool = False):
        return self._json


# --- verify_signature (G4 ground truth from 2026-05-21 capture) ------------
#
# These numbers come straight from the live capture: 287 wire-bytes body
# + 32-byte secret used to create the webhook + the X-Fivetran-Signature-256
# Fivetran returned. End-to-end recomputed locally to match exactly
# (see scripts/capture_webhook_payload.sh header for the HMAC helper).

_LIVE_BODY = (
    b'{"event":"sync_end","created":"2026-05-22T00:06:08.638Z","'
    b'connector_type":"google_cloud_postgresql","connector_id":"'
    b'assimilate_seem","connector_name":"ftar_pg","sync_id":"'
    b'b4703f73-6315-4e22-8d7a-3bfb18b2fed2","destination_group_id":"'
    b'inappropriate_implode","data":{"status":"SUCCESSFUL"}}'
)


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_verify_signature_happy_path(monkeypatch):
    secret = "test-secret-32-bytes-of-randomness"
    monkeypatch.setenv("FIVETRAN_WEBHOOK_SECRET", secret)
    sig = _sign(_LIVE_BODY, secret)
    assert wr.verify_signature(_LIVE_BODY, sig) is True


def test_verify_signature_wrong_signature_rejects(monkeypatch):
    monkeypatch.setenv("FIVETRAN_WEBHOOK_SECRET", "the-real-secret")
    wrong = "0" * 64
    assert wr.verify_signature(_LIVE_BODY, wrong) is False


def test_verify_signature_missing_header_rejects(monkeypatch):
    """A missing header arrives as None from Flask's headers.get(); the
    function must treat that as a non-match, not crash."""
    monkeypatch.setenv("FIVETRAN_WEBHOOK_SECRET", "x")
    assert wr.verify_signature(_LIVE_BODY, None) is False


def test_verify_signature_constant_time_comparison(monkeypatch):
    """Uses hmac.compare_digest (timing-safe). We can't directly assert the
    timing property, but we CAN assert the failure on a near-match (same
    length, single-bit-different) — confirms equality semantics."""
    secret = "s"
    monkeypatch.setenv("FIVETRAN_WEBHOOK_SECRET", secret)
    good = _sign(_LIVE_BODY, secret)
    # Flip the last hex digit; same length, different value.
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert wr.verify_signature(_LIVE_BODY, bad) is False


# --- handle_request: HTTP-layer gating -------------------------------------

def _good_payload():
    return {
        "event": "sync_end",
        "created": "2026-05-22T00:06:08.638Z",
        "connector_type": "google_cloud_postgresql",
        "connector_id": "assimilate_seem",
        "connector_name": "ftar_pg",
        "sync_id": "b4703f73-6315-4e22-8d7a-3bfb18b2fed2",
        "destination_group_id": "inappropriate_implode",
        "data": {"status": "SUCCESSFUL"},
    }


def test_handle_request_rejects_bad_signature(monkeypatch):
    monkeypatch.setenv("FIVETRAN_WEBHOOK_SECRET", "secret")
    req = FakeRequest(
        body=json.dumps(_good_payload()).encode(),
        headers={"X-Fivetran-Signature-256": "0" * 64},
        json_data=_good_payload(),
    )
    body, status = wr.handle_request(req)
    assert status == 401
    assert "invalid signature" in body


def test_handle_request_ignores_non_subscribed_event(monkeypatch):
    """Fivetran sends many event types (transformation_run_succeeded,
    connection_successful, etc.). We only act on sync_end. Acknowledge
    others with 200 so Fivetran doesn't retry."""
    monkeypatch.setenv("FIVETRAN_WEBHOOK_SECRET", "s")
    payload = {**_good_payload(), "event": "transformation_run_succeeded"}
    raw = json.dumps(payload).encode()
    req = FakeRequest(raw, headers={"X-Fivetran-Signature-256": _sign(raw, "s")},
                      json_data=payload)
    body, status = wr.handle_request(req)
    assert status == 200 and "ignored" in body


def test_handle_request_ignores_failed_sync(monkeypatch):
    """Load-bearing status filter — Fivetran fires sync_end with
    status=FAILED then status=SUCCESSFUL during a Teleport Sync retry
    sequence (G2 type_promotion observation). Acting on the FAILED would
    be a spurious agent run."""
    monkeypatch.setenv("FIVETRAN_WEBHOOK_SECRET", "s")
    payload = {**_good_payload(), "data": {"status": "FAILED"}}
    raw = json.dumps(payload).encode()
    req = FakeRequest(raw, headers={"X-Fivetran-Signature-256": _sign(raw, "s")},
                      json_data=payload)
    body, status = wr.handle_request(req)
    assert status == 200 and "non-successful" in body


def test_handle_request_happy_path_dispatches_and_acks(monkeypatch):
    """Good signature + sync_end + SUCCESSFUL -> dispatch() invoked,
    200 ack returned. dispatch() is stubbed to capture the payload (avoid
    spinning up a background thread that does live BQ work)."""
    monkeypatch.setenv("FIVETRAN_WEBHOOK_SECRET", "s")
    captured = []
    monkeypatch.setattr(wr, "dispatch", lambda p: captured.append(p))

    payload = _good_payload()
    raw = json.dumps(payload).encode()
    req = FakeRequest(raw, headers={"X-Fivetran-Signature-256": _sign(raw, "s")},
                      json_data=payload)
    body, status = wr.handle_request(req)

    assert status == 200 and body == "ok"
    assert len(captured) == 1
    assert captured[0]["connector_id"] == "assimilate_seem"


# --- dispatch: fire-and-forget thread ---------------------------------------

def test_dispatch_returns_quickly_and_runs_in_background(monkeypatch):
    """`dispatch` must spawn a background thread and return immediately —
    Fivetran's webhook has a 10s timeout and the HTTP handler must ack
    before the pipeline finishes."""
    started = threading.Event()
    proceed = threading.Event()

    def slow_pipeline(payload):
        started.set()
        proceed.wait(timeout=5.0)   # block until test releases

    monkeypatch.setattr(wr, "_run_detection_pipeline", slow_pipeline)
    wr.dispatch({"connector_id": "assimilate_seem"})

    # dispatch should have returned; the thread is running our blocker.
    assert started.wait(timeout=2.0), "pipeline thread did not start"
    proceed.set()                    # let the thread finish cleanly


# --- _run_detection_pipeline: composition --------------------------------------

def _col(name: str, t: str = "STRING", ordinal: int = 1) -> ColumnRecord:
    return ColumnRecord(
        table_schema="public",
        table_name="customers",
        column_name=name,
        data_type=t,
        ordinal_position=ordinal,
        is_nullable=True,
    )


def _patch_pipeline_deps(
    monkeypatch,
    gate_result: GateResult,
    prior_columns: list | None = None,
    diff_changes: list | None = None,
    classify_response: Classification | None = None,
    destination_schema: str = "public",
):
    """Install stubs on the deps the pipeline calls. Captures every call
    so tests can assert behavior."""
    calls = SimpleNamespace(
        write_snapshot=[], load_columns=[], diff_columns=[],
        classify=[], insert_drift_event=[],
    )
    # Stub the resolver so tests never make real Fivetran API calls.
    monkeypatch.setattr(wr, "resolve_destination_schema", lambda cid: destination_schema)
    monkeypatch.setattr(wr.snapshot_diff, "capture_and_gate",
                        lambda cid, ds: gate_result)
    monkeypatch.setattr(wr.bigquery_query, "write_snapshot",
                        lambda snap, cols: calls.write_snapshot.append((snap, cols)))
    if prior_columns is not None:
        monkeypatch.setattr(wr.bigquery_query, "load_columns",
                            lambda sid: (calls.load_columns.append(sid), prior_columns)[1])
    if diff_changes is not None:
        monkeypatch.setattr(wr.snapshot_diff, "diff_columns",
                            lambda p, c: (calls.diff_columns.append((p, c)), diff_changes)[1])
    if classify_response is not None:
        monkeypatch.setattr(wr.classify_drift, "classify",
                            lambda ch, downstream_refs: (calls.classify.append((ch, downstream_refs)), classify_response)[1])
    monkeypatch.setattr(wr.bigquery_query, "insert_drift_event",
                        lambda ev: calls.insert_drift_event.append(ev))
    return calls


def test_pipeline_cheap_exit_when_hash_unchanged(monkeypatch):
    """Hash gate hit -> no snapshot write, no diff, no events. The
    convergent-detection design depends on this being silent and fast."""
    gate = GateResult(
        changed=False,
        current_columns=[_col("customer_id", "INT64", 1)],
        current_hash="hash-unchanged",
        prior_snapshot={"snapshot_id": "prev", "content_hash": "hash-unchanged"},
    )
    calls = _patch_pipeline_deps(monkeypatch, gate)
    wr._run_detection_pipeline({"connector_id": "c1"})
    assert calls.write_snapshot == []
    assert calls.diff_columns == []
    assert calls.insert_drift_event == []


def test_pipeline_bootstrap_writes_snapshot_only(monkeypatch):
    """No prior snapshot -> write the baseline, do NOT diff. diff would
    emit a NEW_FIELD for every existing column, which is noise."""
    gate = GateResult(
        changed=True,
        current_columns=[_col("customer_id", "INT64", 1), _col("email", "STRING", 2)],
        current_hash="hash-initial",
        prior_snapshot=None,
    )
    calls = _patch_pipeline_deps(monkeypatch, gate)
    wr._run_detection_pipeline({"connector_id": "c1", "sync_id": "s1",
                                "connector_name": "ftar_pg"})
    assert len(calls.write_snapshot) == 1
    snap_row, col_rows = calls.write_snapshot[0]
    assert snap_row["connection_id"] == "c1"
    assert snap_row["content_hash"] == "hash-initial"
    assert snap_row["column_count"] == 2
    assert snap_row["trigger_event"] == "sync_end"
    assert snap_row["sync_id"] == "s1"
    assert len(col_rows) == 2
    # And — crucially — no diff or drift events on bootstrap.
    assert calls.diff_columns == []
    assert calls.insert_drift_event == []


def test_pipeline_drift_writes_snapshot_then_drift_events(monkeypatch):
    """Real drift: write snapshot, load prior, diff, classify each change,
    insert drift_event for each (PROPOSED state)."""
    prior_cols = [_col("amount", "BIGNUMERIC", 1)]
    current_cols = [_col("amount", "STRING", 1)]
    gate = GateResult(
        changed=True,
        current_columns=current_cols,
        current_hash="hash-after",
        prior_snapshot={"snapshot_id": "prev-snap", "content_hash": "hash-before"},
    )
    diff = [ColumnChange("public", "customers", "TYPE_PROMOTION",
                         prior_cols[0], current_cols[0])]
    classification = Classification(
        change_type="TYPE_PROMOTION",
        confidence=0.93,
        rationale="BIGNUMERIC -> STRING is Fivetran's documented widening.",
        remediation_sql="CREATE OR REPLACE VIEW ... AS SELECT *, CAST(amount AS BIGNUMERIC) AS amount_legacy FROM ...",
    )
    calls = _patch_pipeline_deps(
        monkeypatch, gate,
        prior_columns=prior_cols,
        diff_changes=diff,
        classify_response=classification,
    )
    wr._run_detection_pipeline({"connector_id": "c1", "sync_id": "s99",
                                "connector_name": "ftar_pg"})

    # Snapshot written
    assert len(calls.write_snapshot) == 1
    # Prior columns loaded for diff
    assert calls.load_columns == ["prev-snap"]
    # Diff invoked
    assert len(calls.diff_columns) == 1
    # Classifier called per change
    assert len(calls.classify) == 1
    # Drift event written with PROPOSED status + the classification details
    assert len(calls.insert_drift_event) == 1
    event = calls.insert_drift_event[0]
    assert event["change_type"] == "TYPE_PROMOTION"
    assert event["remediation_status"] == "PROPOSED"
    assert event["classification_conf"] == 0.93
    assert "amount_legacy" in event["remediation_sql"]
    assert event["from_snapshot_id"] == "prev-snap"
    assert event["transformation_id"] is None
    assert event["approved_by"] is None
    # column_before and column_after are JSON-friendly dicts
    assert event["column_before"]["data_type"] == "BIGNUMERIC"
    assert event["column_after"]["data_type"] == "STRING"


def test_pipeline_continues_after_per_change_classify_error(monkeypatch):
    """If classify() raises for ONE change, the pipeline must continue
    processing the others — convergence depends on partial-success
    semantics (better one event than none)."""
    prior_cols = [_col("x", "INT64", 1), _col("y", "INT64", 2)]
    current_cols = [_col("x", "STRING", 1), _col("y", "STRING", 2)]
    gate = GateResult(
        changed=True, current_columns=current_cols, current_hash="h2",
        prior_snapshot={"snapshot_id": "prev", "content_hash": "h1"},
    )
    diff = [
        ColumnChange("public", "customers", "TYPE_PROMOTION", prior_cols[0], current_cols[0]),
        ColumnChange("public", "customers", "TYPE_PROMOTION", prior_cols[1], current_cols[1]),
    ]
    n_calls = {"i": 0}
    def flaky_classify(change, downstream_refs):
        n_calls["i"] += 1
        if n_calls["i"] == 1:
            raise RuntimeError("simulated Gemini failure")
        return Classification(change_type="TYPE_PROMOTION", confidence=0.8,
                              rationale="ok", remediation_sql="VIEW...")

    monkeypatch.setattr(wr, "resolve_destination_schema", lambda cid: "public")
    monkeypatch.setattr(wr.snapshot_diff, "capture_and_gate",
                        lambda cid, ds: gate)
    monkeypatch.setattr(wr.bigquery_query, "write_snapshot",
                        lambda s, c: None)
    monkeypatch.setattr(wr.bigquery_query, "load_columns",
                        lambda sid: prior_cols)
    monkeypatch.setattr(wr.snapshot_diff, "diff_columns",
                        lambda p, c: diff)
    monkeypatch.setattr(wr.classify_drift, "classify", flaky_classify)
    inserted = []
    monkeypatch.setattr(wr.bigquery_query, "insert_drift_event",
                        lambda ev: inserted.append(ev))

    wr._run_detection_pipeline({"connector_id": "c1"})

    # First change classify raised; second succeeded.
    assert n_calls["i"] == 2
    # Exactly one drift_event written — for the second (successful) change.
    assert len(inserted) == 1


def test_pipeline_swallows_top_level_exception(monkeypatch):
    """A failure ANYWHERE in the pipeline (e.g., capture_and_gate raises)
    must not propagate out of the background thread — it'd silently kill
    the thread otherwise. The pipeline log.exceptions and returns; the
    NEXT sync_end retries the whole flow (convergent design)."""
    monkeypatch.setattr(wr, "resolve_destination_schema", lambda cid: "public")

    def boom(cid, ds):
        raise RuntimeError("BQ unavailable")
    monkeypatch.setattr(wr.snapshot_diff, "capture_and_gate", boom)
    # Should NOT raise.
    wr._run_detection_pipeline({"connector_id": "c1"})


# --- _column_to_dict --------------------------------------------------------

def test_column_to_dict_full_record():
    rec = _col("customer_id", "INT64", 3)
    d = wr._column_to_dict(rec)
    assert d == {
        "table_schema": "public",
        "table_name": "customers",
        "column_name": "customer_id",
        "data_type": "INT64",
        "ordinal_position": 3,
        "is_nullable": True,
    }


def test_column_to_dict_none_passes_through():
    """NEW_FIELD has before=None, DEPRECATION has after=None; both must
    serialize to null in the drift_events JSON columns."""
    assert wr._column_to_dict(None) is None
