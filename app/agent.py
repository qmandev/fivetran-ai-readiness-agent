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

import functools
import inspect
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
from .tools.bigquery_query import (  # noqa: E402
    check_freshness_sla,
    list_freshness_status,
    list_proposed_drift_events,
)
from .tools.readiness_score import (  # noqa: E402
    analyze_drift_volatility,
    list_readiness_scores,
    score_ai_readiness,
)
from .tools.schema_docs import generate_schema_docs  # noqa: E402
from .tools.sensitivity_classifier import (  # noqa: E402
    classify_column_sensitivity,
    list_sensitive_columns,
)
from .tools.use_case_auditor import audit_use_case_coverage  # noqa: E402
from .tools.json_flattener import detect_json_columns, generate_json_flattener  # noqa: E402
from .tools.entity_detector import detect_entity_overlaps  # noqa: E402
from .tools.failure_diagnosis import diagnose_sync_failures  # noqa: E402

INSTRUCTION = (Path(__file__).parent / "system_instructions.md").read_text()


def _llm_tool(fn):
    """Expose a tool to ADK with test-injection keyword-only params hidden.

    Several v3 tools take a keyword-only ``model_fn: Callable[[str], str]`` for
    dependency injection in unit tests. ADK's automatic function-calling schema
    builder iterates ``inspect.signature(fn).parameters`` and processes
    KEYWORD_ONLY params (see google/adk/tools/_automatic_function_calling_util.py),
    but cannot represent a Callable as JSON schema, so it raises
    "Failed to parse the parameter model_fn". Because that builder honours
    ``__signature__``, we register the tool with a filtered signature that drops
    keyword-only params. The wrapper still delegates to ``fn`` (which falls back
    to its default ``model_fn=_call_gemini``); unit tests call ``fn`` directly
    with ``model_fn=`` and are unaffected.
    """
    sig = inspect.signature(fn)
    visible = [
        p for p in sig.parameters.values()
        if p.kind is not inspect.Parameter.KEYWORD_ONLY
    ]

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return fn(*args, **kwargs)

    wrapper.__signature__ = sig.replace(parameters=visible)
    return wrapper


# === Fivetran MCP toolsets ===================================================
# Split into two McpToolsets (same server, different tool_filter slices) so
# we can use require_confirmation=True/False (bool) per ADK 1.x's actual
# calling convention. ADK calls the require_confirmation callable with
# **tool_input_args, not the tool object — so a predicate can't inspect the
# tool name. Splitting by write vs read avoids that limitation entirely.
#
# READ toolset — flows through immediately (no confirmation prompt).
# WRITE toolset — every call pauses for explicit human approval, realising
# Resolved Decision #2's "propose-not-apply" default at the tool-binding
# layer.

def _secret_or_env(env_var: str, secret_name: str) -> str:
    val = os.environ.get(env_var, "")
    if val:
        return val
    try:
        from google.cloud import secretmanager  # noqa: PLC0415
        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
        return client.access_secret_version(request={"name": name}).payload.data.decode()
    except Exception:
        return ""
          
def _mcp_env() -> dict:
    return {
        "FIVETRAN_API_KEY": _secret_or_env("FIVETRAN_API_KEY", "fivetran-api-key"),
        "FIVETRAN_API_SECRET": _secret_or_env("FIVETRAN_API_SECRET", "fivetran-api-secret"),
        "FIVETRAN_ALLOW_WRITES": "true",
    }   

def _mcp_server() -> StdioServerParameters:
    import sys, pathlib
    scripts = pathlib.Path(sys.executable).parent
    fivetran_bin = scripts / "fivetran-mcp"
    return StdioServerParameters(
        command=str(fivetran_bin),
        args=[],
        env=_mcp_env(),
    ) 

fivetran_mcp_reads = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=_mcp_server(), timeout=10.0
    ),
    tool_filter=[
        "list_connections", "get_connection_details",
        "list_webhooks", "test_webhook",
        "get_connection_schema_config", "get_connection_column_config",
        "get_transformation_details", "get_connection_state",
    ],
    require_confirmation=False,
)

fivetran_mcp_writes = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=_mcp_server(), timeout=10.0
    ),
    tool_filter=[
        "create_account_webhook",
        "modify_connection_column_config",
        "delete_connection_column_config",
        "create_transformation",
        "run_transformation",
        "sync_connection",
    ],
    require_confirmation=True,
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
        list_proposed_drift_events,
        check_freshness_sla,
        list_freshness_status,
        approve_drift,
        reject_drift,
        mark_drift_applied,
        mark_drift_verified,
        # v3 Phase 1 — AI-readiness scoring + drift volatility
        # Gemini tools wrapped via _llm_tool() to hide their keyword-only
        # model_fn (DI for tests) from ADK automatic-function-calling schema gen.
        _llm_tool(score_ai_readiness),
        _llm_tool(list_readiness_scores),
        _llm_tool(analyze_drift_volatility),
        # v3 Phase 2 — schema docs, sensitivity classification, use-case auditing
        _llm_tool(generate_schema_docs),
        _llm_tool(classify_column_sensitivity),
        _llm_tool(list_sensitive_columns),
        _llm_tool(audit_use_case_coverage),
        # v3 Phase 3 — JSON flattener + entity/silo detector
        detect_json_columns,  # no model_fn — registered directly
        _llm_tool(generate_json_flattener),
        _llm_tool(detect_entity_overlaps),
        # v3 Phase 4 — pipeline failure diagnosis
        _llm_tool(diagnose_sync_failures),
        fivetran_mcp_reads,
        fivetran_mcp_writes,
    ],
)

app = App(root_agent=root_agent, name="app")
