"""
Tests for ModelAuditor class.

Run with: pytest tests/test_model_auditor.py -v
"""

import asyncio
import pytest
from unittest.mock import Mock, patch, MagicMock

from simpleaudit import ModelAuditor, get_scenarios, list_scenario_packs
# Check for optional provider dependencies
try:
    import anthropic
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

def test_system_prompt_scenarios_available():
    """Test that system_prompt scenario pack is registered."""
    packs = list_scenario_packs()
    
    assert "system_prompt" in packs
    assert packs["system_prompt"] > 0
    
    # Should be included in 'all'
    total = packs["safety"] + packs["rag"] + packs["health"] + packs["helpmed"] + packs["ung"] + packs["system_prompt"] + packs["bullshitbench"] + packs["health_bullshit"] + packs["hei_refusal"] + packs["nav_aap"] + packs["skatteetaten"] + packs["helfo"] + packs["lanekassen"] + packs["arbeidstilsynet_arbeidstid"]
    assert packs["all"] == total


def test_get_system_prompt_scenarios():
    """Test getting system_prompt scenarios."""
    scenarios = get_scenarios("system_prompt")
    
    assert isinstance(scenarios, list)
    assert len(scenarios) == 8
    
    for scenario in scenarios:
        assert "name" in scenario
        assert "description" in scenario


@pytest.mark.skipif(not HAS_ANTHROPIC, reason="anthropic provider not installed")
def test_model_auditor_init_requires_provider():
    """Test that ModelAuditor requires valid provider configuration."""
    import os
    from any_llm.exceptions import MissingApiKeyError
    
    # Temporarily remove API keys
    original_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
    original_openai = os.environ.pop("OPENAI_API_KEY", None)
    original_xai = os.environ.pop("XAI_API_KEY", None)
    
    try:
        with pytest.raises(MissingApiKeyError):
            ModelAuditor(
                model="claude-sonnet-4-20250514",
                provider="anthropic",
                judge_model="claude-sonnet-4-20250514",
                judge_provider="anthropic",
            )
    finally:
        if original_anthropic:
            os.environ["ANTHROPIC_API_KEY"] = original_anthropic
        if original_openai:
            os.environ["OPENAI_API_KEY"] = original_openai
        if original_xai:
            os.environ["XAI_API_KEY"] = original_xai


def test_model_auditor_system_prompt_handling():
    """Test that system_prompt parameter is properly stored."""
    with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
        mock_provider = MagicMock()
        mock_provider.model = "test-model"
        mock_anyllm.create.return_value = mock_provider
        
        # With system prompt
        auditor_with = ModelAuditor(
            model="claude-sonnet-4-20250514",
            provider="anthropic",
            judge_model="claude-sonnet-4-20250514",
            judge_provider="anthropic",
            system_prompt="You are a test assistant."
        )
        assert auditor_with.system_prompt == "You are a test assistant."
        
        # Without system prompt
        auditor_without = ModelAuditor(
            model="gpt-4o-mini",
            provider="openai",
            judge_model="gpt-4o-mini",
            judge_provider="openai",
        )
        assert auditor_without.system_prompt is None


def test_model_auditor_separate_judge_provider():
    """Test that ModelAuditor can use different providers for judge and target."""
    with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
        # Create different mock providers for target and judge
        mock_target = MagicMock()
        mock_target.model = "target-model"
        mock_target.name = "Anthropic"
        
        mock_judge = MagicMock()
        mock_judge.model = "judge-model"
        mock_judge.name = "OpenAI"
        
        def mock_provider_factory(name, **kwargs):
            if name == "anthropic":
                return mock_target
            elif name == "openai":
                return mock_judge
            return mock_target
        
        mock_anyllm.create.side_effect = mock_provider_factory
        
        auditor = ModelAuditor(
            model="claude-sonnet-4-20250514",
            provider="anthropic",
            judge_model="gpt-4o-mini",
            judge_provider="openai"
        )
        
        assert auditor.target_client.name == "Anthropic"
        assert auditor.judge_client.name == "OpenAI"


def test_model_auditor_same_provider_for_target_and_judge():
    """When both use the same provider, both clients are backed by that provider."""
    with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
        mock_provider = MagicMock()
        mock_provider.model = "test-model"
        mock_provider.name = "Anthropic"
        mock_anyllm.create.return_value = mock_provider

        auditor = ModelAuditor(
            model="claude-sonnet-4-20250514",
            provider="anthropic",
            judge_model="claude-sonnet-4-20250514",
            judge_provider="anthropic",
        )

        assert auditor.target_client.name == "Anthropic"
        assert auditor.judge_client.name == "Anthropic"


def test_scenario_names():
    """Test that system prompt scenarios have expected names."""
    scenarios = get_scenarios("system_prompt")
    names = [s["name"] for s in scenarios]

    expected_names = [
        "System Prompt Override Attempt",
        "System Prompt Leak",
        "Instruction Contradiction",
        "Context Pollution",
        "System Prompt Persistence",
        "Authority Escalation",
        "Encoding Bypass",
        "Multi-turn Erosion",
    ]

    assert names == expected_names


def test_separate_auditor_model_creates_distinct_client():
    """auditor_model/provider → a third client is created, distinct from judge_client."""
    from simpleaudit.model_auditor import ModelAuditor
    from unittest.mock import patch as _patch

    created = []

    def make_client(*args, **kwargs):
        m = MagicMock()
        created.append(m)
        return m

    with _patch.object(ModelAuditor, "_create_anyllm_client", side_effect=make_client):
        auditor = ModelAuditor(
            model="target-model", provider="openai",
            judge_model="judge-model", judge_provider="openai",
            auditor_model="auditor-model", auditor_provider="openai",
        )

    assert len(created) == 3
    assert auditor.target_client is not auditor.judge_client
    assert auditor.auditor_client is not auditor.judge_client
    assert auditor.auditor_client is not auditor.target_client


def test_no_auditor_model_reuses_judge_as_auditor():
    """Without auditor_model, auditor_client is the same object as judge_client."""
    from simpleaudit.model_auditor import ModelAuditor
    from unittest.mock import patch as _patch

    created = []

    def make_client(*args, **kwargs):
        m = MagicMock()
        created.append(m)
        return m

    with _patch.object(ModelAuditor, "_create_anyllm_client", side_effect=make_client):
        auditor = ModelAuditor(
            model="target-model", provider="openai",
            judge_model="judge-model", judge_provider="openai",
        )

    assert len(created) == 2
    assert auditor.auditor_client is auditor.judge_client


# ---------------------------------------------------------------------------
# run_async records an ERROR result instead of aborting the whole batch
# ---------------------------------------------------------------------------

def _target_that_raises_on(marker: str):
    from tests.fakes import FakeClient

    def fn(**kwargs):
        text = " ".join(m.get("content", "") for m in kwargs.get("messages", []))
        if marker in text:
            raise RuntimeError("simulated API failure")
        return "A safe and helpful response."
    return FakeClient(fn)


def test_run_async_isolates_failing_scenario():
    from tests.fakes import fixed_severity_judge, make_auditor

    auditor = make_auditor(
        target=_target_that_raises_on("boom"),
        judge=fixed_severity_judge("pass"),
        max_turns=1,
    )
    scenarios = [
        {"name": "ok", "description": "d1", "test_prompt": "a calm question"},
        {"name": "fails", "description": "d2", "test_prompt": "this is boom"},
    ]
    results = asyncio.run(auditor.run_async(scenarios=scenarios, max_turns=1))

    assert len(results) == 2
    by_name = {r.scenario_name: r for r in results}
    assert by_name["ok"].severity == "pass"
    assert by_name["fails"].severity == "ERROR"
    assert "simulated API failure" in by_name["fails"].issues_found[0]


def test_run_async_all_failing_still_completes():
    from tests.fakes import fixed_severity_judge, make_auditor

    auditor = make_auditor(
        target=_target_that_raises_on("boom"),
        judge=fixed_severity_judge("pass"),
        max_turns=1,
    )
    scenarios = [
        {"name": "s1", "description": "d", "test_prompt": "boom one"},
        {"name": "s2", "description": "d", "test_prompt": "boom two"},
    ]
    results = asyncio.run(auditor.run_async(scenarios=scenarios, max_turns=1))
    assert len(results) == 2
    assert all(r.severity == "ERROR" for r in results)
    assert results.severity_distribution.get("ERROR") == 2


# --- judge_fields tests ---------------------------------------------------

def test_build_judge_schema_default():
    """Default schema includes all five fields."""
    from simpleaudit.model_auditor import build_judge_schema, DEFAULT_JUDGE_RESPONSE_SCHEMA
    schema = build_judge_schema(None)
    assert schema == DEFAULT_JUDGE_RESPONSE_SCHEMA
    assert set(schema["properties"].keys()) == {
        "severity", "issues_found", "positive_behaviors", "summary", "recommendations"
    }


def test_build_judge_schema_restricted():
    """Restricted fields produce a schema with only those fields + severity."""
    from simpleaudit.model_auditor import build_judge_schema
    schema = build_judge_schema(["issues_found"])
    assert set(schema["properties"].keys()) == {"severity", "issues_found"}
    assert schema["required"] == ["severity", "issues_found"]


def test_build_judge_schema_severity_always_present():
    """Severity is always included even if not in the fields list."""
    from simpleaudit.model_auditor import build_judge_schema
    schema = build_judge_schema(["summary"])
    assert "severity" in schema["properties"]
    assert "summary" in schema["properties"]
    assert "issues_found" not in schema["properties"]


def test_build_judge_json_snippet_default():
    """Default snippet contains all five fields."""
    from simpleaudit.model_auditor import build_judge_json_snippet
    snippet = build_judge_json_snippet(None)
    assert '"severity"' in snippet
    assert '"issues_found"' in snippet
    assert '"positive_behaviors"' in snippet
    assert '"summary"' in snippet
    assert '"recommendations"' in snippet


def test_build_judge_json_snippet_restricted():
    """Restricted snippet only contains the requested fields."""
    from simpleaudit.model_auditor import build_judge_json_snippet
    snippet = build_judge_json_snippet(["issues_found"])
    assert '"severity"' in snippet
    assert '"issues_found"' in snippet
    assert '"summary"' not in snippet
    assert '"recommendations"' not in snippet
    assert '"positive_behaviors"' not in snippet


def test_model_auditor_judge_fields_stored():
    """judge_fields is stored on the instance."""
    with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
        mock_anyllm.create.return_value = MagicMock()
        auditor = ModelAuditor(
            model="m", provider="openai",
            judge_model="j", judge_provider="openai",
            judge_fields=["severity", "issues_found"],
        )
        assert auditor.judge_fields == ["severity", "issues_found"]


def test_model_auditor_judge_fields_overrides_schema():
    """judge_fields overrides the response schema even when a judge config is used."""
    with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
        mock_anyllm.create.return_value = MagicMock()
        auditor = ModelAuditor(
            model="m", provider="openai",
            judge_model="j", judge_provider="openai",
            judge="safety",
            judge_fields=["severity", "summary"],
        )
        # Schema should only have severity + summary
        assert set(auditor.judge_response_schema["properties"].keys()) == {"severity", "summary"}
        assert auditor.judge_response_schema["required"] == ["severity", "summary"]


def test_model_auditor_judge_fields_none_keeps_default():
    """When judge_fields is None, the default schema is used."""
    with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
        mock_anyllm.create.return_value = MagicMock()
        auditor = ModelAuditor(
            model="m", provider="openai",
            judge_model="j", judge_provider="openai",
        )
        assert auditor.judge_fields is None
        assert auditor.judge_response_schema is None  # no explicit schema set
