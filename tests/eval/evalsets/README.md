# Evaluation Sets

This directory contains evaluation sets for testing agent tool trajectory using
`agents-cli eval run`.

## Running Evaluations

```bash
# Run the main trajectory evalset (recommended)
uvx google-agents-cli eval run --evalset tests/eval/evalsets/drift_trajectories.evalset.json

# Run all evalsets
uvx google-agents-cli eval run --all
```

## Current Evalsets

### `drift_trajectories.evalset.json` — 7 cases

The primary evalset. Tests that the agent calls the correct BQ FunctionTools in
response to natural-language requests. MCP tools (list_connections, sync_connection,
etc.) are excluded — they require live Fivetran API calls and MCP subprocess
spawning that exceeds the eval runner's 10s session timeout. MCP tool behavior is
validated in the playground instead.

| Case | Tool called |
|---|---|
| `list_proposed_events` | `list_proposed_drift_events` |
| `list_proposed_events_alt_phrasing` | `list_proposed_drift_events` |
| `approve_drift_event` | `approve_drift` |
| `reject_drift_event` | `reject_drift` |
| `verify_drift_event` | `mark_drift_verified` |
| `check_single_connection_freshness` | `check_freshness_sla` |
| `list_all_freshness_status` | `list_freshness_status` |

### `basic.evalset.json`

Template default — kept as a reference. Not actively maintained.

## Eval Config (`eval_config.json`)

```json
{
  "criteria": {
    "tool_trajectory_avg_score": 1.0
  }
}
```

`response_match_score` is intentionally excluded. These cases have no
`final_response` field — ROUGE would score 0.0 against an empty string and
fail every case. For a HITL agent, correct tool trajectory is the meaningful
correctness signal.

## Evalset Format

```json
{
  "eval_set_id": "unique_id",
  "name": "Human-readable name",
  "eval_cases": [
    {
      "eval_id": "case_id",
      "conversation": [
        {
          "user_content": {
            "parts": [{"text": "User message"}]
          },
          "intermediate_data": {
            "tool_uses": [
              {"name": "tool_name", "args": {"param": "value"}}
            ]
          }
        }
      ],
      "session_input": {
        "app_name": "app",
        "user_id": "eval_user",
        "state": {}
      }
    }
  ]
}
```

## Key Fields

- `eval_cases`: Array of test scenarios
- `intermediate_data.tool_uses`: Expected tool calls (name + args) for trajectory matching
- `session_input`: Initial session state — `app_name` must match `App(name=...)` in `agent.py`

## Adding Cases

1. Write the user message that should trigger the tool call
2. Set `intermediate_data.tool_uses` with the exact tool `name` and expected `args`
3. Leave `final_response` absent (not needed for trajectory-only eval)
4. Run `uvx google-agents-cli eval run --evalset ...` to verify the new case passes

**Gotcha — `approved_by` arg:** If the tool takes an `approved_by` parameter, the
user message must name the reviewer explicitly. The agent defaults to its own identity
(`root_agent`) when no reviewer is specified, which won't match an expected arg of
`eval_user` and will score 0.0.

See [ADK documentation](https://google.github.io/adk-docs/) for advanced evaluation options.
