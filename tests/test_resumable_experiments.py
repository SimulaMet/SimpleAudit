"""
Tests for resumable experiment runs (save_dir / disk caching).

Covers:
- save_dir creates run_N.json files under {save_dir}/{label}/
- experiment_results.json written at save_dir root
- Resuming a partial experiment only re-runs missing slots
- Final RepeatedExperimentResults still has the expected total run count
- Corrupt/unreadable cached run files are re-run instead of crashing the resume
- Labels are sanitized for the filesystem and collisions are rejected upfront
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.experiment import AuditExperiment
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult, AuditResults


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCENARIOS = [
    {"name": "s1", "description": "d1"},
]


def _make_results(severity: str = "pass") -> AuditResults:
    return AuditResults([
        AuditResult(
            scenario_name="scenario_0",
            scenario_description="desc",
            conversation=[],
            severity=severity,
            issues_found=[],
            positive_behaviors=[],
            summary="",
            recommendations=[],
        )
    ])


def _run_experiment(exp: AuditExperiment, call_counter: dict, return_severity: str = "low"):
    """Run exp.run_async() with patched ModelAuditor, counting live run_async calls."""

    async def fake_run_async(self_a, scenarios, **kwargs):
        call_counter["count"] += 1
        return _make_results(return_severity)

    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()), \
         patch.object(ModelAuditor, "run_async", new=fake_run_async):
        return asyncio.run(exp.run_async(scenarios=SCENARIOS))


def _make_experiment(save_dir: str, n_repetitions: int = 3, label: str = "m1") -> AuditExperiment:
    return AuditExperiment(
        models=[{"model": label, "provider": "openai"}],
        judge_model="judge",
        judge_provider="openai",
        show_progress=False,
        n_repetitions=n_repetitions,
        save_dir=save_dir,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSaveDirCreatesFiles:
    def test_run_files_created_for_each_repetition(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=3)
        counter = {"count": 0}
        _run_experiment(exp, counter)

        assert (tmp_path / "m1" / "run_0.json").exists()
        assert (tmp_path / "m1" / "run_1.json").exists()
        assert (tmp_path / "m1" / "run_2.json").exists()

    def test_experiment_results_json_written(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=2)
        _run_experiment(exp, {"count": 0})

        assert (tmp_path / "experiment_results.json").exists()

    def test_experiment_results_json_has_correct_n_repetitions(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=2)
        _run_experiment(exp, {"count": 0})

        data = json.loads((tmp_path / "experiment_results.json").read_text())
        assert data["n_repetitions"] == 2

    def test_run_files_are_valid_audit_results_json(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        _run_experiment(exp, {"count": 0}, return_severity="high")

        loaded = AuditResults.load(str(tmp_path / "m1" / "run_0.json"))
        assert loaded[0].severity == "high"


class TestResumeFromPartialRuns:
    def test_resumes_and_skips_existing_runs(self, tmp_path):
        """Pre-create run_0 and run_1; experiment should only execute run_2."""
        run_dir = tmp_path / "m1"
        run_dir.mkdir()
        _make_results("pass").save(str(run_dir / "run_0.json"))
        _make_results("pass").save(str(run_dir / "run_1.json"))

        exp = _make_experiment(str(tmp_path), n_repetitions=3)
        counter = {"count": 0}
        _run_experiment(exp, counter, return_severity="critical")

        assert counter["count"] == 1

    def test_resumed_results_has_full_run_count(self, tmp_path):
        run_dir = tmp_path / "m1"
        run_dir.mkdir()
        _make_results("pass").save(str(run_dir / "run_0.json"))

        exp = _make_experiment(str(tmp_path), n_repetitions=3)
        results = _run_experiment(exp, {"count": 0})

        assert len(results._runs["m1"]) == 3

    def test_resumed_runs_preserve_cached_severity(self, tmp_path):
        """The pre-saved run_0 (severity=medium) should appear in the results."""
        run_dir = tmp_path / "m1"
        run_dir.mkdir()
        _make_results("medium").save(str(run_dir / "run_0.json"))

        exp = _make_experiment(str(tmp_path), n_repetitions=2)
        results = _run_experiment(exp, {"count": 0}, return_severity="low")

        assert results._runs["m1"][0][0].severity == "medium"
        assert results._runs["m1"][1][0].severity == "low"

    def test_fully_cached_experiment_makes_no_calls(self, tmp_path):
        """If all run files already exist, no ModelAuditor calls should be made."""
        run_dir = tmp_path / "m1"
        run_dir.mkdir()
        _make_results("pass").save(str(run_dir / "run_0.json"))
        _make_results("pass").save(str(run_dir / "run_1.json"))

        exp = _make_experiment(str(tmp_path), n_repetitions=2)
        counter = {"count": 0}
        _run_experiment(exp, counter)

        assert counter["count"] == 0


class TestCorruptCachedRuns:
    def test_garbage_bytes_run_file_is_rerun(self, tmp_path, capsys):
        """A truncated/corrupt run_0.json must be treated as uncached: warn,
        re-run the slot, and overwrite the bad file with a clean result."""
        run_dir = tmp_path / "m1"
        run_dir.mkdir()
        (run_dir / "run_0.json").write_bytes(b"garbage not json{{{")

        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        counter = {"count": 0}
        results = _run_experiment(exp, counter, return_severity="pass")

        assert counter["count"] == 1
        assert results._runs["m1"][0][0].severity == "pass"
        assert "ignoring unreadable cached run" in capsys.readouterr().out
        # The corrupt file was overwritten by the fresh run.
        reloaded = AuditResults.load(str(run_dir / "run_0.json"))
        assert reloaded[0].severity == "pass"

    def test_schema_incompatible_run_file_is_rerun(self, tmp_path):
        """Valid JSON that does not match the AuditResults schema is also
        treated as uncached rather than crashing the resume."""
        run_dir = tmp_path / "m1"
        run_dir.mkdir()
        (run_dir / "run_0.json").write_text('{"unexpected": "shape"}')

        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        counter = {"count": 0}
        results = _run_experiment(exp, counter, return_severity="low")

        assert counter["count"] == 1
        assert results._runs["m1"][0][0].severity == "low"

    def test_corrupt_file_does_not_invalidate_other_slots(self, tmp_path):
        """Only the corrupt slot is re-run; intact cached runs are still reused."""
        run_dir = tmp_path / "m1"
        run_dir.mkdir()
        _make_results("medium").save(str(run_dir / "run_0.json"))
        (run_dir / "run_1.json").write_text("not json")

        exp = _make_experiment(str(tmp_path), n_repetitions=2)
        counter = {"count": 0}
        results = _run_experiment(exp, counter, return_severity="critical")

        assert counter["count"] == 1
        assert results._runs["m1"][0][0].severity == "medium"
        assert results._runs["m1"][1][0].severity == "critical"


class TestLabelSanitization:
    def test_labels_colliding_after_sanitization_raise(self):
        """'org/model' and 'org:model' share a cache directory once sanitized
        — the experiment must reject them upfront."""
        with pytest.raises(ValueError, match="sanitization"):
            AuditExperiment(
                models=[
                    {"model": "org/model", "provider": "openai"},
                    {"model": "org:model", "provider": "openai"},
                ],
                judge_model="judge",
                judge_provider="openai",
                show_progress=False,
            )

    def test_sanitized_label_path_saves_and_resumes(self, tmp_path):
        """A model named 'org/model:tag' caches under org_model_tag/ and a
        second experiment resumes from that directory without live calls."""
        exp = _make_experiment(str(tmp_path), n_repetitions=1, label="org/model:tag")
        counter = {"count": 0}
        _run_experiment(exp, counter, return_severity="high")

        assert counter["count"] == 1
        run_path = tmp_path / "org_model_tag" / "run_0.json"
        assert run_path.exists()
        assert AuditResults.load(str(run_path))[0].severity == "high"

        resumed = _make_experiment(str(tmp_path), n_repetitions=1, label="org/model:tag")
        counter2 = {"count": 0}
        results = _run_experiment(resumed, counter2, return_severity="pass")

        assert counter2["count"] == 0
        assert results._runs["org/model:tag"][0][0].severity == "high"


class TestConfigFingerprint:
    """Cached runs are only reused under the configuration that produced them."""

    def test_same_config_resumes(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        counter = {"count": 0}
        _run_experiment(exp, counter, return_severity="high")
        assert counter["count"] == 1
        assert (tmp_path / "m1" / "config.json").exists()

        resumed = _make_experiment(str(tmp_path), n_repetitions=1)
        counter2 = {"count": 0}
        results = _run_experiment(resumed, counter2)
        assert counter2["count"] == 0
        assert results._runs["m1"][0][0].severity == "high"

    def test_changed_judge_invalidates_cache(self, tmp_path, capsys):
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        counter = {"count": 0}
        _run_experiment(exp, counter, return_severity="high")
        assert counter["count"] == 1

        changed = AuditExperiment(
            models=[{"model": "m1", "provider": "openai"}],
            judge_model="a-different-judge",
            judge_provider="openai",
            show_progress=False,
            n_repetitions=1,
            save_dir=str(tmp_path),
        )
        counter2 = {"count": 0}
        results = _run_experiment(changed, counter2, return_severity="pass")

        assert counter2["count"] == 1  # cache rejected, run re-executed
        assert results._runs["m1"][0][0].severity == "pass"
        assert "different configuration" in capsys.readouterr().out

    def test_legacy_cache_without_marker_is_accepted(self, tmp_path):
        exp = _make_experiment(str(tmp_path), n_repetitions=1)
        counter = {"count": 0}
        _run_experiment(exp, counter, return_severity="high")
        (tmp_path / "m1" / "config.json").unlink()

        resumed = _make_experiment(str(tmp_path), n_repetitions=1)
        counter2 = {"count": 0}
        results = _run_experiment(resumed, counter2)
        assert counter2["count"] == 0
        assert results._runs["m1"][0][0].severity == "high"
        # marker rewritten for the next resume
        assert (tmp_path / "m1" / "config.json").exists()

    def test_fingerprint_excludes_api_keys(self, tmp_path):
        exp = AuditExperiment(
            models=[{"model": "m1", "provider": "openai"}],
            judge_model="judge",
            judge_provider="openai",
            judge_api_key="super-secret",
            show_progress=False,
            n_repetitions=1,
            save_dir=str(tmp_path),
        )
        counter = {"count": 0}
        _run_experiment(exp, counter)
        stored = json.loads((tmp_path / "m1" / "config.json").read_text())
        assert "super-secret" not in json.dumps(stored)
