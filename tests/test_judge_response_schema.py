"""
Tests for the per-judge `response_schema` mechanism.

The framework's _judge_conversation_async always passes a JSON-schema
response_format to the judge LLM when json_format=True. Historically this
schema was hardcoded (5-field severity shape), which broke any judge with
a non-default output shape (e.g. binary classifiers, the built-in
abstention judge). After the refactor:

  - DEFAULT_JUDGE_RESPONSE_SCHEMA is a module-level constant
  - judges may declare an optional `response_schema` in their config
  - ModelAuditor picks it up automatically and threads it through
  - explicit judge_response_schema constructor arg wins over the config

These tests verify each link in that chain without making any API calls.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from simpleaudit.judges import get_judge, list_judge_configs
from simpleaudit.model_auditor import (
    DEFAULT_JUDGE_RESPONSE_SCHEMA,
    ModelAuditor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_auditor(judge=None, judge_prompt=None, judge_response_schema=None, json_format=True):
    """Construct a ModelAuditor with patched AnyLLM (no real API calls)."""
    mock_client = MagicMock()
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=mock_client):
        auditor = ModelAuditor(
            model="target-model",
            provider="openai",
            judge_model="judge-model",
            judge_provider="openai",
            judge=judge,
            judge_prompt=judge_prompt,
            judge_response_schema=judge_response_schema,
            json_format=json_format,
            max_turns=1,
            verbose=False,
            show_progress=False,
        )
    auditor.target_client = mock_client
    auditor.judge_client = mock_client
    return auditor


def _mock_call_async_returning(payload: dict):
    """Mock _call_async so it returns the serialised JSON payload string."""
    return AsyncMock(return_value=json.dumps(payload))


# ---------------------------------------------------------------------------
# 1. Constant shape — backwards-compatible with the old hardcoded schema
# ---------------------------------------------------------------------------

def test_default_schema_is_well_formed():
    s = DEFAULT_JUDGE_RESPONSE_SCHEMA
    assert s["type"] == "object"
    assert set(s["required"]) == {
        "severity", "issues_found", "positive_behaviors", "summary", "recommendations",
    }
    sev = s["properties"]["severity"]
    assert sev["type"] == "string"
    assert set(sev["enum"]) == {"critical", "high", "medium", "low", "pass"}


# ---------------------------------------------------------------------------
# 2. judge config registry — binary_abstention declares its own schema
# ---------------------------------------------------------------------------

def test_binary_abstention_registered():
    assert "binary_abstention" in list_judge_configs()


def test_binary_abstention_declares_response_schema():
    cfg = get_judge("binary_abstention")
    assert "response_schema" in cfg
    schema = cfg["response_schema"]
    assert schema["type"] == "object"
    assert set(schema["required"]) == {"abstained", "reasoning"}
    assert schema["properties"]["abstained"]["type"] == "boolean"
    assert schema["properties"]["reasoning"]["type"] == "string"
    # Must NOT carry the severity enum — that would defeat the point.
    assert "severity" not in schema["properties"]


def test_severity_judges_do_not_declare_response_schema():
    """Existing severity judges rely on the default schema (backwards compat)."""
    for name in ("safety", "helsedir_sexhealth_no", "helsedir_sexhealth_no_rag"):
        cfg = get_judge(name)
        assert "response_schema" not in cfg, f"{name} unexpectedly declared response_schema"


# ---------------------------------------------------------------------------
# 3. ModelAuditor.__init__ — picks up response_schema from config
# ---------------------------------------------------------------------------

def test_auditor_picks_up_binary_schema_from_config():
    auditor = _make_auditor(judge="binary_abstention")
    assert auditor.judge_response_schema is not None
    assert set(auditor.judge_response_schema["required"]) == {"abstained", "reasoning"}


def test_auditor_judge_response_schema_none_for_severity_judges():
    """Severity judges have no response_schema in their config; the framework
    should treat this as 'use the default at call time', i.e. None on the auditor."""
    auditor = _make_auditor(judge="safety")
    assert auditor.judge_response_schema is None


def test_explicit_schema_overrides_config():
    custom = {
        "type": "object",
        "properties": {"verdict": {"type": "string"}},
        "required": ["verdict"],
    }
    auditor = _make_auditor(judge="binary_abstention", judge_response_schema=custom)
    assert auditor.judge_response_schema is custom


def test_no_judge_name_no_override_yields_none():
    auditor = _make_auditor()
    assert auditor.judge_response_schema is None


# ---------------------------------------------------------------------------
# 4. _judge_conversation_async — threads schema into response_format
# ---------------------------------------------------------------------------

def _extract_sent_response_format(mock_call_async) -> dict:
    """Pull out the response_format kwarg from the last _call_async invocation."""
    assert mock_call_async.await_count >= 1, "expected at least one judge call"
    _, kwargs = mock_call_async.call_args
    return kwargs.get("response_format")


def test_judge_call_uses_provided_schema_when_given():
    custom = {
        "type": "object",
        "properties": {"flag": {"type": "boolean"}},
        "required": ["flag"],
    }
    fake_response = json.dumps({"flag": True})
    with patch.object(ModelAuditor, "_call_async", new=AsyncMock(return_value=fake_response)) as m:
        asyncio.run(
            ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="judge",
                scenario="desc",
                conversation=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
                judge_prompt="custom prompt",
                json_format=True,
                response_schema=custom,
            )
        )
        rf = _extract_sent_response_format(m)
    assert rf is not None
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"] is custom


def test_judge_call_falls_back_to_default_schema():
    fake_response = json.dumps({
        "severity": "pass",
        "issues_found": [],
        "positive_behaviors": [],
        "summary": "ok",
        "recommendations": [],
    })
    with patch.object(ModelAuditor, "_call_async", new=AsyncMock(return_value=fake_response)) as m:
        asyncio.run(
            ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="judge",
                scenario="desc",
                conversation=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
                json_format=True,
                response_schema=None,  # explicit None -> default
            )
        )
        rf = _extract_sent_response_format(m)
    assert rf is not None
    assert rf["json_schema"]["schema"] is DEFAULT_JUDGE_RESPONSE_SCHEMA


def test_judge_call_omits_response_format_when_json_format_false():
    fake_response = json.dumps({"abstained": True, "reasoning": "..."})
    with patch.object(ModelAuditor, "_call_async", new=AsyncMock(return_value=fake_response)) as m:
        asyncio.run(
            ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="judge",
                scenario="desc",
                conversation=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
                judge_prompt="custom",
                json_format=False,
                response_schema={"type": "object"},  # ignored when json_format=False
            )
        )
        rf = _extract_sent_response_format(m)
    assert rf is None


# ---------------------------------------------------------------------------
# 5. End-to-end: a custom-schema judge returns its raw payload
# ---------------------------------------------------------------------------

def test_binary_judge_parses_back_to_raw_payload():
    """Custom-prompt path returns the parsed JSON verbatim (no severity coercion)."""
    payload = {"abstained": True, "reasoning": "Bot replied: 'I can't help with that.'"}
    fake_response = json.dumps(payload)
    with patch.object(ModelAuditor, "_call_async", new=AsyncMock(return_value=fake_response)):
        out = asyncio.run(
            ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="judge",
                scenario="desc",
                conversation=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
                judge_prompt="binary judge prompt",
                json_format=True,
                response_schema=get_judge("binary_abstention")["response_schema"],
            )
        )
    assert out == payload
    assert "severity" not in out


def test_binary_judge_malformed_response_yields_error_marker():
    """If the LLM returns garbage, we get the ERROR severity fallback (existing behavior)."""
    with patch.object(ModelAuditor, "_call_async", new=AsyncMock(return_value="not json at all")):
        out = asyncio.run(
            ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="judge",
                scenario="desc",
                conversation=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}],
                judge_prompt="binary judge prompt",
                json_format=True,
                response_schema=get_judge("binary_abstention")["response_schema"],
            )
        )
    assert out["severity"] == "ERROR"
