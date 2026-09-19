"""
Tests for the reframing check: re-grading stored transcripts under
paraphrased judge prompts.

The judge is faked throughout — no network calls. The target is wired to a
client that raises if it is touched at all, because "judge tokens only" is the
claim this path makes.
"""

import asyncio
import itertools
import json
import warnings
from unittest.mock import patch

import pytest

from simpleaudit.cross_judge import _SEVERITY_ORDER
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.reframing import (
    PanelResults,
    PromptVariant,
    SampleStats,
    StoredRecord,
    VariantEffect,
    _check_transform_output,
    load_stored_records,
    make_judge_client,
    reframing_check,
    reframing_check_async,
)
from simpleaudit.repeated_results import _normalised_entropy, _ordinal_spread
from simpleaudit.utils import SEVERITY_ORDER
from tests.fakes import FakeClient, ScriptedClient, _make_judge_json, _make_response

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


# ===========================================================================
# Fixed-transcript judge robustness: judge axis, resampling, transforms,
# effects, panels, client helper.
# ===========================================================================

def _judge_by_model(mapping, default="pass"):
    """Judge whose severity depends on the model name it was called with."""
    def respond(**kwargs):
        return _make_judge_json(mapping.get(kwargs.get("model"), default))
    return FakeClient(respond)


def _cycling(severities):
    pool = itertools.cycle(severities)
    return FakeClient(lambda **_: _make_judge_json(next(pool)))


# ---------------------------------------------------------------------------
# Judge axis: a variant may carry its own judge
# ---------------------------------------------------------------------------

def test_variant_judge_model_overrides_call_level_model():
    judge = _judge_by_model({"gemma": "high"}, default="low")
    variants = [PromptVariant("a", "R"), PromptVariant("b", "R", judge_model="gemma")]

    results = reframing_check(judge, "fake-judge", [_record()], variants)

    assert results.per_scenario["Klagefrist"] == {"a": "low", "b": "high"}
    assert results.shifts()[0]["direction"] > 0
    assert results.variant_meta["a"]["judge_model"] == "fake-judge"
    assert results.variant_meta["b"]["judge_model"] == "gemma"


def test_variant_judge_client_overrides_call_level_client():
    default_calls = []

    def default_respond(**kwargs):
        default_calls.append(kwargs.get("model"))
        return _make_judge_json("low")

    own = FakeClient(lambda **_: _make_judge_json("critical"))
    variants = [PromptVariant("a", "R"), PromptVariant("b", "R", judge_client=own)]

    results = reframing_check(FakeClient(default_respond), "fake-judge", [_record()], variants)

    assert results.per_scenario["Klagefrist"] == {"a": "low", "b": "critical"}
    assert len(default_calls) == 1
    assert results.variant_meta["a"]["judge_client_id"] != results.variant_meta["b"]["judge_client_id"]


# ---------------------------------------------------------------------------
# Resampling: k samples per cell
# ---------------------------------------------------------------------------

def test_k_repetitions_call_judge_k_times_per_pair():
    calls = []

    def respond(**kwargs):
        calls.append(1)
        return _make_judge_json("pass")

    records = [_record("One"), _record("Two")]
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]

    results = reframing_check(FakeClient(respond), "fake-judge", records, variants, k=3)

    assert len(calls) == 2 * 2 * 3
    assert results.k == 3


def test_k_samples_are_kept_in_order():
    results = reframing_check(
        _cycling(["pass", "critical", "pass"]), "fake-judge", [_record()],
        [PromptVariant("a", "A")], k=3,
    )

    assert results.samples["Klagefrist"]["a"] == ["pass", "critical", "pass"]
    assert results.per_scenario["Klagefrist"]["a"] == "pass"


def test_stability_cell_matches_per_scenario_and_reuses_repeated_results_statistics():
    results = reframing_check(
        _cycling(["pass", "critical", "pass"]), "fake-judge", [_record()],
        [PromptVariant("a", "A")], k=3,
    )

    cell = results.stability()["Klagefrist"]["a"]

    assert isinstance(cell, SampleStats)
    assert cell.modal == results.per_scenario["Klagefrist"]["a"]
    assert cell.agreement_rate == pytest.approx(2 / 3)
    assert cell.n == 3
    assert cell.normalised_entropy == round(_normalised_entropy(cell.severities), 4)
    assert cell.ordinal_spread == round(_ordinal_spread(cell.severities), 4)


def test_expected_index_is_mean_ladder_position():
    results = reframing_check(
        _cycling(["pass", "critical"]), "fake-judge", [_record()],
        [PromptVariant("a", "A")], k=2,
    )
    cell = results.stability()["Klagefrist"]["a"]

    assert cell.expected_index == (SEVERITY_ORDER.index("pass") + SEVERITY_ORDER.index("critical")) / 2


def test_expected_index_and_spread_are_none_when_a_sample_is_off_the_ladder():
    pool = itertools.cycle([_make_judge_json("pass"), "not json at all"])
    results = reframing_check(
        FakeClient(lambda **_: next(pool)), "fake-judge", [_record()],
        [PromptVariant("a", "A")], k=2,
    )
    cell = results.stability()["Klagefrist"]["a"]

    assert cell.severities == ["pass", "ERROR"]
    assert cell.expected_index is None
    assert cell.ordinal_spread is None
    assert cell.normalised_entropy > 0


def test_shifts_and_invariant_rate_use_the_modal_under_resampling():
    # Calls run record -> variant -> repetition, so each cell sees
    # pass, pass, critical: samples differ, modals agree.
    results = reframing_check(
        _cycling(["pass", "pass", "critical"]), "fake-judge", [_record()],
        [PromptVariant("a", "A"), PromptVariant("b", "B")], k=3,
    )

    assert results.samples["Klagefrist"]["a"] == ["pass", "pass", "critical"]
    assert results.samples["Klagefrist"]["b"] == ["pass", "pass", "critical"]
    assert results.shifts()[0]["shifted"] is False
    assert results.invariant_rate() == 1.0


def test_judgments_hold_the_sample_behind_the_modal():
    results = reframing_check(
        _cycling(["critical", "pass", "pass"]), "fake-judge", [_record()],
        [PromptVariant("a", "A")], k=3,
    )

    assert results.judgments["Klagefrist"]["a"]["severity"] == "pass"
    assert len(results.sample_judgments["Klagefrist"]["a"]) == 3


def test_fragile_returns_low_agreement_cells_and_warns_at_k_one():
    results = reframing_check(
        _cycling(["pass", "critical", "low", "pass", "critical"]), "fake-judge", [_record()],
        [PromptVariant("a", "A")], k=5,
    )

    assert "Klagefrist" in results.fragile(threshold=0.6)
    assert results.fragile(threshold=0.3) == {}
    with pytest.raises(ValueError, match="threshold"):
        results.fragile(threshold=1.5)

    single = reframing_check(
        _judge_by_prompt({"A": "low"}), "fake-judge", [_record()],
        [PromptVariant("a", "A"), PromptVariant("b", "A")],
    )
    with pytest.warns(UserWarning, match="k >= 2"):
        assert single.fragile() == {}


def test_tokens_are_accumulated_per_variant():
    client = ScriptedClient([(_make_judge_json("low"), 10, 5), (_make_judge_json("high"), 20, 7)])

    results = reframing_check(client, "fake-judge", [_record()],
                              [PromptVariant("a", "A"), PromptVariant("b", "B")])

    assert results.tokens_by_variant == {"a": {"input": 10, "output": 5},
                                         "b": {"input": 20, "output": 7}}
    assert (results.input_tokens, results.output_tokens) == (30, 12)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def test_single_variant_allowed_when_k_gt_1():
    results = reframing_check(_judge_by_prompt({"A": "low"}), "fake-judge", [_record()],
                              [PromptVariant("only", "A")], k=2)
    assert results.samples["Klagefrist"]["only"] == ["low", "low"]


def test_k_below_one_is_rejected():
    with pytest.raises(ValueError, match="k must be"):
        reframing_check(FakeClient(lambda **_: ""), "fake-judge", [_record()],
                        [PromptVariant("a", "A"), PromptVariant("b", "B")], k=0)


def test_max_concurrency_below_one_is_rejected():
    with pytest.raises(ValueError, match="max_concurrency"):
        reframing_check(FakeClient(lambda **_: ""), "fake-judge", [_record()],
                        [PromptVariant("a", "A"), PromptVariant("b", "B")], max_concurrency=0)


def test_baseline_must_be_a_variant_label():
    with pytest.raises(ValueError, match="baseline"):
        reframing_check(FakeClient(lambda **_: ""), "fake-judge", [_record()],
                        [PromptVariant("a", "A"), PromptVariant("b", "B")], baseline="zzz")


def test_duplicate_scenario_names_warn_and_last_record_wins():
    judge = _judge_by_prompt({"A": "low", "B": "high"})
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]

    with pytest.warns(UserWarning, match="Duplicate scenario names"):
        results = reframing_check(judge, "fake-judge", [_record("Same"), _record("Same")], variants)

    assert list(results.per_scenario) == ["Same"]
    assert results.samples["Same"]["a"] == ["low"]


def test_concurrency_preserves_result_assignment():
    judge = _judge_by_prompt({"A": "low", "B": "high"})
    records = [_record("One"), _record("Two"), _record("Three")]
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]

    sequential = reframing_check(judge, "fake-judge", records, variants, k=2, max_concurrency=1)
    parallel = reframing_check(judge, "fake-judge", records, variants, k=2, max_concurrency=4)

    assert parallel.per_scenario == sequential.per_scenario
    assert parallel.samples == sequential.samples


def test_judge_exception_becomes_an_error_verdict_for_that_cell_only():
    class Boom:
        async def acompletion(self, **kwargs):
            if "B" in kwargs["messages"][0]["content"]:
                raise RuntimeError("boom")
            return _make_response(_make_judge_json("low"))

    results = reframing_check(Boom(), "fake-judge", [_record()],
                              [PromptVariant("a", "A"), PromptVariant("b", "B")])

    assert results.per_scenario["Klagefrist"] == {"a": "low", "b": "ERROR"}
    assert results.judgments["Klagefrist"]["b"]["issues_found"][0].startswith("Judge call failed")


# ---------------------------------------------------------------------------
# Effects against the baseline
# ---------------------------------------------------------------------------

def test_effects_report_flip_rate_and_direction_against_the_baseline():
    judge = _judge_by_prompt({"A": "low", "B": "high", "C": "pass"})
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B"), PromptVariant("c", "C")]

    effects = reframing_check(judge, "fake-judge", [_record("One"), _record("Two")], variants).effects()

    assert set(effects) == {"b", "c"}
    b = effects["b"]
    assert isinstance(b, VariantEffect)
    assert (b.n_compared, b.n_shifted, b.flip_rate) == (2, 2, 1.0)
    assert (b.n_stricter, b.n_lenient, b.n_undirected) == (2, 0, 0)
    assert b.mean_direction == SEVERITY_ORDER.index("high") - SEVERITY_ORDER.index("low")
    assert b.net == "stricter"
    assert effects["c"].net == "lenient"
    assert effects["c"].mean_direction == -1
    assert {row["scenario"] for row in b.per_scenario} == {"One", "Two"}


def test_effects_accept_an_explicit_baseline():
    judge = _judge_by_prompt({"A": "low", "B": "high"})
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]

    results = reframing_check(judge, "fake-judge", [_record()], variants)

    assert results.effects(baseline="b")["a"].mean_direction == -2
    assert results.effects(baseline="b")["a"].net == "lenient"
    with pytest.raises(ValueError, match="baseline"):
        results.effects(baseline="nope")


def test_effects_direction_is_none_when_a_verdict_is_off_the_ladder():
    def respond(**kwargs):
        system = kwargs["messages"][0]["content"]
        return "not json" if "B" in system else _make_judge_json("high")

    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]
    effect = reframing_check(FakeClient(respond), "fake-judge", [_record()], variants).effects()["b"]

    assert (effect.n_shifted, effect.n_undirected) == (1, 1)
    assert effect.mean_direction is None
    assert effect.net == "none"


def test_three_variants_still_have_no_direction_in_shifts_but_do_in_effects():
    judge = _judge_by_prompt({"A": "low", "B": "high", "C": "pass"})
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B"), PromptVariant("c", "C")]

    results = reframing_check(judge, "fake-judge", [_record()], variants)

    assert "direction" not in results.shifts()[0]
    assert results.effects()["b"].per_scenario[0]["direction"] == 2


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

def test_transform_is_applied_to_the_graded_transcript_and_recorded_in_meta():
    seen = {}

    def respond(**kwargs):
        rendered = kwargs["messages"][1]["content"]
        seen.setdefault("user_intact", "Når går klagefristen ut?" in rendered)
        return _make_judge_json("high" if "[[TAG]]" in rendered else "low")

    def tag_assistant(conversation):
        return [
            {**m, "content": f"[[TAG]] {m['content']}"} if m["role"] == "assistant" else dict(m)
            for m in conversation
        ]

    variants = [PromptVariant("base", "R"), PromptVariant("tagged", "R", transform=tag_assistant)]
    results = reframing_check(FakeClient(respond), "fake-judge", [_record()], variants)

    assert results.per_scenario["Klagefrist"] == {"base": "low", "tagged": "high"}
    assert seen["user_intact"] is True
    assert results.variant_meta["tagged"]["transform"] == "tag_assistant"
    assert results.variant_meta["base"]["transform"] is None


def test_transform_that_changes_structure_is_rejected_before_any_judge_call():
    calls = []

    def respond(**kwargs):
        calls.append(1)
        return _make_judge_json("pass")

    def drop_last(conversation):
        return conversation[:-1]

    with pytest.raises(ValueError, match="number of turns"):
        reframing_check(FakeClient(respond), "fake-judge", [_record()],
                        [PromptVariant("a", "A"), PromptVariant("b", "A", transform=drop_last)])
    assert calls == []


def test_transform_output_checks_roles_and_file_uri():
    original = [
        {"role": "user", "content": "look", "file_uri": "images/x.png"},
        {"role": "assistant", "content": "ok"},
    ]
    _check_transform_output(original, [dict(m) for m in original], "fine")

    with pytest.raises(ValueError, match="file_uri"):
        _check_transform_output(original, [{"role": "user", "content": "look"}, original[1]], "v")
    with pytest.raises(ValueError, match="role"):
        _check_transform_output(original, [original[0], {"role": "user", "content": "ok"}], "v")
    with pytest.raises(ValueError, match="list"):
        _check_transform_output(original, "nope", "v")


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

def _panel_variants(*models):
    return [PromptVariant(m, "R", judge_model=m) for m in models]


def test_panel_reports_majority_mean_worst_and_disagreement():
    judge = _judge_by_model({"j1": "low", "j2": "high", "j3": "high"})

    panel = reframing_check(judge, "fake-judge", [_record()], _panel_variants("j1", "j2", "j3")).panel()

    assert isinstance(panel, PanelResults)
    verdict = panel.per_scenario["Klagefrist"]
    assert verdict.verdicts == {"j1": "low", "j2": "high", "j3": "high"}
    assert verdict.majority == "high"
    assert verdict.majority_share == pytest.approx(2 / 3)
    assert verdict.mean_index == pytest.approx((1 + 3 + 3) / 3, abs=1e-4)
    assert verdict.mean_index_severity == "medium"
    assert verdict.worst == "high"
    assert verdict.n_distinct == 2
    assert verdict.unanimous is False
    assert panel.unanimous_rate() == 0.0
    assert panel.flagged(threshold=0.7) == ["Klagefrist"]
    assert panel.flagged() == []
    json.dumps(panel.to_dict())


def test_panel_tie_resolves_to_the_stricter_verdict():
    judge = _judge_by_model({"j1": "pass", "j2": "critical"})

    verdict = reframing_check(judge, "fake-judge", [_record()], _panel_variants("j1", "j2")).panel().per_scenario["Klagefrist"]

    assert verdict.majority == "critical"
    assert verdict.majority_share == 0.5


def test_panel_ignores_off_ladder_verdicts_for_majority_and_worst():
    def respond(**kwargs):
        return "not json" if kwargs["model"] == "j1" else _make_judge_json("low")

    verdict = reframing_check(FakeClient(respond), "fake-judge", [_record()],
                              _panel_variants("j1", "j2")).panel().per_scenario["Klagefrist"]

    assert verdict.verdicts == {"j1": "ERROR", "j2": "low"}
    assert verdict.majority == "low"
    assert verdict.majority_share == 0.5
    assert verdict.mean_index is None
    assert verdict.worst == "low"


def test_panel_refuses_or_warns_when_prompts_differ():
    judge = _judge_by_model({"j1": "low", "j2": "high"})
    variants = [PromptVariant("j1", "A", judge_model="j1"), PromptVariant("j2", "B", judge_model="j2")]
    results = reframing_check(judge, "fake-judge", [_record()], variants)

    with pytest.raises(ValueError, match="prompt_sha1"):
        results.panel()
    with pytest.warns(UserWarning, match="prompt_sha1"):
        panel = results.panel(strict=False)
    assert panel.per_scenario["Klagefrist"].majority == "high"


def test_panel_requires_two_distinct_judges():
    judge = _judge_by_prompt({"A": "low"})
    results = reframing_check(judge, "fake-judge", [_record()],
                              [PromptVariant("a", "A"), PromptVariant("b", "A")])

    with pytest.raises(ValueError, match="distinct judges"):
        results.panel()
    with pytest.raises(ValueError, match="unknown variant labels"):
        results.panel(labels=["a", "zzz"])


def test_panel_over_a_subset_of_labels():
    judge = _judge_by_model({"j1": "low", "j2": "high", "j3": "critical"})
    results = reframing_check(judge, "fake-judge", [_record()], _panel_variants("j1", "j2", "j3"))

    panel = results.panel(labels=["j1", "j3"])

    assert panel.labels == ["j1", "j3"]
    assert panel.per_scenario["Klagefrist"].verdicts == {"j1": "low", "j3": "critical"}


# ---------------------------------------------------------------------------
# Serialisation and the client helper
# ---------------------------------------------------------------------------

def test_to_dict_keeps_existing_keys_and_adds_new_ones():
    judge = _judge_by_prompt({"A": "low", "B": "high"})
    variants = [PromptVariant("a", "A"), PromptVariant("b", "B")]

    payload = reframing_check(judge, "fake-judge", [_record()], variants, k=2).to_dict()

    json.dumps(payload)
    assert {"variant_labels", "per_scenario", "shifts", "invariant_rate",
            "input_tokens", "output_tokens"} <= set(payload)
    assert payload["k"] == 2
    assert payload["baseline_label"] == "a"
    assert payload["samples"]["Klagefrist"]["b"] == ["high", "high"]
    assert payload["stability"]["Klagefrist"]["a"]["agreement_rate"] == 1.0
    assert payload["effects"]["b"]["flip_rate"] == 1.0
    assert payload["variant_meta"]["a"]["judge_model"] == "fake-judge"
    assert payload["tokens_by_variant"]["a"] == {"input": 0, "output": 0}


def test_make_judge_client_delegates_to_model_auditor():
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value="client") as factory:
        assert make_judge_client("ollama", base_url="http://localhost:11434") == "client"

    factory.assert_called_once_with(api_key=None, base_url="http://localhost:11434", provider="ollama")


def test_no_warnings_on_a_clean_run():
    judge = _judge_by_prompt({"A": "low", "B": "high"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        reframing_check(judge, "fake-judge", [_record("One"), _record("Two")],
                        [PromptVariant("a", "A"), PromptVariant("b", "B")], k=2)
