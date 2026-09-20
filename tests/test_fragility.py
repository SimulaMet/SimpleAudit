"""
Tests for the per-scenario fragility signal: disagreement across the
``n_repetitions`` runs an AuditExperiment has already produced.

Nothing here is allowed to reach a model. The whole point of this path is
that it re-reads verdicts that were already paid for, so the AnyLLM factory
is patched to explode for the duration of every test in this module.
"""

import itertools
import json
import math
import warnings
from pathlib import Path

import pytest

from simpleaudit.repeated_results import (
    FRAGILE_THRESHOLD_DEFAULT,
    RepeatedExperimentResults,
    ScenarioStats,
    _build_stability_report,
    _normalised_entropy,
    _ordinal_spread,
)
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.utils import SEVERITY_ORDER


# ---------------------------------------------------------------------------
# No-call guarantee
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _forbid_model_calls(monkeypatch):
    """Fail any test in this module that tries to construct an LLM client.

    The fragility statistics are derived from stored verdicts. If a change
    ever routes them through a model, these tests break rather than quietly
    start spending tokens.
    """
    def explode(*args, **kwargs):
        raise AssertionError("a model client was created during a fragility computation")

    import any_llm

    monkeypatch.setattr(any_llm.AnyLLM, "create", staticmethod(explode))
    monkeypatch.setattr("simpleaudit.model_auditor.AnyLLM.create", staticmethod(explode))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(severity: str, name: str) -> AuditResult:
    return AuditResult(
        scenario_name=name,
        scenario_description="desc",
        conversation=[],
        severity=severity,
        issues_found=[],
        positive_behaviors=[],
        summary="",
        recommendations=[],
    )


def _runs(*per_run: dict) -> list:
    """Build one AuditResults per dict of {scenario_name: severity}."""
    return [
        AuditResults([_result(sev, name) for name, sev in mapping.items()])
        for mapping in per_run
    ]


def _report(*per_run: dict):
    return _build_stability_report("test-model", _runs(*per_run))


def _stats(severities: list) -> ScenarioStats:
    return _report(*[{"s": sev} for sev in severities]).per_scenario["s"]


# ---------------------------------------------------------------------------
# Normalised entropy
# ---------------------------------------------------------------------------

def test_entropy_is_zero_when_every_run_agrees():
    assert _normalised_entropy(["pass"] * 5) == 0.0


def test_entropy_is_zero_for_a_single_run():
    assert _normalised_entropy(["critical"]) == 0.0


def test_entropy_is_zero_for_no_runs():
    assert _normalised_entropy([]) == 0.0


def test_entropy_reaches_one_only_across_the_full_ladder():
    assert _normalised_entropy(list(SEVERITY_ORDER)) == pytest.approx(1.0)


def test_entropy_normalises_against_the_ladder_not_observed_levels():
    """A 50/50 two-way split must not read as maximal disagreement.

    This is the design decision worth pinning: normalising against the two
    levels actually seen would report 1.0 here, making a pass/critical coin
    flip indistinguishable from a scenario spread evenly over all five
    severities.
    """
    two_way = _normalised_entropy(["pass", "critical"])
    assert two_way == pytest.approx(math.log(2) / math.log(5))
    assert two_way < _normalised_entropy(list(SEVERITY_ORDER))


def test_entropy_depends_on_proportions_not_run_count():
    assert _normalised_entropy(["pass", "high"]) == pytest.approx(
        _normalised_entropy(["pass", "pass", "high", "high"])
    )


def test_entropy_rises_with_disagreement():
    unanimous = _normalised_entropy(["pass"] * 4)
    lopsided = _normalised_entropy(["pass"] * 3 + ["critical"])
    even = _normalised_entropy(["pass", "pass", "critical", "critical"])
    assert unanimous < lopsided < even


def test_entropy_counts_off_ladder_verdicts_as_their_own_level():
    """An errored run is a run that did not agree, not a run to drop."""
    assert _normalised_entropy(["pass", "ERROR"]) > 0.0


def test_entropy_is_clamped_to_one_when_wider_than_the_ladder():
    wider = list(SEVERITY_ORDER) + ["ERROR", "custom-verdict"]
    assert _normalised_entropy(wider) == 1.0


# ---------------------------------------------------------------------------
# Ordinal spread
# ---------------------------------------------------------------------------

def test_spread_is_zero_when_every_run_agrees():
    assert _ordinal_spread(["medium"] * 4) == 0.0


def test_spread_is_zero_for_a_single_run():
    assert _ordinal_spread(["critical"]) == 0.0


def test_spread_is_zero_for_no_runs():
    assert _ordinal_spread([]) == 0.0


def test_spread_is_maximal_across_the_ends_of_the_ladder():
    assert _ordinal_spread(["pass", "critical"]) == 2.0


def test_spread_respects_ordinal_distance():
    """Adjacent verdicts disagree less than opposite ones."""
    assert _ordinal_spread(["high", "critical"]) < _ordinal_spread(["pass", "critical"])


def test_spread_is_none_when_a_verdict_is_off_the_ladder():
    """None means "no position on the scale", which 0.0 would misreport."""
    assert _ordinal_spread(["pass", "ERROR"]) is None


def test_spread_is_none_even_when_off_ladder_verdicts_are_unanimous():
    assert _ordinal_spread(["ERROR", "ERROR"]) is None


def test_spread_is_not_std_score():
    """std_score moves with the run score; spread moves with one verdict.

    Both runs score the same, so std_score is 0.0, while the scenario itself
    swung from pass to critical. If spread were derived from score this test
    would fail.
    """
    report = _build_stability_report("m", _runs(
        {"a": "pass", "b": "critical"},
        {"a": "critical", "b": "pass"},
    ))
    assert report.std_score == 0.0
    assert report.per_scenario["a"].ordinal_spread == 2.0
    assert report.per_scenario["b"].ordinal_spread == 2.0


# ---------------------------------------------------------------------------
# Edges on the assembled ScenarioStats
# ---------------------------------------------------------------------------

def test_single_run_reports_no_disagreement():
    stats = _stats(["high"])
    assert stats.agreement_rate == 1.0
    assert stats.normalised_entropy == 0.0
    assert stats.ordinal_spread == 0.0


def test_unanimous_runs_report_no_disagreement():
    stats = _stats(["pass"] * 6)
    assert stats.agreement_rate == 1.0
    assert stats.normalised_entropy == 0.0
    assert stats.ordinal_spread == 0.0


def test_maximal_split_reports_maximal_disagreement():
    stats = _stats(list(SEVERITY_ORDER))
    assert stats.agreement_rate == pytest.approx(0.2)
    assert stats.normalised_entropy == pytest.approx(1.0)
    assert stats.ordinal_spread == pytest.approx(math.sqrt(2.0), abs=1e-4)


def test_off_ladder_severity_does_not_crash_and_is_not_silently_scored():
    stats = _stats(["pass", "ERROR", "pass"])
    assert stats.ordinal_spread is None          # no position on the 0-4 scale
    assert stats.normalised_entropy > 0.0        # but the disagreement is real
    assert stats.severity_distribution["ERROR"] == 1


def test_scenario_missing_from_some_runs_uses_the_runs_it_appears_in():
    report = _build_stability_report("m", _runs(
        {"a": "pass", "b": "pass"},
        {"a": "critical"},
    ))
    assert report.per_scenario["a"].agreement_rate == 0.5
    assert report.per_scenario["b"].agreement_rate == 1.0
    assert report.per_scenario["b"].normalised_entropy == 0.0


def test_scenario_records_how_many_runs_it_was_seen_in():
    report = _build_stability_report("m", _runs(
        {"a": "pass", "b": "pass"},
        {"a": "critical"},
        {"a": "medium"},
    ))
    assert report.n_runs == 3
    assert report.per_scenario["a"].n_observations == 3
    assert report.per_scenario["b"].n_observations == 1


def test_a_scenario_seen_once_is_not_printed_as_stable(capsys):
    """A scenario that dropped out of later runs must not read as unanimous.

    Its entropy is 0.0 for want of a second observation, not because the
    verdict held. Keying the column on the report's n_runs would show 0.00
    here, which is indistinguishable from a genuinely stable scenario.
    """
    report = _build_stability_report("m", _runs(
        {"a": "pass", "b": "pass"},
        {"a": "critical"},
        {"a": "medium"},
        {"a": "high"},
    ))
    report.summary()
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip().startswith(("a ", "b "))]
    row_b = next(ln for ln in lines if ln.strip().startswith("b "))
    row_a = next(ln for ln in lines if ln.strip().startswith("a "))
    assert "—" in row_b and "0.00" not in row_b
    assert "—" not in row_a and "0.86" in row_a


# ---------------------------------------------------------------------------
# fragile() accessor
# ---------------------------------------------------------------------------

def test_fragile_selects_scenarios_below_the_threshold():
    report = _report(
        {"stable": "pass", "shaky": "pass"},
        {"stable": "pass", "shaky": "critical"},
        {"stable": "pass", "shaky": "medium"},
    )
    fragile = report.fragile(threshold=0.6)
    assert set(fragile) == {"shaky"}
    assert isinstance(fragile["shaky"], ScenarioStats)


def test_fragile_default_threshold_matches_the_module_constant():
    report = _report({"s": "pass"}, {"s": "critical"}, {"s": "medium"})
    assert report.fragile() == report.fragile(threshold=FRAGILE_THRESHOLD_DEFAULT)
    assert FRAGILE_THRESHOLD_DEFAULT == 0.6


def test_fragile_comparison_is_strict_at_the_boundary():
    """A scenario sitting exactly on the threshold is not fragile."""
    report = _report(
        {"s": "pass"}, {"s": "pass"}, {"s": "pass"}, {"s": "critical"}, {"s": "high"},
    )
    assert report.per_scenario["s"].agreement_rate == pytest.approx(0.6)
    assert "s" not in report.fragile(threshold=0.6)
    assert "s" in report.fragile(threshold=0.61)


def test_fragile_returns_empty_when_everything_agrees():
    report = _report({"s": "pass"}, {"s": "pass"})
    assert report.fragile() == {}


def test_fragile_returns_everything_unstable_at_threshold_one():
    report = _report({"s": "pass"}, {"s": "critical"})
    assert set(report.fragile(threshold=1.0)) == {"s"}


def test_fragile_rejects_a_threshold_outside_zero_to_one():
    report = _report({"s": "pass"}, {"s": "critical"})
    for bad in (-0.1, 1.5, 60):
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            report.fragile(threshold=bad)


def test_fragile_warns_on_a_single_run_report():
    """One run always agrees with itself — empty here means "not measured"."""
    report = _report({"s": "pass"})
    with pytest.warns(UserWarning, match="at least 2 runs"):
        assert report.fragile() == {}


def test_fragile_does_not_warn_with_enough_runs():
    report = _report({"s": "pass"}, {"s": "critical"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        report.fragile()


# ---------------------------------------------------------------------------
# Reporting surface
# ---------------------------------------------------------------------------

def test_summary_flags_fragile_scenarios(capsys):
    report = _report(
        {"stable": "pass", "shaky": "pass"},
        {"stable": "pass", "shaky": "critical"},
        {"stable": "pass", "shaky": "medium"},
    )
    report.summary()
    out = capsys.readouterr().out
    assert "Entropy" in out
    assert "(fragile)" in out
    assert "1 of 2 scenarios fragile" in out
    assert "shaky" in out


def test_summary_does_not_print_fragility_numbers_for_a_single_run(capsys):
    """0.00 on one run would read as "stable" when it means "unmeasured"."""
    report = _report({"s": "pass"})
    report.summary()
    out = capsys.readouterr().out
    assert "0.00" not in out
    assert "(fragile)" not in out


def test_summary_omits_the_fragile_block_when_nothing_is_fragile(capsys):
    report = _report({"s": "pass"}, {"s": "pass"})
    report.summary()
    out = capsys.readouterr().out
    assert "fragile" not in out


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def test_scenario_stats_to_dict_carries_the_new_fields():
    payload = _stats(["pass", "critical"]).to_dict()
    assert payload["normalised_entropy"] > 0.0
    assert payload["ordinal_spread"] == 2.0


def test_stability_report_is_json_serializable():
    report = _report({"s": "pass"}, {"s": "critical"}, {"s": "ERROR"})
    payload = json.loads(json.dumps(report.to_dict()))
    stats = payload["per_scenario"]["s"]
    assert stats["ordinal_spread"] is None       # None survives as JSON null
    assert stats["normalised_entropy"] > 0.0


def test_repeated_results_to_dict_carries_stability(tmp_path):
    results = RepeatedExperimentResults({"m": _runs({"s": "pass"}, {"s": "critical"})})
    payload = json.loads(json.dumps(results.to_dict()))
    stats = payload["stability"]["m"]["per_scenario"]["s"]
    assert stats["agreement_rate"] == 0.5
    assert stats["normalised_entropy"] > 0.0


def test_saved_json_carries_fragility_and_still_loads(tmp_path):
    results = RepeatedExperimentResults({"m": _runs({"s": "pass"}, {"s": "critical"})})
    path = tmp_path / "runs.json"
    results.save(str(path))

    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    assert saved["stability"]["m"]["per_scenario"]["s"]["normalised_entropy"] > 0.0

    reloaded = RepeatedExperimentResults.load(str(path))
    assert set(reloaded.stability("m").fragile(threshold=1.0)) == {"s"}


def test_saving_a_reloaded_result_reproduces_the_same_bytes(tmp_path):
    """save -> load -> save must come back byte-for-byte.

    The existing round-trip tests pin content: run counts, severities, judge
    metadata. None of them would catch a serialisation that reorders keys,
    drops the new ``stability`` block on the second write, or renders None
    differently once it has been through JSON. Two models, disagreement in
    one scenario, and an off-ladder verdict so an ``ordinal_spread`` of None
    is on the path.
    """
    results = RepeatedExperimentResults({
        "model-a": _runs({"shaky": "pass", "steady": "low"},
                         {"shaky": "critical", "steady": "low"},
                         {"shaky": "medium", "steady": "low"}),
        "model-b": _runs({"errored": "ERROR", "steady": "pass"},
                         {"errored": "pass", "steady": "pass"}),
    })

    # The None case has to be live, or the test proves nothing about it.
    assert results.stability("model-b").per_scenario["errored"].ordinal_spread is None

    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    results.save(str(first))
    RepeatedExperimentResults.load(str(first)).save(str(second))

    assert first.read_bytes() == second.read_bytes()


def test_load_tolerates_json_written_before_the_stability_key_existed(tmp_path):
    """Older result files have no "stability" key; they must still load."""
    results = RepeatedExperimentResults({"m": _runs({"s": "pass"}, {"s": "critical"})})
    payload = results.to_dict()
    del payload["stability"]
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = RepeatedExperimentResults.load(str(path))
    assert reloaded.stability("m").per_scenario["s"].agreement_rate == 0.5


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------

def test_existing_scenario_fields_are_unchanged():
    stats = _stats(["pass", "pass", "critical"])
    assert stats.pass_rate == pytest.approx(2 / 3)
    assert stats.agreement_rate == pytest.approx(2 / 3)
    assert stats.most_common_severity == "pass"
    assert stats.severity_distribution == {"pass": 2, "critical": 1}


def test_existing_report_fields_are_unchanged():
    report = _report({"s": "pass"}, {"s": "critical"})
    assert report.n_runs == 2
    assert report.scores == [100.0, 0.0]
    assert report.mean_score == 50.0
    assert report.std_score == pytest.approx(70.71, abs=0.01)
    assert report.min_score == 0.0
    assert report.max_score == 100.0


def test_scenario_stats_can_still_be_built_without_the_new_fields():
    """Third-party code constructing ScenarioStats directly keeps working."""
    stats = ScenarioStats(
        pass_rate=1.0,
        severity_distribution={"pass": 1},
        most_common_severity="pass",
        agreement_rate=1.0,
    )
    assert stats.normalised_entropy == 0.0
    assert stats.ordinal_spread == 0.0
    assert json.dumps(stats.to_dict())


def test_existing_top_level_json_keys_are_unchanged():
    results = RepeatedExperimentResults({"m": _runs({"s": "pass"}, {"s": "critical"})})
    payload = results.to_dict()
    for key in ("version", "n_repetitions", "models", "judge", "aggregate", "runs"):
        assert key in payload
    assert payload["version"] == "1.0"
    assert payload["n_repetitions"] == 2


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_repeated_computation_is_identical():
    runs = _runs({"s": "pass"}, {"s": "critical"}, {"s": "medium"})
    first = _build_stability_report("m", runs).to_dict()
    second = _build_stability_report("m", runs).to_dict()
    assert first == second
    assert json.dumps(first) == json.dumps(second)


def test_scenario_key_order_follows_first_appearance():
    report = _build_stability_report("m", _runs(
        {"b": "pass", "a": "pass"},
        {"a": "pass", "b": "pass"},
    ))
    assert list(report.per_scenario) == ["b", "a"]


def test_summary_does_not_warn_on_a_single_run_report():
    """Printing a report is not asking for fragility, so it must stay quiet."""
    report = _report({"s": "pass"})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        report.summary()


# ---------------------------------------------------------------------------
# Findings from adversarial review
# ---------------------------------------------------------------------------

def test_entropy_ceiling_matches_the_documented_formula_below_five_runs():
    """Docstring claims log(n)/log(5) while n < 5. Pin it."""
    for n in range(1, 5):
        best = max(
            _normalised_entropy([SEVERITY_ORDER[i] for i in combo])
            for combo in itertools.combinations_with_replacement(range(5), n)
        )
        assert best == pytest.approx(math.log(n) / math.log(5), abs=1e-4)


def test_entropy_reaches_one_only_when_five_divides_the_run_count():
    """Docstring claims exactly 1.0 iff 5 | n, just under otherwise."""
    for n in (5, 10):
        best = max(
            _normalised_entropy([SEVERITY_ORDER[i] for i in combo])
            for combo in itertools.combinations_with_replacement(range(5), n)
        )
        assert best == pytest.approx(1.0, abs=1e-4)
    for n in (6, 7, 8, 9):
        best = max(
            _normalised_entropy([SEVERITY_ORDER[i] for i in combo])
            for combo in itertools.combinations_with_replacement(range(5), n)
        )
        assert best < 1.0


def test_ordinal_spread_reaches_two_only_at_even_run_counts():
    """Docstring claims 2.0 needs an even split, so odd n falls short."""
    def ceiling(n):
        return max(
            _ordinal_spread([SEVERITY_ORDER[i] for i in combo])
            for combo in itertools.combinations_with_replacement(range(5), n)
        )
    assert ceiling(2) == pytest.approx(2.0)
    assert ceiling(4) == pytest.approx(2.0)
    assert ceiling(3) < 2.0
    assert ceiling(5) < 2.0


def test_saving_does_not_warn_about_duplicate_scenario_names():
    """save() never warned before the stability key existed; it must not start.

    Under -W error a new warning here would turn save() into a raise on files
    it previously wrote without complaint.
    """
    runs = [
        AuditResults([_result("pass", "dup"), _result("critical", "dup")]),
        AuditResults([_result("pass", "dup"), _result("pass", "dup")]),
    ]
    results = RepeatedExperimentResults({"m": runs})
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        payload = results.to_dict()
    assert "stability" in payload


def test_stability_still_warns_about_duplicate_scenario_names():
    """Suppressing the warning in to_dict() must not suppress it in stability()."""
    runs = [
        AuditResults([_result("pass", "dup"), _result("critical", "dup")]),
        AuditResults([_result("pass", "dup"), _result("pass", "dup")]),
    ]
    results = RepeatedExperimentResults({"m": runs})
    with pytest.warns(UserWarning, match="duplicate scenario names"):
        results.stability("m")


def test_unanimous_error_verdicts_are_agreement_not_stability():
    """Locks the documented reading: 3/3 ERROR is agreement on a non-verdict.

    agreement_rate predates this change, so fragile() does not reinterpret it.
    The signal is carried by most_common_severity and by ordinal_spread=None.
    """
    report = _report({"s": "ERROR"}, {"s": "ERROR"}, {"s": "ERROR"})
    stats = report.per_scenario["s"]
    assert stats.agreement_rate == 1.0
    assert stats.normalised_entropy == 0.0
    assert report.fragile() == {}
    assert stats.most_common_severity == "ERROR"   # visible in the table
    assert stats.ordinal_spread is None            # and off the ladder
