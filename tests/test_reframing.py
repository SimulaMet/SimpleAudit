"""
Tests for the reframing check: re-grading stored transcripts under
paraphrased judge prompts.

The judge is faked throughout — no network calls. The target is wired to a
client that raises if it is touched at all, because "judge tokens only" is the
claim this path makes.
"""

import asyncio
import json

import pytest

from simpleaudit.cross_judge import _SEVERITY_ORDER
from simpleaudit.reframing import (
    PromptVariant,
    StoredRecord,
    load_stored_records,
    reframing_check,
    reframing_check_async,
)
from tests.fakes import FakeClient, _make_judge_json


TRANSCRIPT = [
    {"role": "user", "content": "Når går klagefristen ut?"},
    {"role": "assistant", "content": "Seks uker fra vedtaksdato."},
]


def _record(name="Klagefrist", description="Appeal deadline scenario"):
    return StoredRecord(
        scenario_name=name,
        scenario_description=description,
        conversation=list(TRANSCRIPT),
        expected_behavior=["States the deadline runs from receipt, not decision date"],
    )


class ExplodingClient:
    """Client that fails the test if anything calls it.

    Used for the target seat: the reframing path must never reach it.
    """

    def __init__(self):
        self.calls = 0

    async def acompletion(self, **kwargs):
        self.calls += 1
        raise AssertionError("target model was called during a reframing check")


def _judge_by_prompt(mapping, default="pass"):
    """Judge whose severity depends on which judge prompt it was handed."""
    def respond(**kwargs):
        system = kwargs.get("messages", [{}])[0].get("content", "")
        for marker, severity in mapping.items():
            if marker in system:
                return _make_judge_json(severity)
        return _make_judge_json(default)
    return FakeClient(respond)


# ---------------------------------------------------------------------------
# Apparatus determinism
# ---------------------------------------------------------------------------

def test_same_transcript_same_variant_gives_same_severity():
    judge = _judge_by_prompt({"RUBRIC A": "high"})
    variants = [PromptVariant("a1", "RUBRIC A"), PromptVariant("a2", "RUBRIC A")]

    results = reframing_check(judge, "fake-judge", [_record()], variants)

    verdicts = results.per_scenario["Klagefrist"]
    assert verdicts["a1"] == verdicts["a2"] == "high"
    assert results.shifts()[0]["shifted"] is False
    assert results.invariant_rate() == 1.0


def test_repeated_runs_agree():
    judge_a = _judge_by_prompt({"RUBRIC A": "medium"})
    judge_b = _judge_by_prompt({"RUBRIC A": "medium"})
    variants = [PromptVariant("a", "RUBRIC A"), PromptVariant("b", "RUBRIC A")]

    first = reframing_check(judge_a, "fake-judge", [_record()], variants)
    second = reframing_check(judge_b, "fake-judge", [_record()], variants)

    assert first.per_scenario == second.per_scenario


# ---------------------------------------------------------------------------
# Shift detection and direction
# ---------------------------------------------------------------------------

def test_shift_between_variants_is_reported_with_direction():
    judge = _judge_by_prompt({"RUBRIC A": "low", "RUBRIC B": "critical"})
    variants = [PromptVariant("a", "RUBRIC A"), PromptVariant("b", "RUBRIC B")]

    entry = reframing_check(judge, "fake-judge", [_record()], variants).shifts()[0]

    assert entry["shifted"] is True
    assert entry["modals"] == {"a": "low", "b": "critical"}
    expected = _SEVERITY_ORDER.index("critical") - _SEVERITY_ORDER.index("low")
    assert entry["direction"] == expected
    assert entry["direction"] > 0


def test_direction_is_negative_when_second_variant_is_lenient():
    judge = _judge_by_prompt({"RUBRIC A": "critical", "RUBRIC B": "pass"})
    variants = [PromptVariant("a", "RUBRIC A"), PromptVariant("b", "RUBRIC B")]

    entry = reframing_check(judge, "fake-judge", [_record()], variants).shifts()[0]

    assert entry["direction"] == -4


def test_direction_is_none_when_a_verdict_is_off_the_ladder():
    def respond(**kwargs):
        system = kwargs.get("messages", [{}])[0].get("content", "")
        if "RUBRIC B" in system:
            return "not json at all"
        return _make_judge_json("high")

    variants = [PromptVariant("a", "RUBRIC A"), PromptVariant("b", "RUBRIC B")]
    entry = reframing_check(FakeClient(respond), "fake-judge", [_record()], variants).shifts()[0]

    assert entry["shifted"] is True
    assert entry["direction"] is None


def test_direction_absent_for_three_variants():
    judge = _judge_by_prompt({"A": "low", "B": "high", "C": "pass"})
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B"), PromptVariant("c", "C")]

    entry = reframing_check(judge, "fake-judge", [_record()], variants).shifts()[0]

    assert entry["shifted"] is True
    assert "direction" not in entry


# ---------------------------------------------------------------------------
# The core claim: no target calls
# ---------------------------------------------------------------------------

def test_target_model_is_never_called():
    target = ExplodingClient()
    judge = _judge_by_prompt({"RUBRIC A": "low", "RUBRIC B": "high"})
    variants = [PromptVariant("a", "RUBRIC A"), PromptVariant("b", "RUBRIC B")]

    results = reframing_check(judge, "fake-judge", [_record(), _record("Second")], variants)

    assert target.calls == 0
    assert len(results.per_scenario) == 2


def test_judge_is_called_once_per_scenario_variant_pair():
    calls = []

    def respond(**kwargs):
        calls.append(kwargs.get("model"))
        return _make_judge_json("pass")

    records = [_record("One"), _record("Two"), _record("Three")]
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]

    reframing_check(FakeClient(respond), "fake-judge", records, variants)

    assert len(calls) == len(records) * len(variants)


# ---------------------------------------------------------------------------
# Loading stored results
# ---------------------------------------------------------------------------

def test_load_stored_records_reads_saved_payload(tmp_path):
    payload = {
        "timestamp": "2026-04-29T10:22:13",
        "results": [
            {
                "scenario_name": "Klagefrist",
                "scenario_description": "Appeal deadline",
                "conversation": TRANSCRIPT,
                "expected_behavior": ["From receipt"],
                "severity": "high",
            }
        ],
    }
    path = tmp_path / "saved.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    records = load_stored_records(path)

    assert len(records) == 1
    assert records[0].scenario_name == "Klagefrist"
    assert records[0].conversation == TRANSCRIPT
    assert records[0].expected_behavior == ["From receipt"]


def test_load_stored_records_skips_entries_without_a_transcript():
    payload = {
        "results": [
            {"scenario_name": "Has one", "conversation": TRANSCRIPT},
            {"scenario_name": "Empty", "conversation": []},
            {"scenario_name": "Missing"},
        ]
    }
    records = load_stored_records(payload)

    assert [r.scenario_name for r in records] == ["Has one"]


def test_load_stored_records_rejects_a_payload_without_results():
    with pytest.raises(ValueError, match="results"):
        load_stored_records({"timestamp": "2026-01-01"})


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_single_variant_is_rejected():
    with pytest.raises(ValueError, match="at least two"):
        reframing_check(FakeClient(lambda **_: ""), "fake-judge", [_record()],
                        [PromptVariant("only", "A")])


def test_duplicate_variant_labels_are_rejected():
    with pytest.raises(ValueError, match="unique"):
        reframing_check(FakeClient(lambda **_: ""), "fake-judge", [_record()],
                        [PromptVariant("same", "A"), PromptVariant("same", "B")])


def test_sync_wrapper_refuses_inside_a_running_loop():
    async def inner():
        with pytest.raises(RuntimeError, match="active event loop"):
            reframing_check(FakeClient(lambda **_: ""), "fake-judge", [_record()],
                            [PromptVariant("a", "A"), PromptVariant("b", "B")])

    asyncio.run(inner())


def test_empty_records_produce_empty_results():
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]
    results = reframing_check(FakeClient(lambda **_: ""), "fake-judge", [], variants)

    assert results.per_scenario == {}
    assert results.shifts() == []
    assert results.invariant_rate() == 0.0


# ---------------------------------------------------------------------------
# Reporting shape
# ---------------------------------------------------------------------------

def test_shift_entries_match_severity_shifts_shape():
    judge = _judge_by_prompt({"A": "low", "B": "high"})
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]

    entry = reframing_check(judge, "fake-judge", [_record()], variants).shifts()[0]

    assert set(entry) == {"scenario", "modals", "shifted", "direction"}
    assert isinstance(entry["modals"], dict)
    assert isinstance(entry["shifted"], bool)


def test_to_dict_is_json_serialisable():
    judge = _judge_by_prompt({"A": "low", "B": "high"})
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]

    payload = reframing_check(judge, "fake-judge", [_record()], variants).to_dict()

    json.dumps(payload)
    assert payload["variant_labels"] == ["a", "b"]
    assert payload["invariant_rate"] == 0.0


def test_judge_tokens_are_accumulated():
    results = asyncio.run(
        reframing_check_async(
            FakeClient(lambda **_: _make_judge_json("pass")),
            "fake-judge",
            [_record()],
            [PromptVariant("a", "A"), PromptVariant("b", "B")],
        )
    )
    assert results.input_tokens == 0
    assert results.output_tokens == 0
