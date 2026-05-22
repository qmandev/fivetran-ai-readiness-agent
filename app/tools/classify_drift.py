"""Gemini classification + remediation SQL generation (algorithm steps 6-7).

Skeleton — prompt contract and signatures. Implementations TODO.

NAME-MAPPING CAVEAT (G3 finding, 2026-05-21):
  Diff input comes from BigQuery INFORMATION_SCHEMA — i.e., DESTINATION-side
  column names. Fivetran's column-config APIs (`modify_connection_column_config`,
  `delete_connection_column_config`) take SOURCE-side names. For ordinary user
  columns these match (`customer_id` = `customer_id`). For Fivetran-synthetic
  columns they diverge — observed: source `ctid` lands in BQ as
  `ctid_fivetran_id`. When this module produces a remediation that calls
  Fivetran APIs, it MUST pass source-side names, not the BQ names from the
  diff. `exclude_system_columns` in snapshot_diff.py currently filters all
  observed Fivetran synthetics, so the agent never targets them for
  remediation — but treat that as a load-bearing assumption: if the
  exclusion rules ever loosen, name-mapping logic must be added here.
"""

from __future__ import annotations

from dataclasses import dataclass

from .snapshot_diff import ColumnChange

CHANGE_TYPES = ("RENAME", "TYPE_PROMOTION", "REORDER", "NEW_FIELD", "DEPRECATION")


@dataclass(frozen=True)
class Classification:
    change_type: str
    confidence: float          # 0..1
    rationale: str
    remediation_sql: str       # VIEW shim, deployed via Fivetran transformations API


def classify(change: ColumnChange, downstream_refs: list[str]) -> Classification:
    """Ask Gemini to (a) confirm the change type — especially RENAME vs
    DEPRECATION+NEW_FIELD — using column-name semantics, type, and position,
    and (b) generate a VIEW-based coercion shim.

    Gemini prompt MUST include: before/after column, ordinal delta, data_type
    delta, and the list of downstream consumers so the shim preserves the
    contract those consumers expect.
    """
    # TODO: build structured prompt; call Gemini; parse to Classification
    # TODO: constrain output to CHANGE_TYPES; reject low-confidence silently? -> no,
    #       surface low confidence to the user (human-in-the-loop)
    raise NotImplementedError


# No handler() shim — `classify` is registered directly as an ADK
# FunctionTool on the classifier LlmAgent in agent.py.
