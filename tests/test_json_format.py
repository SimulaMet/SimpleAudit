"""
Tests for the json_format parameter on ModelAuditor.

json_format=True (default) tells _judge_conversation_async to use OpenAI-style
JSON mode by passing response_format={"type": "json_object"} to _call_async.
json_format=False omits it — required for providers that don't support it (e.g. Ollama).

The flag must also be stored on ModelAuditor and forwarded through run_scenario.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.model_auditor import ModelAuditor
from tests.fakes import fixed_probe_auditor, fixed_severity_judge, fixed_target, make_auditor


_PASS_JSON = json.dumps({
    "severity": "pass",
    "issues_found": [],
    "positive_behaviors": [],
    "summary": "",
    "recommendations": [],
})


def _capture_judge_call():
    """Return (captured_list, fake_call) where captured_list accumulates response_format per call."""
    captured = []

    async def fake_call(client, model, system, user, response_format=None, history=None, **kwargs):
        captured.append(response_format)
        return (_PASS_JSON, 0, 0)

    return captured, fake_call


# ---------------------------------------------------------------------------
# _judge_conversation_async — response_format kwarg
# ---------------------------------------------------------------------------

class TestJsonFormatJudgeConversation:
    """_judge_conversation_async respects the json_format flag directly."""

    def test_json_format_true_passes_response_format(self):
        """json_format=True → a non-None response_format dict is forwarded to _call_async."""
        captured, fake_call = _capture_judge_call()
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(ModelAuditor._judge_conversation_async(
                client=MagicMock(), model="m",
                scenario="test", conversation=[],
                json_format=True,
            ))
        assert captured[0] is not None
        assert isinstance(captured[0], dict)

    def test_json_format_false_omits_response_format(self):
        """json_format=False → response_format is None (not forwarded)."""
        captured, fake_call = _capture_judge_call()
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(ModelAuditor._judge_conversation_async(
                client=MagicMock(), model="m",
                scenario="test", conversation=[],
                json_format=False,
            ))
        assert captured[0] is None

    def test_json_format_default_is_true(self):
        """Calling without json_format → JSON mode is active (default True)."""
        captured, fake_call = _capture_judge_call()
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(ModelAuditor._judge_conversation_async(
                client=MagicMock(), model="m",
                scenario="test", conversation=[],
            ))
        assert captured[0] is not None
        assert isinstance(captured[0], dict)


# ---------------------------------------------------------------------------
# ModelAuditor stores json_format and it flows through run_scenario
# ---------------------------------------------------------------------------

class TestJsonFormatEndToEnd:
    """json_format on ModelAuditor is forwarded to _judge_conversation_async."""

    def _fake_judge(self, captured_jf):
        async def _judge(client, model, scenario, conversation, expected_behavior=None, **kwargs):
            captured_jf.append(kwargs.get("json_format"))
            return (
                {"severity": "pass", "issues_found": [], "positive_behaviors": [],
                 "summary": "", "recommendations": []},
                0, 0,
            )
        return _judge

    def test_json_format_false_stored_and_forwarded(self):
        captured_jf = []
        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            json_format=False, max_turns=1, show_progress=False,
        )
        with patch.object(ModelAuditor, "_judge_conversation_async",
                          side_effect=self._fake_judge(captured_jf)):
            asyncio.run(auditor.run_scenario(name="t", description="d", max_turns=1))
        assert captured_jf[0] is False

    def test_json_format_true_stored_and_forwarded(self):
        captured_jf = []
        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            json_format=True, max_turns=1, show_progress=False,
        )
        with patch.object(ModelAuditor, "_judge_conversation_async",
                          side_effect=self._fake_judge(captured_jf)):
            asyncio.run(auditor.run_scenario(name="t", description="d", max_turns=1))
        assert captured_jf[0] is True

    def test_json_format_default_true_forwarded(self):
        """make_auditor() without json_format → json_format=True reaches the judge."""
        captured_jf = []
        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )
        with patch.object(ModelAuditor, "_judge_conversation_async",
                          side_effect=self._fake_judge(captured_jf)):
            asyncio.run(auditor.run_scenario(name="t", description="d", max_turns=1))
        assert captured_jf[0] is True
