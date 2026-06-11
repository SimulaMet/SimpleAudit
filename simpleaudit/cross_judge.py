"""
Cross-judge stability analysis for SimpleAudit.

Provides CrossJudgeExperiment, which orchestrates identical AuditExperiment
runs under multiple judge models to measure how judge version affects severity
ratings and score distributions.

Motivated by empirical findings that judge model version can materially shift
modal severity on identical subject responses — including safety-relevant
scenarios — without any change in the subject model itself.
"""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from simpleaudit.experiment import AuditExperiment
from simpleaudit.repeated_results import RepeatedExperimentResults


# Canonical ordering from least to most severe (used for computing shift direction).
_SEVERITY_ORDER = ["pass", "low", "medium", "high", "critical"]


# ---------------------------------------------------------------------------
# CrossJudgeResults
# ---------------------------------------------------------------------------

class CrossJudgeResults:
    """Results from CrossJudgeExperiment — one RepeatedExperimentResults per judge.

    Provides cross-judge severity shift detection, score deltas, and CV deltas
    per subject model. Results from each judge are kept independent; comparison
    is computed on demand.

    Attributes
    ----------
    judges : list of str
        Ordered list of judge labels present in this result set.
    """

    def __init__(self, results_by_judge: Dict[str, RepeatedExperimentResults]) -> None:
        if not results_by_judge:
            raise ValueError("results_by_judge must contain at least one entry.")
        self._results: Dict[str, RepeatedExperimentResults] = results_by_judge

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def judges(self) -> List[str]:
        """Ordered list of judge labels."""
        return list(self._results.keys())

    def __getitem__(self, judge_label: str) -> RepeatedExperimentResults:
        return self._results[judge_label]

    def __contains__(self, judge_label: object) -> bool:
        return judge_label in self._results

    def score_summary(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Per-subject, per-judge score statistics.

        Returns
        -------
        dict
            ``{subject_label: {judge_label: {mean, std, cv, min, max, n_runs}}}``
        """
        summary: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for judge_label, rep_results in self._results.items():
            for subject_label in rep_results._runs:
                report = rep_results.stability(subject_label)
                summary.setdefault(subject_label, {})[judge_label] = {
                    "n_runs": report.n_runs,
                    "mean": round(report.mean_score, 2),
                    "std": round(report.std_score, 2),
                    "cv": round(report.cv, 2),
                    "min": round(report.min_score, 2),
                    "max": round(report.max_score, 2),
                }
        return summary

    def severity_shifts(self, subject_label: str) -> List[Dict[str, Any]]:
        """Per-scenario modal severity across all judges, with shift detection.

        Parameters
        ----------
        subject_label : str
            The subject model label to compare across judges.

        Returns
        -------
        list of dict
            One entry per scenario containing:
            - ``scenario``: scenario name
            - ``modals``: ``{judge_label: modal_severity}``
            - ``shifted``: True if any two judges disagree on modal severity
            - ``direction``: (two-judge case only) ``idx_b - idx_a`` in
              ``_SEVERITY_ORDER``; positive means judge_b is stricter.
        """
        judges = self.judges
        per_judge: Dict[str, Dict[str, str]] = {}
        scenario_names: Optional[List[str]] = None

        for judge_label in judges:
            rep_results = self._results[judge_label]
            report = rep_results.stability(subject_label)
            per_judge[judge_label] = {
                name: stats.most_common_severity
                for name, stats in report.per_scenario.items()
            }
            if scenario_names is None:
                scenario_names = list(report.per_scenario.keys())

        if scenario_names is None:
            return []

        result = []
        for name in scenario_names:
            modals = {j: per_judge[j].get(name, "pass") for j in judges}
            shifted = len(set(modals.values())) > 1
            entry: Dict[str, Any] = {"scenario": name, "modals": modals, "shifted": shifted}
            if shifted and len(judges) == 2:
                a, b = judges
                idx_a = _SEVERITY_ORDER.index(modals[a]) if modals[a] in _SEVERITY_ORDER else -1
                idx_b = _SEVERITY_ORDER.index(modals[b]) if modals[b] in _SEVERITY_ORDER else -1
                entry["direction"] = idx_b - idx_a
            result.append(entry)
        return result

    def to_dict(self) -> Dict[str, Any]:
        """Serialize all results to a JSON-compatible dict."""
        return {
            "judges": self.judges,
            "score_summary": self.score_summary(),
            "runs": {
                judge_label: rep_results.to_dict()
                for judge_label, rep_results in self._results.items()
            },
        }


# ---------------------------------------------------------------------------
# CrossJudgeExperiment
# ---------------------------------------------------------------------------

class CrossJudgeExperiment:
    """Orchestrate AuditExperiment runs across multiple judge models.

    Runs one AuditExperiment per judge with save_dir namespaced by judge label
    to prevent file collision. Reuses PR #20's resume logic: each judge
    maintains its own run cache under ``save_dir/<judge_label>/``.

    Parameters
    ----------
    models : list of dict
        Subject models to evaluate. Same format as ``AuditExperiment.models``
        — each dict must contain a ``model`` key and optionally ``provider``,
        ``label``, and provider-specific credentials.
    judge_models : list of dict
        Judge models to compare. Each dict must contain a ``model`` key and
        optionally ``provider``, ``label``, ``api_key``, and ``base_url``.
        If ``label`` is omitted it is derived from the model string.
        Must contain at least two entries.
    auditor_models : list of dict or None, optional
        Auditor (probe generator) configuration, one entry per judge in the
        same order as ``judge_models``. Each dict may contain ``model``,
        ``provider``, ``api_key``, and ``base_url``; all four are forwarded to
        the underlying ``AuditExperiment``. If None, each judge serves as its
        own auditor, mirroring ``AuditExperiment`` default behaviour.
    n_repetitions : int, default 3
        Repetitions per (judge × subject) combination. Passed through to each
        ``AuditExperiment``.
    save_dir : str, Path, or None, optional
        Parent directory for saved results. Each judge's runs land under
        ``save_dir/<judge_label>/<subject_label>/run_N.json``. Pass the same
        ``save_dir`` on a resumed call to continue interrupted runs.
    **experiment_kwargs
        Additional keyword arguments forwarded to every ``AuditExperiment``
        (e.g. ``probe_prompt``, ``judge_prompt``, ``json_format``,
        ``show_progress``).

    Examples
    --------
    >>> exp = CrossJudgeExperiment(
    ...     models=[{"model": "claude-haiku-4-5-20251001", "provider": "anthropic",
    ...              "label": "haiku-4.5"}],
    ...     judge_models=[
    ...         {"model": "claude-opus-4-7", "provider": "anthropic"},
    ...         {"model": "claude-opus-4-8", "provider": "anthropic"},
    ...     ],
    ...     n_repetitions=3,
    ...     save_dir="/path/to/results",
    ... )
    >>> results = await exp.run_async(scenarios="nav_aap", language="Norwegian")
    >>> print(results.score_summary())
    """

    def __init__(
        self,
        models: List[Dict[str, Any]],
        judge_models: List[Dict[str, Any]],
        auditor_models: Optional[List[Dict[str, Any]]] = None,
        n_repetitions: int = 3,
        save_dir: Optional[Union[str, Path]] = None,
        **experiment_kwargs: Any,
    ) -> None:
        if not models or any("model" not in m for m in models):
            raise ValueError("models must be a non-empty list of dicts each containing a 'model' key.")
        if len(judge_models) < 2:
            raise ValueError("judge_models must contain at least two entries.")
        if auditor_models is not None and len(auditor_models) != len(judge_models):
            raise ValueError(
                "auditor_models must be None or have the same length as judge_models."
            )
        if n_repetitions < 1:
            raise ValueError("n_repetitions must be >= 1")

        self.models = models
        self.judge_models = judge_models
        self.auditor_models = auditor_models
        self.n_repetitions = n_repetitions
        self.save_dir = Path(save_dir) if save_dir else None
        self._experiment_kwargs = experiment_kwargs

        # Build one AuditExperiment per judge, namespacing save_dir by label.
        self._experiments: Dict[str, AuditExperiment] = {}
        for i, judge_info in enumerate(judge_models):
            label = self._safe_judge_label(judge_info)
            if label in self._experiments:
                raise ValueError(
                    f"Duplicate judge label {label!r}. Add a 'label' key to distinguish "
                    "judge models that share the same derived label."
                )
            judge_save_dir: Optional[str] = str(self.save_dir / label) if self.save_dir else None
            auditor_info: Optional[Dict[str, Any]] = (
                auditor_models[i] if auditor_models is not None else None
            )
            self._experiments[label] = AuditExperiment(
                models=models,
                judge_model=judge_info["model"],
                judge_provider=judge_info.get("provider"),
                judge_api_key=judge_info.get("api_key"),
                judge_base_url=judge_info.get("base_url"),
                auditor_model=auditor_info["model"] if auditor_info else None,
                auditor_provider=auditor_info.get("provider") if auditor_info else None,
                auditor_api_key=auditor_info.get("api_key") if auditor_info else None,
                auditor_base_url=auditor_info.get("base_url") if auditor_info else None,
                n_repetitions=n_repetitions,
                save_dir=judge_save_dir,
                **experiment_kwargs,
            )

    @property
    def judge_labels(self) -> List[str]:
        """Labels of all configured judges, in order."""
        return list(self._experiments.keys())

    @staticmethod
    def _safe_judge_label(judge_info: Dict[str, Any]) -> str:
        """Derive a filesystem-safe label from a judge info dict.

        Uses the ``label`` key if present. Otherwise strips the provider
        prefix (e.g. ``anthropic:``) and the ``claude-`` model-family prefix,
        then sanitises the result for use as a directory name.

        Examples
        --------
        ``{"model": "claude-opus-4-8", "provider": "anthropic"}`` → ``"opus-4-8"``
        ``{"model": "claude-opus-4-7", "label": "opus47"}`` → ``"opus47"``
        ``{"model": "gpt-4o", "provider": "openai"}`` → ``"gpt-4o"``
        """
        if "label" in judge_info:
            return str(judge_info["label"])
        model: str = judge_info["model"]
        if ":" in model:
            model = model.split(":", 1)[1]
        if model.startswith("claude-"):
            model = model[len("claude-"):]
        return model.replace("/", "_").replace(":", "_").replace(" ", "_")

    async def run_async(
        self,
        scenarios: Union[str, List[Dict[str, Any]]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> CrossJudgeResults:
        """Execute all judge variants and return combined results.

        Judges are run sequentially. Each judge's underlying ``AuditExperiment``
        loads any cached runs from disk before issuing new API calls, so
        interrupted runs can be resumed by re-calling this method with the
        same ``save_dir``.

        Parameters
        ----------
        scenarios : str or list of dict
            Scenario pack name (e.g. ``"nav_aap"``) or an inline list of
            scenario dicts. Passed unchanged to each ``AuditExperiment``.
        max_turns : int or None, optional
            Maximum conversation turns per scenario.
        language : str, default "English"
            Probe language forwarded to the auditor.
        max_workers : int, default 1
            Concurrency within each ``AuditExperiment`` run.

        Returns
        -------
        CrossJudgeResults
        """
        results_by_judge: Dict[str, RepeatedExperimentResults] = {}
        for judge_label, exp in self._experiments.items():
            results_by_judge[judge_label] = await exp.run_async(
                scenarios=scenarios,
                max_turns=max_turns,
                language=language,
                max_workers=max_workers,
            )
        return CrossJudgeResults(results_by_judge)

    def run(
        self,
        scenarios: Union[str, List[Dict[str, Any]]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> CrossJudgeResults:
        """Synchronous wrapper around run_async.

        Cannot be called from an active event loop; use
        ``await run_async()`` from async contexts.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.run_async(
                    scenarios=scenarios,
                    max_turns=max_turns,
                    language=language,
                    max_workers=max_workers,
                )
            )
        msg = (
            "CrossJudgeExperiment.run() cannot be called from an active event loop. "
            "Use await run_async() instead."
        )
        raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# compare_judges utility
# ---------------------------------------------------------------------------

def compare_judges(
    results_a: RepeatedExperimentResults,
    results_b: RepeatedExperimentResults,
    subject_label: str,
    label_a: str = "judge_a",
    label_b: str = "judge_b",
) -> Dict[str, Any]:
    """Compare two RepeatedExperimentResults for a given subject model.

    Computes per-scenario modal severity shifts, score delta, and CV delta.
    Useful for post-hoc comparison of separately completed experiments
    without re-running CrossJudgeExperiment.

    Args:
        results_a: First judge's repeated results.
        results_b: Second judge's repeated results.
        subject_label: Model label to compare. Must exist in both results.
        label_a: Display name for the first judge (default ``"judge_a"``).
        label_b: Display name for the second judge (default ``"judge_b"``).

    Returns:
        dict with keys:

        - ``subject``: subject_label
        - ``judge_a``, ``judge_b``: the label strings
        - ``score_delta``: mean_b − mean_a (positive = judge_b scores higher)
        - ``cv_delta``: cv_b − cv_a (positive = judge_b is more variable)
        - ``stats_a``, ``stats_b``: ``{mean, std, cv, min, max, n_runs}``
        - ``severity_shifts``: list of dicts for scenarios where modal
          severity differs, each with ``scenario``, ``modal_a``, ``modal_b``,
          and ``direction`` (positive = stricter under judge_b).
        - ``n_shifted``: count of shifted scenarios
        - ``n_total``: scenario count for the reference judge (results_a)
        - ``n_compared``: scenarios present in BOTH judges and actually
          compared (``<= n_total``; the correct denominator for a shift rate)

    Raises:
        KeyError: If subject_label is not found in either results object.
    """
    report_a = results_a.stability(subject_label)
    report_b = results_b.stability(subject_label)

    def _stats(report: Any) -> Dict[str, Any]:
        return {
            "n_runs": report.n_runs,
            "mean": round(report.mean_score, 2),
            "std": round(report.std_score, 2),
            "cv": round(report.cv, 2),
            "min": round(report.min_score, 2),
            "max": round(report.max_score, 2),
        }

    shifts = []
    n_compared = 0
    for name, stats_a in report_a.per_scenario.items():
        if name not in report_b.per_scenario:
            continue
        n_compared += 1
        modal_a = stats_a.most_common_severity
        modal_b = report_b.per_scenario[name].most_common_severity
        if modal_a != modal_b:
            idx_a = _SEVERITY_ORDER.index(modal_a) if modal_a in _SEVERITY_ORDER else -1
            idx_b = _SEVERITY_ORDER.index(modal_b) if modal_b in _SEVERITY_ORDER else -1
            shifts.append({
                "scenario": name,
                "modal_a": modal_a,
                "modal_b": modal_b,
                "direction": idx_b - idx_a,
            })

    return {
        "subject": subject_label,
        "judge_a": label_a,
        "judge_b": label_b,
        "score_delta": round(report_b.mean_score - report_a.mean_score, 2),
        "cv_delta": round(report_b.cv - report_a.cv, 2),
        "stats_a": _stats(report_a),
        "stats_b": _stats(report_b),
        "severity_shifts": shifts,
        "n_shifted": len(shifts),
        "n_total": len(report_a.per_scenario),
        "n_compared": n_compared,
    }
