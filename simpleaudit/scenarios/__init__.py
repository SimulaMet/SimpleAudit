"""
Built-in scenario packs for SimpleAudit.

Available packs:
- safety: General AI safety scenarios
- rag: RAG-specific scenarios
- health: Healthcare domain scenarios
- system_prompt: System prompt adherence/bypass testing
- all: All scenarios combined
"""

from typing import List, Dict

from .safety import SAFETY_SCENARIOS
from .rag import RAG_SCENARIOS
from .health import HEALTH_SCENARIOS
from .system_prompt import SYSTEM_PROMPT_SCENARIOS


SCENARIO_PACKS = {
    "safety": SAFETY_SCENARIOS,
    "rag": RAG_SCENARIOS,
    "health": HEALTH_SCENARIOS,
    "system_prompt": SYSTEM_PROMPT_SCENARIOS,
    "all": SAFETY_SCENARIOS + RAG_SCENARIOS + HEALTH_SCENARIOS + SYSTEM_PROMPT_SCENARIOS,
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
