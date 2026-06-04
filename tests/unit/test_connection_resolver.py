"""Unit tests for app/tools/connection_resolver.py (canonical resolver).

Covers:
  - _fetch_schema: missing/partial creds, network error, missing schema field,
    success via config.schema, success via schema_prefix, correct auth header.
  - resolve_destination_schema: fallback to BQ_DESTINATION_DATASET, default
    'public', cache hit on repeated call, fallback not cached.
"""

import base64
import json

import pytest

import app.tools.connection_resolver as resolver_mod
from app.tools.connection_resolver import (
    _fetch_schema,
    resolve_destination_schema,
)


class _FakeResp:
    """Minimal context-manager stub matching urllib.request.urlopen's interface."""
    def __init__(self, body: dict):
        self._data = json.dumps(body).encode()
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return self._data


@pytest.fixture(autouse=True)
def clear_cache():
    """Reset the module-level cache before and after every test."""
    resolver_mod._cache.clear()
    yield
    resolver_mod._cache.clear()


# --- _fetch_schema -----------------------------------------------------------

def test_fetch_schema_no_credentials_returns_none(monkeypatch):
    monkeypatch.delenv("FIVETRAN_API_KEY", raising=False)
    monkeypatch.delenv("FIVETRAN_API_SECRET", raising=False)
    assert _fetch_schema("conn1") is None


def test_fetch_schema_partial_credentials_key_only_returns_none(monkeypatch):
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.delenv("FIVETRAN_API_SECRET", raising=False)
    assert _fetch_schema("conn1") is None


def test_fetch_schema_partial_credentials_secret_only_returns_none(monkeypatch):
    monkeypatch.delenv("FIVETRAN_API_KEY", raising=False)
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")
    assert _fetch_schema("conn1") is None


def test_fetch_schema_network_error_returns_none(monkeypatch):
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")

    def boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(resolver_mod, "urlopen", boom)
    assert _fetch_schema("conn1") is None


def test_fetch_schema_missing_schema_field_returns_none(monkeypatch):
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")
    body = {"data": {"id": "conn1", "config": {"host": "db.example.com"}}}
    monkeypatch.setattr(resolver_mod, "urlopen", lambda *a, **kw: _FakeResp(body))
    assert _fetch_schema("conn1") is None


def test_fetch_schema_success_via_config_schema(monkeypatch):
    """Primary field: data.config.schema (PostgreSQL connector)."""
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")
    body = {"data": {"id": "conn1", "config": {"schema": "orders_v2", "host": "db"}}}
    monkeypatch.setattr(resolver_mod, "urlopen", lambda *a, **kw: _FakeResp(body))
    assert _fetch_schema("conn1") == "orders_v2"


def test_fetch_schema_success_via_schema_prefix(monkeypatch):
    """Secondary field: data.schema_prefix (used when config.schema absent)."""
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")
    body = {"data": {"id": "conn1", "schema_prefix": "analytics", "config": {}}}
    monkeypatch.setattr(resolver_mod, "urlopen", lambda *a, **kw: _FakeResp(body))
    assert _fetch_schema("conn1") == "analytics"


def test_fetch_schema_config_schema_takes_priority_over_schema_prefix(monkeypatch):
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")
    body = {
        "data": {
            "id": "conn1",
            "schema_prefix": "prefix_val",
            "config": {"schema": "config_val"},
        }
    }
    monkeypatch.setattr(resolver_mod, "urlopen", lambda *a, **kw: _FakeResp(body))
    assert _fetch_schema("conn1") == "config_val"


def test_fetch_schema_sends_correct_basic_auth_header(monkeypatch):
    monkeypatch.setenv("FIVETRAN_API_KEY", "mykey")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "mysecret")
    body = {"data": {"config": {"schema": "ds1"}}}
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return _FakeResp(body)

    monkeypatch.setattr(resolver_mod, "urlopen", fake_urlopen)
    _fetch_schema("conn_abc")

    assert len(captured) == 1
    expected_token = base64.b64encode(b"mykey:mysecret").decode()
    assert captured[0].get_header("Authorization") == f"Basic {expected_token}"


def test_fetch_schema_hits_correct_url(monkeypatch):
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")
    body = {"data": {"config": {"schema": "ds1"}}}
    captured = []

    def fake_urlopen(req, timeout=None):
        captured.append(req)
        return _FakeResp(body)

    monkeypatch.setattr(resolver_mod, "urlopen", fake_urlopen)
    _fetch_schema("my_connection_id")

    assert captured[0].full_url == "https://api.fivetran.com/v1/connectors/my_connection_id"


# --- resolve_destination_schema ---------------------------------------------

def test_resolve_falls_back_to_env_var_when_credentials_missing(monkeypatch):
    monkeypatch.delenv("FIVETRAN_API_KEY", raising=False)
    monkeypatch.delenv("FIVETRAN_API_SECRET", raising=False)
    monkeypatch.setenv("BQ_DESTINATION_DATASET", "my_bq_dataset")
    assert resolve_destination_schema("conn1") == "my_bq_dataset"


def test_resolve_falls_back_to_public_default_when_env_unset(monkeypatch):
    monkeypatch.delenv("FIVETRAN_API_KEY", raising=False)
    monkeypatch.delenv("FIVETRAN_API_SECRET", raising=False)
    monkeypatch.delenv("BQ_DESTINATION_DATASET", raising=False)
    assert resolve_destination_schema("conn1") == "public"


def test_resolve_caches_successful_api_response(monkeypatch):
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")
    body = {"data": {"config": {"schema": "orders"}}}
    call_count = {"n": 0}

    def fake_urlopen(req, timeout=None):
        call_count["n"] += 1
        return _FakeResp(body)

    monkeypatch.setattr(resolver_mod, "urlopen", fake_urlopen)
    assert resolve_destination_schema("conn1") == "orders"
    assert resolve_destination_schema("conn1") == "orders"  # cache hit
    assert call_count["n"] == 1


def test_resolve_fallback_not_cached(monkeypatch):
    """Failed API result must NOT be cached; the next call should retry the API.
    This ensures that transient failures (bad creds, network blip) are
    retried on the next sync_end rather than being silently stuck in fallback."""
    monkeypatch.delenv("FIVETRAN_API_KEY", raising=False)
    monkeypatch.delenv("FIVETRAN_API_SECRET", raising=False)
    monkeypatch.setenv("BQ_DESTINATION_DATASET", "fallback")

    assert resolve_destination_schema("conn1") == "fallback"
    assert "conn1" not in resolver_mod._cache


def test_resolve_different_connections_independent(monkeypatch):
    """Each connection_id resolves and caches independently."""
    monkeypatch.setenv("FIVETRAN_API_KEY", "k")
    monkeypatch.setenv("FIVETRAN_API_SECRET", "s")

    schemas = {"conn_a": "dataset_a", "conn_b": "dataset_b"}

    def fake_urlopen(req, timeout=None):
        conn_id = req.full_url.rsplit("/", 1)[-1]
        return _FakeResp({"data": {"config": {"schema": schemas[conn_id]}}})

    monkeypatch.setattr(resolver_mod, "urlopen", fake_urlopen)
    assert resolve_destination_schema("conn_a") == "dataset_a"
    assert resolve_destination_schema("conn_b") == "dataset_b"
    assert resolver_mod._cache == {"conn_a": "dataset_a", "conn_b": "dataset_b"}
