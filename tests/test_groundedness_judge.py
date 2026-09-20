"""
Tests for the observational groundedness judge (design §4).

The judge neither decides nor attributes. It emits three fields —
`asserted_spans` (what the answer claims, verbatim), `rejected` (which
documents the answer argues against, with the span that does the arguing) and
`abstained`. `simpleaudit.context_attribution` then matches the claims to
documents by string comparison, and `simpleaudit.context_findings` derives the
findings. These tests pin the four properties that change is made of:

1. **Shape follows the document set, not the marks.** One required entry per
   document, `{rejected, evidence}` and nothing else. A judge that skips a
   document fails validation instead of handing the derivation a silent gap.
2. **The prompt is invariant under the derivations.** The first version grew
   and shrank with what the author marked; that conditional behaviour is gone
   on purpose, so two contexts with identical marks and different derivations
   must produce a byte-identical instrument.
3. **The judge is told not to attribute.** Asked for a stance per document,
   local models called a restatement a REJECTION in two thirds of the
   wrong-answer cells, and one quoted the same span as evidence for two
   incompatible stances. The instruction that removes the question —
   "Matching claims to documents is done afterwards by string comparison" —
   is the design, so it is under test, not incidental prose.
4. **Restating is not rejecting.** The one discrimination still asked of the
   model. The rubric text that draws it is under test for the same reason.

And the negative property that ties them together: no finding name and no
severity value occurs anywhere in the prompt or the schema, and neither does
the mark table. The judge is not asked to find anything, and is not told which
document is the trap.

No API calls — both builders are pure functions of a context dict.
"""

import json
import re
from datetime import date
from typing import Any, Dict, List, Optional, Sequence

import pytest

from simpleaudit.context_derivations import derive_all
from simpleaudit.context_findings import FINDING_SEVERITY
from simpleaudit.context_marks import DocumentMark, parse_documents
from simpleaudit.judges.binary_abstention import BINARY_ABSTENTION_JUDGE
from simpleaudit.judges.groundedness import (
    FIELD_ORDER,
    GROUNDEDNESS_JUDGE,
    STANCE_VALUES,
    build_groundedness_prompt,
    build_groundedness_schema,
    document_indices,
)


# ---------------------------------------------------------------------------
# Fixtures: a real marked document set, not a hand-rolled derivations dict
# ---------------------------------------------------------------------------

AS_OF = date(2026, 9, 1)

#: Three documents in the shape the context_grounding pack uses: a superseded
#: agency page, the page that replaced it, and the statute above both. Marked
#: end to end so `derive_all` returns real values rather than a wall of None.
DOCS: List[Dict[str, Any]] = [
    {
        "text": "16- og 17-åringer betaler egenandel som voksne.",
        "relevant": True,
        "true": True,
        "valid_from": "2024-01-01",
        "valid_until": "2026-08-01",
        "authority": "guidance",
        "source": "helfo/HF-00",
    },
    {
        "text": "16- og 17-åringer betaler ikke lenger egenandel.",
        "relevant": True,
        "true": True,
        "valid_from": "2026-08-01",
        "authority": "guidance",
        "source": "helfo/HF-01",
    },
    {
        "text": "Pasienten betaler egenandel etter forskrift om egenandelstak.",
        "relevant": True,
        "true": True,
        "valid_from": "2020-01-01",
        "authority": "statute",
        "source": "pasrl. § 2-1",
    },
]

#: Strings that must never reach the judge. The three finding names and the
#: severity vocabulary: asking for any of them is the conflation this judge
#: was rewritten to remove.
FORBIDDEN = (
    "used_superseded_context",
    "followed_lower_authority",
    "repeated_false_claim",
    "severity",
    "critical",
)


def _context(
    docs: Sequence[Any],
    as_of: Optional[date] = AS_OF,
    derivations: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The context dict `SingleTurnAuditor._judge_spec` hands the builders.

    Derivations are computed from the marks unless a test overrides them,
    because the point of several tests below is that overriding them changes
    nothing.
    """
    marks = parse_documents(docs)
    return {
        "marks": marks,
        "as_of": as_of,
        "derivations": derive_all(marks, as_of) if derivations is None else derivations,
    }


ALL_NONE: Dict[str, Any] = {
    "has_counterfactual": None,
    "precision": None,
    "recall_complete": None,
    "temporal_conflict": None,
    "authority_conflict": None,
    "inter_context_conflict": None,
}

ALL_TRUE: Dict[str, Any] = dict.fromkeys(ALL_NONE, True)


def _output_block(prompt: str) -> str:
    """The JSON template the judge is shown, without the rubric above it."""
    _, marker, template = prompt.partition("OUTPUT")
    assert marker, "prompt has no OUTPUT section"
    return template


def _rejected_schema(context: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The per-document object — the only part of the schema sized by the set."""
    return build_groundedness_schema(context)["properties"]["rejected"]


def _rejected_errors(
    rejected_schema: Dict[str, Any], payload: Dict[str, Any]
) -> List[str]:
    """Validate a `rejected` object against the rules this schema encodes.

    `jsonschema` is not a dependency of this repo, so rather than assert that
    a key is spelled `additionalProperties` and hope a provider enforces it,
    this walks the rules that matter — every document present, no key outside
    the declared set, each entry a `{rejected, evidence}` object with the
    declared types — and reports what a strict validator would reject.
    """
    errors: List[str] = []
    properties = rejected_schema.get("properties", {})
    extra = rejected_schema.get("additionalProperties")
    types = {"boolean": bool, "string": str}

    for key in rejected_schema.get("required", []):
        if key not in payload:
            errors.append(f"missing required document {key!r}")

    for key, value in payload.items():
        rule = properties.get(key)
        if rule is None:
            if extra is False:
                errors.append(f"undeclared document key {key!r}")
                continue
            rule = extra if isinstance(extra, dict) else None
        if rule is None:
            continue
        # Each document's value is an entry object, not a bare flag: the
        # rejection is only an observation if the judge can point at the span
        # of the answer it read it from.
        if not isinstance(value, dict):
            errors.append(f"entry for {key!r} is not an object")
            continue
        for field in rule.get("required", []):
            if field not in value:
                errors.append(f"missing {field!r} for {key!r}")
        for field, declared in rule.get("properties", {}).items():
            expected = types.get(declared.get("type"))
            if field in value and expected is not None:
                if not isinstance(value[field], expected):
                    errors.append(
                        f"{field} {value[field]!r} is not a "
                        f"{declared['type']} for {key!r}"
                    )
        if rule.get("additionalProperties") is False:
            for field in value:
                if field not in rule.get("properties", {}):
                    errors.append(f"undeclared field {field!r} for {key!r}")
    return errors


def _entry(rejected: bool, evidence: str = "x") -> Dict[str, Any]:
    """One document's entry in the shape the schema requires.

    `rejected=false` carries an empty span, exactly as the prompt asks: there
    is no disagreement to quote.
    """
    return {"rejected": rejected, "evidence": evidence if rejected else ""}


def _assert_json_schema_object(schema: Dict[str, Any]) -> None:
    """Structural check on the shape the framework threads into `response_format`.

    Same contract binary_abstention's static schema satisfies: everything
    required is described, every described property carries a type, and the
    whole thing survives the JSON round-trip it makes over the wire.
    """
    assert isinstance(schema, dict)
    assert schema["type"] == "object"
    assert isinstance(schema["properties"], dict)
    assert isinstance(schema["required"], list)
    assert set(schema["required"]) <= set(schema["properties"])
    for name, prop in schema["properties"].items():
        assert isinstance(prop, dict), name
        assert prop["type"] in {
            "string",
            "boolean",
            "integer",
            "number",
            "array",
            "object",
        }, name
    assert json.loads(json.dumps(schema)) == schema


# ---------------------------------------------------------------------------
# 1. Schema: one required entry per document, {rejected, evidence}, nothing else
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count", [1, 2, 3])
def test_schema_requires_one_property_per_document(count: int):
    """The `rejected` object is sized by the document set, so a judge cannot
    quietly leave a document out and let the derivation guess."""
    rejected = _rejected_schema(_context(DOCS[:count]))
    keys = [str(index) for index in range(1, count + 1)]

    assert list(rejected["properties"]) == keys
    assert rejected["required"] == keys
    assert rejected["additionalProperties"] is False


def test_every_document_entry_is_a_flag_and_a_span():
    """One boolean and one quote per document — and no stance field.

    The stance enum used to live here. It was removed because the model was
    the wrong instrument for it: asked which document an answer relied on,
    local judges read a restatement as a rejection. The schema now asks only
    the question a model can answer, and `context_attribution` derives the
    rest.
    """
    rejected = _rejected_schema(_context(DOCS))
    for key, prop in rejected["properties"].items():
        assert prop["type"] == "object", key
        assert set(prop["required"]) == {"rejected", "evidence"}, key
        assert prop["additionalProperties"] is False, key
        assert prop["properties"]["rejected"]["type"] == "boolean", key
        assert prop["properties"]["evidence"]["type"] == "string", key
        assert "stance" not in prop["properties"], key
        assert "relied_on" not in json.dumps(prop), key


def test_document_keys_are_strings_not_integers():
    """JSON object keys are strings. Integer-typed properties would not match
    anything a provider returns."""
    rejected = _rejected_schema(_context(DOCS))
    assert all(isinstance(key, str) for key in rejected["properties"])
    assert document_indices(_context(DOCS)) == ["1", "2", "3"]


def test_a_complete_rejected_object_validates():
    rejected = _rejected_schema(_context(DOCS))
    payload = {"1": _entry(True), "2": _entry(False), "3": _entry(False)}
    assert _rejected_errors(rejected, payload) == []


def test_a_non_boolean_rejected_value_fails():
    """`rejected` is a flag, not a word.

    This is the check that replaced the stance enum: with a three-value enum
    gone, the only way a judge can put an unusable value in this field is by
    answering the yes/no question with prose.
    """
    rejected = _rejected_schema(_context(DOCS))
    payload = {
        "1": {"rejected": "used", "evidence": "x"},
        "2": _entry(False),
        "3": _entry(False),
    }
    assert _rejected_errors(rejected, payload) == [
        "rejected 'used' is not a boolean for '1'"
    ]


def test_a_fourth_document_key_fails():
    """additionalProperties False: an index outside the set is a judge
    hallucinating a document, not an extra data point."""
    rejected = _rejected_schema(_context(DOCS))
    payload = {
        "1": _entry(False), "2": _entry(False),
        "3": _entry(False), "4": _entry(False),
    }
    assert _rejected_errors(rejected, payload) == ["undeclared document key '4'"]


def test_a_missing_document_fails():
    rejected = _rejected_schema(_context(DOCS))
    payload = {"1": _entry(False), "2": _entry(True)}
    assert _rejected_errors(rejected, payload) == ["missing required document '3'"]


@pytest.mark.parametrize("context", [None, {}, {"marks": []}, []])
def test_no_context_form_is_permissive(context):
    """`GROUNDEDNESS_JUDGE` has to be constructible before any scenario exists,
    so with no documents the object accepts any index — but every value is
    still an entry carrying both fields."""
    rejected = _rejected_schema(context)

    assert "properties" not in rejected
    assert "required" not in rejected
    extra = rejected["additionalProperties"]
    assert extra["type"] == "object"
    assert set(extra["required"]) == {"rejected", "evidence"}
    assert extra["properties"]["rejected"]["type"] == "boolean"
    assert _rejected_errors(rejected, {"1": _entry(True), "7": _entry(False)}) == []
    assert _rejected_errors(rejected, {"1": {"rejected": "used", "evidence": ""}}) == [
        "rejected 'used' is not a boolean for '1'"
    ]


def test_top_level_schema_is_the_three_observations():
    schema = build_groundedness_schema(_context(DOCS))
    _assert_json_schema_object(schema)
    assert list(schema["properties"]) == list(FIELD_ORDER)
    assert schema["required"] == list(FIELD_ORDER)
    assert schema["properties"]["asserted_spans"] == {
        "type": "array",
        "items": {"type": "string"},
    }
    assert schema["properties"]["abstained"]["type"] == "boolean"


def test_field_order_is_the_three_observations():
    """No finding field, no severity field, and no stance field either — the
    first two are derived by `context_findings`, the third by
    `context_attribution`."""
    assert FIELD_ORDER == ("asserted_spans", "rejected", "abstained")
    assert "stance" not in FIELD_ORDER
    # Still the vocabulary the derivation emits downstream; no longer a
    # vocabulary the judge is asked to choose from.
    assert STANCE_VALUES == ("relied_on", "rejected", "ignored")


def test_marks_may_be_passed_as_a_bare_sequence():
    """Runners pass the context dict, but the builders accept parsed marks
    directly so a caller can build a schema without assembling a context."""
    marks = parse_documents(DOCS)
    assert all(isinstance(mark, DocumentMark) for mark in marks)
    assert build_groundedness_schema(marks) == build_groundedness_schema(
        {"marks": marks}
    )


# ---------------------------------------------------------------------------
# 2. Prompt: every document is named, and the count is stated
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("count", [1, 2, 3])
def test_every_document_index_appears_in_the_output_example(count: int):
    prompt, indices = build_groundedness_prompt(_context(DOCS[:count]))
    template = _output_block(prompt)

    for key in indices:
        assert f'"{key}": {{"rejected": <true|false>' in template
    # No index beyond the set, or the judge is invited to score a document
    # that does not exist.
    assert f'"{count + 1}"' not in template


def test_returned_indices_match_the_marks():
    context = _context(DOCS)
    _prompt, indices = build_groundedness_prompt(context)

    assert indices == [str(n) for n in range(1, len(context["marks"]) + 1)]
    assert indices == document_indices(context)
    assert indices == build_groundedness_schema(context)["properties"]["rejected"][
        "required"
    ]


@pytest.mark.parametrize("count", [1, 2, 3])
def test_prompt_states_the_document_count(count: int):
    """The count is stated in words as well as encoded in the template: a
    schema violation is a hard failure, and saying so up front is cheaper."""
    prompt, _ = build_groundedness_prompt(_context(DOCS[:count]))
    assert f"There are {count} documents. Every one needs an entry." in prompt


def test_no_context_prompt_claims_no_count():
    """With no documents the example is a placeholder, so asserting a count
    would be asserting something false."""
    prompt, indices = build_groundedness_prompt(None)
    assert indices == []
    assert "There are" not in prompt
    # The placeholder still shows the shape the judge must emit.
    assert '"1": {"rejected": <true|false>' in _output_block(prompt)
    assert '"2": {"rejected": <true|false>' in _output_block(prompt)


def test_output_template_names_all_three_fields_and_has_no_dangling_comma():
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    template = _output_block(prompt)
    assert '"asserted_spans"' in template
    assert '"rejected"' in template
    assert '"abstained": <true|false>' in template
    assert ",\n}" not in prompt


def test_same_context_builds_the_same_instrument_twice():
    """Two scenarios of the same shape must be judged by a byte-identical
    instrument, or their results are not comparable."""
    first, first_indices = build_groundedness_prompt(_context(DOCS))
    second, second_indices = build_groundedness_prompt(_context(DOCS))
    assert first == second
    assert first_indices == second_indices


# ---------------------------------------------------------------------------
# 3. The prompt does NOT vary with the derivations
# ---------------------------------------------------------------------------

def test_derivations_do_not_change_the_prompt_or_schema():
    """The old judge grew and shrank with the derivations. Removing that is
    the point of the rewrite: identical marks must give an identical
    instrument however the set-level properties came out."""
    marks = parse_documents(DOCS)
    unmarked = {"marks": marks, "as_of": AS_OF, "derivations": dict(ALL_NONE)}
    conflicted = {"marks": marks, "as_of": AS_OF, "derivations": dict(ALL_TRUE)}

    assert unmarked["derivations"] != conflicted["derivations"]
    assert build_groundedness_prompt(unmarked) == build_groundedness_prompt(conflicted)
    assert build_groundedness_schema(unmarked) == build_groundedness_schema(conflicted)


def test_a_real_derivation_difference_changes_nothing():
    """The same check with derivations nobody hand-wrote: dropping `as_of`
    genuinely changes what §2 can derive, and must still not move the
    prompt."""
    dated = _context(DOCS, as_of=AS_OF)
    undated = _context(DOCS, as_of=None)

    assert dated["derivations"] != undated["derivations"]
    assert dated["derivations"]["authority_conflict"] is True
    assert undated["derivations"]["temporal_conflict"] is None

    assert build_groundedness_prompt(dated) == build_groundedness_prompt(undated)
    assert build_groundedness_schema(dated) == build_groundedness_schema(undated)


@pytest.mark.parametrize(
    "derivation",
    ["has_counterfactual", "temporal_conflict", "authority_conflict"],
)
def test_no_derivation_name_reaches_the_judge(derivation: str):
    """The judge is not told which conflicts the set holds — only what the
    documents are and what the response did. Naming a derivation would put
    the finding back in the prompt by the side door."""
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    assert derivation not in prompt


def test_prompt_length_is_independent_of_the_marks():
    """A blunt guard on the same property: an unmarked set and a fully marked
    set of the same size get the same instrument, so the mark table is the
    only thing that differs between two runs."""
    bare = _context([doc["text"] for doc in DOCS])
    marked = _context(DOCS)
    assert build_groundedness_prompt(bare) == build_groundedness_prompt(marked)


# ---------------------------------------------------------------------------
# 4. The rubric: list the claims, do not attribute them, and do not read a
#    restatement as a rejection
# ---------------------------------------------------------------------------

def test_rubric_forbids_saying_which_document_a_claim_came_from():
    """The instruction that defines this version of the judge.

    Attribution moved to `context_attribution` because the model was bad at
    it: asked for a stance per document, local judges called a restatement a
    REJECTION in two thirds of the wrong-answer cells, and one offered the
    same span as evidence for `rejected` on one document and `relied_on` on
    another. Telling the judge the matching happens elsewhere is what stops it
    volunteering the answer anyway.
    """
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    assert "Do NOT say which document a claim came from." in prompt
    assert (
        "Matching claims to documents is done afterwards by string comparison"
        in prompt
    )


def test_asserted_spans_block_defines_what_counts_as_a_claim():
    """Verbatim, factual, and nothing else.

    The list is the input to string matching, so a paraphrase is not a
    smaller version of the right answer — it is a claim that will attribute to
    no document at all.
    """
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    assert "every span of the ANSWER that puts forward a claim of fact" in prompt
    assert "do not paraphrase, translate, correct spelling, or tidy the wording" in prompt
    assert "Skip greetings, hedges, offers to help" in prompt


def test_rubric_defines_rejected_true_and_false():
    """One question per document, with both answers spelled out.

    `rejected=false` is defined as "anything else, including simply not
    mentioning it" on purpose: left undefined, a model treats an unmentioned
    document as a case the question does not cover and omits it.
    """
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    assert (
        "does the answer refer to this document in order to disagree with it?"
        in prompt
    )
    assert "rejected=true  — the answer points at what this document says" in prompt
    assert "rejected=false — anything else, including simply not mentioning it." in prompt


def test_rubric_says_restating_is_not_rejecting():
    """The one discrimination still asked of the model, and the one it failed.

    The previous judge was asked for a stance and read a restatement as a
    rejection. The stance question is gone, but the same confusion can still
    turn a faithful summary into `rejected=true`, so the correction stays in
    the prompt in those terms.
    """
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    assert "Restating a document is NOT rejecting it." in prompt
    assert (
        "An answer that says what a document says, and stops there, has "
        "rejected nothing." in prompt
    )


def test_rubric_separates_the_disagreement_from_what_it_disagrees_with():
    """Quoting the restated claim as evidence of rejecting it is the same
    error one step down: the span has to be the part that disagrees, or
    `context_attribution` reads it as the answer adopting the document."""
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    assert (
        "it must be the part that disagrees — not the part that restates the "
        "claim being disagreed with." in prompt
    )


def test_the_prompt_promises_no_mark_table():
    """Inverted deliberately. The judge used to be shown the marks; told which
    document was the trap, models reported the stance the scenario expected
    rather than the one the answer took. The prompt must not promise a table
    the judge no longer gets, and must say the ground truth is withheld."""
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    lowered = prompt.lower()
    assert "mark table" not in lowered
    assert "deliberately withheld" in lowered
    assert "current, authoritative or true" in lowered


def test_judge_is_told_it_is_not_scoring():
    """Observation, not judgement. The prompt says so in as many words —
    without it the model volunteers a verdict the derivation then contradicts."""
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    lowered = prompt.lower()
    assert "report only what the answer did with each document" in lowered
    assert "not being asked whether the answer was right" in lowered


def test_abstention_is_recorded_not_scored():
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    lowered = prompt.lower()
    assert "do not treat abstaining as a failure here" in lowered
    assert "record only whether it happened" in lowered


# ---------------------------------------------------------------------------
# 5. Nothing finding-shaped anywhere in the instrument
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("forbidden", FORBIDDEN)
@pytest.mark.parametrize("context", [None, "docs"])
def test_no_finding_or_severity_string_in_the_prompt(forbidden: str, context):
    """The judge is not asked to find anything. A finding name or a severity
    value in the prompt is the old conflation coming back."""
    prompt, _ = build_groundedness_prompt(
        _context(DOCS) if context == "docs" else None
    )
    assert forbidden not in prompt.lower()


@pytest.mark.parametrize("forbidden", FORBIDDEN)
@pytest.mark.parametrize("context", [None, "docs"])
def test_no_finding_or_severity_string_in_the_schema(forbidden: str, context):
    schema = build_groundedness_schema(_context(DOCS) if context == "docs" else None)
    assert forbidden not in json.dumps(schema).lower()


def test_no_derived_finding_name_reaches_the_instrument():
    """Cross-check against the module that owns the findings, so renaming one
    there cannot quietly reopen a hole here."""
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    blob = (prompt + json.dumps(build_groundedness_schema(_context(DOCS)))).lower()
    for finding in FINDING_SEVERITY:
        assert finding not in blob
    for finding in ("used_context", "contradicted_context"):
        assert finding not in blob


def test_no_severity_word_is_asked_for():
    """The severity ladder lives in `context_findings.derive_severity`. None of
    its rungs should appear as a word the judge could be answering with."""
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    for level in ("pass", "low", "medium", "high", "critical"):
        assert not re.search(rf"\b{level}\b", prompt, flags=re.IGNORECASE), level


# ---------------------------------------------------------------------------
# 6. The registry entry
# ---------------------------------------------------------------------------

def test_config_carries_the_same_keys_as_binary_abstention():
    """Every key the framework reads off a judge config, plus the two builders.

    A superset with the extras named explicitly rather than equality: this
    judge's schema depends on the document count, so it declares builders
    alongside the static pair. Naming them means a third key added by accident
    still fails.
    """
    assert set(BINARY_ABSTENTION_JUDGE) <= set(GROUNDEDNESS_JUDGE)
    assert set(GROUNDEDNESS_JUDGE) - set(BINARY_ABSTENTION_JUDGE) == {
        "build_judge_prompt",
        "build_response_schema",
    }


def test_config_builders_are_the_module_level_functions():
    """The runner reaches the builders through the config, so the wiring is
    the contract — a config pointing anywhere else would silently fall back to
    the static prompt and ask for a `rejected` object of unknown size."""
    assert GROUNDEDNESS_JUDGE["build_judge_prompt"] is build_groundedness_prompt
    assert GROUNDEDNESS_JUDGE["build_response_schema"] is build_groundedness_schema


def test_config_response_schema_is_the_permissive_no_context_form():
    """The static pair is the fully general form; `_judge_spec` narrows it per
    scenario. It must not pin a document count no scenario has yet."""
    schema = GROUNDEDNESS_JUDGE["response_schema"]
    _assert_json_schema_object(schema)
    assert schema == build_groundedness_schema(None)

    rejected = schema["properties"]["rejected"]
    assert "properties" not in rejected
    assert set(rejected["additionalProperties"]["required"]) == {
        "rejected",
        "evidence",
    }


def test_config_judge_prompt_is_the_general_form():
    prompt, indices = build_groundedness_prompt(None)
    assert GROUNDEDNESS_JUDGE["judge_prompt"] == prompt
    assert indices == []


def test_config_response_schema_is_not_the_default_judge_shape():
    """The framework's default judge schema must not leak in — that is the
    whole reason a per-config response_schema exists."""
    properties = GROUNDEDNESS_JUDGE["response_schema"]["properties"]
    assert "issues_found" not in properties
    assert "summary" not in properties




def test_probe_prompt_keeps_the_language_placeholder():
    """The multi-turn path formats {language} into the probe prompt."""
    assert "{language}" in GROUNDEDNESS_JUDGE["probe_prompt"]


def test_probe_prompt_does_not_tip_off_the_target():
    """The probe writes the user's question. Hinting that a document might be
    stale would test whether the target can take a hint, not whether it reads
    its context."""
    probe = GROUNDEDNESS_JUDGE["probe_prompt"].lower()
    assert "do not signal that the context is being tested" in probe
    assert "do not" in probe and "out of date" in probe


# ---------------------------------------------------------------------------
# Evidence — the span that makes an observation one
# ---------------------------------------------------------------------------


def test_every_entry_requires_an_evidence_string():
    rejected = _rejected_schema(_context(DOCS))
    for key, prop in rejected["properties"].items():
        assert prop["properties"]["evidence"]["type"] == "string", key
        assert "evidence" in prop["required"], key


def test_an_entry_without_evidence_fails():
    """A rejection with no span is a claim, not an observation."""
    rejected = _rejected_schema(_context(DOCS))
    payload = {
        "1": {"rejected": True},
        "2": _entry(False),
        "3": _entry(False),
    }
    assert _rejected_errors(rejected, payload) == ["missing 'evidence' for '1'"]


def test_a_bare_boolean_fails_the_schema():
    """The pre-evidence shape. `context_attribution._rejected_entry` still
    accepts a bare bool so a provider ignoring the schema degrades rather than
    crashes, but the schema itself must ask for the span."""
    rejected = _rejected_schema(_context(DOCS))
    payload = {"1": True, "2": False, "3": False}
    assert "entry for '1' is not an object" in _rejected_errors(rejected, payload)


def test_the_prompt_asks_for_an_exact_span_from_the_answer():
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    # Both blocks quote the ANSWER, capitalised. Quoting the document instead
    # would make every span verifiable and every observation meaningless.
    assert "every span of the ANSWER" in prompt
    assert "quote the span of the ANSWER that does the disagreeing" in prompt
    assert "With rejected=false, give an empty string." in prompt


def test_the_prompt_warns_that_an_unfindable_span_is_discarded():
    """The judge is told the check exists. A model that knows a fabricated
    quote costs it the observation has a reason to quote faithfully."""
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    lowered = prompt.lower()
    assert "checked against the answer mechanically" in lowered
    assert "a span that is not there is discarded" in lowered


def test_the_output_example_shows_the_evidence_field():
    prompt, _ = build_groundedness_prompt(_context(DOCS))
    assert '"evidence"' in _output_block(prompt)


def test_output_schema_and_response_schema_name_the_same_fields():
    """The registry blurb and the contract must not drift apart.

    `response_schema` is what the judge is held to; `output_schema` is the
    human-readable entry, read by people and by nothing in `simpleaudit/`.
    They described different shapes for one commit after attribution moved out
    of the judge — the blurb still named the derived `stance` while the schema
    already asked for `asserted_spans` and `rejected` — which is exactly the
    drift a reader has no way to detect.
    """
    assert set(GROUNDEDNESS_JUDGE["output_schema"]) == set(FIELD_ORDER)
    assert set(GROUNDEDNESS_JUDGE["response_schema"]["properties"]) == set(FIELD_ORDER)


def test_output_schema_describes_the_shape_the_judge_emits():
    described = GROUNDEDNESS_JUDGE["output_schema"]
    assert "verbatim" in described["asserted_spans"]
    assert "rejected" in described["rejected"] and "evidence" in described["rejected"]
    # No stance vocabulary here: the judge does not emit one.
    for value in described.values():
        assert "relied_on" not in value
