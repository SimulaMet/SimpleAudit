"""
Tests for ModelAuditor class.

Run with: pytest tests/test_model_auditor.py -v
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from simpleaudit import ModelAuditor, get_scenarios, list_scenario_packs


def test_system_prompt_scenarios_available():
    """Test that system_prompt scenario pack is registered."""
    packs = list_scenario_packs()
    
    assert "system_prompt" in packs
    assert packs["system_prompt"] > 0
    
    # Should be included in 'all'
    total_without_system = packs["safety"] + packs["rag"] + packs["health"]
    assert packs["all"] == total_without_system + packs["system_prompt"]


def test_get_system_prompt_scenarios():
    """Test getting system_prompt scenarios."""
    scenarios = get_scenarios("system_prompt")
    
    assert isinstance(scenarios, list)
    assert len(scenarios) == 8
    
    for scenario in scenarios:
        assert "name" in scenario
        assert "description" in scenario


def test_model_auditor_init_requires_provider():
    """Test that ModelAuditor requires valid provider configuration."""
    import os
    
    # Temporarily remove API keys
    original_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
    original_openai = os.environ.pop("OPENAI_API_KEY", None)
    original_xai = os.environ.pop("XAI_API_KEY", None)
    
    try:
        with pytest.raises((ValueError, ImportError)):
            ModelAuditor(provider="anthropic", prompt_for_key=False)
    finally:
        if original_anthropic:
            os.environ["ANTHROPIC_API_KEY"] = original_anthropic
        if original_openai:
            os.environ["OPENAI_API_KEY"] = original_openai
        if original_xai:
            os.environ["XAI_API_KEY"] = original_xai


def test_model_auditor_system_prompt_handling():
    """Test that system_prompt parameter is properly stored."""
    with patch("simpleaudit.model_auditor.get_provider") as mock_get_provider:
        mock_provider = MagicMock()
        mock_provider.model = "test-model"
        mock_get_provider.return_value = mock_provider
        
        # With system prompt
        auditor_with = ModelAuditor(
            provider="anthropic",
            system_prompt="You are a test assistant."
        )
        assert auditor_with.system_prompt == "You are a test assistant."
        
        # Without system prompt
        auditor_without = ModelAuditor(provider="openai")
        assert auditor_without.system_prompt is None


def test_model_auditor_separate_judge_provider():
    """Test that ModelAuditor can use different providers for judge and target."""
    with patch("simpleaudit.model_auditor.get_provider") as mock_get_provider:
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
        
        mock_get_provider.side_effect = mock_provider_factory
        
        auditor = ModelAuditor(
            provider="anthropic",
            judge_provider="openai"
        )
        
        assert auditor.target_provider.name == "Anthropic"
        assert auditor.judge_provider.name == "OpenAI"


def test_model_auditor_same_provider_when_no_judge():
    """Test that judge defaults to same provider as target."""
    with patch("simpleaudit.model_auditor.get_provider") as mock_get_provider:
        mock_provider = MagicMock()
        mock_provider.model = "test-model"
        mock_provider.name = "Anthropic"
        mock_get_provider.return_value = mock_provider
        
        auditor = ModelAuditor(provider="anthropic")
        
        # Should be same instance
        assert auditor.target_provider is auditor.judge_provider


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
