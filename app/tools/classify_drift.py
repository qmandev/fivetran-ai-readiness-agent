"""Gemini classification + remediation SQL generation (algorithm steps 6-7).

Skeleton — prompt contract and signatures. Implementations TODO.
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
