"""Cross-connection entity / data silo detector — v3 Phase 3, Feature 7.

detect_entity_overlaps() reads the schemas for every connection that has ever
synced (from sync_log), sends all table+column summaries to Gemini in one call,
and asks it to identify tables that likely represent the same real-world entity
(e.g. "customers" in Postgres + "accounts" in Salesforce). Detected overlaps are
written to `entity_map` (streaming insert) and returned to the caller.

This surfaces data silos — the "different versions of the truth" problem from the
Fivetran AI-readiness blog post — so operators know which connections to JOIN when
building downstream AI features.

Gemini call pattern: reuses CLASSIFIER_MODEL + _call_gemini from readiness_score.py.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Callable

from .bigquery_query import (
    BQ_LOCATION,
    _client,
    _fetch_schema_for_connection,
    _project,
    _state_dataset,
    _state_table_fqn,
)
from .readiness_score import _call_gemini, _extract_json

_ENTITY_CATALOG_PROMPT = """\
You are a data architect analyzing a single Fivetran connection's schema.

Given the tables below, identify the key business entities represented in this dataset.
For each entity:
- Name it (e.g. "Customer", "Order", "Product")
- Identify the primary table that contains it
- Identify the best join key column (the stable unique ID)
- Note any intra-schema data quality observations: nullable columns that should be
  NOT NULL, column names that suggest a relationship with another table, etc.

SCHEMAS (connection_id → table_key → {{column_name: type}}):
{schemas_json}

Return ONLY a JSON array (no markdown fences):
[
  {{
    "entity_name": "...",
    "confidence": 0.0,
    "occurrences": [{{"connection_id": "...", "table": "...", "join_key": "..."}}],
    "split_truth_conflicts": ["..."]
  }}
]

split_truth_conflicts should list intra-connection quality observations (e.g.
"email should be NOT NULL in customers but is nullable"). Include entities with
confidence >= 0.5. If no recognisable entities, return [].
"""

_ENTITY_PROMPT = """\
You are a data architect analyzing schemas from multiple Fivetran connections.

Given the table schemas below, identify groups of tables that likely represent \
the same real-world entity (e.g. "Customer" appearing as `users` in Postgres, \
`accounts` in Salesforce, `customers` in Stripe). Different column names for \
the same concept is expected — focus on semantic overlap.

SCHEMAS (connection_id → table_key → [column_name: type]):
{schemas_json}

For each detected entity group:
- Name the entity (e.g. "Customer", "Order", "Product")
- List all occurrences with their connection_id, table key, and the best candidate \
  join key column (the column most likely to hold a stable unique ID across sources)
- Flag any "split-truth conflicts": cases where the same logical field has \
  incompatible definitions (e.g. email is NOT NULL in one source but NULLABLE in another)
- Assign a confidence score (0.0–1.0) for how certain you are this is a real overlap

Return ONLY a JSON array (no markdown fences):
[
  {{
    "entity_name": "...",
    "confidence": 0.0,
    "occurrences": [
      {{"connection_id": "...", "table": "...", "join_key": "..."}}
    ],
    "split_truth_conflicts": ["..."]
  }}
]

Only include entities with confidence >= 0.5. If no overlaps are found, return [].
"""


def detect_entity_overlaps(
    *,
    model_fn: Callable[[str], str] = _call_gemini,
) -> list[dict]:
    """Detect entity overlaps across connections, or catalog entities for a single connection.

    With 2+ connections: identifies tables that represent the same real-world entity
    across sources (data silos), with join key suggestions and split-truth conflicts.

    With 1 connection: catalogs the key business entities in that connection, their
    join keys, and any intra-schema data quality observations. Returns
    analysis_mode="single_connection" in each result dict.

    With 0 connections: returns [].

    In all cases writes results to `entity_map` and returns a list of dicts:
    entity_name, confidence (float), occurrences (list of {connection_id, table,
    join_key}), split_truth_conflicts (list[str]), analysis_mode (str).
    """
    # List all connections that have ever synced
    sql = (
        f"SELECT DISTINCT connection_id FROM {_state_table_fqn('sync_log')} "
        "ORDER BY connection_id"
    )
    rows = _client().query(sql, location=BQ_LOCATION).result()
    connection_ids = [r["connection_id"] for r in rows]

    if not connection_ids:
        return []

    # Build compact schema summary: connection_id → table_key → {col: type}
    schemas_summary: dict[str, dict[str, dict[str, str]]] = {}
    for cid in connection_ids:
        conn_schema = _fetch_schema_for_connection(cid)
        schemas_summary[cid] = {
            table_key: {col.column_name: col.data_type for col in cols}
            for table_key, cols in conn_schema.items()
        }

    if len(connection_ids) == 1:
        # Single-connection mode: catalog entities within the one connection.
        prompt = _ENTITY_CATALOG_PROMPT.format(schemas_json=json.dumps(schemas_summary, indent=2))
        analysis_mode = "single_connection"
    else:
        # Multi-connection mode: detect cross-connection entity overlaps / data silos.
        prompt = _ENTITY_PROMPT.format(schemas_json=json.dumps(schemas_summary, indent=2))
        analysis_mode = "cross_connection"
    raw = model_fn(prompt)
    try:
        entities = _extract_json(raw)
        if not isinstance(entities, list):
            entities = []
    except (json.JSONDecodeError, AttributeError):
        entities = []

    if not entities:
        return []

    # Write to entity_map (streaming insert — append-only)
    detection_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    log_rows = []
    for entity in entities:
        for occ in entity.get("occurrences", []):
            log_rows.append({
                "detection_id": detection_id,
                "entity_name": entity.get("entity_name", ""),
                "connection_id": occ.get("connection_id", ""),
                "table_name": occ.get("table", ""),
                "join_key_col": occ.get("join_key"),
                "confidence": float(entity.get("confidence", 0.0)),
                "conflicts": json.dumps(entity.get("split_truth_conflicts", [])),
                "detected_at": now,
            })

    if log_rows:
        try:
            ref = f"{_project()}.{_state_dataset()}.entity_map"
            errors = _client().insert_rows_json(ref, log_rows)
            if errors:
                import logging
                logging.getLogger(__name__).warning(
                    "entity_map insert failed: %s", errors
                )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "entity_map insert error (non-fatal): %s", exc
            )

    return [
        {
            "entity_name": e.get("entity_name", ""),
            "confidence": float(e.get("confidence", 0.0)),
            "occurrences": e.get("occurrences", []),
            "split_truth_conflicts": e.get("split_truth_conflicts", []),
            "analysis_mode": analysis_mode,
        }
        for e in entities
    ]
