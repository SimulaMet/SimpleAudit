"""
Tests for API key configuration in ModelAuditor class.

The Auditor class for HTTP endpoint testing has been removed.
Use ModelAuditor for direct API testing with different providers.

Run with: pytest tests/test_target_api_key.py
"""

import pytest
from simpleaudit import ModelAuditor

# Check if openai is available
try:
    import openai
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

@pytest.mark.skipif(not HAS_OPENAI, reason="openai package not installed")
def test_model_auditor_with_custom_base_url():
    """Test that ModelAuditor can use custom base_url (e.g., vLLM, Ollama)."""
    from unittest.mock import patch

    with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
        # Create auditor pointing to custom vLLM/Ollama endpoint
        auditor = ModelAuditor(
            model="default",
            provider="openai",
            judge_model="default",
            judge_provider="openai",
            base_url="http://localhost:8000/v1",
            api_key="mock-key",
        )

        # Verify configuration
        assert auditor.target_model == "default"

        # The header-support probe (api_key="probe") is an internal
        # AnyLLM.create call that precedes the real clients, so select the
        # target/judge clients by their credentials rather than by position.
        real_calls = [
            c for c in mock_anyllm.create.call_args_list
            if c.kwargs.get("api_key") != "probe"
        ]
        target_args, target_kwargs = real_calls[0]
        assert target_args == ("openai",)
        assert target_kwargs["api_base"] == "http://localhost:8000/v1"
        assert target_kwargs["api_key"] == "mock-key"

        # The judge got no explicit credentials, so none are forwarded.
        judge_args, judge_kwargs = real_calls[1]
        assert judge_args == ("openai",)
        assert "api_base" not in judge_kwargs
        assert "api_key" not in judge_kwargs


def test_model_auditor_api_key_handling():
    """Test that ModelAuditor properly handles API keys."""
    from unittest.mock import patch

    with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
        # Test with explicit API key
        auditor = ModelAuditor(
            model="gpt-4",
            provider="openai",
            judge_model="gpt-4",
            judge_provider="openai",
            api_key="test-key",
        )

        assert auditor.target_model == "gpt-4"

        # The header-support probe (api_key="probe") is an internal
        # AnyLLM.create call that precedes the real clients, so select the
        # target client by its credentials rather than by position.
        real_calls = [
            c for c in mock_anyllm.create.call_args_list
            if c.kwargs.get("api_key") != "probe"
        ]
        # The target client must be created with the explicit key; no
        # base_url was given, so api_base must not be forwarded.
        target_args, target_kwargs = real_calls[0]
        assert target_args == ("openai",)
        assert target_kwargs["api_key"] == "test-key"
        assert "api_base" not in target_kwargs
