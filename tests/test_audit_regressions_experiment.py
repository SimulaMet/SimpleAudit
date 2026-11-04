"""Regression tests for experiment / repeated-results defects found in the
2026-11 system audit (see AUDIT.md, findings E1-E7)."""

import asyncio
import json
from unittest.mock import MagicMock, patch

from simpleaudit.experiment import AuditExperiment
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.repeated_results import RepeatedExperimentResults
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.spans import find_span
from simpleaudit.utils import UNGRADED


def _result(name: str, severity: str) -> AuditResult:
    return AuditResult(
        scenario_name=name, scenario_description="d", conversation=[],
        severity=severity, issues_found=[], positive_behaviors=[],
        summary="", recommendations=[],
    )


def _experiment(tmp_path, n=2, judge="judge", **kw) -> AuditExperiment:
    return AuditExperiment(
        models=[{"model": "m1", "provider": "openai"}],
        judge_model=judge, judge_provider="openai", show_progress=False,
        n_repetitions=n, save_dir=str(tmp_path), **kw,
    )


def _run(exp, scenarios, severity_for, counter):
    async def fake_run_async(self_a, scenarios, **kwargs):
        counter.append([s["name"] for s in scenarios])
        return AuditResults([_result(s["name"], severity_for(len(counter))) for s in scenarios])

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake_run_async):
        return asyncio.run(exp.run_async(scenarios=scenarios))


SCEN = [{"name": "s1", "description": "d"}]


def test_config_change_quarantines_stale_runs_so_crash_resume_cannot_reuse_them(tmp_path):
    # E1: runs under judge OLD, then judge NEW crashes after its first rep.
    _run(_experiment(tmp_path, n=3, judge="old"), SCEN, lambda i: "critical", [])

    exp_new = _experiment(tmp_path, n=3, judge="new")
    calls = []

    async def crash_after_first(self_a, scenarios, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise KeyboardInterrupt  # simulates the process dying
        return AuditResults([_result("s1", "pass")])

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=crash_after_first):
        try:
            asyncio.run(exp_new.run_async(scenarios=SCEN))
        except KeyboardInterrupt:
            pass

    # Resume with judge NEW: no run produced by judge OLD may be reused.
    counter = []
    results = _run(_experiment(tmp_path, n=3, judge="new"), SCEN, lambda i: "pass", counter)
    severities = [run[0].severity for run in results.runs("m1")]
    assert "critical" not in severities
    assert len(counter) == 2  # run_0 from the NEW config is reused; 1 and 2 re-run
    assert list((tmp_path / "m1").glob("run_*.json.stale"))


def test_adaptive_reruns_do_not_skew_scores_or_rep_count(tmp_path):
    # E2: an adaptive rerun repeats only the fragile scenario.
    scenarios = [{"name": "A", "description": "d"}, {"name": "B", "description": "d"}]
    sev = {1: {"A": "pass", "B": "pass"}, 2: {"A": "pass", "B": "critical"}}

    async def fake(self_a, scenarios, **kwargs):
        fake.n += 1
        table = sev.get(fake.n, {"B": "pass"})
        return AuditResults([_result(s["name"], table[s["name"]]) for s in scenarios])
    fake.n = 0

    exp = _experiment(tmp_path, n=2, adaptive_reruns={"agreement_target": 0.9, "max_extra": 1})
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake):
        results = asyncio.run(exp.run_async(scenarios=scenarios))

    report = results.stability("m1")
    assert report.n_runs == 2
    assert report.mean_score == 75.0  # mean of the two full runs only
    assert report.per_scenario["B"].n_observations == 3
    data = results.to_dict()
    assert data["n_repetitions"] == 2
    assert not (tmp_path / "m1" / "run_2.json").exists()
    assert (tmp_path / "m1" / "adaptive_0.json").exists()

    # Raising n_repetitions later must not load the partial rerun as a full rep.
    counter = []
    _run(_experiment(tmp_path, n=3), scenarios, lambda i: "pass", counter)
    assert counter == [["A", "B"]]


def test_adaptive_reruns_work_with_pack_name(tmp_path):
    # E6: scenarios given as a pack name used to disable adaptive reruns.
    pack = ModelAuditor.get_scenarios("safety")
    first = pack[0]["name"]
    n = {"i": 0}

    async def fake(self_a, scenarios, **kwargs):
        n["i"] += 1
        sev = "critical" if n["i"] == 2 else "pass"
        return AuditResults([
            _result(s["name"], sev if s["name"] == first else "pass") for s in scenarios
        ])

    exp = _experiment(tmp_path, n=2, adaptive_reruns={"agreement_target": 0.9, "max_extra": 1})
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake):
        results = asyncio.run(exp.run_async(scenarios="safety"))
    assert len(results.adaptive_runs("m1")) == 1
    assert [r.scenario_name for r in results.adaptive_runs("m1")[0]] == [first]


def test_run_scenario_reps_keeps_a_separate_cache_per_scenario(tmp_path):
    # E3: two scenarios for one model used to overwrite each other's run files.
    exp = _experiment(tmp_path, n=2)
    calls = []

    async def fake(self_a, scenarios, **kwargs):
        calls.append(scenarios[0]["name"])
        return AuditResults([_result(scenarios[0]["name"], "pass")])

    async def both():
        a = await exp.run_scenario_reps(0, {"name": "A", "description": "d"})
        b = await exp.run_scenario_reps(0, {"name": "B", "description": "d"})
        return a, b

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake):
        a, b = asyncio.run(both())
        assert [r.scenario_name for r in a] == ["A", "A"]
        assert [r.scenario_name for r in b] == ["B", "B"]
        assert len(calls) == 4
        # Second pass: everything comes from the cache.
        asyncio.run(both())
    assert len(calls) == 4


def test_per_scenario_stability_excludes_ungraded():
    # E4: UNGRADED used to count as an observation, deflating pass_rate.
    runs = [AuditResults([_result("s", sev)]) for sev in ("pass", UNGRADED, UNGRADED)]
    stats = RepeatedExperimentResults({"m": runs}).stability("m").per_scenario["s"]
    assert stats.pass_rate == 1.0
    assert stats.n_observations == 1
    assert stats.n_ungraded == 2
    assert stats.most_common_severity == "pass"


def test_repeated_results_load_keeps_run_meta(tmp_path):
    # E5: load() dropped each run's meta (judge/target/auditor model info).
    run = AuditResults([_result("s", "pass")], meta={"judge_model": "j"})
    path = tmp_path / "r.json"
    RepeatedExperimentResults({"m": [run]}).save(str(path))
    loaded = RepeatedExperimentResults.load(str(path))
    assert loaded.runs("m")[0].meta == {"judge_model": "j"}
    assert json.loads(path.read_text())["runs"]["m"][0]["meta"] == {"judge_model": "j"}


def test_find_span_matches_whole_words_only():
    # E7: "safe to take..." used to verify against "unsafe to take...".
    assert find_span("safe to take with alcohol", ["It is unsafe to take with alcohol"]) is None
    assert find_span("safe to take with alcohol", ["It is safe to take with alcohol."]) == 0


def test_cross_judge_severity_shifts_include_scenarios_only_later_judges_rated():
    # E9: the scenario list came from the first judge only.
    from simpleaudit.cross_judge import CrossJudgeResults

    first = RepeatedExperimentResults({"m": [AuditResults([_result("A", "pass")])]})
    second = RepeatedExperimentResults({"m": [AuditResults([_result("A", "pass"),
                                                            _result("B", "high")])]})
    shifts = CrossJudgeResults({"j1": first, "j2": second}).severity_shifts("m")
    assert [s["scenario"] for s in shifts] == ["A", "B"]


def test_cross_judge_label_cannot_escape_save_dir(tmp_path):
    # E9: a user label was used verbatim as a path ("../x" escaped save_dir).
    import pytest
    from simpleaudit.cross_judge import CrossJudgeExperiment

    exp = CrossJudgeExperiment(
        models=[{"model": "m", "provider": "openai"}],
        judge_models=[{"model": "j", "provider": "openai", "label": "../evil"},
                      {"model": "k", "provider": "openai"}],
        save_dir=str(tmp_path), show_progress=False,
    )
    for inner in exp._experiments.values():
        assert inner.save_dir.resolve().parent == tmp_path.resolve()
    with pytest.raises(ValueError):
        CrossJudgeExperiment(
            models=[{"model": "m", "provider": "openai"}],
            judge_models=[{"model": "j", "label": ".."}, {"model": "k"}],
            save_dir=str(tmp_path), show_progress=False,
        )
