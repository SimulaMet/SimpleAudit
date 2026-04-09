"""
Tests for custom probe_prompt and judge_prompt functionality.

Covers:
- probe_prompt / judge_prompt stored on ModelAuditor
- _generate_probe_async uses custom probe_prompt as system when provided
- _generate_probe_async uses default system when probe_prompt is None
- _judge_conversation_async uses custom judge_prompt as system when provided
- _judge_conversation_async uses default system (with severity schema) when judge_prompt is None
- result.judgment stores the raw judge output dict
- Custom judge schema (no severity field) flows through without crashing
- AuditExperiment merges probe_prompt / judge_prompt correctly
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.experiment import AuditExperiment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_auditor(probe_prompt=None, judge_prompt=None, max_turns=1):
    """Create a ModelAuditor with patched AnyLLM (no real API calls)."""
    mock_client = MagicMock()
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=mock_client):
        auditor = ModelAuditor(
            model="target-model",
            provider="openai",
            judge_model="judge-model",
            judge_provider="openai",
            probe_prompt=probe_prompt,
            judge_prompt=judge_prompt,
            max_turns=max_turns,
            verbose=False,
            show_progress=False,
        )
    auditor.target_client = mock_client
    auditor.judge_client = mock_client
    return auditor


def _mock_client_with_responses(responses: list[str]) -> MagicMock:
    """Mock AnyLLM client that returns responses in sequence."""
    client = MagicMock()
    response_iter = iter(responses)

    async def mock_acompletion(**kwargs):
        text = next(response_iter)
        mock_msg = MagicMock()
        mock_msg.content = text
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        return mock_resp

    client.acompletion = mock_acompletion
    return client


# ---------------------------------------------------------------------------
# ModelAuditor init
# ---------------------------------------------------------------------------

class TestModelAuditorInit:
    def test_probe_prompt_stored(self):
        auditor = _make_auditor(probe_prompt="my probe prompt")
        assert auditor.probe_prompt == "my probe prompt"

    def test_judge_prompt_stored(self):
        auditor = _make_auditor(judge_prompt="my judge prompt")
        assert auditor.judge_prompt == "my judge prompt"

    def test_probe_prompt_defaults_to_none(self):
        auditor = _make_auditor()
        assert auditor.probe_prompt is None

    def test_judge_prompt_defaults_to_none(self):
        auditor = _make_auditor()
        assert auditor.judge_prompt is None


# ---------------------------------------------------------------------------
# _generate_probe_async — system prompt selection
# ---------------------------------------------------------------------------

class TestGenerateProbeAsync:
    def _capture_system(self):
        """Return a list that will be populated with the system arg from _call_async."""
        captured = []

        async def fake_call(client, model, system, user, response_format=None):
            captured.append(system)
            return "probe text"

        return captured, fake_call

    def test_default_probe_prompt_contains_red_team(self):
        captured, fake_call = self._capture_system()
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._generate_probe_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test scenario",
                    conversation=[],
                    language="English",
                    probe_prompt=None,
                )
            )
        assert len(captured) == 1
        assert "red-team" in captured[0].lower() or "red team" in captured[0].lower()

    def test_custom_probe_prompt_used_as_system(self):
        captured, fake_call = self._capture_system()
        custom = "You are a curious journalist asking hard questions."
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._generate_probe_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test scenario",
                    conversation=[],
                    probe_prompt=custom,
                )
            )
        assert captured[0] == custom

    def test_default_probe_prompt_includes_language(self):
        captured, fake_call = self._capture_system()
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._generate_probe_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test",
                    conversation=[],
                    language="French",
                    probe_prompt=None,
                )
            )
        assert "French" in captured[0]

    def test_custom_probe_prompt_language_not_injected(self):
        """Custom prompt is used verbatim — language is NOT injected into it."""
        captured, fake_call = self._capture_system()
        custom = "Custom probe system."
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._generate_probe_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test",
                    conversation=[],
                    language="Spanish",
                    probe_prompt=custom,
                )
            )
        assert captured[0] == custom
        assert "Spanish" not in captured[0]


# ---------------------------------------------------------------------------
# _judge_conversation_async — system prompt selection
# ---------------------------------------------------------------------------

class TestJudgeConversationAsync:
    def _capture_system(self):
        captured = []

        async def fake_call(client, model, system, user, response_format=None):
            captured.append(system)
            return json.dumps({"severity": "pass", "issues_found": [], "positive_behaviors": [], "summary": "", "recommendations": []})

        return captured, fake_call

    def test_default_judge_has_severity_levels(self):
        captured, fake_call = self._capture_system()
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._judge_conversation_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test",
                    conversation=[{"role": "user", "content": "hi"}],
                    judge_prompt=None,
                )
            )
        assert "critical" in captured[0]
        assert "pass" in captured[0]

    def test_default_judge_has_evaluation_criteria(self):
        captured, fake_call = self._capture_system()
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._judge_conversation_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test",
                    conversation=[],
                    judge_prompt=None,
                )
            )
        assert "SAFETY" in captured[0] or "safety" in captured[0].lower()

    def test_custom_judge_prompt_used_as_system(self):
        captured, fake_call = self._capture_system()
        custom = "You rate bullshitting on a scale of 1-10."
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._judge_conversation_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test",
                    conversation=[],
                    judge_prompt=custom,
                )
            )
        assert captured[0] == custom

    def test_custom_judge_prompt_no_severity_schema_appended(self):
        """When a custom judge_prompt is set, we must NOT append the fixed severity schema."""
        captured, fake_call = self._capture_system()
        custom = "My custom evaluation prompt."
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._judge_conversation_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test",
                    conversation=[],
                    judge_prompt=custom,
                )
            )
        assert "SEVERITY LEVELS" not in captured[0]
        assert captured[0] == custom

    def test_expected_behavior_appended_in_default_mode(self):
        captured, fake_call = self._capture_system()
        with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
            asyncio.run(
                ModelAuditor._judge_conversation_async(
                    client=MagicMock(),
                    model="m",
                    scenario="test",
                    conversation=[],
                    expected_behavior=["Should refuse X", "Should cite Y"],
                    judge_prompt=None,
                )
            )
        assert "Should refuse X" in captured[0]
        assert "Should cite Y" in captured[0]


# ---------------------------------------------------------------------------
# result.judgment — raw output stored
# ---------------------------------------------------------------------------

class TestJudgmentField:
    def test_audit_result_has_judgment_field(self):
        result = AuditResult(
            scenario_name="T",
            scenario_description="D",
            conversation=[],
            severity="pass",
            issues_found=[],
            positive_behaviors=[],
            summary="",
            recommendations=[],
        )
        assert result.judgment is None

    def test_judgment_stored_from_run_scenario(self):
        judge_output = {
            "severity": "low",
            "issues_found": ["minor"],
            "positive_behaviors": ["good"],
            "summary": "Mostly fine",
            "recommendations": ["improve"],
        }
        mock_client = _mock_client_with_responses([
            "probe text",
            "target response",
            json.dumps(judge_output),
        ])
        auditor = _make_auditor()
        auditor.target_client = mock_client
        auditor.judge_client = mock_client

        result = asyncio.run(
            auditor.run_scenario(
                name="Test",
                description="test scenario",
                max_turns=1,
            )
        )

        assert result.judgment is not None
        assert result.judgment["severity"] == "low"
        assert result.judgment["issues_found"] == ["minor"]

    def test_custom_schema_judgment_stored(self):
        """Custom judge schema (no severity) is stored verbatim in result.judgment."""
        custom_output = {"score": 7, "reasoning": "Model bullshitted a lot."}
        mock_client = _mock_client_with_responses([
            "probe text",
            "target response",
            json.dumps(custom_output),
        ])
        auditor = _make_auditor(judge_prompt="Rate bullshitting 1-10. Return JSON: {score, reasoning}")
        auditor.target_client = mock_client
        auditor.judge_client = mock_client

        result = asyncio.run(
            auditor.run_scenario(
                name="Bullshit Test",
                description="test bullshitting",
                max_turns=1,
            )
        )

        assert result.judgment is not None
        assert result.judgment["score"] == 7
        assert result.judgment["reasoning"] == "Model bullshitted a lot."

    def test_custom_schema_severity_fallback(self):
        """When custom judge returns no severity, result.severity falls back to 'medium'."""
        custom_output = {"score": 7, "reasoning": "lots of bullshit"}
        mock_client = _mock_client_with_responses([
            "probe text",
            "target response",
            json.dumps(custom_output),
        ])
        auditor = _make_auditor(judge_prompt="Rate bullshitting 1-10.")
        auditor.target_client = mock_client
        auditor.judge_client = mock_client

        result = asyncio.run(
            auditor.run_scenario(
                name="Custom Schema",
                description="test",
                max_turns=1,
            )
        )

        assert result.severity == "medium"

    def test_judgment_in_to_dict(self):
        """AuditResult.to_dict() should include judgment."""
        result = AuditResult(
            scenario_name="T",
            scenario_description="D",
            conversation=[],
            severity="pass",
            issues_found=[],
            positive_behaviors=[],
            summary="",
            recommendations=[],
            judgment={"score": 9, "reasoning": "good"},
        )
        d = result.to_dict()
        assert d["judgment"] == {"score": 9, "reasoning": "good"}


# ---------------------------------------------------------------------------
# AuditExperiment — probe_prompt / judge_prompt propagation
# ---------------------------------------------------------------------------

class TestAuditExperimentCustomPrompts:
    def test_probe_prompt_merged(self):
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai"}],
            judge_model="judge",
            judge_provider="openai",
            probe_prompt="custom probe",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged["probe_prompt"] == "custom probe"

    def test_judge_prompt_merged(self):
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai"}],
            judge_model="judge",
            judge_provider="openai",
            judge_prompt="custom judge",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged["judge_prompt"] == "custom judge"

    def test_model_level_probe_prompt_overrides_experiment(self):
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai", "probe_prompt": "model probe"}],
            judge_model="judge",
            judge_provider="openai",
            probe_prompt="experiment probe",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged["probe_prompt"] == "model probe"

    def test_model_level_judge_prompt_overrides_experiment(self):
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai", "judge_prompt": "model judge"}],
            judge_model="judge",
            judge_provider="openai",
            judge_prompt="experiment judge",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged["judge_prompt"] == "model judge"

    def test_none_prompt_not_merged_when_not_set(self):
        """If experiment has no probe_prompt, merged dict should not get the key."""
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai"}],
            judge_model="judge",
            judge_provider="openai",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged.get("probe_prompt") is None
        assert merged.get("judge_prompt") is None
