"""
Built-in judge configurations for SimpleAudit.

Available judges:
- safety:      Constitutional AI safety evaluation (Bai et al., 2022)
                Severity: critical | high | medium | low | pass
- abstention:  Refusal/abstention appropriateness, AbstentionBench taxonomy
                (Kirichenko et al., 2025). Score 1–10, with abstained/appropriate flags
- helpfulness: Response quality across four MT-Bench dimensions (Zheng et al., 2023)
                Score 1–10 with relevance, accuracy, clarity, completeness sub-scores
- factuality:  Hallucination and factual error detection (Liu et al., 2023)
                Score 1–10 with verdict and error lists

Score-based judges (abstention, helpfulness, factuality) emit a 1-10 score and
no severity; the framework derives AuditResult.severity from the score
(9-10 pass, 7-8 low, 5-6 medium, 3-4 high, 1-2 critical — see
utils.severity_from_score) so summaries and plots stay meaningful. The raw
judgment dict is stored unchanged.
- harm:        HELM Safety harm categorisation (Liang et al., 2022)
                harmful flag, category, severity across six harm types
- helsedir_sexhealth_no:
                Norwegian sexual-health judge for young users — generic variant.
                Six criteria from Helsedirektoratet domain review (2026), neutral 1–5
                scale mapped silently to {critical, high, medium, low, pass}.
- helsedir_sexhealth_no_rag:
                Same as above, RAG framing — criteria reference «dokumentene»
                verbatim per the reviewer's wording. Use for bots with a fixed
                source corpus.
- binary_abstention:
                Language-agnostic binary classifier: did the model abstain
                (decline to deliver the substantive content requested),
                yes or no? Emits {abstained, reasoning} only — no severity.
                Declares its own response_schema; works with json_format=True.
- checklist:   Evidence-anchored checklist (RULERS 2026, TICK 2024, GuidedBench
                2025). The judge reports met/violated/not_applicable per
                expected_behavior item with a verbatim quote; code verifies the
                quotes and derives severity from verified violations, capped by
                the scenario's designed severity. Declares `postprocess` and
                `requires_expected_behavior` (scenarios without expectations
                fall back to the default judge).

Every config declares ``output``, the shape of its grade (see OUTPUT_KINDS):
"severity" (the ladder), "score" (1–10, severity derived), "binary" (a yes/no
classification, graded against scenario ground truth or UNGRADED) or
"checklist" (per-expectation items, severity derived).

Every config also declares ``criteria`` (what to evaluate) and
``format_prompt`` (the output contract), and ``judge_prompt`` is the two
joined. customize_judge() swaps a config's criteria and keeps its format;
build_judge() writes new criteria in a generic severity, score, binary or
checklist format. Both return config dicts accepted wherever a judge name is
(see judges/compose.py).

DEFAULT_JUDGE describes what grades a run when no config is named (it is not
registered in JUDGE_CONFIGS: passing its prompts explicitly would take the
custom-prompt path, which formats the judge call differently).

A config may carry two optional hooks read by ModelAuditor and the judge-only
paths in reframing: `postprocess(judgment, *, conversation, expected_behavior,
scenario_meta)` transforms the parsed judge output, and
`requires_expected_behavior=True` routes scenarios without expected_behavior
to the default judge.

Usage:
    from simpleaudit import ModelAuditor

    # Use a named judge config
    auditor = ModelAuditor(..., judge="helpfulness")

    # Named judge + custom probe (probe_prompt overrides the config's probe_prompt)
    auditor = ModelAuditor(..., judge="factuality", probe_prompt="Ask about X...")

    # Fully custom (judge_prompt takes precedence over any named judge)
    auditor = ModelAuditor(..., judge_prompt="You are a custom judge...")
"""

from typing import Any, Dict, Union

from .safety import SAFETY_JUDGE
from .abstention import ABSTENTION_JUDGE
from .helpfulness import HELPFULNESS_JUDGE
from .factuality import FACTUALITY_JUDGE
from .harm import HARM_JUDGE
from .helsedir_sexhealth_no import HELSEDIR_SEXHEALTH_NO_JUDGE
from .helsedir_sexhealth_no_rag import HELSEDIR_SEXHEALTH_NO_RAG_JUDGE
from .binary_abstention import BINARY_ABSTENTION_JUDGE
from .checklist import CHECKLIST_JUDGE
from .groundedness import GROUNDEDNESS_JUDGE
from .default import DEFAULT_JUDGE
from .compose import BUILD_OUTPUTS, build_judge, compose_prompt, customize_judge, dimension_key

#: Values of a judge config's ``output`` key.
OUTPUT_KINDS = ("severity", "score", "binary", "checklist")


JUDGE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "safety":                     SAFETY_JUDGE,
    "abstention":                 ABSTENTION_JUDGE,
    "helpfulness":                HELPFULNESS_JUDGE,
    "factuality":                 FACTUALITY_JUDGE,
    "harm":                       HARM_JUDGE,
    "helsedir_sexhealth_no":      HELSEDIR_SEXHEALTH_NO_JUDGE,
    "helsedir_sexhealth_no_rag":  HELSEDIR_SEXHEALTH_NO_RAG_JUDGE,
    "binary_abstention":          BINARY_ABSTENTION_JUDGE,
    "checklist":                  CHECKLIST_JUDGE,
    "groundedness":               GROUNDEDNESS_JUDGE,
}


def get_judge(name: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Get a judge configuration: a built-in by name, or a config dict as given.

    Args:
        name: Judge config name (e.g. "safety", "helpfulness"), or a config
            dict such as one from build_judge() or customize_judge()

    Returns:
        Judge config dict with probe_prompt, judge_prompt, output_schema, source

    Raises:
        ValueError: If name is not recognised, or a dict has no judge prompt
    """
    if isinstance(name, dict):
        config = dict(name)
        if "judge_prompt" not in config:
            if "criteria" not in config or "format_prompt" not in config:
                raise ValueError("A judge config needs judge_prompt, or criteria and format_prompt.")
            config["judge_prompt"] = compose_prompt(config["criteria"], config["format_prompt"])
        return config
    if name not in JUDGE_CONFIGS:
        available = ", ".join(JUDGE_CONFIGS.keys())
        raise ValueError(f"Unknown judge config '{name}'. Available: {available}")
    # Shallow copy: callers tweaking e.g. config["judge_prompt"] must not
    # rewrite the shared registry entry for every later get_judge() call.
    return dict(JUDGE_CONFIGS[name])


def list_judge_configs() -> Dict[str, str]:
    """
    List available judge configs and their descriptions.

    Returns:
        Dict mapping config names to one-line descriptions
    """
    return {name: config["description"] for name, config in JUDGE_CONFIGS.items()}


__all__ = [
    "get_judge", "list_judge_configs", "JUDGE_CONFIGS", "DEFAULT_JUDGE", "OUTPUT_KINDS",
    "build_judge", "customize_judge", "compose_prompt", "dimension_key", "BUILD_OUTPUTS",
]
