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
"""Unit tests for app.tools.classify_drift.

Pure-helper coverage — no live LLM. Tests use `model_fn=` dependency
injection on `classify()` to feed canned Gemini responses, and the
parse/build helpers directly for boundary cases.
"""

import json

import pytest

from app.tools.bigquery_query import ColumnRecord
from app.tools.classify_drift import (
    CHANGE_TYPES,
    CLASSIFIER_MODEL,
    Classification,
    _build_prompt,
    _column_summary,
    _extract_json,
    _ordinal_delta,
    _parse_response,
    classify,
)
from app.tools.snapshot_diff import ColumnChange


def _col(name, t="STRING", ordinal=1, schema="public", table="customers"):
    return ColumnRecord(schema, table, name, t, ordinal, True)


def _change(change_type, before, after, schema="public", table="customers"):
    """Build a ColumnChange for tests.

    IMPORTANT: in real usage `diff_columns` derives ColumnChange's
    table_schema / table_name from the ColumnRecords it pairs — so the
    ChangeChange's table and its before/after columns' tables always agree.

    In tests we set them independently here. If a test passes a non-default
    table to `_col(...)` for the before/after, it MUST also pass the same
    `table=` to `_change(...)`, or the ColumnChange will carry the default
    'customers' while its inner records say (e.g.) 'orders' — a state that
    cannot arise from real `diff_columns` output and that will break any
    assertion checking the prompt's `<schema>.<table>` FQN. Caught once
    (test_build_prompt_includes_table_fqn_and_columns, 2026-05-22); this
    note exists so the same trap doesn't recur in future scenarios.
    """
    return ColumnChange(schema, table, change_type, before, after)


# --- model + change-type constants -----------------------------------------

def test_change_types_complete():
    """Five and only five change types — extending this set requires
    coordinated changes in diff_columns + the prompt + the lifecycle docs."""
    assert set(CHANGE_TYPES) == {
        "RENAME", "TYPE_PROMOTION", "REORDER", "NEW_FIELD", "DEPRECATION",
    }


def test_classifier_model_matches_agent_default():
    """Per CLAUDE.md 'NEVER change the model' and the design's keep-in-sync
    rationale, the classifier model must align with app/agent.py's default."""
    assert CLASSIFIER_MODEL == "gemini-flash-latest"


# --- _column_summary --------------------------------------------------------

def test_column_summary_dict_shape():
    rec = _col("customer_id", "INT64", 3)
    s = _column_summary(rec)
    assert s == {
        "schema": "public",
        "table": "customers",
        "name": "customer_id",
        "type": "INT64",
        "ordinal": 3,
        "nullable": True,
    }


def test_column_summary_none_passthrough():
    assert _column_summary(None) is None


# --- _ordinal_delta ---------------------------------------------------------

def test_ordinal_delta_both_sides_present():
    ch = _change("RENAME", _col("a", "INT64", 1), _col("b", "INT64", 4))
    assert _ordinal_delta(ch) == 3


def test_ordinal_delta_added_column_is_none():
    """NEW_FIELD has no before -> no delta to compute."""
    ch = _change("NEW_FIELD", None, _col("a", "INT64", 1))
    assert _ordinal_delta(ch) is None


def test_ordinal_delta_removed_column_is_none():
    """DEPRECATION has no after -> no delta."""
    ch = _change("DEPRECATION", _col("a", "INT64", 1), None)
    assert _ordinal_delta(ch) is None


# --- _build_prompt ----------------------------------------------------------

def test_build_prompt_includes_candidate_change_type():
    ch = _change("RENAME", _col("customer_id", "INT64", 1), _col("cust_id", "INT64", 1))
    p = _build_prompt(ch, downstream_refs=[])
    assert '"candidate_change_type": "RENAME"' in p


def test_build_prompt_includes_table_fqn_and_columns():
    # _change defaults table='customers'; explicitly set table='orders' here
    # so the ColumnChange's table_name matches the columns inside it (which
    # is what diff_columns produces in real usage).
    ch = _change("TYPE_PROMOTION",
                 _col("amount", "BIGNUMERIC", 1, table="orders"),
                 _col("amount", "STRING", 1, table="orders"),
                 table="orders")
    p = _build_prompt(ch, downstream_refs=["dbt.orders_summary"])
    assert "public.orders" in p
    assert '"amount"' in p
    assert "BIGNUMERIC" in p and "STRING" in p
    assert "dbt.orders_summary" in p


def test_build_prompt_lists_all_five_change_types():
    ch = _change("NEW_FIELD", None, _col("y", "STRING", 2))
    p = _build_prompt(ch, downstream_refs=[])
    for ct in CHANGE_TYPES:
        assert ct in p, f"prompt missing change_type {ct}"


def test_build_prompt_marks_ordinal_as_advisory():
    """Decision #3 — ordinal_delta is a SIGNAL, not a rule. The prompt
    explicitly says so."""
    ch = _change("RENAME", _col("a", "INT64", 1), _col("b", "INT64", 99))
    p = _build_prompt(ch, downstream_refs=[])
    assert "advisory" in p.lower() or "do not gate" in p.lower() or "do NOT gate" in p


# --- _extract_json ----------------------------------------------------------

def test_extract_json_plain():
    out = _extract_json('{"change_type": "RENAME"}')
    assert out == {"change_type": "RENAME"}


def test_extract_json_strips_json_fence():
    """Some Gemini outputs wrap JSON in ```json ... ``` despite instructions."""
    raw = "```json\n{\"change_type\": \"RENAME\"}\n```"
    out = _extract_json(raw)
    assert out == {"change_type": "RENAME"}


def test_extract_json_strips_bare_fence():
    raw = "```\n{\"change_type\": \"RENAME\"}\n```"
    out = _extract_json(raw)
    assert out == {"change_type": "RENAME"}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError, match="not JSON"):
        _extract_json("not json at all")


def test_extract_json_rejects_non_object():
    """Top-level array (or other non-object) violates the contract."""
    with pytest.raises(ValueError, match="not a JSON object"):
        _extract_json('["RENAME", 0.9]')


# --- _parse_response --------------------------------------------------------

_GOOD_RESPONSE = json.dumps({
    "change_type": "RENAME",
    "confidence": 0.92,
    "rationale": "customer_id -> cust_id is a plausible abbreviation rename.",
    "remediation_sql": "CREATE OR REPLACE VIEW public.customers_shim AS SELECT *, cust_id AS customer_id FROM public.customers",
})


def test_parse_response_happy_path():
    result = _parse_response(_GOOD_RESPONSE)
    assert isinstance(result, Classification)
    assert result.change_type == "RENAME"
    assert result.confidence == 0.92
    assert "cust_id AS customer_id" in result.remediation_sql


def test_parse_response_missing_field():
    incomplete = json.dumps({"change_type": "RENAME", "confidence": 0.9, "rationale": "x"})
    with pytest.raises(ValueError, match="missing field: remediation_sql"):
        _parse_response(incomplete)


def test_parse_response_unknown_change_type():
    bad = json.dumps({
        "change_type": "BANANA",
        "confidence": 0.9,
        "rationale": "x",
        "remediation_sql": "",
    })
    with pytest.raises(ValueError, match="unknown change_type"):
        _parse_response(bad)


def test_parse_response_confidence_out_of_range():
    bad = json.dumps({
        "change_type": "RENAME",
        "confidence": 1.5,
        "rationale": "x",
        "remediation_sql": "",
    })
    with pytest.raises(ValueError, match="confidence out of"):
        _parse_response(bad)


def test_parse_response_confidence_non_numeric():
    bad = json.dumps({
        "change_type": "RENAME",
        "confidence": "very-sure",
        "rationale": "x",
        "remediation_sql": "",
    })
    with pytest.raises(ValueError, match="confidence not a number"):
        _parse_response(bad)


def test_parse_response_empty_remediation_sql_is_valid():
    """REORDER and NEW_FIELD legitimately return empty SQL (no shim needed)."""
    good_empty = json.dumps({
        "change_type": "NEW_FIELD",
        "confidence": 0.95,
        "rationale": "Additive; no shim required.",
        "remediation_sql": "",
    })
    result = _parse_response(good_empty)
    assert result.change_type == "NEW_FIELD"
    assert result.remediation_sql == ""


# --- classify (end-to-end with injected model_fn) --------------------------

def test_classify_composes_prompt_and_parse_with_injected_model():
    """End-to-end without an LLM: inject `model_fn` to return a canned
    Gemini-shaped response. Verifies the prompt reaches the model and the
    parser converts the response to a Classification."""
    captured_prompt: list[str] = []

    def fake_model(prompt: str) -> str:
        captured_prompt.append(prompt)
        return _GOOD_RESPONSE

    ch = _change("RENAME", _col("customer_id", "INT64", 1), _col("cust_id", "INT64", 1))
    result = classify(ch, downstream_refs=["dashboards.churn"], model_fn=fake_model)

    # Prompt got built and forwarded
    assert len(captured_prompt) == 1
    assert "customer_id" in captured_prompt[0]
    assert "cust_id" in captured_prompt[0]
    assert "dashboards.churn" in captured_prompt[0]

    # Response parsed correctly
    assert result.change_type == "RENAME"
    assert 0 < result.confidence <= 1


def test_classify_propagates_parse_errors():
    """A malformed Gemini response surfaces as ValueError — the caller
    workflow decides whether to retry, downgrade confidence, or escalate
    to human."""
    def fake_model(prompt: str) -> str:
        return "not even close to JSON"

    ch = _change("RENAME", _col("a", "INT64", 1), _col("b", "INT64", 1))
    with pytest.raises(ValueError):
        classify(ch, downstream_refs=[], model_fn=fake_model)


def test_classify_default_downstream_refs_empty_list():
    """`downstream_refs=None` (the default) should be treated as an empty
    list — must not be passed to .format() as None."""
    def fake_model(prompt: str) -> str:
        # Verify the prompt did get a rendered empty list (not the literal
        # word "None" lying around).
        assert "[]" in prompt
        return _GOOD_RESPONSE

    ch = _change("RENAME", _col("a", "INT64", 1), _col("b", "INT64", 1))
    result = classify(ch, model_fn=fake_model)
    assert result.change_type == "RENAME"
