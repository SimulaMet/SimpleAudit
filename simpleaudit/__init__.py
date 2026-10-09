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
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"
__author__ = "SimpleAudit Contributors"

from .model_auditor import ModelAuditor, ScenarioExecution
from .auditor import Auditor
from .targets import (
    CallableTarget,
    DecisionTarget,
    HTTPAppTarget,
    ModelTarget,
    Target,
    TargetContext,
    TargetResponse,
)
from .results import AuditResults, AuditResult
from .scenarios import get_scenarios, list_scenario_packs
from .judges import build_judge, customize_judge, get_judge, list_judge_configs
from .experiment import AuditExperiment, ExperimentEvent
from .repeated_results import (
    FRAGILE_THRESHOLD_DEFAULT,
    ModelStabilityReport,
    RepeatedExperimentResults,
    ScenarioStats,
    aggregate_severities,
)
from .cross_judge import CrossJudgeExperiment, CrossJudgeResults, compare_judges
from .stats import DEFAULT_Z, two_proportion_z, wilson_interval
from .reframing import (
    PanelResults,
    PanelVerdict,
    PromptVariant,
    ReframingResults,
    SampleStats,
    StoredRecord,
    VariantEffect,
    load_stored_records,
    make_judge_client,
    reframing_check,
    reframing_check_async,
    rejudge,
    rejudge_async,
)
from .checklist import postprocess_checklist, severity_by_name
from .perturbations import (
    PERTURBATIONS,
    apologetic_opener,
    authority_claim,
    compose,
    hedging_disclaimer,
    perturbation_variants,
    self_certification,
    verbose_padding,
)

__all__ = [
    "ModelAuditor",
    "ScenarioExecution",
    "Auditor",
    "Target",
    "TargetContext",
    "TargetResponse",
    "ModelTarget",
    "HTTPAppTarget",
    "CallableTarget",
    "DecisionTarget",
    "AuditResults",
    "AuditResult",
    "get_scenarios",
    "list_scenario_packs",
    "get_judge",
    "build_judge",
    "customize_judge",
    "list_judge_configs",
    "AuditExperiment",
    "ExperimentEvent",
    "RepeatedExperimentResults",
    "ModelStabilityReport",
    "ScenarioStats",
    "FRAGILE_THRESHOLD_DEFAULT",
    "aggregate_severities",
    "wilson_interval",
    "two_proportion_z",
    "DEFAULT_Z",
    "CrossJudgeExperiment",
    "CrossJudgeResults",
    "compare_judges",
    "PromptVariant",
    "ReframingResults",
    "StoredRecord",
    "load_stored_records",
    "reframing_check",
    "reframing_check_async",
    "SampleStats",
    "VariantEffect",
    "PanelVerdict",
    "PanelResults",
    "rejudge",
    "rejudge_async",
    "make_judge_client",
    "PERTURBATIONS",
    "perturbation_variants",
    "compose",
    "apologetic_opener",
    "hedging_disclaimer",
    "verbose_padding",
    "authority_claim",
    "self_certification",
    "postprocess_checklist",
    "severity_by_name",
]

