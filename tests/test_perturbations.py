"""
Tests for the deterministic transcript perturbations.

Pure functions, no model anywhere: a perturbation changes the surface of the
assistant turns and nothing else, and does the same thing every time.
"""

import copy

import pytest

from simpleaudit.perturbations import (
    _TEXTS,
    PERTURBATIONS,
    SUPPORTED_LANGUAGES,
    apologetic_opener,
    compose,
    perturbation_variants,
    self_certification,
)
from simpleaudit.reframing import PromptVariant, _check_transform_output

CONVERSATION = [
    {"role": "user", "content": "Hva er egenandelstaket i år?", "file_uri": "images/frikort.png"},
    {"role": "assistant", "content": "Egenandelstaket er 3 278 kroner.", "meta": {"turn": 1}},
    {"role": "user", "content": "Er du sikker?"},
    {"role": "assistant", "content": "Ja, det gjelder for 2026."},
]


@pytest.mark.parametrize("name", list(PERTURBATIONS))
@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_each_perturbation_wraps_every_assistant_turn_and_leaves_user_turns_alone(name, language):
    prefix, suffix = _TEXTS[name][language]

    out = PERTURBATIONS[name](language)(CONVERSATION)

    assert len(out) == len(CONVERSATION)
    for before, after in zip(CONVERSATION, out, strict=True):
        assert after["role"] == before["role"]
        if before["role"] == "assistant":
            assert after["content"] == f"{prefix}{before['content']}{suffix}"
            assert after["content"] != before["content"]
        else:
            assert after == before
    assert prefix or suffix


@pytest.mark.parametrize("name", list(PERTURBATIONS))
def test_perturbations_are_pure_and_deterministic(name):
    snapshot = copy.deepcopy(CONVERSATION)
    transform = PERTURBATIONS[name]("Norwegian")

    first = transform(CONVERSATION)
    second = transform(CONVERSATION)

    assert CONVERSATION == snapshot
    assert first == second
    assert first is not CONVERSATION
    assert all(a is not b for a, b in zip(first, CONVERSATION, strict=True))


def test_extra_keys_and_file_uri_survive():
    out = apologetic_opener("English")(CONVERSATION)

    assert out[0]["file_uri"] == "images/frikort.png"
    assert out[1]["meta"] == {"turn": 1}
    _check_transform_output(CONVERSATION, out, "apologetic_opener")


@pytest.mark.parametrize("name", list(PERTURBATIONS))
def test_every_builtin_passes_the_engine_structure_check(name):
    out = PERTURBATIONS[name]("English")(CONVERSATION)
    _check_transform_output(CONVERSATION, out, name)


def test_transform_name_is_the_perturbation_name():
    for name, factory in PERTURBATIONS.items():
        assert factory("English").__name__ == name


def test_non_string_assistant_content_raises():
    conversation = [{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}]
    with pytest.raises(TypeError, match="non-string content"):
        apologetic_opener("English")(conversation)


def test_unsupported_language_raises_and_aliases_resolve():
    with pytest.raises(ValueError, match="Unsupported perturbation language"):
        apologetic_opener("Klingon")

    norwegian = _TEXTS["apologetic_opener"]["Norwegian"][0]
    for alias in ("no", "nb", "norsk", "NORWEGIAN"):
        assert apologetic_opener(alias)(CONVERSATION)[1]["content"].startswith(norwegian)
    english = _TEXTS["apologetic_opener"]["English"][0]
    assert apologetic_opener("en")(CONVERSATION)[1]["content"].startswith(english)


def test_compose_applies_in_order_and_names_itself():
    composed = compose(apologetic_opener("English"), self_certification("English"))

    out = composed(CONVERSATION)

    opener = _TEXTS["apologetic_opener"]["English"][0]
    certification = _TEXTS["self_certification"]["English"][1]
    assert out[1]["content"].startswith(opener)
    assert out[1]["content"].endswith(certification)
    assert out[0] == CONVERSATION[0]
    assert composed.__name__ == "apologetic_opener+self_certification"
    with pytest.raises(ValueError, match="at least one"):
        compose()


def test_perturbation_variants_builds_baseline_plus_one_per_perturbation():
    variants = perturbation_variants("RUBRIC", language="Norwegian")

    assert [v.label for v in variants] == ["baseline", *PERTURBATIONS]
    assert all(isinstance(v, PromptVariant) for v in variants)
    assert all(v.judge_prompt == "RUBRIC" for v in variants)
    assert variants[0].transform is None
    for variant in variants[1:]:
        assert variant.transform.__name__ == variant.label
        assert variant.judge_model is None and variant.judge_client is None


def test_perturbation_variants_accept_a_subset_and_a_schema():
    schema = {"type": "object"}
    variants = perturbation_variants(
        "RUBRIC", names=["hedging_disclaimer"], baseline_label="plain", response_schema=schema
    )

    assert [v.label for v in variants] == ["plain", "hedging_disclaimer"]
    assert all(v.response_schema is schema for v in variants)


def test_perturbation_variants_reject_unknown_names_and_label_collisions():
    with pytest.raises(ValueError, match="Unknown perturbation"):
        perturbation_variants("RUBRIC", names=["sarcasm"])
    with pytest.raises(ValueError, match="collides"):
        perturbation_variants("RUBRIC", baseline_label="verbose_padding")
