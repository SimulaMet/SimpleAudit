"""
Tests for the groundedness judge (design §4).

The property under test is the conditional construction: a judge field
mirrors exactly one derivation, and when that derivation is None the QUESTION
is absent from the prompt and the FIELD is absent from the schema. The judge
is never asked about something the scenario author did not mark, so an
unmarked property can never turn into a finding.

The spec names one case explicitly — a scenario with no `as_of` derives
`temporal_conflict = None`, and the built prompt must then not contain the
word "superseded" at all. The mirror case and the two sibling pairs
(counterfactual, authority) are tested alongside it.

No API calls: both builders are pure functions of a derivations dict.
"""

import json
from typing import Any, Dict, Optional

import pytest

from simpleaudit.judges.binary_abstention import BINARY_ABSTENTION_JUDGE
from simpleaudit.judges.groundedness import (
    CONDITIONAL_FIELDS,
    FIELD_ORDER,
    GROUNDEDNESS_JUDGE,
    SEVERITY_ENUM,
    active_fields,
    build_groundedness_prompt,
    build_groundedness_schema,
)
from simpleaudit.utils import SEVERITY_ORDER


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Every key `derive_all` emits (§2), all None — the shape produced by a
#: scenario of bare-string documents with no `as_of`.
ALL_NONE: Dict[str, Any] = {
    "has_counterfactual": None,
    "precision": None,
    "recall_complete": None,
    "temporal_conflict": None,
    "authority_conflict": None,
    "inter_context_conflict": None,
}

#: The four fields the judge always answers, whatever the author marked.
UNCONDITIONAL = {"used_context", "contradicted_context", "abstained", "severity"}


def _derivations(**overrides: Any) -> Dict[str, Any]:
    """A full derivations dict, all-None except the named overrides."""
    derived = dict(ALL_NONE)
    derived.update(overrides)
    return derived


def _assert_json_schema_object(schema: Dict[str, Any]) -> None:
    """
    Structural check that `schema` is a JSON Schema object of the shape the
    framework threads into `response_format` — the shape binary_abstention
    declares. `jsonschema` is not a dependency of this repo, so this checks
    the shape the framework actually relies on rather than full validity.
    """
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert isinstance(schema["required"], list)
    # Everything required must be described; nothing described may be optional
    # (providers reject a partially-required schema in strict json_schema mode).
    assert set(schema["required"]) == set(schema["properties"])
    for name, prop in schema["properties"].items():
        assert isinstance(prop, dict), name
        assert prop["type"] in {"string", "boolean", "integer", "number", "array", "object"}
        if prop["type"] == "array":
            assert isinstance(prop["items"], dict), name
            assert "type" in prop["items"], name
    # Must survive a JSON round-trip — it is sent over the wire as JSON.
    assert json.loads(json.dumps(schema)) == schema


def _template_keys(prompt: str) -> list:
    """Field names quoted as JSON keys inside the prompt's OUTPUT template."""
    _, _, template = prompt.partition("OUTPUT")
    return [field for field in FIELD_ORDER if f'"{field}"' in template]


# ---------------------------------------------------------------------------
# 1. The case the spec names: no as_of -> no temporal question at all
# ---------------------------------------------------------------------------

def test_no_as_of_drops_superseded_question_entirely():
    """
    A scenario without `as_of` cannot derive temporal_conflict. The word
    "superseded" must then not appear anywhere in the prompt — not in a
    softened form, not as an optional field — and used_superseded_context
    must be absent from the schema.
    """
    derived = _derivations()  # no as_of -> temporal_conflict is None
    prompt, fields = build_groundedness_prompt(derived)
    schema = build_groundedness_schema(derived)

    assert "superseded" not in prompt.lower()
    assert "used_superseded_context" not in fields
    assert "used_superseded_context" not in schema["properties"]
    assert "used_superseded_context" not in schema["required"]


def test_temporal_conflict_present_asks_the_superseded_question():
    """Mirror of the above: the derivation exists, so the question is asked."""
    derived = _derivations(temporal_conflict=True)
    prompt, fields = build_groundedness_prompt(derived)
    schema = build_groundedness_schema(derived)

    assert "superseded" in prompt.lower()
    assert "used_superseded_context" in fields
    assert "used_superseded_context" in schema["required"]


def test_temporal_conflict_false_still_asks():
    """
    False is not None. The author marked the windows and the set happens to
    carry no conflict — the judge is still asked, because "no conflict here"
    is a finding and "nobody marked it" is not.
    """
    prompt, fields = build_groundedness_prompt(_derivations(temporal_conflict=False))
    assert "superseded" in prompt.lower()
    assert "used_superseded_context" in fields


# ---------------------------------------------------------------------------
# 2. Same pair for the counterfactual and authority questions
# ---------------------------------------------------------------------------

def test_unmarked_truth_drops_the_counterfactual_question():
    derived = _derivations()  # has_counterfactual is None
    prompt, fields = build_groundedness_prompt(derived)
    schema = build_groundedness_schema(derived)

    assert "counterfactual" not in prompt.lower()
    assert "repeated_false_claim" not in fields
    assert "repeated_false_claim" not in schema["properties"]


def test_has_counterfactual_present_asks_the_false_claim_question():
    derived = _derivations(has_counterfactual=True)
    prompt, fields = build_groundedness_prompt(derived)
    schema = build_groundedness_schema(derived)

    assert "counterfactual" in prompt.lower()
    assert "repeated_false_claim" in fields
    assert "repeated_false_claim" in schema["required"]


def test_unmarked_authority_drops_the_authority_question():
    derived = _derivations()  # authority_conflict is None
    prompt, fields = build_groundedness_prompt(derived)
    schema = build_groundedness_schema(derived)

    assert "authority" not in prompt.lower()
    assert "followed_lower_authority" not in fields
    assert "followed_lower_authority" not in schema["properties"]


def test_authority_conflict_present_asks_the_authority_question():
    derived = _derivations(authority_conflict=True)
    prompt, fields = build_groundedness_prompt(derived)
    schema = build_groundedness_schema(derived)

    assert "authority" in prompt.lower()
    assert "followed_lower_authority" in fields
    assert "followed_lower_authority" in schema["required"]


@pytest.mark.parametrize("field,derivation", sorted(CONDITIONAL_FIELDS.items()))
def test_each_conditional_field_is_gated_by_its_own_derivation_only(field, derivation):
    """One derivation, one field. Marking one conflict never opens another."""
    _, fields = build_groundedness_prompt(_derivations(**{derivation: True}))
    assert field in fields
    for other in CONDITIONAL_FIELDS:
        if other != field:
            assert other not in fields


def test_precision_and_recall_gate_nothing():
    """
    precision, recall_complete and inter_context_conflict are derived but
    mirror no judge field; marking them must not add a question.
    """
    derived = _derivations(
        precision=1.0, recall_complete=True, inter_context_conflict=False
    )
    assert set(active_fields(derived)) == UNCONDITIONAL


# ---------------------------------------------------------------------------
# 3. The unconditional four are never dropped
# ---------------------------------------------------------------------------

def test_unconditional_fields_survive_a_fully_unmarked_set():
    prompt, fields = build_groundedness_prompt(_derivations())
    schema = build_groundedness_schema(_derivations())

    assert set(fields) == UNCONDITIONAL
    assert set(schema["required"]) == UNCONDITIONAL
    # used_context / contradicted_context are lists that are never null, so an
    # empty list has to be an available answer — say so in the prompt.
    assert "empty list" in prompt.lower()


@pytest.mark.parametrize("derivations", [None, {}])
def test_missing_derivations_are_treated_as_unmarked(derivations: Optional[dict]):
    """A missing key is unknown, exactly like an explicit None."""
    prompt, fields = build_groundedness_prompt(derivations)
    assert set(fields) == UNCONDITIONAL
    assert "superseded" not in prompt.lower()
    assert set(build_groundedness_schema(derivations)["required"]) == UNCONDITIONAL


def test_all_conflicts_marked_yields_all_seven_fields():
    derived = _derivations(
        has_counterfactual=True, temporal_conflict=True, authority_conflict=True
    )
    _, fields = build_groundedness_prompt(derived)
    assert fields == list(FIELD_ORDER)
    assert set(build_groundedness_schema(derived)["required"]) == set(FIELD_ORDER)


# ---------------------------------------------------------------------------
# 4. Prompt and schema cannot drift apart
# ---------------------------------------------------------------------------

ALL_SHAPES = [
    {},
    {"has_counterfactual": False},
    {"temporal_conflict": True},
    {"authority_conflict": True},
    {"has_counterfactual": True, "temporal_conflict": False},
    {"has_counterfactual": True, "temporal_conflict": True, "authority_conflict": True},
]


@pytest.mark.parametrize("overrides", ALL_SHAPES)
def test_prompt_template_schema_and_fields_all_agree(overrides):
    derived = _derivations(**overrides)
    prompt, fields = build_groundedness_prompt(derived)
    schema = build_groundedness_schema(derived)

    assert fields == list(schema["required"])
    # The JSON template the judge is shown lists exactly the required fields.
    assert _template_keys(prompt) == fields
    # Dropping a field must not leave a dangling comma in the template.
    assert ",\n}" not in prompt
    assert prompt.rstrip().endswith("}")


@pytest.mark.parametrize("overrides", ALL_SHAPES)
def test_built_schema_is_a_wellformed_json_schema(overrides):
    _assert_json_schema_object(build_groundedness_schema(_derivations(**overrides)))


@pytest.mark.parametrize("overrides", ALL_SHAPES)
def test_field_order_is_stable(overrides):
    """
    Same shape in, same prompt out. Two scenarios of the same mark shape must
    be judged by a byte-identical instrument, or their results are not
    comparable.
    """
    derived = _derivations(**overrides)
    first, first_fields = build_groundedness_prompt(derived)
    second, second_fields = build_groundedness_prompt(dict(derived))
    assert first == second
    assert first_fields == second_fields == [f for f in FIELD_ORDER if f in first_fields]


# ---------------------------------------------------------------------------
# 5. The registry entry — same shape as binary_abstention's
# ---------------------------------------------------------------------------

def test_config_carries_the_same_keys_as_binary_abstention():
    """Every key the framework reads off a judge config, plus the two builders.

    Asserted as a superset with the extra keys named explicitly rather than as
    equality: this judge's prompt and schema depend on what the scenario author
    marked, so it declares builders alongside the static pair. Naming them here
    means a third key added by accident still fails the test.
    """
    assert set(BINARY_ABSTENTION_JUDGE) <= set(GROUNDEDNESS_JUDGE)
    assert set(GROUNDEDNESS_JUDGE) - set(BINARY_ABSTENTION_JUDGE) == {
        "build_judge_prompt",
        "build_response_schema",
    }


def test_config_builders_are_the_module_level_functions():
    """The runner reaches the builders through the config, so the wiring is the
    contract — a config pointing at anything else would silently fall back to
    the static prompt and ask about properties the author never marked."""
    assert GROUNDEDNESS_JUDGE["build_judge_prompt"] is build_groundedness_prompt
    assert GROUNDEDNESS_JUDGE["build_response_schema"] is build_groundedness_schema


def test_config_response_schema_is_wellformed_and_not_the_severity_shape():
    schema = GROUNDEDNESS_JUDGE["response_schema"]
    _assert_json_schema_object(schema)
    assert set(schema["required"]) == set(FIELD_ORDER)
    # The default judge schema's shape must not leak in — that is the whole
    # reason a per-config response_schema exists.
    assert "issues_found" not in schema["properties"]
    assert "summary" not in schema["properties"]


def test_config_is_the_fully_general_form():
    """The registry entry asks every question; runners narrow it per scenario."""
    prompt, fields = build_groundedness_prompt(
        {"has_counterfactual": True, "temporal_conflict": True, "authority_conflict": True}
    )
    assert GROUNDEDNESS_JUDGE["judge_prompt"] == prompt
    assert list(GROUNDEDNESS_JUDGE["response_schema"]["required"]) == fields


def test_output_schema_and_response_schema_agree():
    """Human-readable and machine-readable descriptions list the same fields."""
    assert set(GROUNDEDNESS_JUDGE["output_schema"]) == set(
        GROUNDEDNESS_JUDGE["response_schema"]["properties"]
    )


def test_severity_uses_the_frameworks_existing_ordinal():
    schema = GROUNDEDNESS_JUDGE["response_schema"]["properties"]["severity"]
    assert schema["type"] == "string"
    assert set(schema["enum"]) == set(SEVERITY_ORDER)
    # Most severe first, matching the other judge schemas in the framework.
    assert SEVERITY_ENUM == list(reversed(SEVERITY_ORDER))
    assert schema["enum"][0] == "critical"


def test_probe_prompt_keeps_the_language_placeholder():
    """The multi-turn path formats {language} into the probe prompt."""
    assert "{language}" in GROUNDEDNESS_JUDGE["probe_prompt"]


# ---------------------------------------------------------------------------
# 6. Rubric content the design requires verbatim in spirit
# ---------------------------------------------------------------------------

def test_abstention_is_scored_as_an_outcome_not_a_failure():
    prompt, _ = build_groundedness_prompt(_derivations())
    lowered = prompt.lower()
    assert "abstained=true is not automatically a failure" in lowered
    # A reasoned abstention under conflict is the best available outcome.
    assert "'pass'" in prompt


def test_authority_rubric_calls_it_published_guidance_not_hallucination():
    """
    Design §6: following the agency page is *following published guidance*,
    not hallucination, and the severity is calibrated accordingly. Without
    this the judge scores a model that trusted an official page as if it had
    invented the answer.
    """
    prompt, _ = build_groundedness_prompt(_derivations(authority_conflict=True))
    lowered = prompt.lower()
    assert "following published guidance" in lowered
    assert "not hallucination" in lowered
    assert "'medium' at most" in lowered


def test_superseded_is_not_presented_as_falsehood():
    """A superseded document was true when written; the rubric must say so."""
    prompt, _ = build_groundedness_prompt(_derivations(temporal_conflict=True))
    assert "not a false document" in prompt.lower()


def test_marks_are_described_as_judge_only():
    """
    The target never sees the marks (design §3). The judge is told so, or it
    will penalise the model for not knowing what it was never shown.
    """
    prompt, _ = build_groundedness_prompt(_derivations())
    assert "never saw the mark table" in prompt.lower()
