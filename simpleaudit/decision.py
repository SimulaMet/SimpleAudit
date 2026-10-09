"""
Structured decision questions for scenarios.

A scenario may carry a ``decision`` block: one closed question with a fixed set
of answer options and, optionally, the accepted answer. The same scenario then
serves two kinds of target:

1. A **chat model** gets the question as text: the scenario's ``test_prompt``,
   or :func:`render_decision_prompt` when the scenario has none.
2. A **decision model** (a model that returns a choice and probabilities
   rather than prose, e.g. served through a System One ``/v1/systemone``
   endpoint) gets the structured question through
   ``TargetContext.extra["decision"]``.

Two rules, the same as for document marks (see ``context_marks``):

1. **Unknown keys raise.** A mis-spelled ``acepted`` would otherwise leave the
   scenario without an answer key and quietly stop grading it.
2. **The answer key never reaches the target.** :func:`public_decision` drops
   ``accepted`` before the block is handed to a target. The full block reaches
   only the judge, through ``scenario_meta["decision"]``.

A decision block::

    "decision": {
        "id": "verdict",                       # optional, default "answer"
        "type": "choice",                      # the only type so far
        "instructions": "Did the court find the defendant guilty?",
        "criteria": {"yes": "Found guilty", "no": "Not found guilty"},
        "accepted": ["yes"],                   # optional; never sent to the target
        "state": {"jurisdiction": "Kosovo"},   # optional extra input for decision models
    }

``criteria`` maps each option key to its description (or ``None``). Limits on
the number of options belong to the target that enforces them, not to the
scenario: a chat model can choose among any number of options.
"""

from collections.abc import Mapping
from typing import Any

#: Every key a decision block may carry. Anything else is a typo, and typos raise.
DECISION_KEYS = ("id", "type", "instructions", "criteria", "accepted", "state")

#: Question types a decision block may declare.
DECISION_TYPES = ("choice",)

#: Identifier used when a decision block does not name its question.
DEFAULT_DECISION_ID = "answer"


def validate_decision(decision: Any) -> dict[str, Any]:
    """
    Check a scenario's ``decision`` block and return a normalised copy.

    The copy has every optional key filled in (``id``, ``type``) and its own
    ``criteria`` and ``accepted`` containers, so callers can pass it on
    without sharing state with the scenario.

    Raises:
        ValueError: if the block is malformed, with a message naming the problem.
    """
    if not isinstance(decision, Mapping):
        raise ValueError(f"decision must be a mapping, got {type(decision).__name__}")
    unknown = sorted(set(decision) - set(DECISION_KEYS))
    if unknown:
        raise ValueError(f"decision has unknown keys {unknown}; allowed: {list(DECISION_KEYS)}")

    decision_id = decision.get("id", DEFAULT_DECISION_ID)
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ValueError("decision.id must be a non-empty string")

    decision_type = decision.get("type", "choice")
    if decision_type not in DECISION_TYPES:
        raise ValueError(f"decision.type {decision_type!r} not in {list(DECISION_TYPES)}")

    instructions = decision.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("decision.instructions must be a non-empty string")

    criteria = decision.get("criteria")
    if not isinstance(criteria, Mapping) or len(criteria) < 2:
        raise ValueError("decision.criteria must map at least two option keys to descriptions")
    for key, description in criteria.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("decision.criteria keys must be non-empty strings")
        if description is not None and not isinstance(description, str):
            raise ValueError(f"decision.criteria[{key!r}] must be a string or None")

    normalised: dict[str, Any] = {
        "id": decision_id,
        "type": decision_type,
        "instructions": instructions,
        "criteria": dict(criteria),
    }

    if "accepted" in decision:
        accepted = decision["accepted"]
        if (
            not isinstance(accepted, (list, tuple))
            or not accepted
            or not all(isinstance(key, str) for key in accepted)
        ):
            raise ValueError("decision.accepted must be a non-empty list of option keys")
        missing = [key for key in accepted if key not in criteria]
        if missing:
            raise ValueError(f"decision.accepted keys {missing} are not in decision.criteria")
        normalised["accepted"] = list(accepted)

    if "state" in decision:
        if not isinstance(decision["state"], Mapping):
            raise ValueError("decision.state must be a mapping")
        normalised["state"] = dict(decision["state"])

    return normalised


def public_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
    """The decision block as a target may see it: everything except ``accepted``."""
    return {key: value for key, value in decision.items() if key != "accepted"}


def render_decision_prompt(decision: Mapping[str, Any]) -> str:
    """
    The decision question as a chat prompt, for scenarios without a ``test_prompt``.

    Lists every option with its key and asks for the key on the first line, so
    a chat model's answer can be matched against the options. ``accepted`` and
    ``state`` are never rendered.
    """
    lines = [decision["instructions"].strip(), "", "Options:"]
    for key, description in decision["criteria"].items():
        lines.append(f"- {key}: {description}" if description else f"- {key}")
    lines += [
        "",
        "Reply with the key of exactly one option on the first line, then explain your answer briefly.",
    ]
    return "\n".join(lines)
