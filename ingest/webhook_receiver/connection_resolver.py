"""Resolve connection_id → BigQuery destination dataset name via Fivetran REST API.

v1 hardcoded BQ_DESTINATION_DATASET (single-connection assumption). This
module replaces that with a live lookup + in-process cache, so each distinct
Fivetran connection lands in its own BQ dataset without requiring redeployment.

API: GET https://api.fivetran.com/v1/connectors/{connection_id}
Auth: HTTP Basic (FIVETRAN_API_KEY : FIVETRAN_API_SECRET)
Field: data.config.schema — the source schema name Fivetran maps verbatim
       to the BigQuery dataset name for the google_cloud_postgresql connector
       (F finding 2026-05-20). schema_prefix in data is tried as a secondary
       field for connector types that don't expose config.schema.

Cache: module-level dict; survives the Cloud Run instance lifetime. The set
       of connection_ids per instance is tiny so eviction is unnecessary.
       Only successful API responses are cached; fallbacks are not, so the
       next sync_end for an uncacheable connection retries the lookup.

Fallback: BQ_DESTINATION_DATASET env var (default: 'public'), matching v1
          single-connection behavior for any connection the API can't resolve.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

_FIVETRAN_API_BASE = "https://api.fivetran.com/v1"

# In-process cache: connection_id -> resolved dataset name.
_cache: dict[str, str] = {}


def resolve_destination_schema(connection_id: str) -> str:
    """Return the BigQuery dataset name for connection_id.

    Hits the Fivetran REST API on first call per connection_id; subsequent
    calls for the same ID are served from the in-process cache. Falls back
    to BQ_DESTINATION_DATASET (default: 'public') on any error so the
    detection pipeline continues to work even when credentials are missing
    or the API is temporarily unavailable.
    """
    if connection_id in _cache:
        return _cache[connection_id]

    schema = _fetch_schema(connection_id)
    if schema:
        _cache[connection_id] = schema
        return schema

    fallback = os.environ.get("BQ_DESTINATION_DATASET", "public")
    log.warning(
        "resolve_destination_schema: API lookup failed for connection=%s; "
        "falling back to BQ_DESTINATION_DATASET=%r",
        connection_id, fallback,
    )
    return fallback


def _fetch_schema(connection_id: str) -> str | None:
    """Call GET /v1/connectors/{connection_id} and return the schema/dataset name.

    Returns None on missing credentials, any HTTP/network error, or when the
    response doesn't contain a recognisable schema field. Callers treat None
    as "use the env-var fallback."
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
        log.warning("Fivetran API request failed for connection=%s: %s", connection_id, exc)
        return None

    data = body.get("data", {})
    # config.schema is the primary field for PostgreSQL-family connectors.
    # schema_prefix is used by some other connector types.
    schema = data.get("config", {}).get("schema") or data.get("schema_prefix")
    if not schema:
        log.warning(
            "No schema field in Fivetran API response for connection=%s; "
            "data keys=%s config keys=%s",
            connection_id,
            list(data.keys()),
            list(data.get("config", {}).keys()),
        )
    return schema or None
