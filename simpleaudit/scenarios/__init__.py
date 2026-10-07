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
- hei_refusal: Norwegian youth Q&A refusal/guidance edge cases (47 scenarios)
- nav_aap: NAV Arbeidsavklaringspenger / Norwegian welfare scenarios (15 scenarios)
- skatteetaten: Norwegian Tax Administration scenarios (in development)
- helfo: Helfo health-economics scenarios (8 scenarios)
- lanekassen: Lånekassen student-finance scenarios (8 scenarios)
- arbeidstilsynet_arbeidstid: Working-time rules — person category, age and
  working-time arrangement axes (11 scenarios)
- skatteetaten_legitimasjon: Skatteetaten identification requirements at in-person
  attendance — citizenship, service and channel axes (11 scenarios)
- toll_reisegodskvote: Tolletaten traveller allowances — value limit, person
  category, residence and age axes (11 scenarios)
- human_rights_water: International human rights law, right to water (15 scenarios)
- human_rights_education: International human rights law, right to education (13 scenarios)
- human_rights_fair_trial: International human rights law, liberty and fair trial (14 scenarios)
- vision_integrity: Chart-reading integrity for vision models (8 scenarios,
  requires vision-capable target, judge and auditor; not part of 'all')
- nb_kryss_ordning: National Library cross-scheme transfer, 13 scenarios in 6 matched pairs
- context_grounding: Marked retrieval context — counterfactual, superseded and
  lower-authority chunks (3 scenarios, requires SingleTurnAuditor; not part of 'all')
- healthbench_behaviours: One scenario per HealthBench consensus category (17 scenarios)
- all: All scenarios combined

HealthBench is not a built-in pack: OpenAI asks that its examples are not reposted in
plain text, so load_healthbench_scenarios() downloads it and builds scenarios at run time.
"""

import re
from collections import Counter
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Union

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
from .hei_refusal import HEI_REFUSAL_SCENARIOS
from .nav_aap import NAV_AAP_SCENARIOS
from .skatteetaten import SKATTEETATEN_SCENARIOS
from .helfo import HELFO_SCENARIOS
from .lanekassen import LANEKASSEN_SCENARIOS
from .arbeidstilsynet_arbeidstid import ARBEIDSTILSYNET_ARBEIDSTID_SCENARIOS
from .skatteetaten_legitimasjon import SKATTEETATEN_LEGITIMASJON_SCENARIOS
from .toll_reisegodskvote import TOLL_REISEGODSKVOTE_SCENARIOS
from .human_rights_water import HUMAN_RIGHTS_WATER_SCENARIOS
from .human_rights_education import HUMAN_RIGHTS_EDUCATION_SCENARIOS
from .human_rights_fair_trial import HUMAN_RIGHTS_FAIR_TRIAL_SCENARIOS
from .vision_integrity import VISION_INTEGRITY_SCENARIOS
from .nb_kryss_ordning import NB_KRYSS_ORDNING_SCENARIOS
from .context_grounding import CONTEXT_GROUNDING_SCENARIOS
from .healthbench_behaviours import HEALTHBENCH_BEHAVIOURS_SCENARIOS
from .healthbench_loader import load_healthbench_scenarios


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
    "hei_refusal":      HEI_REFUSAL_SCENARIOS,
    "nav_aap":          NAV_AAP_SCENARIOS,
    "skatteetaten":     SKATTEETATEN_SCENARIOS,
    "helfo":            HELFO_SCENARIOS,
    "lanekassen":       LANEKASSEN_SCENARIOS,
    "arbeidstilsynet_arbeidstid": ARBEIDSTILSYNET_ARBEIDSTID_SCENARIOS,
    "skatteetaten_legitimasjon": SKATTEETATEN_LEGITIMASJON_SCENARIOS,
    "toll_reisegodskvote": TOLL_REISEGODSKVOTE_SCENARIOS,
    "human_rights_water": HUMAN_RIGHTS_WATER_SCENARIOS,
    "human_rights_education": HUMAN_RIGHTS_EDUCATION_SCENARIOS,
    "human_rights_fair_trial": HUMAN_RIGHTS_FAIR_TRIAL_SCENARIOS,

    # Attachments go to target, judge AND auditor, so this pack needs three
    # vision-capable models. It is deliberately kept out of "all" and
    # "epistemic_safety": folding it in would make those packs fail for every
    # text-only setup that runs them today.
    "vision_integrity": VISION_INTEGRITY_SCENARIOS,
    "nb_kryss_ordning": NB_KRYSS_ORDNING_SCENARIOS,
    "healthbench_behaviours": HEALTHBENCH_BEHAVIOURS_SCENARIOS,

    # Scored under a fixed pack, and the scores only hold if the documents
    # reach the target in the ranking the author gave them. The multi-turn
    # loop regenerates the probe from turn 1, so this pack needs
    # SingleTurnAuditor and is kept out of "all" for the same reason
    # vision_integrity is: folding it in would silently change what "all"
    # measures for every setup running it today.
    "context_grounding": CONTEXT_GROUNDING_SCENARIOS,
    "all":              SAFETY_SCENARIOS + RAG_SCENARIOS + HEALTH_SCENARIOS
                        + SYSTEM_PROMPT_SCENARIOS + HELPMED_SCENARIOS + UNG_SCENARIOS
                        + BULLSHITBENCH_SCENARIOS + BROKEN_PREMISE_SCENARIOS
                        + HEI_REFUSAL_SCENARIOS + NAV_AAP_SCENARIOS
                        + SKATTEETATEN_SCENARIOS + HELFO_SCENARIOS
                        + LANEKASSEN_SCENARIOS
                        + NB_KRYSS_ORDNING_SCENARIOS
                        + SKATTEETATEN_LEGITIMASJON_SCENARIOS
                        + TOLL_REISEGODSKVOTE_SCENARIOS
                        + ARBEIDSTILSYNET_ARBEIDSTID_SCENARIOS
                        + HUMAN_RIGHTS_WATER_SCENARIOS
                        + HUMAN_RIGHTS_EDUCATION_SCENARIOS
                        + HUMAN_RIGHTS_FAIR_TRIAL_SCENARIOS
                        + HEALTHBENCH_BEHAVIOURS_SCENARIOS,
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

    # Shallow copy: callers appending/filtering must not mutate the shared
    # registry list that every later get_scenarios() call hands out.
    return list(SCENARIO_PACKS[pack_name])


def list_scenario_packs() -> Dict[str, int]:
    """
    List available scenario packs and their sizes.

    Returns:
        Dict mapping pack names to number of scenarios
    """
    return {name: len(scenarios) for name, scenarios in SCENARIO_PACKS.items()}


def duplicate_scenario_names(scenarios: List[Dict]) -> Dict[str, int]:
    """
    Return scenario names that occur more than once, mapped to their count.

    Per-scenario stability statistics are keyed by scenario name (see
    ``RepeatedExperimentResults.stability``), so duplicate names within a pack
    silently collapse into a single entry and corrupt the aggregates. Use this
    to validate a custom scenario list before auditing.

    Args:
        scenarios: List of scenario dicts (each expected to have a ``name`` key)

    Returns:
        Dict mapping each duplicated name to the number of times it appears
        (empty if all names are unique)
    """
    counts = Counter(s.get("name") for s in scenarios)
    return {name: c for name, c in counts.items() if c > 1}


_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _fact_date(value: Any, where: str) -> date:
    if isinstance(value, str) and _ISO_DATE.fullmatch(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError(f"{where}: {value!r} is not a valid YYYY-MM-DD date")


def stale_facts(packs: Mapping[str, List[Dict]], as_of: Union[date, str]) -> List[Dict[str, Any]]:
    """
    Return the dated facts whose ``review_by`` date is before ``as_of``.

    A scenario can list the dated facts it rests on in ``metadata.facts``, each a dict
    with ``claim``, ``value``, ``valid_from``, ``verified_at``, ``review_by``,
    ``source_url`` and ``source_quote``. Dates are ``YYYY-MM-DD`` strings. ``valid_from``
    is None when the source gives no date. ``review_by`` follows the rule's own rhythm
    and is None for a figure fixed in statute, which is never returned. Scenarios
    without ``facts`` are skipped.

    No clock is read: ``as_of`` is required, so a call with a fixed date gives the same
    answer on any day.

    Args:
        packs: Mapping of pack name to scenario list, e.g. ``SCENARIO_PACKS``. A scenario
            in several packs (as every scenario in ``all`` is) is reported once, under
            the first pack it appears in.
        as_of: The date to check against, as a ``date`` or a ``YYYY-MM-DD`` string

    Returns:
        The stale facts, each with ``pack`` and ``scenario`` added

    Raises:
        ValueError: If ``as_of`` or a date in a fact is not a valid ``YYYY-MM-DD`` date,
            or a fact has no ``review_by`` key
    """
    if isinstance(as_of, datetime):
        as_of = as_of.date()
    elif not isinstance(as_of, date):
        as_of = _fact_date(as_of, "as_of")

    stale, seen = [], set()
    for pack, scenarios in packs.items():
        for s in scenarios:
            facts = (s.get("metadata") or {}).get("facts") or []
            for i, fact in enumerate(facts):
                where = f"{pack} / {s.get('name')} / facts[{i}]"
                # A missing key is an error, not "never stale": a misspelt review_by
                # would otherwise hide the fact from every check.
                if "review_by" not in fact:
                    raise ValueError(f"{where}: no review_by (use None for a figure fixed in statute)")
                dates = {field: _fact_date(fact[field], f"{where}.{field}")
                         for field in ("valid_from", "verified_at", "review_by")
                         if fact.get(field) is not None}
                key = (s.get("name"), fact.get("claim"))
                if "review_by" in dates and dates["review_by"] < as_of and key not in seen:
                    seen.add(key)
                    stale.append({"pack": pack, "scenario": s.get("name"), **fact})
    return stale


__all__ = [
    "get_scenarios",
    "list_scenario_packs",
    "duplicate_scenario_names",
    "stale_facts",
    "load_healthbench_scenarios",
    "SCENARIO_PACKS",
]
