# ruff: noqa
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

"""Fivetran AI-Readiness Agent — v1 schema-drift downstream resolver.

ADK 1.x `Agent` with domain lifecycle tools + the Fivetran MCP toolset.
Per-webhook detection (capture_and_gate -> diff -> classify ->
insert_drift_event) runs out-of-band via `ingest/webhook_receiver/main.py`
`dispatch()`; this module is the human-facing review / approval / remediation
surface that the user interacts with through `agents-cli playground`.

Architecture rationale — single LlmAgent rather than a SequentialAgent:
  - Per-webhook detection is deterministic Python; chaining it through ADK
    LLM-driven sub-agents would burn Gemini calls on mechanical steps and
    risks non-deterministic skips. The detection pipeline is invoked
    directly (Python composition in dispatch()), not as an LLM workflow.
  - This agent handles the conversational HITL layer: review proposed drift
    events, approve/reject (cheap state-store writes), apply remediation
    via Fivetran MCP (gated by ADK 1.x Action confirmations on every write),
    mark verified.
  - SequentialAgent remains a viable Phase-2 enhancement once
    TRUSTED_ADDITIVE / FULL_AUTO autonomy levels are required.

Action confirmations (Resolved Decision #2 — propose-not-apply default):
  The McpToolset accepts `require_confirmation` as a predicate; we mark all
  Fivetran WRITE tools (create_*, modify_*, delete_*, run_*, sync_*) as
  requiring confirmation. Read tools (list/get/test) flow through. This
  realises the "tool-gated approval step" in the design.
"""

import os
from pathlib import Path

import google.auth
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.genai import types
from mcp import StdioServerParameters

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

from .tools import bigquery_query  # noqa: E402  — must follow auth bootstrap

INSTRUCTION = (Path(__file__).parent / "system_instructions.md").read_text()


# === Fivetran MCP toolset ====================================================
# 14-tool filter per design doc API Surface Map §B2/B3 (the only Fivetran API
# surface the agent actually needs for v1 schema-drift remediation).
#
# The `require_confirmation` predicate is the ADK 1.x Action-confirmations
# gate: WRITE tools prompt the user before executing; READ tools flow
# through immediately. This realises Resolved Decision #2's
# "propose-not-apply" default at the tool-binding layer — even if the LLM
# decides to call a write tool unexpectedly, ADK pauses for human approval.
_FIVETRAN_WRITE_TOOLS = frozenset({
    "create_account_webhook",
    "modify_connection_column_config",
    "delete_connection_column_config",
    "create_transformation",
    "run_transformation",
    "sync_connection",
})


def _require_confirmation(tool, *_args, **_kwargs) -> bool:
    """Return True iff the given MCP tool is a Fivetran write. ADK 1.x calls
    this predicate per tool invocation; True triggers an Action-confirmation
    prompt before the tool actually runs."""
    name = getattr(tool, "name", None) or str(tool)
    # Tool names from the MCP server arrive verbatim; the conservative match
    # below substring-tests the write-tool set so any prefixed/wrapped variant
    # still gates correctly.
    return any(write_name in name for write_name in _FIVETRAN_WRITE_TOOLS)


fivetran_mcp = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command="uvx",
            args=[
                "--from", "git+https://github.com/fivetran/fivetran-mcp",
                "fivetran-mcp",
            ],
            env={
                "FIVETRAN_API_KEY": os.environ.get("FIVETRAN_API_KEY", ""),
                "FIVETRAN_API_SECRET": os.environ.get("FIVETRAN_API_SECRET", ""),
                # Writes are enabled at the MCP env-var layer; the actual
                # gating happens via `require_confirmation` above so users
                # see an explicit confirmation prompt before any write.
                "FIVETRAN_ALLOW_WRITES": "true",
            },
        ),
        timeout=10.0,
    ),
    tool_filter=[
        "list_connections", "get_connection_details",
        "create_account_webhook", "list_webhooks", "test_webhook",
        "get_connection_schema_config", "get_connection_column_config",
        "modify_connection_column_config", "delete_connection_column_config",
        "create_transformation", "run_transformation",
        "get_transformation_details", "get_connection_state",
        "sync_connection",
    ],
    require_confirmation=_require_confirmation,
)


# === Drift-events lifecycle tools ===========================================
# Thin ADK-compatible wrappers over bigquery_query.update_drift_event for
# the PROPOSED -> APPROVED/REJECTED -> APPLIED -> VERIFIED lifecycle. Docstrings
# are load-bearing — ADK generates the LLM-visible tool schema from them.

def approve_drift(drift_id: str, approved_by: str) -> str:
    """Mark a PROPOSED drift event as APPROVED, allowing subsequent
    remediation. Use when a human reviewer has accepted the proposed VIEW
    shim or column-config change.

    Args:
        drift_id: drift_events.drift_id (UUID) from the proposed event.
        approved_by: identifier of the human reviewer (for the audit trail).
    """
    bigquery_query.update_drift_event(
        drift_id,
        remediation_status="APPROVED",
        approved_by=approved_by,
    )
    return f"drift {drift_id} APPROVED by {approved_by}"


def reject_drift(drift_id: str, approved_by: str) -> str:
    """Mark a PROPOSED drift event as REJECTED. The proposed remediation
    will NOT be applied; use when the human reviewer determines no action
    is needed or the classification was wrong.

    Args:
        drift_id: drift_events.drift_id (UUID) from the proposed event.
        approved_by: identifier of the human reviewer (for the audit trail).
    """
    bigquery_query.update_drift_event(
        drift_id,
        remediation_status="REJECTED",
        approved_by=approved_by,
    )
    return f"drift {drift_id} REJECTED by {approved_by}"


def mark_drift_applied(drift_id: str, transformation_id: str) -> str:
    """Mark an APPROVED drift event as APPLIED. Call this AFTER a Fivetran
    transformation has been successfully created via the MCP
    `create_transformation` tool — pass the transformation_id returned by
    that tool here for the audit trail.

    Args:
        drift_id: drift_events.drift_id (UUID).
        transformation_id: Fivetran transformation_id returned by the MCP
            create_transformation call.
    """
    bigquery_query.update_drift_event(
        drift_id,
        remediation_status="APPLIED",
        transformation_id=transformation_id,
    )
    return f"drift {drift_id} APPLIED with transformation {transformation_id}"


def mark_drift_verified(drift_id: str) -> str:
    """Mark an APPLIED drift event as VERIFIED — the remediation has been
    confirmed to land correctly in BigQuery (post-sync `INFORMATION_SCHEMA`
    check passes).

    Args:
        drift_id: drift_events.drift_id (UUID).
    """
    bigquery_query.update_drift_event(drift_id, remediation_status="VERIFIED")
    return f"drift {drift_id} VERIFIED"


# === Root agent + App wrapper ===============================================
# Single LlmAgent. Model pinned to gemini-flash-latest per CLAUDE.md
# "NEVER change the model" — matches classify_drift.CLASSIFIER_MODEL.

root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=INSTRUCTION,
    tools=[
        approve_drift,
        reject_drift,
        mark_drift_applied,
        mark_drift_verified,
        fivetran_mcp,
    ],
)

app = App(root_agent=root_agent, name="app")
