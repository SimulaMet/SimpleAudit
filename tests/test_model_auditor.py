"""
Tests for ModelAuditor class.

Run with: pytest tests/test_model_auditor.py -v
"""

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
    total_without_system = packs["safety"] + packs["rag"] + packs["health"] + packs["helpmed"] + packs["ung"]
    assert packs["all"] == total_without_system + packs["system_prompt"]


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


def test_model_auditor_same_provider_when_no_judge():
    """Test that judge defaults to same provider as target."""
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
        
        # Should be same instance (both should use same provider mock)
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
