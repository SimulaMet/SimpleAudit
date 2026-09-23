"""
Tests for AuditExperiment fine-grained execution primitives:
- on_rep_done callback (sync and async)
- cancel_event (cooperative cancellation)
- max_retries_per_rep (auto-retry on ERROR)
- rep_is_done (external idempotency)
- run_scenario_reps (single-scenario, multi-rep)
- run_streamed (async generator yielding ExperimentEvent)
- Per-rep cache invalidation with max_error_reps threshold
- ModelAuditor.run_scenario_repeated
"""

import asyncio
import json
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.experiment import AuditExperiment, ExperimentEvent
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

SINGLE_SCENARIO = {"name": "s1", "description": "d1"}


def _make_results(severities: list) -> AuditResults:
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


def _make_experiment(n_repetitions: int = 3, **kwargs) -> AuditExperiment:
    return AuditExperiment(
        models=[{"model": "test-model", "provider": "openai"}],
        judge_model="judge",
        judge_provider="openai",
        show_progress=False,
        n_repetitions=n_repetitions,
        **kwargs,
    )


def _patch_run_async(results_sequence: List[AuditResults]):
    """Patch ModelAuditor.run_async to return results from a sequence."""
    seq = iter(results_sequence)

    async def fake_run_async(self_a, scenarios, **kwargs):
        return next(seq)

    return patch.object(ModelAuditor, "run_async", new=fake_run_async), \
           patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock())


# ---------------------------------------------------------------------------
# on_rep_done callback
# ---------------------------------------------------------------------------

class TestOnRepDone:
    def test_sync_callback_called_per_rep(self):
        calls = []

        def on_rep_done(label, rep_index, total_reps, result):
            calls.append((label, rep_index, total_reps))

        exp = _make_experiment(n_repetitions=3, on_rep_done=on_rep_done)
        results = [_make_results(["pass", "pass"]) for _ in range(3)]
        p1, p2 = _patch_run_async(results)
        with p1, p2:
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert len(calls) == 3
        assert calls[0] == ("test-model", 0, 3)
        assert calls[1] == ("test-model", 1, 3)
        assert calls[2] == ("test-model", 2, 3)

    def test_async_callback_called_per_rep(self):
        calls = []

        async def on_rep_done(label, rep_index, total_reps, result):
            calls.append((label, rep_index, total_reps))

        exp = _make_experiment(n_repetitions=2, on_rep_done=on_rep_done)
        results = [_make_results(["pass"]) for _ in range(2)]
        p1, p2 = _patch_run_async(results)
        with p1, p2:
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert len(calls) == 2
        assert calls[0][1] == 0
        assert calls[1][1] == 1

    def test_callback_not_called_when_cached(self, tmp_path):
        """If all reps are cached, on_rep_done should not fire."""
        calls = []
        exp = _make_experiment(
            n_repetitions=2,
            on_rep_done=lambda *a: calls.append(a),
            save_dir=str(tmp_path),
        )
        # Pre-populate cache
        label_dir = tmp_path / "test-model"
        label_dir.mkdir()
        for i in range(2):
            _make_results(["pass"]).save(str(label_dir / f"run_{i}.json"))
        # Write config fingerprint so cache is accepted
        import hashlib
        merged = exp._merge_common(exp.models[0])
        fp = exp._config_fingerprint(merged, SCENARIOS, None, "English")
        with open(label_dir / "config.json", "w") as f:
            json.dump(fp, f)

        p1, p2 = _patch_run_async([])
        with p1, p2:
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert len(calls) == 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

class TestCancellation:
    def test_cancel_event_stops_after_current_rep(self):
        cancel_event = asyncio.Event()
        results = [_make_results(["pass"]) for _ in range(5)]
        call_count = 0

        async def fake_run_async(self_a, scenarios, **kwargs):
            nonlocal call_count
            call_count += 1
            # Cancel after the second rep completes
            if call_count == 2:
                cancel_event.set()
            return next(iter(results))

        exp = _make_experiment(n_repetitions=5, cancel_event=cancel_event)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            result = asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert result.cancelled is True
        # Only 2 reps should have executed
        assert call_count == 2

    def test_cancel_event_not_set_runs_all(self):
        cancel_event = asyncio.Event()  # never set
        results = [_make_results(["pass"]) for _ in range(3)]
        exp = _make_experiment(n_repetitions=3, cancel_event=cancel_event)
        p1, p2 = _patch_run_async(results)
        with p1, p2:
            result = asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert result.cancelled is False
        assert len(result.runs("test-model")) == 3

    def test_cancelled_flag_in_results(self):
        cancel_event = asyncio.Event()
        cancel_event.set()  # pre-set: cancel immediately

        async def fake_run_async(self_a, scenarios, **kwargs):
            raise AssertionError("Should not be called")

        exp = _make_experiment(n_repetitions=3, cancel_event=cancel_event)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            result = asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert result.cancelled is True


# ---------------------------------------------------------------------------
# Auto-retry on ERROR
# ---------------------------------------------------------------------------

class TestAutoRetry:
    def test_retry_on_error_then_success(self):
        """First attempt returns ERROR, second returns PASS."""
        attempt = 0

        async def fake_run_async(self_a, scenarios, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                return _make_results(["ERROR"])
            return _make_results(["pass"])

        exp = _make_experiment(n_repetitions=1, max_retries_per_rep=2)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            result = asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert attempt == 2  # one failure + one success
        runs = result.runs("test-model")
        assert runs[0][0].severity == "pass"

    def test_retry_exhausted_keeps_last_error(self):
        """All attempts return ERROR; final result is ERROR."""
        attempt = 0

        async def fake_run_async(self_a, scenarios, **kwargs):
            nonlocal attempt
            attempt += 1
            return _make_results(["ERROR"])

        exp = _make_experiment(n_repetitions=1, max_retries_per_rep=2)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            result = asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert attempt == 3  # 1 + 2 retries
        runs = result.runs("test-model")
        assert runs[0][0].severity == "ERROR"

    def test_no_retry_when_max_is_zero(self):
        """max_retries_per_rep=0 (default): no retry."""
        attempt = 0

        async def fake_run_async(self_a, scenarios, **kwargs):
            nonlocal attempt
            attempt += 1
            return _make_results(["ERROR"])

        exp = _make_experiment(n_repetitions=1, max_retries_per_rep=0)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            result = asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert attempt == 1


# ---------------------------------------------------------------------------
# External idempotency (rep_is_done)
# ---------------------------------------------------------------------------

class TestRepIsDone:
    def test_skip_reps_marked_done(self):
        """reps 0-1 are 'done' externally; only rep 2 executes."""
        executed = []

        async def fake_run_async(self_a, scenarios, **kwargs):
            executed.append(1)
            return _make_results(["pass"])

        def rep_is_done(label, rep_index):
            return rep_index < 2  # reps 0 and 1 are done

        exp = _make_experiment(n_repetitions=3, rep_is_done=rep_is_done)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert len(executed) == 1  # only rep 2

    def test_async_rep_is_done(self):
        executed = []

        async def fake_run_async(self_a, scenarios, **kwargs):
            executed.append(1)
            return _make_results(["pass"])

        async def rep_is_done(label, rep_index):
            return rep_index == 0

        exp = _make_experiment(n_repetitions=2, rep_is_done=rep_is_done)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        assert len(executed) == 1  # only rep 1


# ---------------------------------------------------------------------------
# run_scenario_reps
# ---------------------------------------------------------------------------

class TestRunScenarioReps:
    def test_returns_n_results(self):
        results = [_make_results(["pass"]) for _ in range(3)]
        exp = _make_experiment(n_repetitions=3)
        p1, p2 = _patch_run_async(results)
        with p1, p2:
            rep_results = asyncio.run(
                exp.run_scenario_reps(model_index=0, scenario=SINGLE_SCENARIO)
            )
        assert len(rep_results) == 3

    def test_respects_cancel_event(self):
        cancel_event = asyncio.Event()
        call_count = 0

        async def fake_run_async(self_a, scenarios, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                cancel_event.set()
            return _make_results(["pass"])

        exp = _make_experiment(n_repetitions=5, cancel_event=cancel_event)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            rep_results = asyncio.run(
                exp.run_scenario_reps(model_index=0, scenario=SINGLE_SCENARIO)
            )
        # First rep runs, then cancel is set; second rep check sees it
        assert call_count <= 2

    def test_calls_on_rep_done(self):
        calls = []
        results = [_make_results(["pass"]) for _ in range(2)]

        def on_rep_done(label, rep_index, total_reps, result):
            calls.append(rep_index)

        exp = _make_experiment(n_repetitions=2, on_rep_done=on_rep_done)
        p1, p2 = _patch_run_async(results)
        with p1, p2:
            asyncio.run(exp.run_scenario_reps(model_index=0, scenario=SINGLE_SCENARIO))

        assert calls == [0, 1]


# ---------------------------------------------------------------------------
# run_streamed
# ---------------------------------------------------------------------------

class TestRunStreamed:
    def test_yields_rep_started_and_rep_done(self):
        results = [_make_results(["pass"]) for _ in range(2)]
        exp = _make_experiment(n_repetitions=2)
        p1, p2 = _patch_run_async(results)

        async def collect():
            events = []
            async for event in exp.run_streamed(scenarios=SCENARIOS):
                events.append(event)
            return events

        with p1, p2:
            events = asyncio.run(collect())

        types = [e.type for e in events]
        assert "rep_started" in types
        assert "rep_done" in types
        assert "model_done" in types
        # 2 reps → 2 rep_started + 2 rep_done + 1 model_done
        assert types.count("rep_started") == 2
        assert types.count("rep_done") == 2
        assert types.count("model_done") == 1

    def test_rep_done_has_result(self):
        results = [_make_results(["high"]) for _ in range(1)]
        exp = _make_experiment(n_repetitions=1)
        p1, p2 = _patch_run_async(results)

        async def collect():
            events = []
            async for event in exp.run_streamed(scenarios=SCENARIOS):
                events.append(event)
            return events

        with p1, p2:
            events = asyncio.run(collect())

        done_events = [e for e in events if e.type == "rep_done"]
        assert len(done_events) == 1
        assert done_events[0].result is not None
        # result is full AuditResults; first item has the severity
        assert done_events[0].result[0].severity == "high"

    def test_model_done_has_partial(self):
        results = [_make_results(["pass"]) for _ in range(1)]
        exp = _make_experiment(n_repetitions=1)
        p1, p2 = _patch_run_async(results)

        async def collect():
            events = []
            async for event in exp.run_streamed(scenarios=SCENARIOS):
                events.append(event)
            return events

        with p1, p2:
            events = asyncio.run(collect())

        model_done = [e for e in events if e.type == "model_done"]
        assert len(model_done) == 1
        assert model_done[0].partial is not None
        assert isinstance(model_done[0].partial, RepeatedExperimentResults)

    def test_cancelled_event_yielded(self):
        cancel_event = asyncio.Event()
        cancel_event.set()  # pre-set

        async def fake_run_async(self_a, scenarios, **kwargs):
            raise AssertionError("Should not be called")

        exp = _make_experiment(n_repetitions=3, cancel_event=cancel_event)
        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):

            async def collect():
                events = []
                async for event in exp.run_streamed(scenarios=SCENARIOS):
                    events.append(event)
                return events

            events = asyncio.run(collect())

        assert any(e.type == "cancelled" for e in events)


# ---------------------------------------------------------------------------
# Per-rep cache invalidation
# ---------------------------------------------------------------------------

class TestPerRepCache:
    def test_error_rep_rerun_clean_reps_cached(self, tmp_path):
        """Rep 0 is clean, rep 1 has ERROR → only rep 1 is re-run."""
        save_dir = tmp_path / "cache"
        label_dir = save_dir / "test-model"
        label_dir.mkdir(parents=True)

        # Write clean rep 0
        _make_results(["pass"]).save(str(label_dir / "run_0.json"))
        # Write ERROR rep 1
        _make_results(["ERROR"]).save(str(label_dir / "run_1.json"))
        # Write config
        exp = _make_experiment(n_repetitions=2, save_dir=str(save_dir))
        merged = exp._merge_common(exp.models[0])
        fp = exp._config_fingerprint(merged, SCENARIOS, None, "English")
        with open(label_dir / "config.json", "w") as f:
            json.dump(fp, f)

        executed = []

        async def fake_run_async(self_a, scenarios, **kwargs):
            executed.append(1)
            return _make_results(["pass"])

        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        # Only rep 1 should have been re-run
        assert len(executed) == 1

    def test_error_threshold_invalidates_all(self, tmp_path):
        """4 of 5 reps have ERROR, max_error_reps=3 → full invalidation."""
        save_dir = tmp_path / "cache"
        label_dir = save_dir / "test-model"
        label_dir.mkdir(parents=True)

        # 1 clean, 4 ERROR
        _make_results(["pass"]).save(str(label_dir / "run_0.json"))
        for i in range(1, 5):
            _make_results(["ERROR"]).save(str(label_dir / f"run_{i}.json"))

        exp = _make_experiment(n_repetitions=5, save_dir=str(save_dir), max_error_reps=3)
        merged = exp._merge_common(exp.models[0])
        fp = exp._config_fingerprint(merged, SCENARIOS, None, "English")
        with open(label_dir / "config.json", "w") as f:
            json.dump(fp, f)

        executed = []

        async def fake_run_async(self_a, scenarios, **kwargs):
            executed.append(1)
            return _make_results(["pass"])

        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        # All 5 reps re-run (full invalidation)
        assert len(executed) == 5

    def test_below_threshold_preserves_clean_reps(self, tmp_path):
        """2 of 5 reps have ERROR, max_error_reps=3 → only those 2 re-run."""
        save_dir = tmp_path / "cache"
        label_dir = save_dir / "test-model"
        label_dir.mkdir(parents=True)

        for i in range(5):
            if i in (2, 3):
                _make_results(["ERROR"]).save(str(label_dir / f"run_{i}.json"))
            else:
                _make_results(["pass"]).save(str(label_dir / f"run_{i}.json"))

        exp = _make_experiment(n_repetitions=5, save_dir=str(save_dir), max_error_reps=3)
        merged = exp._merge_common(exp.models[0])
        fp = exp._config_fingerprint(merged, SCENARIOS, None, "English")
        with open(label_dir / "config.json", "w") as f:
            json.dump(fp, f)

        executed = []

        async def fake_run_async(self_a, scenarios, **kwargs):
            executed.append(1)
            return _make_results(["pass"])

        with patch.object(ModelAuditor, "run_async", new=fake_run_async), \
             patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            asyncio.run(exp.run_async(scenarios=SCENARIOS))

        # Only reps 2 and 3 re-run
        assert len(executed) == 2


# ---------------------------------------------------------------------------
# ExperimentEvent dataclass
# ---------------------------------------------------------------------------

class TestExperimentEvent:
    def test_defaults(self):
        e = ExperimentEvent(type="rep_done", model="m1")
        assert e.rep_index == -1
        assert e.total_reps == 0
        assert e.scenario_name is None
        assert e.result is None
        assert e.partial is None

    def test_full_construction(self):
        result = AuditResult(
            scenario_name="s", scenario_description="d", conversation=[],
            severity="pass", issues_found=[], positive_behaviors=[],
            summary="", recommendations=[],
        )
        e = ExperimentEvent(
            type="rep_done", model="m1", rep_index=2, total_reps=5,
            scenario_name="s", result=result,
        )
        assert e.rep_index == 2
        assert e.total_reps == 5
        assert e.result.severity == "pass"
