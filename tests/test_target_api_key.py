"""
Test for target_api_key parameter in Auditor class.

Run with: pytest tests/test_target_api_key.py
"""

import pytest
from simpleaudit import Auditor


def test_target_api_key_passed_to_client():
    """Test that target_api_key is properly passed to TargetClient."""
    # Create auditor with a target_api_key
    auditor = Auditor(
        target="http://localhost:8000/v1/chat/completions",
        provider="openai",
        api_key="test-provider-key",
        target_api_key="test-target-key",
        prompt_for_key=False,
    )
    
    # Verify that the target client received the API key
    assert auditor.target.api_key == "test-target-key"
    
    # Verify that the Authorization header is set in the client
    assert "Authorization" in auditor.target.client.headers
    assert auditor.target.client.headers["Authorization"] == "Bearer test-target-key"


def test_target_without_api_key():
    """Test that target client works without API key (for public endpoints)."""
    auditor = Auditor(
        target="http://localhost:8000/v1/chat/completions",
        provider="openai",
        api_key="test-provider-key",
        prompt_for_key=False,
    )
    
    # Verify that the target client has no API key
    assert auditor.target.api_key is None
    
    # Verify that the Authorization header is not set
    assert "Authorization" not in auditor.target.client.headers


def test_target_api_key_none_explicit():
    """Test that explicitly passing None for target_api_key works."""
    auditor = Auditor(
        target="http://localhost:8000/v1/chat/completions",
        provider="openai",
        api_key="test-provider-key",
        target_api_key=None,
        prompt_for_key=False,
    )
    
    # Verify that the target client has no API key
    assert auditor.target.api_key is None
