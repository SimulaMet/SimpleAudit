"""
SimpleAudit - Lightweight AI Safety Auditing Framework

A simple, effective tool for red-teaming AI systems using LLMs as auditor and judge.

Supports multiple providers:
- Anthropic (Claude) - default
- OpenAI (GPT-4, GPT-5, etc.)
- Grok (xAI)
- Ollama (local models)
- vLLM (local model serving)
- Any OpenAI-compatible API

Usage:
    from simpleaudit import ModelAuditor, get_scenarios
    
    # Audit model directly via API
    auditor = ModelAuditor(provider="anthropic", system_prompt="You are helpful.")
    results = auditor.run(get_scenarios("safety"))
    results.summary()
"""

__version__ = "0.1.0"
__author__ = "SimpleAudit Contributors"

from .model_auditor import ModelAuditor
from .results import AuditResults, AuditResult
from .scenarios import get_scenarios, list_scenario_packs
from .experiment import AuditExperiment
__all__ = [
    "ModelAuditor",
    "AuditResults", 
    "AuditResult",
    "get_scenarios",
    "list_scenario_packs",
    "AuditExperiment",
]

