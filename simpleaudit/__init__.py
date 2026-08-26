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

    # Audit a model directly via its API
    auditor = ModelAuditor(
        model="gpt-4o-mini",
        provider="openai",
        judge_model="gpt-4o",
        judge_provider="openai",
        system_prompt="You are helpful.",
    )
    results = auditor.run(get_scenarios("safety"))
    results.summary()
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("simpleaudit")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.1.9"
__author__ = "SimpleAudit Contributors"

from .model_auditor import ModelAuditor
from .results import AuditResults, AuditResult
from .scenarios import get_scenarios, list_scenario_packs
from .judges import get_judge, list_judge_configs
from .experiment import AuditExperiment
from .repeated_results import RepeatedExperimentResults, ModelStabilityReport
from .cross_judge import CrossJudgeExperiment, CrossJudgeResults, compare_judges

__all__ = [
    "ModelAuditor",
    "AuditResults",
    "AuditResult",
    "get_scenarios",
    "list_scenario_packs",
    "get_judge",
    "list_judge_configs",
    "AuditExperiment",
    "RepeatedExperimentResults",
    "ModelStabilityReport",
    "CrossJudgeExperiment",
    "CrossJudgeResults",
    "compare_judges",
]

