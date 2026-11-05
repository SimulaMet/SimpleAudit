"""
Tests for :mod:`simpleaudit.calibration`.

No model is called. Subjects are built from AuditResults in memory or from
the fake judge clients in tests/fakes.py through reframing_check, so every
number here is pinned to a verdict we chose.
"""

import json
import warnings

import pytest

from simpleaudit.calibration import (
    MIN_ITEMS_FOR_ALPHA,
    CalibrationReport,
    calibrate,
    label,
    list_calibrations,
    load_labels,
    model_family,
    save_labels,
)
from simpleaudit.reframing import PromptVariant, StoredRecord, reframing_check, rejudge
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.utils import SEVERITY_ORDER

from .fakes import cycling_severity_judge, fixed_severity_judge


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _result(name: str, severity: str) -> AuditResult:
    return AuditResult(
        scenario_name=name,
        scenario_description=f"Scenario {name}",
        conversation=[
            {"role": "user", "content": f"Question {name}?"},
            {"role": "assistant", "content": f"Answer {name}."},
        ],
        severity=severity,
        issues_found=[],
        positive_behaviors=[],
        summary="",
        recommendations=[],
        expected_behavior=["Be right"],
    )


def _results(verdicts: dict, meta: dict = None) -> AuditResults:
    return AuditResults([_result(n, s) for n, s in verdicts.items()], meta=meta or {})


TWELVE = {f"s{i:02d}": SEVERITY_ORDER[i % 5] for i in range(12)}


# ---------------------------------------------------------------------------
# model_family
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model, family", [
    ("claude-haiku-4-5", "anthropic"),
    ("anthropic/claude-opus-4-7", "anthropic"),
    ("gpt-5.2", "openai"),
    ("openai/gpt-4o-mini", "openai"),
    ("o3-mini", "openai"),
    ("gemini-2.5-pro", "google"),
    ("openrouter/google/gemma-3-27b", "google"),
    ("ollama:qwen3:8b", "qwen"),
    ("meta-llama/Llama-3.3-70B-Instruct", "meta"),
    ("mistralai/Mistral-7B-Instruct", "mistral"),
    ("deepseek-r1", "deepseek"),
    ("phi-4", "microsoft"),
    ("grok-4", "xai"),
    ("my-finetune-v3", None),
    ("", None),
    (None, None),
])
def test_model_family(model, family):
    assert model_family(model) == family


# ---------------------------------------------------------------------------
# human reference
# ---------------------------------------------------------------------------

def test_judge_equal_to_reference_is_perfect():
    report = calibrate(_results(TWELVE), dict(TWELVE))
    a = report["stored"]
    assert a.n_compared == 12 and a.exact == 12 and a.within_one == 12
    assert a.bias_mean == 0 and a.n_stricter == 0 and a.n_lenient == 0
    assert a.alpha == pytest.approx(1.0)
    assert not a.indicative_only
    assert a.disagreements == []


def test_judge_one_level_stricter_everywhere():
    human = {n: s for n, s in TWELVE.items() if s != "critical"}
    judge = {n: SEVERITY_ORDER[SEVERITY_ORDER.index(s) + 1] for n, s in human.items()}
    a = calibrate(_results(judge), human)["stored"]
    assert a.exact == 0
    assert a.within_one == a.n_compared
    assert a.bias_mean == pytest.approx(1.0)
    assert a.n_stricter == a.n_compared and a.n_lenient == 0


def test_few_labels_are_indicative_and_still_reported():
    labels = {"s00": "pass", "s01": "low", "s02": "medium"}
    report = calibrate(_results(TWELVE), labels)
    a = report["stored"]
    assert a.n_compared == 3 and a.indicative_only
    assert a.alpha is None                       # withheld below the threshold
    assert a.exact == 3                          # but the counts are there
    lines = []
    report.summary(print_fn=lines.append)
    text = "\n".join(lines)
    assert "INDICATIVE ONLY" in text
    assert "3/3 = 1.00" in text


def test_error_and_ungraded_count_in_coverage_not_agreement():
    verdicts = dict(TWELVE)
    verdicts["s00"] = "ERROR"
    verdicts["s01"] = "ungraded"
    a = calibrate(_results(verdicts), dict(TWELVE))["stored"]
    assert a.n_reference == 12
    assert a.n_error == 1 and a.n_ungraded == 1
    assert a.n_compared == 10
    assert a.exact == 10
    assert a.confusion["pass"]["ERROR"] == 1


def test_unsure_and_blank_labels_are_left_out():
    labels = dict(TWELVE)
    labels["s00"] = "unsure"
    labels["s01"] = ""
    labels["s02"] = None
    report = calibrate(_results(TWELVE), labels)
    assert report.n_items == 9
    assert report.reference.n_unsure == 3


def test_label_not_in_reference_is_not_judged_and_vice_versa():
    labels = {"s00": "pass", "not-a-scenario": "high"}
    a = calibrate(_results(TWELVE), labels)["stored"]
    assert a.n_unjudged == 1
    assert a.n_compared == 1


def test_typo_in_a_label_raises():
    with pytest.raises(ValueError, match="not a severity"):
        calibrate(_results(TWELVE), {"s00": "pas"})


def test_shortcut_letters_are_accepted():
    a = calibrate(_results(TWELVE), {"s00": "p", "s01": "L", "s02": " m "})["stored"]
    assert a.exact == 3


def test_pass_fail_view():
    human = {"a": "pass", "b": "pass", "c": "low", "d": "medium", "e": "high", "f": "critical"}
    judge = {"a": "pass", "b": "low", "c": "pass", "d": "medium", "e": "pass", "f": "critical"}
    a = calibrate(_results(judge), human)["stored"]
    assert a.sensitivity == (2, 4)       # c and e missed
    assert a.specificity == (1, 2)       # b flagged
    assert a.severe_misses == (1, 2)     # e: human high, judge pass
    a2 = calibrate(_results(judge), human, fail_at="high")["stored"]
    assert a2.sensitivity == (1, 2)
    assert a2.specificity == (4, 4)


def test_fail_at_must_be_a_failure_level():
    with pytest.raises(ValueError):
        calibrate(_results(TWELVE), dict(TWELVE), fail_at="pass")


def test_two_raters_median_and_split_strata():
    human_a = {"a": "pass", "b": "low", "c": "high", "d": "medium"}
    human_b = {"a": "pass", "b": "medium", "c": "critical", "d": "medium"}
    judge = {"a": "pass", "b": "medium", "c": "critical", "d": "medium"}
    report = calibrate(_results(judge), [human_a, human_b])
    # even split resolves to the stricter middle
    assert report.reference.consensus == {"a": "pass", "b": "medium", "c": "critical", "d": "medium"}
    assert report.n_split == 2
    strata = report.strata["stored"]
    assert strata["unanimous"].n_compared == 2 and strata["unanimous"].exact == 2
    assert strata["split"].n_compared == 2 and strata["split"].exact == 2
    assert report.reference.human_ceiling() is None   # too few items for alpha


def test_human_ceiling_with_enough_items():
    human_a = dict(TWELVE)
    human_b = dict(TWELVE)
    report = calibrate(_results(TWELVE), [human_a, human_b])
    assert report.reference.human_ceiling() == pytest.approx(1.0)


def test_duplicate_scenario_names_warn():
    results = AuditResults([_result("dup", "pass"), _result("dup", "high")])
    with pytest.warns(UserWarning, match="duplicate scenario names"):
        a = calibrate(results, {"dup": "high"})["stored"]
    assert a.exact == 1   # last one wins


# ---------------------------------------------------------------------------
# model reference and the family check
# ---------------------------------------------------------------------------

def test_cross_family_reference_works_and_names_both():
    subject = _results(TWELVE, meta={"judge_model": "claude-haiku-4-5"})
    reference = _results(TWELVE, meta={"judge_model": "gpt-5.2"})
    report = calibrate(subject, reference)
    assert report.reference.kind == "model"
    assert report.reference.raters[0].family == "openai"
    assert report["stored"].judge_model == "claude-haiku-4-5"
    lines = []
    report.summary(print_fn=lines.append)
    assert "gpt-5.2 (openai)" in "\n".join(lines)
    assert "claude-haiku-4-5, anthropic" in "\n".join(lines)


def test_same_family_reference_is_refused():
    subject = _results(TWELVE, meta={"judge_model": "claude-haiku-4-5"})
    reference = _results(TWELVE, meta={"judge_model": "claude-sonnet-4-6"})
    with pytest.raises(ValueError, match="both anthropic"):
        calibrate(subject, reference)
    # explicit override for experiments
    report = calibrate(subject, reference, allow_same_family=True)
    assert report["stored"].n_compared == 12


def test_model_ids_can_be_passed_when_the_files_do_not_record_them():
    subject = _results(TWELVE)
    reference = _results(TWELVE)
    with pytest.raises(ValueError, match="both openai"):
        calibrate(subject, reference, judge_model="gpt-4o", reference_model="gpt-5.2")
    report = calibrate(subject, reference, judge_model="gpt-4o", reference_model="gemini-2.5-pro")
    assert report["stored"].judge_model == "gpt-4o"


def test_unknown_subject_model_warns_instead_of_refusing():
    subject = _results(TWELVE)
    reference = _results(TWELVE, meta={"judge_model": "gpt-5.2"})
    with pytest.warns(UserWarning, match="no judge_model recorded"):
        calibrate(subject, reference)
    with pytest.warns(UserWarning, match="not a recognised model family"):
        calibrate(subject, reference, judge_model="my-finetune")


def test_two_model_references_must_differ_from_each_other():
    subject = _results(TWELVE, meta={"judge_model": "claude-haiku-4-5"})
    ref_a = _results(TWELVE, meta={"judge_model": "gpt-5.2"})
    ref_b = _results(TWELVE, meta={"judge_model": "gpt-4o"})
    with pytest.raises(ValueError, match="both openai"):
        calibrate(subject, [ref_a, ref_b])
    ref_c = _results(TWELVE, meta={"judge_model": "gemini-2.5-pro"})
    report = calibrate(subject, [ref_a, ref_c])
    assert len(report.reference.raters) == 2


def test_mixed_human_and_model_reference():
    subject = _results(TWELVE, meta={"judge_model": "claude-haiku-4-5"})
    reference = _results(TWELVE, meta={"judge_model": "gpt-5.2"})
    report = calibrate(subject, [dict(TWELVE), reference])
    assert report.reference.kind == "mixed"


def test_rejudge_records_the_new_judge_model(tmp_path):
    source = _results({"a": "pass", "b": "pass"}, meta={"judge_model": "claude-haiku-4-5", "judge_provider": "anthropic"})
    rejudged = rejudge(source, fixed_severity_judge("high"), "gpt-5.2")
    assert rejudged.meta["judge_model"] == "gpt-5.2"
    assert rejudged.meta["rejudged_from"]["judge_model"] == "claude-haiku-4-5"
    report = calibrate(source, rejudged)
    assert report["stored"].n_stricter == 0 and report["stored"].n_lenient == 2


# ---------------------------------------------------------------------------
# ReframingResults: one judge per variant
# ---------------------------------------------------------------------------

def _records(names) -> list:
    return [
        StoredRecord(
            scenario_name=n,
            scenario_description=f"Scenario {n}",
            conversation=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
            expected_behavior=["Be right"],
        )
        for n in names
    ]


def test_reframing_results_give_one_row_per_variant():
    names = [f"s{i:02d}" for i in range(12)]
    human = {n: "high" for n in names}
    judge = cycling_severity_judge(["high", "high", "pass"])   # the fake cycles across calls
    variants = [
        PromptVariant("strict", "Grade strictly.", judge_model="gpt-5.2"),
        PromptVariant("lenient", "Grade leniently.", judge_model="gemini-2.5-pro"),
    ]
    results = reframing_check(judge, "gpt-5.2", _records(names), variants)
    report = calibrate(results, human)
    assert set(report.judges) == {"strict", "lenient"}
    assert report["strict"].judge_model == "gpt-5.2"
    assert report["lenient"].judge_model == "gemini-2.5-pro"
    total_exact = report["strict"].exact + report["lenient"].exact
    assert total_exact == 16                     # 24 calls, two in three are "high"
    board = report.leaderboard()
    assert len(board) == 2 and board[0].exact >= board[1].exact


def test_reframing_modal_at_k_greater_than_one():
    names = ["a", "b"]
    results = reframing_check(fixed_severity_judge("medium"), "gpt-5.2", _records(names),
                              [PromptVariant("v", "Grade.")], k=3)
    a = calibrate(results, {"a": "medium", "b": "low"})["v"]
    assert a.n_compared == 2 and a.exact == 1


# ---------------------------------------------------------------------------
# label(): interactive, resumable, blind
# ---------------------------------------------------------------------------

def test_label_writes_as_it_goes_and_resumes(tmp_path):
    results = _results({"a": "critical", "b": "pass", "c": "pass"})
    path = tmp_path / "run.json"
    results.save(str(path))
    printed = []
    answers = iter(["h", "q"])
    labels = label(str(path), input_fn=lambda _: next(answers), print_fn=printed.append)
    assert labels == {"a": "high"}
    out = tmp_path / "run.labels.json"
    assert out.exists()
    assert load_labels(out) == {"a": "high"}
    # the judge's own verdict is never shown (the help line lists all levels, skip it)
    assert "critical" not in "\n".join(printed[2:])
    assert "Answer a." in "\n".join(printed)
    # second session skips the labelled item; a typo is asked again, a blank line skips
    answers = iter(["bad", "unsure", ""])
    labels = label(str(path), input_fn=lambda _: next(answers), print_fn=printed.append)
    assert labels == {"a": "high", "b": "unsure"}
    assert any("not a severity" in line for line in printed)
    # third session only asks about the one item still unlabelled
    answers = iter(["pass"])
    labels = label(str(path), input_fn=lambda _: next(answers), print_fn=printed.append)
    assert labels == {"a": "high", "b": "unsure", "c": "pass"}
    report = calibrate(str(path), out)
    assert report.n_items == 2 and report.reference.n_unsure == 1


def test_label_with_an_object_needs_out(tmp_path):
    with pytest.raises(ValueError, match="out="):
        label(_results({"a": "pass"}), input_fn=lambda _: "q", print_fn=lambda _: None)


def test_labels_file_round_trip_keeps_non_ascii(tmp_path):
    labels = {"AAP Medlemskap - EØS Sammenlegging": "låg".replace("låg", "low"), "Skatt, moms; mva": "high"}
    path = tmp_path / "labels.json"
    save_labels(labels, path, source="x.json", rater="Åse")
    assert load_labels(path) == labels
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["rater"] == "Åse"


def test_load_labels_accepts_a_bare_mapping(tmp_path):
    path = tmp_path / "bare.json"
    path.write_text(json.dumps({"a": "pass"}), encoding="utf-8")
    assert load_labels(path) == {"a": "pass"}
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps([1, 2]), encoding="utf-8")
    with pytest.raises(ValueError):
        load_labels(bad)


# ---------------------------------------------------------------------------
# report persistence
# ---------------------------------------------------------------------------

def test_report_round_trips_through_json_and_lists(tmp_path):
    subject = _results(TWELVE, meta={"judge_model": "claude-haiku-4-5", "judge_name": "safety"})
    report = calibrate(subject, dict(TWELVE))
    data = report.to_dict()
    json.dumps(data)                                  # serialisable, no tuples left behind
    assert data["judges"]["safety"]["exact"]["value"] == 1.0
    assert data["judges"]["safety"]["alpha"]["value"] == pytest.approx(1.0)
    path = report.save(directory=tmp_path)
    assert path.parent == tmp_path and path.name.startswith("AuditResults__human__")
    rows = list_calibrations(tmp_path)
    assert len(rows) == 1
    assert rows[0]["judge"] == "safety" and rows[0]["judge_model"] == "claude-haiku-4-5"
    assert rows[0]["n_compared"] == 12 and rows[0]["indicative_only"] is False
    (tmp_path / "junk.json").write_text("[]", encoding="utf-8")
    assert len(list_calibrations(tmp_path)) == 1


def test_model_reference_default_path_names_the_model(tmp_path):
    subject = _results(TWELVE, meta={"judge_model": "claude-haiku-4-5"})
    reference = _results(TWELVE, meta={"judge_model": "gpt-5.2"})
    path = calibrate(subject, reference).save(directory=tmp_path)
    assert "__model-gpt-5-2__" in path.name


def test_empty_reference_raises():
    with pytest.raises(ValueError):
        calibrate(_results(TWELVE), [])


def test_bad_types_raise():
    with pytest.raises(TypeError):
        calibrate(_results(TWELVE), 42)
    with pytest.raises(TypeError):
        calibrate(42, dict(TWELVE))


def test_min_items_threshold_is_exposed():
    assert MIN_ITEMS_FOR_ALPHA == 10
    assert isinstance(calibrate(_results(TWELVE), dict(TWELVE)), CalibrationReport)
