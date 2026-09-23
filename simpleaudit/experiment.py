from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Union
import asyncio
import hashlib
import inspect
import json
from collections import Counter

from tqdm.auto import tqdm
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.repeated_results import RepeatedExperimentResults


@dataclass
class ExperimentEvent:
    """Typed event yielded by :meth:`AuditExperiment.run_streamed`.

    Attributes:
        type: One of ``"rep_started"``, ``"rep_done"``, ``"scenario_done"``,
            ``"model_done"``, ``"cancelled"``.
        model: Model label the event belongs to.
        rep_index: Zero-based rep index (``-1`` for non-rep events).
        total_reps: Total reps planned for this model.
        scenario_name: Set for scenario-level events.
        result: The :class:`AuditResult` for a single rep (set on ``rep_done``).
        partial: Partial :class:`RepeatedExperimentResults` (set on ``model_done``).
    """

    type: str
    model: str
    rep_index: int = -1
    total_reps: int = 0
    scenario_name: Optional[str] = None
    result: Optional[AuditResult] = None
    partial: Optional["RepeatedExperimentResults"] = None


class AuditExperiment:
    def __init__(
        self,
        models: List[Dict[str, Any]],
        judge_model: Optional[str] = None,
        judge_base_url: Optional[str] = None,
        judge_api_key: Optional[str] = None,
        judge_provider: Optional[str] = None,
        auditor_model: Optional[str] = None,
        auditor_provider: Optional[str] = None,
        auditor_api_key: Optional[str] = None,
        auditor_base_url: Optional[str] = None,
        judge: Optional[str] = None,
        probe_prompt: Optional[str] = None,
        judge_prompt: Optional[str] = None,
        judge_response_schema: Optional[Dict[str, Any]] = None,
        json_format: bool = True,
        verbose: bool = False,
        show_progress: bool = True,
        n_repetitions: int = 1,
        adaptive_reruns: Optional[Dict[str, Any]] = None,
        save_dir: Optional[str] = None,
        on_model_done: Optional[Callable[[str, "RepeatedExperimentResults"], None]] = None,
        on_rep_done: Optional[Callable[..., Any]] = None,
        cancel_event: Optional[asyncio.Event] = None,
        max_retries_per_rep: int = 0,
        rep_is_done: Optional[Callable[..., Any]] = None,
        max_error_reps: int = 3,
    ):
        if not models or any("model" not in m for m in models):
            raise ValueError("Models must be dicts with a 'model' key.")
        if n_repetitions < 1:
            raise ValueError("n_repetitions must be >= 1")
        if adaptive_reruns is not None:
            if not isinstance(adaptive_reruns, dict):
                raise ValueError("adaptive_reruns must be a dict")
            if "agreement_target" not in adaptive_reruns:
                raise ValueError("adaptive_reruns requires 'agreement_target'")
            target = adaptive_reruns["agreement_target"]
            if not 0 < target <= 1:
                raise ValueError("adaptive_reruns['agreement_target'] must be in (0, 1]")
            max_extra = adaptive_reruns.get("max_extra", 5)
            if max_extra < 0:
                raise ValueError("adaptive_reruns['max_extra'] must be >= 0")

        labels = [m.get("label") or m["model"] for m in models]
        if len(labels) != len(set(labels)):
            raise ValueError(
                "Duplicate model labels detected. Add a 'label' key to distinguish "
                "models sharing the same 'model' value."
            )
        # The run cache on disk is keyed by the SANITIZED label, so labels
        # that only differ in '/', ':', or ' ' (e.g. 'org/model' vs
        # 'org:model') would share a cache directory and silently swap each
        # other's results on resume. Reject them up front.
        safe_labels = [self._sanitize_label(label) for label in labels]
        if len(safe_labels) != len(set(safe_labels)):
            raise ValueError(
                "Model labels collide after filesystem sanitization ('/', ':' "
                "and ' ' all map to '_'). Add distinct 'label' keys so each "
                "model gets its own run cache directory."
            )

        self.models = models
        self.judge_model = judge_model
        self.judge_base_url = judge_base_url
        self.judge_api_key = judge_api_key
        self.judge_provider = judge_provider
        self.auditor_model = auditor_model
        self.auditor_provider = auditor_provider
        self.auditor_api_key = auditor_api_key
        self.auditor_base_url = auditor_base_url
        self.judge = judge
        self.probe_prompt = probe_prompt
        self.judge_prompt = judge_prompt
        self.judge_response_schema = judge_response_schema
        self.json_format = json_format
        self.verbose = verbose
        self.show_progress = show_progress
        self.n_repetitions = n_repetitions
        self.adaptive_reruns = adaptive_reruns
        self.save_dir = Path(save_dir) if save_dir else None
        self.on_model_done = on_model_done
        self.on_rep_done = on_rep_done
        self.cancel_event = cancel_event
        self.max_retries_per_rep = max_retries_per_rep
        self.rep_is_done = rep_is_done
        self.max_error_reps = max_error_reps

    def _make_label(self, model_info: Dict[str, Any]) -> str:
        return model_info.get("label") or model_info["model"]

    def _merge_common(self, model_info: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(model_info)
        merged.pop("label", None)  # label is experiment-level only; ModelAuditor doesn't accept it
        if merged.get("judge_model") is None and self.judge_model is not None:
            merged["judge_model"] = self.judge_model
        if merged.get("judge_base_url") is None and self.judge_base_url is not None:
            merged["judge_base_url"] = self.judge_base_url
        if merged.get("judge_api_key") is None and self.judge_api_key is not None:
            merged["judge_api_key"] = self.judge_api_key
        if merged.get("judge_provider") is None and self.judge_provider is not None:
            merged["judge_provider"] = self.judge_provider
        for key in ("auditor_model", "auditor_provider", "auditor_api_key", "auditor_base_url"):
            if merged.get(key) is None and getattr(self, key) is not None:
                merged[key] = getattr(self, key)
        if merged.get("judge") is None and self.judge is not None:
            merged["judge"] = self.judge
        if merged.get("probe_prompt") is None and self.probe_prompt is not None:
            merged["probe_prompt"] = self.probe_prompt
        if merged.get("judge_prompt") is None and self.judge_prompt is not None:
            merged["judge_prompt"] = self.judge_prompt
        # provider/judge_provider are documented optional: leave explicit
        # values alone, but guarantee the keys exist so ModelAuditor's
        # required parameters are satisfied (None resolves to its default
        # provider there).
        merged.setdefault("provider", None)
        merged.setdefault("judge_provider", None)
        if merged.get("json_format") is None:
            merged["json_format"] = self.json_format
        if merged.get("judge_response_schema") is None and self.judge_response_schema is not None:
            merged["judge_response_schema"] = self.judge_response_schema
        if merged.get("verbose") is None:
            merged["verbose"] = self.verbose
        if merged.get("show_progress") is None:
            merged["show_progress"] = self.show_progress
        return merged

    @staticmethod
    def _sanitize_label(label: str) -> str:
        """Replace characters that are unsafe in directory names."""
        return label.replace("/", "_").replace(":", "_").replace(" ", "_")

    def _run_path(self, label: str, index: int) -> Path:
        return self.save_dir / self._sanitize_label(label) / f"run_{index}.json"

    def _config_path(self, label: str) -> Path:
        return self.save_dir / self._sanitize_label(label) / "config.json"

    @staticmethod
    def _config_fingerprint(
        merged: Dict[str, Any],
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int],
        language: str,
    ) -> Dict[str, Any]:
        """Describe the configuration a cached run was produced under.

        API keys are excluded (never written to disk). Scenario lists are
        reduced to their names — enough to catch a swapped pack without
        storing full scenario text.
        """
        config = {k: v for k, v in merged.items() if "api_key" not in k}
        if isinstance(scenarios, str):
            scenario_id: Any = scenarios
        else:
            scenario_id = [s.get("name") for s in scenarios]
        source = {
            "config": config,
            "scenarios": scenario_id,
            "max_turns": max_turns,
            "language": language,
        }
        digest = hashlib.sha256(
            json.dumps(source, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return {"fingerprint": digest, "source": source}

    def _check_run_config(self, label: str, fingerprint: Dict[str, Any]) -> bool:
        """Return True when cached runs for this label are safe to reuse.

        A config.json that disagrees with the current configuration means the
        cached runs were produced under different settings (another judge,
        prompt, scenario pack, ...) — mixing them into the aggregates would
        silently corrupt the experiment, so the cache is rejected. A missing
        config.json (caches from older versions) is accepted. Either way the
        current fingerprint is written for the next resume.
        """
        path = self._config_path(label)
        reusable = True
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                if stored.get("fingerprint") != fingerprint["fingerprint"]:
                    tqdm.write(
                        f"  Warning: cached runs for {label!r} were produced under a "
                        "different configuration — ignoring them and re-running "
                        f"(delete {path.parent} to silence this)."
                    )
                    reusable = False
            except (ValueError, KeyError, TypeError):
                pass  # unreadable marker: treat like a legacy cache
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(fingerprint, f, indent=2, ensure_ascii=False)
            tmp_path.replace(path)
        finally:
            tmp_path.unlink(missing_ok=True)
        return reusable

    def _load_cached_runs(self, label: str) -> Dict[int, AuditResults]:
        """Return {index: AuditResults} for every reusable run_N.json on disk.

        Per-rep invalidation: only the specific rep slots containing an ERROR
        result are re-run; clean reps are preserved. If more than
        ``max_error_reps`` reps contain errors, the entire model's cache is
        invalidated (the configuration is likely fundamentally broken).
        """
        cached: Dict[int, AuditResults] = {}
        error_count = 0
        for i in range(self.n_repetitions):
            path = self._run_path(label, i)
            if path.exists():
                try:
                    results = AuditResults.load(str(path))
                except (ValueError, KeyError, TypeError) as exc:
                    tqdm.write(
                        f"  Warning: ignoring unreadable cached run {path} "
                        f"({type(exc).__name__}: {exc}) — this run will be re-executed"
                    )
                    continue
                if any(r.severity == "ERROR" for r in results):
                    error_count += 1
                    continue
                cached[i] = results
        # If too many reps are broken, invalidate everything.
        if error_count > self.max_error_reps:
            tqdm.write(
                f"  Warning: {error_count}/{self.n_repetitions} cached runs for "
                f"{label!r} contain errors (threshold {self.max_error_reps}) — "
                "invalidating full cache."
            )
            return {}
        return cached

    # ------------------------------------------------------------------
    # Internal helpers for the fine-grained execution path
    # ------------------------------------------------------------------

    async def _call_rep_is_done(self, label: str, rep_index: int) -> bool:
        """Check the external idempotency hook. Returns True if the slot is done."""
        if self.rep_is_done is None:
            return False
        result = self.rep_is_done(label, rep_index)
        if inspect.iscoroutine(result):
            result = await result
        return bool(result)

    async def _call_on_rep_done(self, label: str, rep_index: int, result: AuditResult) -> None:
        """Invoke the on_rep_done callback (sync or async)."""
        if self.on_rep_done is None:
            return
        cb = self.on_rep_done(label, rep_index, self.n_repetitions, result)
        if inspect.iscoroutine(cb):
            await cb

    async def _run_single_rep(
        self,
        merged: Dict[str, Any],
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int],
        language: str,
        max_workers: int,
    ) -> AuditResults:
        """Execute one rep with auto-retry on ERROR. Returns the final result."""
        attempts = 1 + self.max_retries_per_rep
        result: Optional[AuditResults] = None
        for attempt in range(attempts):
            auditor = ModelAuditor(**merged)
            result = await auditor.run_async(
                scenarios,
                max_turns=max_turns,
                language=language,
                max_workers=max_workers,
            )
            if not any(r.severity == "ERROR" for r in result):
                break
            if attempt < attempts - 1:
                tqdm.write(
                    f"  Rep returned ERROR (attempt {attempt + 1}/{attempts}) — retrying"
                )
        return result  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Public: single-scenario, multi-rep execution
    # ------------------------------------------------------------------

    async def run_scenario_reps(
        self,
        model_index: int,
        scenario: Dict[str, Any],
        max_turns: Optional[int] = None,
        language: str = "English",
    ) -> List[AuditResult]:
        """Run a single scenario N times for one model.

        Builds a fresh :class:`ModelAuditor` per rep (independent
        conversations). Respects ``n_repetitions``, ``cancel_event``,
        ``on_rep_done``, ``max_retries_per_rep``, ``rep_is_done``, and the
        disk cache (per-rep files under ``save_dir``).

        Args:
            model_index: Index into ``self.models``.
            scenario: A single scenario dict.
            max_turns: Override for max conversation turns.
            language: Language for probe generation.

        Returns:
            List of :class:`AuditResult`, one per completed rep. May be
            shorter than ``n_repetitions`` if cancellation occurred.
        """
        model_info = self.models[model_index]
        label = self._make_label(model_info)
        merged = self._merge_common(model_info)
        results: List[AuditResult] = []

        for i in range(self.n_repetitions):
            # Cancellation check
            if self.cancel_event is not None and self.cancel_event.is_set():
                break

            # External idempotency: skip if caller says this slot is done
            if await self._call_rep_is_done(label, i):
                continue

            # Disk cache: skip if a clean result is already on disk
            if self.save_dir:
                run_path = self._run_path(label, i)
                if run_path.exists():
                    try:
                        cached = AuditResults.load(str(run_path))
                        if not any(r.severity == "ERROR" for r in cached):
                            results.extend(cached)
                            continue
                    except (ValueError, KeyError, TypeError):
                        pass  # fall through and re-run

            # Execute with auto-retry
            rep_result = await self._run_single_rep(
                merged, [scenario], max_turns, language, max_workers=1
            )

            # Persist
            if self.save_dir:
                run_path = self._run_path(label, i)
                run_path.parent.mkdir(parents=True, exist_ok=True)
                rep_result.save(str(run_path))

            results.extend(rep_result)

            # Callback
            await self._call_on_rep_done(label, i, rep_result[0] if rep_result else None)

        return results

    # ------------------------------------------------------------------
    # Public: streaming async generator
    # ------------------------------------------------------------------

    async def run_streamed(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> AsyncIterator[ExperimentEvent]:
        """Execute the experiment, yielding typed events as progress is made.

        This is the core execution loop. :meth:`run_async` is a thin wrapper
        that consumes this generator and returns the final result.

        Yields:
            ExperimentEvent with type in {"rep_started", "rep_done",
            "model_done", "cancelled"}.
        """
        judge_info = {
            k: v for k, v in {
                "judge_model": self.judge_model,
                "judge_base_url": self.judge_base_url,
                "judge_provider": self.judge_provider,
            }.items() if v is not None
        } or None

        runs_by_model: Dict[str, List[AuditResults]] = {}
        cancelled = False

        with tqdm(
            total=len(self.models),
            desc="Models",
            position=3,
            leave=True,
            disable=not self.show_progress,
        ) as pbar_models:
            for model_info in self.models:
                label = self._make_label(model_info)
                merged = self._merge_common(model_info)

                # Load cached runs
                cached: Dict[int, AuditResults] = {}
                if self.save_dir:
                    fingerprint = self._config_fingerprint(
                        merged, scenarios, max_turns, language
                    )
                    if self._check_run_config(label, fingerprint):
                        cached = self._load_cached_runs(label)
                if cached:
                    tqdm.write(f"  Resuming {label}: {len(cached)}/{self.n_repetitions} runs found on disk")

                with tqdm(
                    total=self.n_repetitions,
                    desc=f"{label} — repetitions",
                    position=2,
                    leave=False,
                    disable=(not self.show_progress or self.n_repetitions == 1),
                ) as pbar_reps:
                    pbar_reps.update(len(cached))

                    runs_ordered: Dict[int, AuditResults] = dict(cached)
                    for i in range(self.n_repetitions):
                        # Cancellation
                        if self.cancel_event is not None and self.cancel_event.is_set():
                            yield ExperimentEvent(
                                type="cancelled", model=label,
                                rep_index=i, total_reps=self.n_repetitions,
                            )
                            cancelled = True
                            break

                        # External idempotency
                        if await self._call_rep_is_done(label, i):
                            pbar_reps.update(1)
                            continue

                        if i in cached:
                            continue

                        # Yield rep_started
                        yield ExperimentEvent(
                            type="rep_started", model=label,
                            rep_index=i, total_reps=self.n_repetitions,
                        )

                        # Execute with auto-retry
                        result = await self._run_single_rep(
                            merged, scenarios, max_turns, language, max_workers
                        )

                        # Persist
                        if self.save_dir:
                            run_path = self._run_path(label, i)
                            run_path.parent.mkdir(parents=True, exist_ok=True)
                            result.save(str(run_path))

                        runs_ordered[i] = result
                        pbar_reps.update(1)

                        # Callback + yield rep_done
                        await self._call_on_rep_done(label, i, result[0] if result else None)
                        yield ExperimentEvent(
                            type="rep_done", model=label,
                            rep_index=i, total_reps=self.n_repetitions,
                            result=result[0] if result else None,
                        )

                runs_list = [runs_ordered[i] for i in range(self.n_repetitions) if i in runs_ordered]

                # Adaptive reruns
                if self.adaptive_reruns and not cancelled:
                    target = self.adaptive_reruns["agreement_target"]
                    max_extra = self.adaptive_reruns.get("max_extra", 5)
                    for extra in range(max_extra):
                        scenario_severities: Dict[str, List[str]] = {}
                        for run in runs_list:
                            for r in run:
                                scenario_severities.setdefault(r.scenario_name, []).append(r.severity)
                        below_target = [
                            name for name, sevs in scenario_severities.items()
                            if Counter(sevs).most_common(1)[0][1] / len(sevs) < target
                        ]
                        if not below_target:
                            break
                        tqdm.write(
                            f"  Adaptive rerun {extra + 1}/{max_extra} for {label}: "
                            f"{len(below_target)} scenario(s) below agreement target {target}"
                        )
                        fragile_scenarios = [
                            s for s in (scenarios if isinstance(scenarios, list) else [])
                            if s.get("name") in below_target
                        ]
                        if not fragile_scenarios:
                            break
                        auditor = ModelAuditor(**merged)
                        result = await auditor.run_async(
                            fragile_scenarios,
                            max_turns=max_turns,
                            language=language,
                            max_workers=max_workers,
                        )
                        new_index = len(runs_list)
                        if self.save_dir:
                            run_path = self._run_path(label, new_index)
                            run_path.parent.mkdir(parents=True, exist_ok=True)
                            result.save(str(run_path))
                        runs_list.append(result)
                        runs_ordered[new_index] = result

                runs_by_model[label] = runs_list

                # model_done event
                partial = RepeatedExperimentResults(
                    {label: runs_by_model[label]}, judge=judge_info, cancelled=cancelled
                )
                if self.on_model_done:
                    self.on_model_done(label, partial)
                yield ExperimentEvent(
                    type="model_done", model=label,
                    total_reps=self.n_repetitions, partial=partial,
                )
                pbar_models.update(1)

        # Final save
        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            final = RepeatedExperimentResults(runs_by_model, judge=judge_info, cancelled=cancelled)
            final.save(str(self.save_dir / "experiment_results.json"))

    # ------------------------------------------------------------------
    # Public: batch execution (backward-compatible)
    # ------------------------------------------------------------------

    async def run_async(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> RepeatedExperimentResults:
        """Run the full experiment. Thin wrapper over :meth:`run_streamed`."""
        judge_info = {
            k: v for k, v in {
                "judge_model": self.judge_model,
                "judge_base_url": self.judge_base_url,
                "judge_provider": self.judge_provider,
            }.items() if v is not None
        } or None

        runs_by_model: Dict[str, List[AuditResults]] = {}
        cancelled = False

        async for event in self.run_streamed(
            scenarios, max_turns=max_turns, language=language, max_workers=max_workers
        ):
            if event.type == "model_done" and event.partial is not None:
                for label, runs in event.partial.all_runs().items():
                    runs_by_model[label] = runs
            if event.type == "cancelled":
                cancelled = True

        experiment_results = RepeatedExperimentResults(
            runs_by_model, judge=judge_info, cancelled=cancelled
        )
        return experiment_results

    def run(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> RepeatedExperimentResults:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run_async(
                    scenarios,
                    max_turns=max_turns,
                    language=language,
                    max_workers=max_workers,
                )
            )
        msg = "AuditExperiment.run() cannot be called from an active event loop. Use await <object>.run_async()."
        raise RuntimeError(msg)
