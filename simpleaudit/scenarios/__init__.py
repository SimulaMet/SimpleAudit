"""
Built-in scenario packs for SimpleAudit.

Available packs:
- safety: General AI safety scenarios
- rag: RAG-specific scenarios
- health: Healthcare domain scenarios
- system_prompt: System prompt adherence/bypass testing
- helpmed: Help and medical scenarios
- ung: UNG scenarios
- bullshitbench_v1: BullshitBench v1 (55 scenarios, business/management)
- bullshitbench_v2: BullshitBench v2 (100 scenarios, software/finance/legal/medical/physics)
- bullshitbench: BullshitBench v1+v2 combined (155 scenarios)
- health_bullshit: Health-specific broken premise scenarios (15 scenarios)
- epistemic_safety: All bullshitbench + health_bullshit combined (170 scenarios)
- all: All scenarios combined
"""

from typing import List, Dict

from .safety import SAFETY_SCENARIOS
from .rag import RAG_SCENARIOS
from .health import HEALTH_SCENARIOS
from .system_prompt import SYSTEM_PROMPT_SCENARIOS
from .helpmed import HELPMED_SCENARIOS
from .ung import UNG_SCENARIOS
from .bullshitbench_v1_v2 import (
    BULLSHITBENCH_V1_SCENARIOS,
    BULLSHITBENCH_V2_SCENARIOS,
    BULLSHITBENCH_SCENARIOS,
)
from .bullshitbench_health import BROKEN_PREMISE_SCENARIOS


SCENARIO_PACKS = {
    "safety":           SAFETY_SCENARIOS,
    "rag":              RAG_SCENARIOS,
    "health":           HEALTH_SCENARIOS,
    "system_prompt":    SYSTEM_PROMPT_SCENARIOS,
    "helpmed":          HELPMED_SCENARIOS,
    "ung":              UNG_SCENARIOS,
    "bullshitbench_v1": BULLSHITBENCH_V1_SCENARIOS,
    "bullshitbench_v2": BULLSHITBENCH_V2_SCENARIOS,
    "bullshitbench":    BULLSHITBENCH_SCENARIOS,
    "health_bullshit":  BROKEN_PREMISE_SCENARIOS,
    "epistemic_safety": BULLSHITBENCH_SCENARIOS + BROKEN_PREMISE_SCENARIOS,
    "all":              SAFETY_SCENARIOS + RAG_SCENARIOS + HEALTH_SCENARIOS
                        + SYSTEM_PROMPT_SCENARIOS + HELPMED_SCENARIOS + UNG_SCENARIOS
                        + BULLSHITBENCH_SCENARIOS + BROKEN_PREMISE_SCENARIOS,
}


def get_scenarios(pack_name: str) -> List[Dict]:
    """
    Get scenarios from a built-in pack.

    Args:
        pack_name: Name of the scenario pack

    Returns:
        List of scenario dictionaries

    Raises:
        ValueError: If pack name is not recognized
    """
    if pack_name not in SCENARIO_PACKS:
        available = ", ".join(SCENARIO_PACKS.keys())
        raise ValueError(f"Unknown scenario pack '{pack_name}'. Available: {available}")

    return SCENARIO_PACKS[pack_name]


def list_scenario_packs() -> Dict[str, int]:
    """
    List available scenario packs and their sizes.

    Returns:
        Dict mapping pack names to number of scenarios
    """
    return {name: len(scenarios) for name, scenarios in SCENARIO_PACKS.items()}


__all__ = ["get_scenarios", "list_scenario_packs", "SCENARIO_PACKS"]