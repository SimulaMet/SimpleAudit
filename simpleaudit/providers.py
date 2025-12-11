"""
LLM Provider abstraction for SimpleAudit.

This module provides a unified interface for different LLM providers:
- Anthropic (Claude)
- OpenAI (GPT-4, GPT-5, etc.)
- Grok (xAI)
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


# Provider registry for easy lookup
PROVIDERS = {
    "anthropic": AnthropicProvider,
    "claude": AnthropicProvider,  # Alias
    "openai": OpenAIProvider,
    "gpt": OpenAIProvider,  # Alias
    "grok": GrokProvider,
    "xai": GrokProvider,  # Alias
}


def get_provider(
    name: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    prompt_for_key: bool = True,
    base_url: Optional[str] = None,
) -> LLMProvider:
    """
    Get a provider instance by name.
    
    Args:
        name: Provider name ("anthropic", "openai", "grok", or aliases)
        api_key: Optional API key override
        model: Optional model override
        prompt_for_key: If True, prompt for key if not found in env
    
    Returns:
        LLMProvider instance
    
    Raises:
        ValueError: If provider name is not recognized
    """
    name_lower = name.lower()
    if name_lower not in PROVIDERS:
        available = ", ".join(sorted(set(PROVIDERS.keys())))
        raise ValueError(
            f"Unknown provider: {name}. Available providers: {available}"
        )
    
    provider_class = PROVIDERS[name_lower]
    
    kwargs = {"prompt_for_key": prompt_for_key}
    if api_key:
        kwargs["api_key"] = api_key
    if model:
        kwargs["model"] = model
    # Only pass base_url to providers that support it
    if provider_class in (OpenAIProvider, GrokProvider) and base_url:
        kwargs["base_url"] = base_url
    
    return provider_class(**kwargs)
