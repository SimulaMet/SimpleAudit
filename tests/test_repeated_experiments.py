"""
Tests for n_repetitions support: AuditExperiment → RepeatedExperimentResults.

Covers:
- n_repetitions=N produces N runs per model label
- Backward-compatible dict interface returns first run
- stability() returns correct ModelStabilityReport stats
- summary() runs without error
- to_dict() structure
- save/load round-trip preserves run count and severities
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.experiment import AuditExperiment
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.repeated_results import ModelStabilityReport, RepeatedExperimentResults
from simpleaudit.results import AuditResult, AuditResults


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCENARIOS = [
    {"name": "s1", "description": "d1"},
    {"name": "s2", "description": "d2"},
]


def _make_results(severities: list) -> AuditResults:
    """Build AuditResults with controlled per-scenario severities."""
    return AuditResults([
        AuditResult(
            scenario_name=f"scenario_{i}",
            scenario_description="desc",
            conversation=[],
            severity=sev,
            issues_found=[],
            positive_behaviors=[],
            summary="",
            recommendations=[],
        )
        for i, sev in enumerate(severities)
    ])


def _make_experiment(n_repetitions: int = 1, **kwargs) -> AuditExperiment:
    return AuditExperiment(
        models=[{"model": "test-model", "provider": "openai"}],
        judge_model="judge",
        judge_provider="openai",
        show_progress=False,
        n_repetitions=n_repetitions,
        **kwargs,
    )


def _run_experiment(exp: AuditExperiment, run_results: list) -> RepeatedExperimentResults:
    """Execute exp.run_async() without real API calls, returning controlled results."""
    seq = iter(run_results)

    async def fake_run_async(self_a, scenarios, **kwargs):
        return next(seq)

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake_run_async):
        return asyncio.run(exp.run_async(scenarios=SCENARIOS))


# ---------------------------------------------------------------------------
# AuditExperiment — n_repetitions integration
# ---------------------------------------------------------------------------

class TestAuditExperimentRepetitions:
    def test_n_repetitions_3_produces_3_runs(self):
        exp = _make_experiment(n_repetitions=3)
        r = _make_results(["pass"])
        results = _run_experiment(exp, [r, r, r])

        assert isinstance(results, RepeatedExperimentResults)
        assert len(results._runs["test-model"]) == 3

    def test_n_repetitions_1_is_default_compatible(self):
        exp = _make_experiment(n_repetitions=1)
        r = _make_results(["low"])
        results = _run_experiment(exp, [r])

        assert len(results._runs["test-model"]) == 1

    def test_invalid_n_repetitions_raises(self):
        with pytest.raises(ValueError, match="n_repetitions"):
            AuditExperiment(
                models=[{"model": "m", "provider": "openai"}],
                n_repetitions=0,
            )


# ---------------------------------------------------------------------------
# RepeatedExperimentResults — dict backward compatibility
# ---------------------------------------------------------------------------

class TestBackwardCompatDictInterface:
    def _make(self) -> RepeatedExperimentResults:
        r1 = _make_results(["critical"])
        r2 = _make_results(["pass"])
        return RepeatedExperimentResults({"model-a": [r1, r2], "model-b": [r1]})

    def test_getitem_returns_first_run(self):
        results = self._make()
        first = results["model-a"]
        assert isinstance(first, AuditResults)
        assert first[0].severity == "critical"

    def test_contains(self):
        results = self._make()
        assert "model-a" in results
        assert "model-b" in results
        assert "model-c" not in results

    def test_len(self):
        results = self._make()
        assert len(results) == 2

    def test_iter_yields_model_labels(self):
        results = self._make()
        assert set(results) == {"model-a", "model-b"}

    def test_keys(self):
        results = self._make()
        assert set(results.keys()) == {"model-a", "model-b"}

    def test_values_returns_first_runs(self):
        results = self._make()
        for val in results.values():
            assert isinstance(val, AuditResults)

    def test_items_yields_label_and_first_run(self):
        results = self._make()
        for label, run in results.items():
            assert isinstance(label, str)
            assert isinstance(run, AuditResults)


# ---------------------------------------------------------------------------
# RepeatedExperimentResults — access to all runs
# ---------------------------------------------------------------------------

class TestAllRunsAccess:
    def _make(self) -> RepeatedExperimentResults:
        r1 = _make_results(["critical"])
        r2 = _make_results(["pass"])
        r3 = _make_results(["medium"])
        return RepeatedExperimentResults({"model-a": [r1, r2, r3]})

    def test_runs_returns_all_runs_in_order(self):
        results = self._make()
        runs = results.runs("model-a")
        assert len(runs) == 3
        assert [r[0].severity for r in runs] == ["critical", "pass", "medium"]

    def test_runs_returns_copy(self):
        results = self._make()
        runs = results.runs("model-a")
        runs.clear()
        assert len(results.runs("model-a")) == 3

    def test_runs_unknown_model_raises_keyerror(self):
        results = self._make()
        with pytest.raises(KeyError):
            results.runs("nope")

    def test_tuple_getitem_returns_specific_run(self):
        results = self._make()
        assert results["model-a", 0][0].severity == "critical"
        assert results["model-a", 1][0].severity == "pass"
        assert results["model-a", 2][0].severity == "medium"

    def test_tuple_getitem_matches_runs(self):
        results = self._make()
        for i, run in enumerate(results.runs("model-a")):
            assert results["model-a", i] is run

    def test_tuple_getitem_unknown_model_raises_keyerror(self):
        results = self._make()
        with pytest.raises(KeyError):
            results["nope", 0]

    def test_tuple_getitem_out_of_range_raises_indexerror(self):
        results = self._make()
        with pytest.raises(IndexError):
            results["model-a", 3]
        with pytest.raises(IndexError):
            results["model-a", -1]

    def test_all_runs_returns_dict_of_all_runs(self):
        results = self._make()
        all_runs = results.all_runs()
        assert set(all_runs.keys()) == {"model-a"}
        assert len(all_runs["model-a"]) == 3
        assert [r[0].severity for r in all_runs["model-a"]] == ["critical", "pass", "medium"]

    def test_all_runs_returns_copies(self):
        results = self._make()
        all_runs = results.all_runs()
        all_runs["model-a"].clear()
        assert len(results.runs("model-a")) == 3

    def test_all_runs_multiple_models(self):
        r1 = _make_results(["pass"])
        r2 = _make_results(["high"])
        results = RepeatedExperimentResults({
            "model-a": [r1, r2],
            "model-b": [r1],
        })
        all_runs = results.all_runs()
        assert len(all_runs["model-a"]) == 2
        assert len(all_runs["model-b"]) == 1

    def test_all_runs_empty(self):
        results = RepeatedExperimentResults({})
        assert results.all_runs() == {}


# ---------------------------------------------------------------------------
# RepeatedExperimentResults — stability statistics
# ---------------------------------------------------------------------------

class TestStabilityStats:
    def _three_run_results(self) -> RepeatedExperimentResults:
        # 1 scenario per run; known severities → known scores
        # pass=100, low=75, medium=50
        return RepeatedExperimentResults({
            "m": [
                _make_results(["pass"]),    # score 100
                _make_results(["low"]),     # score  75
                _make_results(["medium"]),  # score  50
            ]
        })

    def test_stability_returns_model_stability_report(self):
        results = self._three_run_results()
        report = results.stability("m")
        assert isinstance(report, ModelStabilityReport)

    def test_stability_n_runs(self):
        results = self._three_run_results()
        assert results.stability("m").n_runs == 3

    def test_stability_mean_score(self):
        results = self._three_run_results()
        report = results.stability("m")
        # (100 + 75 + 50) / 3 = 75.0
        assert report.mean_score == 75.0

    def test_stability_min_max(self):
        results = self._three_run_results()
        report = results.stability("m")
        assert report.min_score == 50.0
        assert report.max_score == 100.0

    def test_stability_per_scenario_pass_rate(self):
        results = self._three_run_results()
        report = results.stability("m")
        # scenario_0: pass in 1/3 runs → pass_rate = 1/3 ≈ 0.333
        stats = report.per_scenario["scenario_0"]
        assert abs(stats.pass_rate - 1 / 3) < 0.01

    def test_stability_per_scenario_agreement_rate(self):
        # All 3 runs return "pass" → agreement = 1.0
        results = RepeatedExperimentResults({
            "m": [_make_results(["pass"]), _make_results(["pass"]), _make_results(["pass"])]
        })
        report = results.stability("m")
        assert report.per_scenario["scenario_0"].agreement_rate == 1.0

    def test_stability_unknown_model_raises(self):
        results = RepeatedExperimentResults({"m": [_make_results(["pass"])]})
        with pytest.raises(KeyError):
            results.stability("nonexistent")

    def test_summary_does_not_crash(self):
        results = self._three_run_results()
        results.summary()  # should not raise


# ---------------------------------------------------------------------------
# RepeatedExperimentResults — fragility signal (Part 1 of #48)
# ---------------------------------------------------------------------------

class TestFragilitySignal:
    """Per-scenario fragility: entropy, ordinal spread, fragile() accessor."""

    def _uniform_results(self) -> RepeatedExperimentResults:
        """3 runs, 1 scenario, all different severities → max disagreement."""
        return RepeatedExperimentResults({
            "m": [
                _make_results(["pass"]),
                _make_results(["medium"]),
                _make_results(["critical"]),
            ]
        })

    def test_entropy_zero_when_all_runs_agree(self):
        results = RepeatedExperimentResults({
            "m": [_make_results(["pass"]), _make_results(["pass"]), _make_results(["pass"])]
        })
        stats = results.stability("m").per_scenario["scenario_0"]
        assert stats.entropy == 0.0

    def test_entropy_positive_when_runs_disagree(self):
        results = self._uniform_results()
        stats = results.stability("m").per_scenario["scenario_0"]
        # 3 distinct severities out of 5 possible → entropy > 0
        assert stats.entropy > 0.0
        # Normalised: max entropy for 3 distinct values is log(3), so
        # entropy should be 1.0 (uniform over 3 categories)
        assert abs(stats.entropy - 1.0) < 0.01

    def test_entropy_between_zero_and_one(self):
        # 2 runs: pass, pass → entropy 0
        # 2 runs: pass, low → entropy 1.0 (uniform over 2)
        results = RepeatedExperimentResults({
            "m": [_make_results(["pass"]), _make_results(["low"])]
        })
        stats = results.stability("m").per_scenario["scenario_0"]
        assert 0.0 < stats.entropy <= 1.0

    def test_ordinal_spread_zero_when_all_runs_agree(self):
        results = RepeatedExperimentResults({
            "m": [_make_results(["high"]), _make_results(["high"]), _make_results(["high"])]
        })
        stats = results.stability("m").per_scenario["scenario_0"]
        assert stats.ordinal_spread == 0.0

    def test_ordinal_spread_positive_when_severities_differ(self):
        # pass=0, medium=2, critical=4 → positions [0, 2, 4]
        # std = sqrt(((0-2)^2 + (2-2)^2 + (4-2)^2) / 2) = sqrt(8/2) = 2.0
        results = self._uniform_results()
        stats = results.stability("m").per_scenario["scenario_0"]
        assert abs(stats.ordinal_spread - 2.0) < 0.01

    def test_ordinal_spread_small_for_adjacent_severities(self):
        # pass=0, low=1 → positions [0, 1] → sample std = 1/√2 ≈ 0.7071
        results = RepeatedExperimentResults({
            "m": [_make_results(["pass"]), _make_results(["low"])]
        })
        stats = results.stability("m").per_scenario["scenario_0"]
        assert abs(stats.ordinal_spread - 0.7071) < 0.01

    def test_fragile_returns_empty_when_all_stable(self):
        results = RepeatedExperimentResults({
            "m": [_make_results(["pass"]), _make_results(["pass"]), _make_results(["pass"])]
        })
        report = results.stability("m")
        assert report.fragile(threshold=0.6) == {}

    def test_fragile_returns_disagreement_scenarios(self):
        # 3 runs, 3 different severities → agreement = 1/3 ≈ 0.33 < 0.6
        results = self._uniform_results()
        report = results.stability("m")
        fragile = report.fragile(threshold=0.6)
        assert "scenario_0" in fragile
        assert fragile["scenario_0"].agreement_rate < 0.6

    def test_fragile_threshold_boundary(self):
        # 2 runs: pass, pass → agreement = 1.0 (not fragile)
        # 2 runs: pass, low  → agreement = 0.5 (fragile at threshold 0.6)
        results = RepeatedExperimentResults({
            "m": [
                _make_results(["pass"]),
                _make_results(["low"]),
            ]
        })
        report = results.stability("m")
        # agreement = 0.5 < 0.6 → fragile
        assert "scenario_0" in report.fragile(threshold=0.6)
        # agreement = 0.5 < 0.5 is False → not fragile at threshold 0.5
        assert "scenario_0" not in report.fragile(threshold=0.5)

    def test_fragile_default_threshold(self):
        # 4 runs: pass, pass, pass, low → agreement = 3/4 = 0.75 (not fragile)
        results = RepeatedExperimentResults({
            "m": [
                _make_results(["pass"]),
                _make_results(["pass"]),
                _make_results(["pass"]),
                _make_results(["low"]),
            ]
        })
        report = results.stability("m")
        assert "scenario_0" not in report.fragile()

    def test_fragile_multiple_scenarios(self):
        # 2 scenarios: s0 stable, s1 unstable
        def _two_scenario(sev0, sev1):
            return AuditResults([
                AuditResult(
                    scenario_name="stable", scenario_description="d",
                    conversation=[], severity=sev0, issues_found=[],
                    positive_behaviors=[], summary="", recommendations=[],
                ),
                AuditResult(
                    scenario_name="unstable", scenario_description="d",
                    conversation=[], severity=sev1, issues_found=[],
                    positive_behaviors=[], summary="", recommendations=[],
                ),
            ])

        results = RepeatedExperimentResults({
            "m": [
                _two_scenario("pass", "pass"),
                _two_scenario("pass", "critical"),
                _two_scenario("pass", "medium"),
            ]
        })
        report = results.stability("m")
        fragile = report.fragile(threshold=0.6)
        assert "stable" not in fragile
        assert "unstable" in fragile

    def test_to_dict_includes_fragility_fields(self):
        results = self._uniform_results()
        report = results.stability("m")
        d = report.to_dict()
        stats = d["per_scenario"]["scenario_0"]
        assert "entropy" in stats
        assert "ordinal_spread" in stats

    def test_single_run_has_zero_fragility(self):
        results = RepeatedExperimentResults({
            "m": [_make_results(["high"])]
        })
        stats = results.stability("m").per_scenario["scenario_0"]
        assert stats.entropy == 0.0
        assert stats.ordinal_spread == 0.0
        assert stats.agreement_rate == 1.0


# ---------------------------------------------------------------------------
# RepeatedExperimentResults — serialization
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_has_expected_top_level_keys(self):
        results = RepeatedExperimentResults({"m": [_make_results(["pass"])]})
        d = results.to_dict()
        assert "n_repetitions" in d
        assert "models" in d
        assert "aggregate" in d
        assert "runs" in d

    def test_to_dict_n_repetitions_matches_run_count(self):
        runs = [_make_results(["pass"]), _make_results(["low"])]
        results = RepeatedExperimentResults({"m": runs})
        assert results.to_dict()["n_repetitions"] == 2

    def test_save_load_roundtrip_preserves_run_count(self, tmp_path):
        r1 = _make_results(["pass"])
        r2 = _make_results(["low"])
        results = RepeatedExperimentResults({"m": [r1, r2]})

        path = str(tmp_path / "exp.json")
        results.save(path)
        loaded = RepeatedExperimentResults.load(path)

        assert len(loaded._runs["m"]) == 2

    def test_save_load_roundtrip_preserves_severities(self, tmp_path):
        r1 = _make_results(["critical"])
        r2 = _make_results(["pass"])
        results = RepeatedExperimentResults({"m": [r1, r2]})

        path = str(tmp_path / "exp.json")
        results.save(path)
        loaded = RepeatedExperimentResults.load(path)

        assert loaded._runs["m"][0][0].severity == "critical"
        assert loaded._runs["m"][1][0].severity == "pass"

    def test_save_load_backward_compat_getitem(self, tmp_path):
        r1 = _make_results(["high"])
        results = RepeatedExperimentResults({"m": [r1]})

        path = str(tmp_path / "exp.json")
        results.save(path)
        loaded = RepeatedExperimentResults.load(path)

        first = loaded["m"]
        assert isinstance(first, AuditResults)
        assert first[0].severity == "high"

    def test_save_load_preserves_judge_metadata(self, tmp_path):
        judge = {"judge_model": "judge-x", "judge_provider": "openai"}
        results = RepeatedExperimentResults({"m": [_make_results(["pass"])]}, judge=judge)

        path = str(tmp_path / "exp.json")
        results.save(path)
        loaded = RepeatedExperimentResults.load(path)

        assert loaded._judge == judge
        assert loaded.to_dict()["judge"]["judge_model"] == "judge-x"


# ---------------------------------------------------------------------------
# AuditExperiment — on_model_done callback
# ---------------------------------------------------------------------------

class TestOnModelDone:
    def _run_two_model_experiment(self, callback):
        exp = AuditExperiment(
            models=[
                {"model": "m1", "provider": "openai"},
                {"model": "m2", "provider": "openai"},
            ],
            judge_model="judge-x",
            judge_provider="openai",
            show_progress=False,
            n_repetitions=1,
            on_model_done=callback,
        )
        run_results = [_make_results(["pass"]), _make_results(["low"])]
        return _run_experiment(exp, run_results)

    def test_callback_fires_once_per_model_label(self):
        seen = []
        self._run_two_model_experiment(lambda label, partial: seen.append(label))
        assert seen == ["m1", "m2"]

    def test_partial_results_contain_only_that_models_runs(self):
        partials = {}
        self._run_two_model_experiment(
            lambda label, partial: partials.__setitem__(label, partial)
        )

        for label, partial in partials.items():
            assert isinstance(partial, RepeatedExperimentResults)
            assert list(partial.keys()) == [label]
            assert len(partial._runs[label]) == 1
        assert partials["m1"]["m1"][0].severity == "pass"
        assert partials["m2"]["m2"][0].severity == "low"

    def test_partial_results_carry_judge_metadata(self):
        partials = {}
        self._run_two_model_experiment(
            lambda label, partial: partials.__setitem__(label, partial)
        )

        for partial in partials.values():
            judge = partial.to_dict()["judge"]
            assert judge is not None
            assert judge["judge_model"] == "judge-x"
            assert judge["judge_provider"] == "openai"


# ---------------------------------------------------------------------------
# AuditExperiment — adaptive reruns (Part 2 of #48)
# ---------------------------------------------------------------------------

class TestAdaptiveReruns:
    """Adaptive rerun allocation: spend extra budget on fragile scenarios."""

    SCENARIOS = [
        {"name": "s1", "description": "d1"},
        {"name": "s2", "description": "d2"},
    ]

    def _make_experiment(self, n_repetitions=2, adaptive_reruns=None, **kwargs):
        return AuditExperiment(
            models=[{"model": "test-model", "provider": "openai"}],
            judge_model="judge",
            judge_provider="openai",
            show_progress=False,
            n_repetitions=n_repetitions,
            adaptive_reruns=adaptive_reruns,
            **kwargs,
        )

    def test_adaptive_reruns_none_is_default(self):
        exp = self._make_experiment()
        assert exp.adaptive_reruns is None

    def test_adaptive_reruns_stored(self):
        config = {"agreement_target": 0.8, "max_extra": 3}
        exp = self._make_experiment(adaptive_reruns=config)
        assert exp.adaptive_reruns == config

    def test_invalid_agreement_target_zero(self):
        with pytest.raises(ValueError, match="agreement_target"):
            self._make_experiment(adaptive_reruns={"agreement_target": 0})

    def test_invalid_agreement_target_above_one(self):
        with pytest.raises(ValueError, match="agreement_target"):
            self._make_experiment(adaptive_reruns={"agreement_target": 1.5})

    def test_missing_agreement_target(self):
        with pytest.raises(ValueError, match="agreement_target"):
            self._make_experiment(adaptive_reruns={"max_extra": 3})

    def test_negative_max_extra(self):
        with pytest.raises(ValueError, match="max_extra"):
            self._make_experiment(adaptive_reruns={"agreement_target": 0.8, "max_extra": -1})

    def test_non_dict_raises(self):
        with pytest.raises(ValueError, match="adaptive_reruns"):
            self._make_experiment(adaptive_reruns="not a dict")

    def test_adaptive_reruns_triggers_extra_run_when_disagreement(self):
        """2 base runs disagree on s1 → 1 adaptive rerun triggered."""
        # Base runs: s1 gets pass/critical (agreement 0.5 < 0.8)
        # s2 gets pass/pass (agreement 1.0 >= 0.8)
        run1 = AuditResults([
            AuditResult(scenario_name="s1", scenario_description="d", conversation=[],
                        severity="pass", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
            AuditResult(scenario_name="s2", scenario_description="d", conversation=[],
                        severity="pass", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
        ])
        run2 = AuditResults([
            AuditResult(scenario_name="s1", scenario_description="d", conversation=[],
                        severity="critical", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
            AuditResult(scenario_name="s2", scenario_description="d", conversation=[],
                        severity="pass", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
        ])
        # Adaptive rerun: s1 now gets pass (agreement becomes 2/3 = 0.67 < 0.8, still below)
        # But max_extra=1 so only 1 extra run
        adaptive_run = AuditResults([
            AuditResult(scenario_name="s1", scenario_description="d", conversation=[],
                        severity="pass", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
        ])

        call_count = 0

        async def fake_run_async(self_a, scenarios, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # Base runs
                return run1 if call_count == 1 else run2
            else:
                # Adaptive rerun (only fragile scenarios)
                assert len(scenarios) == 1
                assert scenarios[0]["name"] == "s1"
                return adaptive_run

        exp = self._make_experiment(
            n_repetitions=2,
            adaptive_reruns={"agreement_target": 0.8, "max_extra": 1},
        )
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
             patch.object(ModelAuditor, "run_async", new=fake_run_async):
            results = asyncio.run(exp.run_async(scenarios=self.SCENARIOS))

        # 2 base runs + 1 adaptive rerun = 3 runs total
        assert len(results._runs["test-model"]) == 3
        assert call_count == 3

    def test_adaptive_reruns_no_extra_when_all_stable(self):
        """All scenarios agree → no adaptive reruns."""
        run1 = AuditResults([
            AuditResult(scenario_name="s1", scenario_description="d", conversation=[],
                        severity="pass", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
            AuditResult(scenario_name="s2", scenario_description="d", conversation=[],
                        severity="pass", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
        ])
        run2 = AuditResults([
            AuditResult(scenario_name="s1", scenario_description="d", conversation=[],
                        severity="pass", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
            AuditResult(scenario_name="s2", scenario_description="d", conversation=[],
                        severity="pass", issues_found=[], positive_behaviors=[],
                        summary="", recommendations=[]),
        ])

        call_count = 0

        async def fake_run_async(self_a, scenarios, **kwargs):
            nonlocal call_count
            call_count += 1
            return run1 if call_count == 1 else run2

        exp = self._make_experiment(
            n_repetitions=2,
            adaptive_reruns={"agreement_target": 0.8, "max_extra": 3},
        )
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
             patch.object(ModelAuditor, "run_async", new=fake_run_async):
            results = asyncio.run(exp.run_async(scenarios=self.SCENARIOS))

        # Only 2 base runs, no adaptive reruns
        assert len(results._runs["test-model"]) == 2
        assert call_count == 2

    def test_adaptive_reruns_respects_max_extra(self):
        """max_extra=2 limits total adaptive reruns to 2."""
        # All runs disagree on s1 → agreement always < 0.8
        def _make_run(sev):
            return AuditResults([
                AuditResult(scenario_name="s1", scenario_description="d", conversation=[],
                            severity=sev, issues_found=[], positive_behaviors=[],
                            summary="", recommendations=[]),
            ])

        severities = ["pass", "critical", "medium", "high", "low"]
        call_count = 0

        async def fake_run_async(self_a, scenarios, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_run(severities[call_count - 1])

        exp = self._make_experiment(
            n_repetitions=2,
            adaptive_reruns={"agreement_target": 0.9, "max_extra": 2},
        )
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
             patch.object(ModelAuditor, "run_async", new=fake_run_async):
            results = asyncio.run(exp.run_async(scenarios=[{"name": "s1", "description": "d"}]))

        # 2 base + 2 adaptive = 4 runs
        assert len(results._runs["test-model"]) == 4
        assert call_count == 4

    def test_adaptive_reruns_backward_compatible(self):
        """Without adaptive_reruns, behavior is unchanged."""
        run1 = _make_results(["pass"])
        run2 = _make_results(["low"])

        exp = self._make_experiment(n_repetitions=2)
        results = _run_experiment(exp, [run1, run2])
        assert len(results._runs["test-model"]) == 2
