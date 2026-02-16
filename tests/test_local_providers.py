"""
Tests for local and open-source model providers (Ollama, vLLM, etc.).

HuggingFace is no longer directly supported. Use vLLM or Ollama for open-source models.

Run with: pytest tests/test_local_providers.py -v
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from simpleaudit import ModelAuditor
from simpleaudit.utils import parse_json_response


class TestOllamaProvider:
    """Tests for using Ollama with ModelAuditor."""
    
    @pytest.mark.skipif(
        True,  # Skip - requires actual Ollama server running
        reason="Requires Ollama server running on localhost:11434"
    )
    def test_model_auditor_with_ollama(self):
        """Test ModelAuditor with Ollama provider."""
        auditor = ModelAuditor(
            model="llama3.2",
            provider="ollama",
            judge_model="llama3.2",
            judge_provider="ollama",
            system_prompt="You are a helpful assistant.",
        )
        assert auditor.target_model == "llama3.2"


class TestVLLMProvider:
    """Tests for using vLLM with ModelAuditor.
    
    vLLM is OpenAI-compatible, so use provider='openai' with custom base_url.
    """
    
    @pytest.mark.skipif(
        True,  # Skip - requires actual vLLM server running
        reason="Requires vLLM server running on localhost:8000"
    )
    def test_model_auditor_with_vllm(self):
        """Test ModelAuditor with vLLM server."""
        auditor = ModelAuditor(
            model="default",
            provider="openai",
            judge_model="default",
            judge_provider="openai",
            base_url="http://localhost:8000/v1",
            api_key="any-value",  # vLLM doesn't require real API key
            system_prompt="You are a helpful assistant.",
        )
        assert auditor.target_model == "default"


class TestJsonParsing:
    """Tests for the robust JSON parsing utility."""
    
    def test_parse_valid_json(self):
        """Test parsing valid JSON."""
        response = '{"severity": "high", "issues_found": ["issue1"], "summary": "Test"}'
        result = parse_json_response(response)
        
        assert result["severity"] == "high"
        assert result["issues_found"] == ["issue1"]
        assert result["summary"] == "Test"
    
    def test_parse_json_in_code_block(self):
        """Test parsing JSON in markdown code block."""
        response = '''```json
{"severity": "pass", "issues_found": [], "summary": "All good"}
```'''
        result = parse_json_response(response)
        
        assert result["severity"] == "pass"
        assert result["issues_found"] == []
    
    def test_parse_json_with_surrounding_text(self):
        """Test parsing JSON with text before/after."""
        response = '''Here's my analysis:
{"severity": "low", "issues_found": ["minor"], "summary": "Minor issue"}
That's all.'''
        result = parse_json_response(response)
        
        assert result["severity"] == "low"
        assert result["issues_found"] == ["minor"]
    
    def test_parse_malformed_json_extracts_severity(self):
        """Test that malformed JSON still extracts severity from text."""
        response = "The severity is critical because the model provided dangerous information."
        result = parse_json_response(response)
        
        assert result["severity"] == "critical"
        assert "The severity is critical" in result["summary"]
    
    def test_parse_pass_indicators(self):
        """Test detection of passing indicators in text."""
        response = "The model handled the request appropriately and safely refused."
        result = parse_json_response(response)
        
        assert result["severity"] == "pass"
    
    def test_parse_empty_response(self):
        """Test handling of empty response."""
        result = parse_json_response("")
        
        assert result["severity"] == "ERROR"
        assert "Could not parse" in result["issues_found"][0]
    
    def test_parse_validates_severity(self):
        """Test that invalid severity values are corrected."""
        response = '{"severity": "INVALID", "issues_found": [], "summary": "Test"}'
        result = parse_json_response(response)
        
        assert result["severity"] == "ERROR"  # Default fallback
