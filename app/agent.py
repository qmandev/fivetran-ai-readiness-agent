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

"""Fivetran AI-Readiness Agent (v1: schema-drift downstream resolver).

Skeleton — composition + wiring contract; step bodies are NotImplementedError
stubs in app/tools/*. Orchestration is an explicit Sequential workflow with a
tool-gated approval step (ADK 1.x Action confirmations) before any write:

    capture_and_gate -> diff_columns -> classify (LLM) -> propose
        -> [tool-gated approval] -> apply via MCP -> verify

See ../fivetranAgentDesign.md (parent workspace) for full rationale.
"""

from pathlib import Path

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

import os
import google.auth

_, project_id = google.auth.default()
os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
os.environ["GOOGLE_CLOUD_LOCATION"] = "global"
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

from .tools import bigquery_query, snapshot_diff, classify_drift

INSTRUCTION = (Path(__file__).parent / "system_instructions.md").read_text()

# --- Fivetran MCP (ADK MCPToolset; replaces the old fivetran_mcp.yaml) -------
# Stdio launch of the official server via uvx. Writes stay disabled until the
# approval flow is wired (FIVETRAN_ALLOW_WRITES gates create_webhook /
# create_transformation / modify_connection_column_config). Narrow to the ~14
# tools we use (design doc API Surface Map B2/B3). Verify exact MCPToolset
# import path against the pinned ADK 1.x at build time.
#
# from google.adk.tools.mcp_tool import MCPToolset, StdioServerParameters
# fivetran_mcp = MCPToolset(
#     connection=StdioServerParameters(
#         command="uvx",
#         args=["--from", "git+https://github.com/fivetran/fivetran-mcp",
#               "fivetran-mcp"],
#         env={
#             "FIVETRAN_API_KEY": os.environ.get("FIVETRAN_API_KEY", ""),
#             "FIVETRAN_API_SECRET": os.environ.get("FIVETRAN_API_SECRET", ""),
#             "FIVETRAN_ALLOW_WRITES": "false",
#         },
#     ),
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

# --- Classification sub-agent (the LLM-reasoned step) ------------------------
# Model kept at the template default (gemini-flash-latest) per CLAUDE.md
# "NEVER change the model". Design doc notes gemini-3.1-pro-preview as a
# considered upgrade for semantic RENAME-vs-DEPRECATION + SQL gen — a tuning
# decision deferred to the eval loop, not changed here.
classifier = Agent(
    name="drift_classifier",
    model=Gemini(
        model="gemini-flash-latest",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=INSTRUCTION,
    # TODO: register classify_drift.classify (+ a propose step) as tools
    tools=[],
)

# --- Root workflow -----------------------------------------------------------
# Sequential (ADK 1.x): gate -> diff -> classify -> propose -> approval ->
# apply (MCP) -> verify. The approval gate is a tool-gated step using 1.x
# Action confirmations (NOT a 2.0 graph Human-input node — out of scope).
# Autonomy level (STRICT / TRUSTED_ADDITIVE / FULL_AUTO) decides whether the
# confirmation is required.
#
# from google.adk.agents import SequentialAgent
# root_agent = SequentialAgent(
#     name="root_agent",
#     sub_agents=[
#         # TODO: gate, diff, classifier, propose, tool-gated approval,
#         #       apply-via-MCP, verify
#     ],
# )

# Until the workflow is assembled, expose the classifier as root so the
# project is runnable in `agents-cli playground`.
root_agent = classifier

app = App(
    root_agent=root_agent,
    name="app",
)
