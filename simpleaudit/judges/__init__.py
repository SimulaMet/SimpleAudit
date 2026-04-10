"""
Built-in judge configurations for SimpleAudit.

Available judges:
- safety:      Constitutional AI safety evaluation (Bai et al., 2022)
                Severity: critical | high | medium | low | pass
- abstention:  Refusal/abstention appropriateness (Feng et al., 2024)
                Score 1–10, with abstained/appropriate flags
- helpfulness: Response quality across four MT-Bench dimensions (Zheng et al., 2023)
                Score 1–10 with relevance, accuracy, clarity, completeness sub-scores
- factuality:  Hallucination and factual error detection (Liu et al., 2023)
                Score 1–10 with verdict and error lists
- harm:        HELM Safety harm categorisation (Liang et al., 2022)
                harmful flag, category, severity across six harm types

Usage:
    from simpleaudit import ModelAuditor

    # Use a named judge config
    auditor = ModelAuditor(..., judge="helpfulness")

    # Named judge + custom probe (probe_prompt overrides the config's probe_prompt)
    auditor = ModelAuditor(..., judge="factuality", probe_prompt="Ask about X...")

    # Fully custom (judge_prompt takes precedence over any named judge)
    auditor = ModelAuditor(..., judge_prompt="You are a custom judge...")
"""

from typing import Dict, Any

from .safety import SAFETY_JUDGE
from .abstention import ABSTENTION_JUDGE
from .helpfulness import HELPFULNESS_JUDGE
from .factuality import FACTUALITY_JUDGE
from .harm import HARM_JUDGE


JUDGE_CONFIGS: Dict[str, Dict[str, Any]] = {
    "safety":      SAFETY_JUDGE,
    "abstention":  ABSTENTION_JUDGE,
    "helpfulness": HELPFULNESS_JUDGE,
    "factuality":  FACTUALITY_JUDGE,
    "harm":        HARM_JUDGE,
}


def get_judge(name: str) -> Dict[str, Any]:
    """
    Get a built-in judge configuration by name.

    Args:
        name: Judge config name (e.g. "safety", "helpfulness")

    Returns:
        Judge config dict with probe_prompt, judge_prompt, output_schema, source

    Raises:
        ValueError: If name is not recognised
    """
    if name not in JUDGE_CONFIGS:
        available = ", ".join(JUDGE_CONFIGS.keys())
        raise ValueError(f"Unknown judge config '{name}'. Available: {available}")
    return JUDGE_CONFIGS[name]


def list_judge_configs() -> Dict[str, str]:
    """
    List available judge configs and their descriptions.

    Returns:
        Dict mapping config names to one-line descriptions
    """
    return {name: config["description"] for name, config in JUDGE_CONFIGS.items()}


__all__ = ["get_judge", "list_judge_configs", "JUDGE_CONFIGS"]
