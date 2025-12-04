"""
SimpleAudit - Lightweight AI Safety Auditing Framework

A simple, effective tool for red-teaming AI systems using LLMs as auditor and judge.

Supports multiple providers:
- Anthropic (Claude) - default
- OpenAI (GPT-4, GPT-5, etc.)
- Grok (xAI)

Usage:
    from simpleaudit import Auditor
    
    # Default (Anthropic)
    auditor = Auditor(target="http://localhost:8000/v1/chat/completions")
    
    # With OpenAI
    auditor = Auditor(target="...", provider="openai")
    
    # With Grok
    auditor = Auditor(target="...", provider="grok")
    
    results = auditor.run("safety")
    results.summary()
"""

__version__ = "0.1.0"
__author__ = "SimpleAudit Contributors"

from .auditor import Auditor
from .results import AuditResults, AuditResult
from .scenarios import get_scenarios, list_scenario_packs
from .providers import (
    LLMProvider,
    AnthropicProvider,
    OpenAIProvider,
    GrokProvider,
    get_provider,
    PROVIDERS,
)

__all__ = [
    "Auditor",
    "AuditResults", 
    "AuditResult",
    "get_scenarios",
    "list_scenario_packs",
    # Provider classes
    "LLMProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "GrokProvider",
    "get_provider",
    "PROVIDERS",
]
