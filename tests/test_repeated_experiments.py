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
