"""
LLM Provider abstraction for SimpleAudit.

This module provides a unified interface for different LLM providers:
- Anthropic (Claude)
- OpenAI (GPT-4, GPT-5, etc.)
- Grok (xAI)
- HuggingFace (local transformers)
- Ollama (local models)
"""

import os
import getpass
from abc import ABC, abstractmethod
from typing import Optional

# Lazy imports for optional dependencies
try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import openai
except ImportError:
    openai = None


def prompt_for_api_key(provider_name: str, env_var: str) -> str:
    """
    Prompt user for API key via console if environment variable is not set.
    
    Args:
        provider_name: Display name of the provider (e.g., "Anthropic", "OpenAI")
        env_var: Environment variable name to check first
    
    Returns:
        The API key (either from env var or user input)
    
    Raises:
        ValueError: If user provides empty input
    """
    api_key = os.environ.get(env_var)
    if api_key:
        return api_key
    
    print(f"\n{env_var} environment variable not found.")
    api_key = getpass.getpass(f"Enter your {provider_name} API key: ")
    
    if not api_key.strip():
        raise ValueError(f"{provider_name} API key is required.")
    
    return api_key.strip()


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""
    
    @abstractmethod
    def call(self, system: str, user: str) -> str:
        """
        Call the LLM with a system prompt and user message.
        
        Args:
            system: System prompt to set the LLM's behavior
            user: User message to respond to
        
        Returns:
            The LLM's response text
        """
        pass
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Return the provider name for display purposes."""
        pass


class AnthropicProvider(LLMProvider):
    """Anthropic Claude provider."""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-20250514",
        prompt_for_key: bool = True,
    ):
        """
        Initialize Anthropic provider.
        
        Args:
            api_key: Anthropic API key (or uses ANTHROPIC_API_KEY env var)
            model: Model to use (default: claude-sonnet-4-20250514)
            prompt_for_key: If True, prompt for key if not found in env
        """
        if anthropic is None:
            raise ImportError(
                "anthropic package required. Install with: pip install anthropic"
            )
        
        if api_key:
            self._api_key = api_key
        elif prompt_for_key:
            self._api_key = prompt_for_api_key("Anthropic", "ANTHROPIC_API_KEY")
        else:
            self._api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not self._api_key:
                raise ValueError(
                    "Anthropic API key required. Either pass api_key parameter "
                    "or set ANTHROPIC_API_KEY environment variable."
                )
        
        self.model = model
        self._client = anthropic.Anthropic(api_key=self._api_key)
    
    @property
    def name(self) -> str:
        return "Anthropic"
    
    def call(self, system: str, user: str) -> str:
        """Call Claude with system and user prompts."""
        response = self._client.messages.create(
            model=self.model,
            max_tokens=2048,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text


class OpenAIProvider(LLMProvider):
    """OpenAI provider (GPT-4, GPT-5, etc.)."""
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o",
        prompt_for_key: bool = True,
        base_url: Optional[str] = None,
    ):
        """
        Initialize OpenAI provider.
        
        Args:
            api_key: OpenAI API key (or uses OPENAI_API_KEY env var)
            model: Model to use (default: gpt-4o)
            prompt_for_key: If True, prompt for key if not found in env
            base_url: Optional custom base URL (for enterprise/compatible endpoints)
        """
        if openai is None:
            raise ImportError(
                "openai package required. Install with: pip install openai "
                "or pip install simpleaudit[openai]"
            )
        
        if api_key:
            self._api_key = api_key
        elif prompt_for_key:
            self._api_key = prompt_for_api_key("OpenAI", "OPENAI_API_KEY")
        else:
            self._api_key = os.environ.get("OPENAI_API_KEY")
            if not self._api_key:
                raise ValueError(
                    "OpenAI API key required. Either pass api_key parameter "
                    "or set OPENAI_API_KEY environment variable."
                )
        
        self.model = model
        self.base_url = base_url
        # Allow optional custom base URL (for enterprise/compat clients)
        if base_url:
            self._client = openai.OpenAI(api_key=self._api_key, base_url=base_url)
        else:
            self._client = openai.OpenAI(api_key=self._api_key)
    
    @property
    def name(self) -> str:
        return "OpenAI"
    
    def get_base_url(self) -> str:
        """Return the configured base URL (useful for debugging)."""
        return self.base_url or "https://api.openai.com/v1"
    
    def call(self, system: str, user: str) -> str:
        """Call OpenAI with system and user prompts."""
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content


class GrokProvider(LLMProvider):
    """Grok provider (xAI)."""
    
    XAI_BASE_URL = "https://api.x.ai/v1"
    
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "grok-3",
        prompt_for_key: bool = True,
        base_url: Optional[str] = None,
    ):
        """
        Initialize Grok provider via xAI API.
        
        Args:
            api_key: xAI API key (or uses XAI_API_KEY env var)
            model: Model to use (default: grok-3)
            prompt_for_key: If True, prompt for key if not found in env
            base_url: Optional custom base URL (defaults to xAI API)
        """
        if openai is None:
            raise ImportError(
                "openai package required for Grok provider. Install with: "
                "pip install openai or pip install simpleaudit[openai]"
            )
        
        if api_key:
            self._api_key = api_key
        elif prompt_for_key:
            self._api_key = prompt_for_api_key("xAI (Grok)", "XAI_API_KEY")
        else:
            self._api_key = os.environ.get("XAI_API_KEY")
            if not self._api_key:
                raise ValueError(
                    "xAI API key required. Either pass api_key parameter "
                    "or set XAI_API_KEY environment variable."
                )
        
        self.model = model
        # Use OpenAI client with xAI base URL (xAI uses OpenAI-compatible API)
        client_base = base_url or self.XAI_BASE_URL
        self._client = openai.OpenAI(
            api_key=self._api_key,
            base_url=client_base,
        )
    
    @property
    def name(self) -> str:
        return "Grok"
    
    def call(self, system: str, user: str) -> str:
        """Call Grok with system and user prompts."""
        response = self._client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content


class HuggingFaceProvider(LLMProvider):
    """HuggingFace transformers provider for local model inference.
    
    Runs models directly using the transformers library. Suitable for
    local inference on GPU or CPU.
    
    Requires: pip install simpleaudit[huggingface]
    """
    
    def __init__(
        self,
        model: str = "meta-llama/Llama-3.2-1B-Instruct",
        device: Optional[str] = None,
        torch_dtype: Optional[str] = None,
        max_new_tokens: int = 2048,
        **pipeline_kwargs,
    ):
        """
        Initialize HuggingFace transformers provider.
        
        Args:
            model: HuggingFace model ID (e.g., "meta-llama/Llama-3.2-1B-Instruct")
            device: Device to run on ("cuda", "cpu", "mps") or None for auto
            torch_dtype: Torch dtype ("float16", "bfloat16", "float32") or None
            max_new_tokens: Maximum tokens to generate (default: 2048)
            **pipeline_kwargs: Additional kwargs passed to pipeline()
        """
        try:
            import transformers
            import torch
        except ImportError:
            raise ImportError(
                "transformers and torch packages required. Install with: "
                "pip install simpleaudit[huggingface]"
            )
        
        self.model = model
        self.max_new_tokens = max_new_tokens
        
        # Build pipeline kwargs
        pipe_kwargs = {"model": model, "task": "text-generation"}
        
        if device is not None:
            pipe_kwargs["device"] = device
        elif torch.cuda.is_available():
            pipe_kwargs["device"] = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            pipe_kwargs["device"] = "mps"
        
        if torch_dtype is not None:
            dtype_map = {
                "float16": torch.float16,
                "bfloat16": torch.bfloat16,
                "float32": torch.float32,
            }
            pipe_kwargs["torch_dtype"] = dtype_map.get(torch_dtype, torch.float16)
        
        pipe_kwargs.update(pipeline_kwargs)
        
        self._pipeline = transformers.pipeline(**pipe_kwargs)
    
    @property
    def name(self) -> str:
        return "HuggingFace"
    
    def call(self, system: str, user: str) -> str:
        """Call HuggingFace model with system and user prompts."""
        # Try using chat template if available
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        
        try:
            # Use chat template via pipeline
            outputs = self._pipeline(
                messages,
                max_new_tokens=self.max_new_tokens,
                do_sample=True,
                temperature=0.7,
                return_full_text=False,
            )
            return outputs[0]["generated_text"]
        except Exception:
            # Fallback: format manually if chat template fails
            if system:
                prompt = f"System: {system}\n\nUser: {user}\n\nAssistant:"
            else:
                prompt = f"User: {user}\n\nAssistant:"
            
            outputs = self._pipeline(
                prompt,
                max_new_tokens=self.max_new_tokens,
                do_sample=True,
                temperature=0.7,
                return_full_text=False,
            )
            return outputs[0]["generated_text"]


class OllamaProvider(LLMProvider):
    """Ollama provider for locally served models.
    
    Connects to a local Ollama instance. No API key required.
    
    Requires: Ollama running locally (ollama serve)
    """
    
    def __init__(
        self,
        model: str = "llama3.2",
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
    ):
        """
        Initialize Ollama provider.
        
        Args:
            model: Ollama model name (e.g., "llama3.2", "mistral", "codellama")
            base_url: Ollama server URL (default: http://localhost:11434)
            timeout: Request timeout in seconds (default: 120)
        """
        import httpx
        
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout)
        
        # Verify connection
        try:
            self._client.get(f"{self.base_url}/api/tags")
        except httpx.ConnectError:
            raise ConnectionError(
                f"Cannot connect to Ollama at {self.base_url}. "
                "Make sure Ollama is running: ollama serve"
            )
    
    @property
    def name(self) -> str:
        return "Ollama"
    
    def call(self, system: str, user: str) -> str:
        """Call Ollama with system and user prompts."""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        
        response = self._client.post(
            f"{self.base_url}/api/chat",
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
            },
        )
        response.raise_for_status()
        
        data = response.json()
        return data.get("message", {}).get("content", "")


# Provider registry for easy lookup
PROVIDERS = {
    "anthropic": AnthropicProvider,
    "claude": AnthropicProvider,  # Alias
    "openai": OpenAIProvider,
    "gpt": OpenAIProvider,  # Alias
    "grok": GrokProvider,
    "xai": GrokProvider,  # Alias
    "huggingface": HuggingFaceProvider,
    "hf": HuggingFaceProvider,  # Alias
    "ollama": OllamaProvider,
    "local": OllamaProvider,  # Alias
}


def get_provider(
    name: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    prompt_for_key: bool = True,
    **kwargs,
) -> LLMProvider:
    """
    Get a provider instance by name.
    
    Args:
        name: Provider name ("anthropic", "openai", "grok", "huggingface", 
              "ollama", or aliases)
        api_key: Optional API key override (not used for local providers)
        model: Optional model override
        prompt_for_key: If True, prompt for key if not found in env
        **kwargs: Additional provider-specific arguments (e.g., base_url)
    
    Returns:
        LLMProvider instance
    
    Raises:
        ValueError: If provider name is not recognized
    
    Examples:
        >>> # Default Anthropic provider
        >>> provider = get_provider("anthropic")
        
        >>> # OpenAI with custom base URL
        >>> provider = get_provider("openai", base_url="https://custom.api.com/v1")
        
        >>> # Local Ollama provider
        >>> provider = get_provider("ollama", model="llama3.2")
    """
    name_lower = name.lower()
    if name_lower not in PROVIDERS:
        available = ", ".join(sorted(set(PROVIDERS.keys())))
        raise ValueError(
            f"Unknown provider: {name}. Available providers: {available}"
        )
    
    provider_class = PROVIDERS[name_lower]
    
    # Local providers don't use API keys
    local_providers = (HuggingFaceProvider, OllamaProvider)
    
    if provider_class in local_providers:
        # Only pass model and additional kwargs for local providers
        init_kwargs = dict(kwargs)
        if model:
            init_kwargs["model"] = model
        return provider_class(**init_kwargs)
    else:
        # API-based providers
        init_kwargs = {"prompt_for_key": prompt_for_key}
        init_kwargs.update(kwargs)
        if api_key:
            init_kwargs["api_key"] = api_key
        if model:
            init_kwargs["model"] = model
        return provider_class(**init_kwargs)