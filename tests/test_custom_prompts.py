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


# ---------------------------------------------------------------------------
# Named judge config resolution
# ---------------------------------------------------------------------------

class TestNamedJudgeConfig:
    def _make(self, **kwargs):
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            return ModelAuditor(
                model="target", provider="openai",
                judge_model="judge", judge_provider="openai",
                show_progress=False,
                **kwargs,
            )

    def test_named_judge_loads_probe_and_judge_prompt(self):
        auditor = self._make(judge="safety")
        assert auditor.probe_prompt is not None
        assert auditor.judge_prompt is not None
        assert "red-team" in auditor.probe_prompt.lower() or "probe" in auditor.probe_prompt.lower()
        assert "safety" in auditor.judge_prompt.lower() or "constitutional" in auditor.judge_prompt.lower()

    def test_named_judge_unknown_name_raises(self):
        with pytest.raises(ValueError, match="Unknown judge config"):
            self._make(judge="nonexistent_judge")

    def test_explicit_probe_prompt_overrides_config_probe(self):
        """Custom probe_prompt overrides the config's probe but config's judge_prompt is kept."""
        auditor = self._make(judge="helpfulness", probe_prompt="my custom probe")
        assert auditor.probe_prompt == "my custom probe"
        # judge_prompt should come from the helpfulness config, not be None
        assert auditor.judge_prompt is not None
        assert "helpfulness" in auditor.judge_prompt.lower() or "mt-bench" in auditor.judge_prompt.lower()

    def test_explicit_judge_prompt_overrides_config_judge(self):
        """Custom judge_prompt overrides the config's judge prompt."""
        auditor = self._make(judge="factuality", judge_prompt="my custom judge")
        assert auditor.judge_prompt == "my custom judge"

    def test_no_judge_leaves_prompts_none(self):
        auditor = self._make()
        assert auditor.probe_prompt is None
        assert auditor.judge_prompt is None

    def test_all_five_named_configs_load(self):
        for name in ("safety", "abstention", "helpfulness", "factuality", "harm"):
            auditor = self._make(judge=name)
            assert auditor.judge_prompt is not None, f"{name} judge_prompt is None"


# ---------------------------------------------------------------------------
# expected_behavior passed through in custom/named judge path
# ---------------------------------------------------------------------------

class TestExpectedBehaviorInCustomPath:
    def test_expected_behavior_included_in_user_message(self):
        """expected_behavior should appear in the user message even when judge_prompt is set."""
        captured_user = []

        async def fake_call(client, model, system, user, response_format=None, history=None):
            captured_user.append(user)
            return '{"score": 8, "verdict": "accurate"}'

        with patch.object(ModelAuditor, "_call_async", side_effect=fake_call):
            asyncio.run(ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="judge",
                scenario="Test scenario",
                conversation=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
                expected_behavior=["Should greet politely", "Should not be rude"],
                judge_prompt="You are a custom judge.",
                json_format=False,
            ))

        assert len(captured_user) == 1
        user_msg = captured_user[0]
        assert "Should greet politely" in user_msg
        assert "Should not be rude" in user_msg

    def test_no_expected_behavior_does_not_add_empty_section(self):
        """No expected_behavior → no SCENARIO EXPECTATIONS section in user message."""
        captured_user = []

        async def fake_call(client, model, system, user, response_format=None, history=None):
            captured_user.append(user)
            return '{"score": 8}'

        with patch.object(ModelAuditor, "_call_async", side_effect=fake_call):
            asyncio.run(ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="judge",
                scenario="Test scenario",
                conversation=[{"role": "user", "content": "hi"}],
                expected_behavior=None,
                judge_prompt="You are a custom judge.",
                json_format=False,
            ))

        assert "SCENARIO EXPECTATIONS" not in captured_user[0]


# ---------------------------------------------------------------------------
# AuditExperiment — judge named config propagation
# ---------------------------------------------------------------------------

class TestAuditExperimentJudgeConfig:
    def test_judge_config_merged_to_model(self):
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai"}],
            judge_model="judge",
            judge_provider="openai",
            judge="helpfulness",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged["judge"] == "helpfulness"

    def test_model_level_judge_overrides_experiment(self):
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai", "judge": "factuality"}],
            judge_model="judge",
            judge_provider="openai",
            judge="helpfulness",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged["judge"] == "factuality"

    def test_no_judge_not_merged(self):
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai"}],
            judge_model="judge",
            judge_provider="openai",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged.get("judge") is None


# ---------------------------------------------------------------------------
# test_prompt sent verbatim on turn 1 (v2 schema honoured)
# ---------------------------------------------------------------------------

class TestTestPromptVerbatim:
    def _auditor(self):
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            a = ModelAuditor(
                model="target", provider="openai",
                judge_model="judge", judge_provider="openai",
                max_turns=1, show_progress=False,
            )
        a.target_client = MagicMock()
        a.judge_client = MagicMock()
        return a

    def test_test_prompt_used_verbatim_on_turn_1(self):
        """When scenario has test_prompt, it must be sent to target as-is
        (no probe generation)."""
        auditor = self._auditor()
        captured = []

        async def fake_call(client, model, system, user, response_format=None, history=None):
            captured.append({"system": system, "user": user, "history": list(history or [])})
            return "target reply"

        async def fake_probe(*args, **kwargs):
            pytest.fail("probe generator must not be called when test_prompt is present")

        async def fake_judge(*args, **kwargs):
            return {"severity": "pass", "issues_found": [], "positive_behaviors": [], "summary": "", "recommendations": []}

        with patch.object(ModelAuditor, "_call_async", side_effect=fake_call), \
             patch.object(ModelAuditor, "_generate_probe_async", side_effect=fake_probe), \
             patch.object(ModelAuditor, "_judge_conversation_async", side_effect=fake_judge):
            asyncio.run(auditor.run_scenario(
                name="test",
                description="some description",
                test_prompt="exact verbatim prompt",
                max_turns=1,
            ))

        # target was called once with the verbatim test_prompt
        assert len(captured) == 1
        assert captured[0]["user"] == "exact verbatim prompt"

    def test_probe_generated_when_no_test_prompt(self):
        """Fallback: when test_prompt is missing, use probe generation as before."""
        auditor = self._auditor()
        probe_calls = []

        async def fake_call(*args, **kwargs):
            return "target reply"

        async def fake_probe(client, model, scenario, conversation, language="English", probe_prompt=None):
            probe_calls.append(scenario)
            return "generated probe"

        async def fake_judge(*args, **kwargs):
            return {"severity": "pass", "issues_found": [], "positive_behaviors": [], "summary": "", "recommendations": []}

        with patch.object(ModelAuditor, "_call_async", side_effect=fake_call), \
             patch.object(ModelAuditor, "_generate_probe_async", side_effect=fake_probe), \
             patch.object(ModelAuditor, "_judge_conversation_async", side_effect=fake_judge):
            asyncio.run(auditor.run_scenario(
                name="test",
                description="some description",
                test_prompt=None,
                max_turns=1,
            ))

        assert len(probe_calls) == 1
        assert probe_calls[0] == "some description"

    def test_probe_generated_on_subsequent_turns_even_with_test_prompt(self):
        """Turn 1 uses test_prompt verbatim, but turn 2+ should still probe-gen
        so multi-turn conversations work."""
        auditor = self._auditor()
        probe_calls = []

        async def fake_call(*args, **kwargs):
            return "target reply"

        async def fake_probe(client, model, scenario, conversation, language="English", probe_prompt=None):
            probe_calls.append(len(conversation))
            return "follow-up probe"

        async def fake_judge(*args, **kwargs):
            return {"severity": "pass", "issues_found": [], "positive_behaviors": [], "summary": "", "recommendations": []}

        with patch.object(ModelAuditor, "_call_async", side_effect=fake_call), \
             patch.object(ModelAuditor, "_generate_probe_async", side_effect=fake_probe), \
             patch.object(ModelAuditor, "_judge_conversation_async", side_effect=fake_judge):
            asyncio.run(auditor.run_scenario(
                name="test",
                description="desc",
                test_prompt="first prompt",
                max_turns=3,
            ))

        # Probe generator called on turns 2 and 3 only
        assert len(probe_calls) == 2
