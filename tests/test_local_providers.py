"""
Tests for local model providers (HuggingFace and Ollama).

Run with: pytest tests/test_local_providers.py -v
"""

import pytest
from unittest.mock import Mock, patch, MagicMock

from simpleaudit import (
    get_provider, 
    PROVIDERS,
    HuggingFaceProvider,
    OllamaProvider,
)
from simpleaudit.utils import parse_json_response


class TestProviderRegistry:
    """Test that new providers are registered correctly."""
    
    def test_huggingface_in_registry(self):
        """Test that HuggingFace provider is registered."""
        assert "huggingface" in PROVIDERS
        assert "hf" in PROVIDERS  # Alias
        assert PROVIDERS["huggingface"] == HuggingFaceProvider
        assert PROVIDERS["hf"] == HuggingFaceProvider
    
    def test_ollama_in_registry(self):
        """Test that Ollama provider is registered."""
        assert "ollama" in PROVIDERS
        assert "local" in PROVIDERS  # Alias
        assert PROVIDERS["ollama"] == OllamaProvider
        assert PROVIDERS["local"] == OllamaProvider


class TestHuggingFaceProvider:
    """Tests for HuggingFaceProvider."""
    
    def test_init_requires_transformers(self):
        """Test that HuggingFaceProvider requires transformers package."""
        # This is tested implicitly - if transformers is not installed,
        # the import in __init__ will raise ImportError
        pass
    
    def test_huggingface_provider_exists(self):
        """Test that HuggingFaceProvider class exists and is exported."""
        assert HuggingFaceProvider is not None
        assert hasattr(HuggingFaceProvider, 'call')
        assert hasattr(HuggingFaceProvider, 'name')
    
    @pytest.mark.skipif(
        True,  # Skip if transformers not available
        reason="Requires transformers and torch packages"
    )
    def test_init_with_model(self):
        """Test HuggingFaceProvider initialization with custom model."""
        # This test requires actual transformers/torch installation
        provider = HuggingFaceProvider(model="test-model")
        assert provider.model == "test-model"
        assert provider.name == "HuggingFace"


class TestOllamaProvider:
    """Tests for OllamaProvider."""
    
    @patch("httpx.Client")
    def test_init_with_defaults(self, mock_client_class):
        """Test OllamaProvider initialization with defaults."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        provider = OllamaProvider()
        
        assert provider.model == "llama3.2"
        assert provider.base_url == "http://localhost:11434"
        assert provider.name == "Ollama"
    
    @patch("httpx.Client")
    def test_init_with_custom_model(self, mock_client_class):
        """Test OllamaProvider initialization with custom model."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        provider = OllamaProvider(model="mistral", base_url="http://custom:11434")
        
        assert provider.model == "mistral"
        assert provider.base_url == "http://custom:11434"
    
    @patch("httpx.Client")
    def test_call_formats_messages(self, mock_client_class):
        """Test that call properly formats messages."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "message": {"content": "Hello! How can I help?"}
        }
        mock_client.post.return_value = mock_response
        mock_client_class.return_value = mock_client
        
        provider = OllamaProvider()
        result = provider.call("You are helpful.", "Hello!")
        
        assert result == "Hello! How can I help?"
        
        # Check the request was formatted correctly
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        json_data = call_kwargs[1]["json"]
        
        assert json_data["model"] == "llama3.2"
        assert len(json_data["messages"]) == 2
        assert json_data["messages"][0]["role"] == "system"
        assert json_data["messages"][1]["role"] == "user"


class TestParseJsonResponse:
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
        
        assert result["severity"] == "medium"
        assert "Could not parse" in result["issues_found"][0]
    
    def test_parse_validates_severity(self):
        """Test that invalid severity values are corrected."""
        response = '{"severity": "INVALID", "issues_found": [], "summary": "Test"}'
        result = parse_json_response(response)
        
        assert result["severity"] == "medium"  # Default fallback


class TestGetProviderWithLocalModels:
    """Test get_provider function with local models."""
    
    @patch("httpx.Client")
    def test_get_ollama_provider(self, mock_client_class):
        """Test getting Ollama provider via get_provider."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        provider = get_provider("ollama", model="mistral")
        
        assert isinstance(provider, OllamaProvider)
        assert provider.model == "mistral"
    
    @patch("httpx.Client")
    def test_get_local_alias(self, mock_client_class):
        """Test getting Ollama provider via 'local' alias."""
        mock_client = MagicMock()
        mock_client_class.return_value = mock_client
        
        provider = get_provider("local")
        
        assert isinstance(provider, OllamaProvider)
