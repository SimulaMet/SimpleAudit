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
    
    with patch("simpleaudit.model_auditor.AnyLLM"):
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


def test_model_auditor_api_key_handling():
    """Test that ModelAuditor properly handles API keys."""
    from unittest.mock import patch
    
    with patch("simpleaudit.model_auditor.AnyLLM"):
        # Test with explicit API key
        auditor = ModelAuditor(
            model="gpt-4",
            provider="openai",
            judge_model="gpt-4",
            judge_provider="openai",
            api_key="test-key",
        )
        
        assert auditor.target_model == "gpt-4"
