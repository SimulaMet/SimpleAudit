"""
SimpleAudit - Lightweight AI Safety Auditing Framework

A simple, effective tool for red-teaming AI systems using LLMs as auditor and judge.

Supports multiple providers:
- Anthropic (Claude) - default
- OpenAI (GPT-4, GPT-5, etc.)
- Grok (xAI)

Usage:
    # Audit HTTP endpoint
    from simpleaudit import Auditor
    auditor = Auditor(target="http://localhost:8000/v1/chat/completions")
    results = auditor.run("safety")
    
    # Audit model directly via API
    from simpleaudit import ModelAuditor
    auditor = ModelAuditor(provider="anthropic", system_prompt="You are helpful.")
    results = auditor.run("system_prompt")
"""

__version__ = "0.1.0"
__author__ = "SimpleAudit Contributors"

from .auditor import Auditor
from .model_auditor import ModelAuditor
from .results import AuditResults, AuditResult
from .scenarios import get_scenarios, list_scenario_packs
from .providers import (
    LLMProvider,
    AnthropicProvider,
    OpenAIProvider,
    GrokProvider,
    HuggingFaceProvider,
    OllamaProvider,
    get_provider,
    PROVIDERS,
)

__all__ = [
    "Auditor",
    "ModelAuditor",
    "AuditResults", 
    "AuditResult",
    "get_scenarios",
    "list_scenario_packs",
    # Provider classes
    "LLMProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "GrokProvider",
    "HuggingFaceProvider",
    "OllamaProvider",
    "get_provider",
    "PROVIDERS",
]

