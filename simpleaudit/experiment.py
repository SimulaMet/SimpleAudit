from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union
import asyncio

from tqdm.auto import tqdm
from simpleaudit.results import AuditResults
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.repeated_results import RepeatedExperimentResults


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
        json_format: bool = True,
        verbose: bool = False,
        show_progress: bool = True,
        n_repetitions: int = 1,
        save_dir: Optional[str] = None,
        on_model_done: Optional[Callable[[str, "RepeatedExperimentResults"], None]] = None,
    ):
        if not models or any("model" not in m for m in models):
            raise ValueError("Models must be dicts with a 'model' key.")
        if n_repetitions < 1:
            raise ValueError("n_repetitions must be >= 1")

        labels = [m.get("label") or m["model"] for m in models]
        if len(labels) != len(set(labels)):
            raise ValueError(
                "Duplicate model labels detected. Add a 'label' key to distinguish "
                "models sharing the same 'model' value."
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
        self.json_format = json_format
        self.verbose = verbose
        self.show_progress = show_progress
        self.n_repetitions = n_repetitions
        self.save_dir = Path(save_dir) if save_dir else None
        self.on_model_done = on_model_done

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
        if merged.get("json_format") is None:
            merged["json_format"] = self.json_format
        if merged.get("verbose") is None:
            merged["verbose"] = self.verbose
        if merged.get("show_progress") is None:
            merged["show_progress"] = self.show_progress
        return merged

    def _run_path(self, label: str, index: int) -> Path:
        # Replace characters that are unsafe in directory names
        safe_label = label.replace("/", "_").replace(":", "_").replace(" ", "_")
        return self.save_dir / safe_label / f"run_{index}.json"

    def _load_cached_runs(self, label: str) -> Dict[int, AuditResults]:
        """Return {index: AuditResults} for every run_N.json that exists on disk."""
        cached: Dict[int, AuditResults] = {}
        for i in range(self.n_repetitions):
            path = self._run_path(label, i)
            if path.exists():
                cached[i] = AuditResults.load(str(path))
        return cached

    async def run_async(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> RepeatedExperimentResults:
        runs_by_model: Dict[str, List[AuditResults]] = {}
        with tqdm(
            total=len(self.models),
            desc="Models",
            position=3,
            leave=True,
            disable=not self.show_progress,
        ) as pbar_models:
            for model_info in self.models:
                label = self._make_label(model_info)

                # Load any runs already saved to disk
                cached = self._load_cached_runs(label) if self.save_dir else {}
                if cached:
                    tqdm.write(f"  Resuming {label}: {len(cached)}/{self.n_repetitions} runs found on disk")

                with tqdm(
                    total=self.n_repetitions,
                    desc=f"{label} — repetitions",
                    position=2,
                    leave=False,
                    disable=(not self.show_progress or self.n_repetitions == 1),
                ) as pbar_reps:
                    # Fast-forward the bar for already-completed runs
                    pbar_reps.update(len(cached))

                    runs_ordered: Dict[int, AuditResults] = dict(cached)
                    for i in range(self.n_repetitions):
                        if i in cached:
                            continue
                        auditor = ModelAuditor(**self._merge_common(model_info))
                        result = await auditor.run_async(
                            scenarios,
                            max_turns=max_turns,
                            language=language,
                            max_workers=max_workers,
                        )
                        if self.save_dir:
                            run_path = self._run_path(label, i)
                            run_path.parent.mkdir(parents=True, exist_ok=True)
                            result.save(str(run_path))
                        runs_ordered[i] = result
                        pbar_reps.update(1)

                runs_by_model[label] = [runs_ordered[i] for i in range(self.n_repetitions)]
                if self.on_model_done:
                    partial = RepeatedExperimentResults({label: runs_by_model[label]})
                    self.on_model_done(label, partial)
                pbar_models.update(1)

        judge_info = {
            k: v for k, v in {
                "judge_model": self.judge_model,
                "judge_base_url": self.judge_base_url,
                "judge_provider": self.judge_provider,
            }.items() if v is not None
        } or None
        experiment_results = RepeatedExperimentResults(runs_by_model, judge=judge_info)

        if self.save_dir:
            self.save_dir.mkdir(parents=True, exist_ok=True)
            experiment_results.save(str(self.save_dir / "experiment_results.json"))

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
