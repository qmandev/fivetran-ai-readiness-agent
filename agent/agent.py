"""ADK agent definition — Fivetran AI-Readiness Agent (v1: schema-drift).

Skeleton. Composition + wiring contract only; step bodies are stubs in
agent/tools/*. Build on ADK 1.x (>=1.15.0,<2.0.0) — matches the agents-cli
sanctioned template; the 2.0 Beta and its graph/Human-input nodes are out
of scope. Stable core: Agent + App + SequentialAgent + FunctionTool +
MCPToolset.

Orchestration is an explicit Sequential workflow (debuggable trajectory,
serves the verifiable goal), with a tool-gated approval step (1.x Action
confirmations) before any write:

    capture_and_gate -> diff_columns -> classify (LLM) -> propose
        -> [tool-gated approval] -> apply via MCP -> verify

NOTE: this is the staging-tree shape. The deployable container is
regenerated via `agents-cli create` (app/ dir, App wrapper, pyproject.toml);
port this logic in. See design doc "Onboarding-Guide Alignment".
"""

from __future__ import annotations

from pathlib import Path

# Imports follow the agents-cli ADK 1.x template (Agent + App + Gemini).
# Verify exact paths against the pinned 1.x at build time.
# from google.adk.agents import Agent, SequentialAgent
# from google.adk.apps import App
# from google.adk.models import Gemini
# from google.adk.tools import FunctionTool
# from google.adk.tools.mcp_tool import MCPToolset, StdioServerParameters

from .tools import bigquery_query, snapshot_diff, classify_drift

INSTRUCTION = (Path(__file__).parent / "system_instructions.md").read_text()

# --- Fivetran MCP (replaces the old fivetran_mcp.yaml) -----------------------
# Stdio launch of the official server via uvx. Writes stay disabled until the
# approval flow is wired (FIVETRAN_ALLOW_WRITES gates create_webhook /
# create_transformation / modify_connection_column_config).
#
# fivetran_mcp = MCPToolset(
#     connection=StdioServerParameters(
#         command="uvx",
#         args=["--from", "git+https://github.com/fivetran/fivetran-mcp",
#               "fivetran-mcp"],
#         env={
#             "FIVETRAN_API_KEY": "${FIVETRAN_API_KEY}",
#             "FIVETRAN_API_SECRET": "${FIVETRAN_API_SECRET}",
#             "FIVETRAN_ALLOW_WRITES": "false",
#         },
#     ),
#     # Narrow to the ~12 tools we actually use (see fivetran_mcp notes in
#     # the design doc API Surface Map B2/B3).
#     tool_filter=[
#         "list_connections", "get_connection_details",
#         "create_account_webhook", "list_webhooks", "test_webhook",
#         "get_connection_schema_config", "get_connection_column_config",
#         "modify_connection_column_config", "delete_connection_column_config",
#         "create_transformation", "run_transformation",
#         "get_transformation_details", "get_connection_state",
#         "sync_connection",
#     ],
# )

# --- Deterministic steps as FunctionTools ------------------------------------
# Discrete typed functions (NOT a handler() dispatch shim) so ADK can generate
# tool schemas from signatures.
#
# detection_tools = [
#     FunctionTool(snapshot_diff.capture_and_gate),
#     FunctionTool(snapshot_diff.diff_columns),
#     FunctionTool(bigquery_query.fetch_landed_columns),
#     FunctionTool(bigquery_query.write_snapshot),
#     FunctionTool(bigquery_query.latest_snapshot),
#     FunctionTool(bigquery_query.load_columns),
#     FunctionTool(bigquery_query.write_drift_event),
# ]

# --- Classification sub-agent (the LLM-reasoned step) ------------------------
# Use the agents-cli template's Gemini() model object (with retry_options),
# not a bare model string.
# classifier = Agent(
#     name="drift_classifier",
#     # flash-latest is the template default; gemini-3.1-pro-preview is
#     # stronger for semantic RENAME-vs-DEPRECATION + SQL gen. Pin at build.
#     model=Gemini(model="gemini-3.1-pro-preview"),
#     instruction=INSTRUCTION,
#     tools=[FunctionTool(classify_drift.classify)],
# )

# --- Root workflow -----------------------------------------------------------
# Sequential (ADK 1.x): gate -> diff -> classify -> propose -> approval ->
#             apply (MCP) -> verify. The approval gate is a tool-gated step
#             using 1.x Action confirmations (NOT a 2.0 graph Human-input
#             node — out of scope). Autonomy level (STRICT / TRUSTED_ADDITIVE
#             / FULL_AUTO) decides whether the confirmation is required.
#
# root_agent = SequentialAgent(
#     name="fivetran_ai_readiness_agent",
#     sub_agents=[
#         # TODO: gate step, diff step, classifier, propose step,
#         #       tool-gated approval step, apply-via-MCP step, verify step
#     ],
# )
#
# Per the agents-cli template, the deployable also needs an App wrapper:
#   app = App(root_agent=root_agent, name="app")  # name must match dir

# ADK entrypoint discovery expects a module-level `root_agent`.
root_agent = None  # TODO: assemble per the commented composition above
