"""
Tests for CrossJudgeExperiment and compare_judges utility.

Covers:
- Separate save_dir per judge (collision prevention)
- n_repetitions respected per judge
- Resume/caching works independently per judge
- Severity shifts detected across judges
- compare_judges utility function
- _safe_judge_label derivation
- Input validation
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.cross_judge import CrossJudgeExperiment, CrossJudgeResults, compare_judges
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.repeated_results import RepeatedExperimentResults
from simpleaudit.results import AuditResult, AuditResults


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCENARIOS = [
    {"name": "s1", "description": "d1"},
    {"name": "s2", "description": "d2"},
]

JUDGE_A = "judge-a"
JUDGE_B = "judge-b"


def _make_results(severity: str = "pass") -> AuditResults:
    """Build AuditResults with two scenarios, both at the given severity."""
    return AuditResults([
        AuditResult(
            scenario_name=f"scenario_{i}",
            scenario_description="desc",
            conversation=[],
            severity=severity,
            issues_found=[],
            positive_behaviors=[],
            summary="",
            recommendations=[],
        )
        for i in range(2)
    ])


def _make_results_named(scenario_names: list, severity: str = "pass") -> AuditResults:
    """Build AuditResults with explicitly specified scenario names."""
    return AuditResults([
        AuditResult(
            scenario_name=name,
            scenario_description="desc",
            conversation=[],
            severity=severity,
            issues_found=[],
            positive_behaviors=[],
            summary="",
            recommendations=[],
        )
        for name in scenario_names
    ])


def _make_experiment(save_dir: str, n_repetitions: int = 1) -> CrossJudgeExperiment:
    return CrossJudgeExperiment(
        models=[{"model": "m1", "provider": "openai"}],
        judge_models=[
            {"model": JUDGE_A, "provider": "openai"},
            {"model": JUDGE_B, "provider": "openai"},
        ],
        n_repetitions=n_repetitions,
        save_dir=save_dir,
        show_progress=False,
    )


def _run_experiment(
    exp: CrossJudgeExperiment,
    severity_for_judge: dict = None,
) -> CrossJudgeResults:
    """Execute exp.run_async() with patched ModelAuditor — no real API calls.

    Each judge receives the severity specified in severity_for_judge keyed by
    judge_model string; defaults to "pass" for any unlisted judge.
    """
    sev_map = severity_for_judge or {}

    async def fake_run_async(self_a, scenarios, **kwargs):
        return _make_results(sev_map.get(self_a.judge_model, "pass"))

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake_run_async):
        return asyncio.run(exp.run_async(scenarios=SCENARIOS))


# ---------------------------------------------------------------------------
# 1. Save dir separation
# ---------------------------------------------------------------------------

class TestSaveDirSeparation:
    def test_two_judges_get_separate_save_dirs(self, tmp_path):
        """Each judge's runs land under save_dir/<judge_label>/."""
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        _run_experiment(exp)

        assert (tmp_path / "judge-a" / "m1" / "run_0.json").exists()
        assert (tmp_path / "judge-b" / "m1" / "run_0.json").exists()

    def test_judge_a_does_not_overwrite_judge_b(self, tmp_path):
        """Different judges producing different severities preserve both files."""
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        _run_experiment(exp, severity_for_judge={JUDGE_A: "high", JUDGE_B: "pass"})

        a_result = AuditResults.load(str(tmp_path / "judge-a" / "m1" / "run_0.json"))
        b_result = AuditResults.load(str(tmp_path / "judge-b" / "m1" / "run_0.json"))
        assert a_result[0].severity == "high"
        assert b_result[0].severity == "pass"


# ---------------------------------------------------------------------------
# 2. n_repetitions per judge
# ---------------------------------------------------------------------------

class TestNRepetitionsPerJudge:
    def test_run_files_created_for_all_reps_and_judges(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=3)
        _run_experiment(exp)

        for judge in ("judge-a", "judge-b"):
            for i in range(3):
                assert (tmp_path / judge / "m1" / f"run_{i}.json").exists(), \
                    f"Missing {judge}/m1/run_{i}.json"

    def test_results_contain_n_runs_per_judge(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=3)
        results = _run_experiment(exp)

        for judge_label in results.judges:
            assert len(results[judge_label]._runs["m1"]) == 3


# ---------------------------------------------------------------------------
# 3. Resume works independently per judge
# ---------------------------------------------------------------------------

class TestResumePerJudge:
    def test_cached_runs_skipped_for_partial_judge(self, tmp_path):
        """Pre-cache 2 runs for judge-a; verify 1 live call for judge-a, 3 for judge-b."""
        run_dir = tmp_path / "judge-a" / "m1"
        run_dir.mkdir(parents=True)
        _make_results("medium").save(str(run_dir / "run_0.json"))
        _make_results("medium").save(str(run_dir / "run_1.json"))

        call_counts: dict = {}

        async def fake_run_async(self_a, scenarios, **kwargs):
            call_counts[self_a.judge_model] = call_counts.get(self_a.judge_model, 0) + 1
            return _make_results("low")

        exp = _make_experiment(str(tmp_path), n_repetitions=3)
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
             patch.object(ModelAuditor, "run_async", new=fake_run_async):
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        # judge-a had 2 cached → only run_2 is live
        assert call_counts.get(JUDGE_A, 0) == 1
        # judge-b had nothing cached → all 3 runs are live
        assert call_counts.get(JUDGE_B, 0) == 3

    def test_resumed_results_preserve_cached_severity(self, tmp_path):
        """run_0 pre-saved as 'critical' should survive the resume."""
        run_dir = tmp_path / "judge-a" / "m1"
        run_dir.mkdir(parents=True)
        _make_results("critical").save(str(run_dir / "run_0.json"))

        exp = _make_experiment(str(tmp_path), n_repetitions=2)
        results = _run_experiment(exp, severity_for_judge={JUDGE_A: "pass", JUDGE_B: "pass"})

        runs_a = results["judge-a"]._runs["m1"]
        assert runs_a[0][0].severity == "critical"  # from cache
        assert runs_a[1][0].severity == "pass"       # from live call


# ---------------------------------------------------------------------------
# 4. Severity shifts detected
# ---------------------------------------------------------------------------

class TestSeverityShiftsDetected:
    def test_shifted_scenarios_identified(self, tmp_path):
        """high under judge-a vs pass under judge-b → both scenarios shift."""
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        results = _run_experiment(
            exp,
            severity_for_judge={JUDGE_A: "high", JUDGE_B: "pass"},
        )

        shifts = results.severity_shifts("m1")
        shifted = [s for s in shifts if s["shifted"]]
        assert len(shifted) == 2

    def test_shift_direction_negative_when_more_lenient(self, tmp_path):
        """high → pass: judge_b is more lenient, so direction < 0."""
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        results = _run_experiment(
            exp,
            severity_for_judge={JUDGE_A: "high", JUDGE_B: "pass"},
        )

        for entry in results.severity_shifts("m1"):
            assert entry.get("direction", 0) < 0

    def test_no_shifts_when_judges_agree(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        results = _run_experiment(
            exp,
            severity_for_judge={JUDGE_A: "medium", JUDGE_B: "medium"},
        )

        assert all(not s["shifted"] for s in results.severity_shifts("m1"))


# ---------------------------------------------------------------------------
# 5. compare_judges utility
# ---------------------------------------------------------------------------

class TestCompareJudgesUtility:
    def test_score_delta_high_to_pass(self):
        """all-high (score 25) vs all-pass (score 100) → delta = 75."""
        runs_a = RepeatedExperimentResults({"m1": [_make_results("high")] * 3})
        runs_b = RepeatedExperimentResults({"m1": [_make_results("pass")] * 3})

        result = compare_judges(runs_a, runs_b, "m1", "opus-4-7", "opus-4-8")

        assert result["score_delta"] == 75.0
        assert result["n_shifted"] == 2
        assert result["n_total"] == 2

    def test_direction_positive_when_stricter(self):
        """pass → high: judge_b is stricter, direction > 0."""
        runs_a = RepeatedExperimentResults({"m1": [_make_results("pass")] * 3})
        runs_b = RepeatedExperimentResults({"m1": [_make_results("high")] * 3})

        result = compare_judges(runs_a, runs_b, "m1")

        for shift in result["severity_shifts"]:
            assert shift["direction"] > 0

    def test_no_shifts_when_identical(self):
        runs = RepeatedExperimentResults({"m1": [_make_results("medium")] * 3})
        result = compare_judges(runs, runs, "m1")

        assert result["n_shifted"] == 0
        assert result["score_delta"] == 0.0

    def test_labels_preserved_in_output(self):
        runs = RepeatedExperimentResults({"m1": [_make_results("pass")] * 3})
        result = compare_judges(runs, runs, "m1", label_a="opus-4-7", label_b="opus-4-8")

        assert result["judge_a"] == "opus-4-7"
        assert result["judge_b"] == "opus-4-8"

    def test_missing_subject_raises_key_error(self):
        runs = RepeatedExperimentResults({"m1": [_make_results("pass")] * 3})
        with pytest.raises(KeyError):
            compare_judges(runs, runs, "nonexistent-model")

    def test_compare_judges_handles_mismatched_scenarios(self):
        """Scenarios present only in results_a are skipped gracefully."""
        runs_a = RepeatedExperimentResults({
            "m1": [_make_results_named(["scenario_0", "scenario_extra"], "high")] * 3,
        })
        runs_b = RepeatedExperimentResults({
            "m1": [_make_results_named(["scenario_0"], "pass")] * 3,
        })

        result = compare_judges(runs_a, runs_b, "m1")

        # scenario_extra is absent from results_b → skipped, no crash
        shift_names = [s["scenario"] for s in result["severity_shifts"]]
        assert "scenario_extra" not in shift_names
        # scenario_0 shifts high → pass, direction < 0
        assert result["n_shifted"] == 1
        assert result["n_total"] == 2   # report_a.per_scenario has 2 entries


# ---------------------------------------------------------------------------
# 6. _safe_judge_label derivation
# ---------------------------------------------------------------------------

class TestSafeJudgeLabel:
    def test_label_key_takes_precedence(self):
        info = {"model": "claude-opus-4-8", "provider": "anthropic", "label": "opus48"}
        assert CrossJudgeExperiment._safe_judge_label(info) == "opus48"

    def test_strips_provider_prefix(self):
        info = {"model": "anthropic:claude-opus-4-8"}
        assert CrossJudgeExperiment._safe_judge_label(info) == "opus-4-8"

    def test_strips_claude_prefix(self):
        info = {"model": "claude-opus-4-7"}
        assert CrossJudgeExperiment._safe_judge_label(info) == "opus-4-7"

    def test_non_anthropic_model_unchanged(self):
        info = {"model": "gpt-4o", "provider": "openai"}
        assert CrossJudgeExperiment._safe_judge_label(info) == "gpt-4o"


# ---------------------------------------------------------------------------
# 7. Input validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_fewer_than_two_judges_raises(self):
        with pytest.raises(ValueError, match="at least two"):
            CrossJudgeExperiment(
                models=[{"model": "m1", "provider": "openai"}],
                judge_models=[{"model": "j1", "provider": "openai"}],
            )

    def test_empty_models_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            CrossJudgeExperiment(
                models=[],
                judge_models=[
                    {"model": "j1", "provider": "openai"},
                    {"model": "j2", "provider": "openai"},
                ],
            )

    def test_duplicate_judge_labels_raises(self):
        # Two dicts that derive the same label ("opus-4-7") should be rejected.
        with pytest.raises(ValueError, match="Duplicate judge label"):
            CrossJudgeExperiment(
                models=[{"model": "m1", "provider": "openai"}],
                judge_models=[
                    {"model": "claude-opus-4-7", "provider": "anthropic"},
                    {"model": "claude-opus-4-7", "provider": "anthropic"},
                ],
                show_progress=False,
            )

    def test_mismatched_auditor_models_raises(self):
        with pytest.raises(ValueError, match="same length"):
            CrossJudgeExperiment(
                models=[{"model": "m1", "provider": "openai"}],
                judge_models=[
                    {"model": "j1", "provider": "openai"},
                    {"model": "j2", "provider": "openai"},
                ],
                auditor_models=[{"model": "a1", "provider": "openai"}],
            )

    def test_n_repetitions_zero_raises(self):
        with pytest.raises(ValueError, match="n_repetitions"):
            CrossJudgeExperiment(
                models=[{"model": "m1", "provider": "openai"}],
                judge_models=[
                    {"model": "j1", "provider": "openai"},
                    {"model": "j2", "provider": "openai"},
                ],
                n_repetitions=0,
            )


# ---------------------------------------------------------------------------
# 8. CrossJudgeResults.to_dict serialization
# ---------------------------------------------------------------------------

class TestCrossJudgeResultsToDict:
    def test_to_dict_structure_and_json_roundtrip(self):
        """to_dict returns expected top-level keys and is JSON-serializable."""
        runs_a = RepeatedExperimentResults({"m1": [_make_results("high")] * 3})
        runs_b = RepeatedExperimentResults({"m1": [_make_results("pass")] * 3})
        cjr = CrossJudgeResults({"judge-a": runs_a, "judge-b": runs_b})

        d = cjr.to_dict()

        # Top-level structure
        assert d["judges"] == ["judge-a", "judge-b"]
        assert "score_summary" in d
        assert "runs" in d

        # score_summary nested structure: subject → judge → stats
        assert "m1" in d["score_summary"]
        assert "judge-a" in d["score_summary"]["m1"]
        stats = d["score_summary"]["m1"]["judge-a"]
        assert "mean" in stats
        assert "n_runs" in stats
        assert stats["n_runs"] == 3

        # Full JSON roundtrip preserves judges list and nested stats
        roundtripped = json.loads(json.dumps(d))
        assert roundtripped["judges"] == ["judge-a", "judge-b"]
        assert roundtripped["score_summary"]["m1"]["judge-b"]["n_runs"] == 3


# ---------------------------------------------------------------------------
# 9. CrossJudgeExperiment.run() sync wrapper
# ---------------------------------------------------------------------------

class TestSyncWrapper:
    def test_run_sync_wrapper_executes(self, tmp_path):
        """run() synchronous wrapper returns CrossJudgeResults outside an event loop."""
        exp = _make_experiment(str(tmp_path), n_repetitions=1)

        async def fake_run_async(self_a, scenarios, **kwargs):
            return _make_results("pass")

        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
             patch.object(ModelAuditor, "run_async", new=fake_run_async):
            results = exp.run(scenarios=SCENARIOS)

        assert isinstance(results, CrossJudgeResults)
        assert set(results.judges) == {JUDGE_A, JUDGE_B}
        # judge_labels property mirrors judges list
        assert exp.judge_labels == list(results.judges)
