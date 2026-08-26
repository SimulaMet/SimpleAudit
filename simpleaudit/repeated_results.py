"""
Repeated-run results for SimpleAudit.

Holds results from running an AuditExperiment multiple times and provides
stability statistics across runs.
"""

import json
import math
import statistics
import warnings
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple, Union

from simpleaudit.results import AuditResult, AuditResults, _atomic_json_dump
from simpleaudit.utils import SEVERITY_ORDER


# ---------------------------------------------------------------------------
# Per-scenario stability stats (one model, N runs)
# ---------------------------------------------------------------------------

@dataclass
class ScenarioStats:
    pass_rate: float                        # fraction of runs where severity == "pass"
    severity_distribution: Dict[str, int]  # raw counts across all N runs
    most_common_severity: str
    agreement_rate: float                  # fraction of runs matching the mode
    entropy: float                         # normalised Shannon entropy over severity distribution (0.0 = perfectly stable)
    ordinal_spread: float                  # std of severity positions on the 0–4 ordinal scale (0.0 = perfectly stable)

    def to_dict(self) -> Dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Per-model stability report
# ---------------------------------------------------------------------------

@dataclass
class ModelStabilityReport:
    model: str
    n_runs: int
    scores: List[float]
    mean_score: float
    std_score: float                       # 0.0 when n_runs == 1
    min_score: float
    max_score: float
    cv: float                              # (std / mean) * 100  — coefficient of variation in %
    per_scenario: Dict[str, ScenarioStats]

    def summary(self) -> None:
        print()
        print("=" * 60)
        print(f"STABILITY REPORT: {self.model} ({self.n_runs} run{'s' if self.n_runs != 1 else ''})")
        print("=" * 60)
        print(f"Mean Score :  {self.mean_score:.1f} / 100")
        if self.n_runs > 1:
            print(f"Std Dev    :  {self.std_score:.1f}  (CV: {self.cv:.1f}%)")
            print(f"Range      :  {self.min_score:.1f} – {self.max_score:.1f}")

        if self.per_scenario:
            print()
            print("Per-Scenario Stability:")
            header = f"  {'Scenario':<35} {'Pass Rate':>9}   {'Agreement':>9}   {'Entropy':>8}   {'Spread':>7}   Mode"
            print(header)
            print("  " + "-" * (len(header) - 2))
            for name, stats in self.per_scenario.items():
                short = name[:34]
                flag = " ⚠" if stats.agreement_rate < 0.6 else ""
                print(
                    f"  {short:<35} {stats.pass_rate * 100:>8.0f}%"
                    f"   {stats.agreement_rate * 100:>8.0f}%"
                    f"   {stats.entropy:>7.2f}"
                    f"   {stats.ordinal_spread:>6.2f}"
                    f"   {stats.most_common_severity}{flag}"
                )
        print()

    def fragile(self, threshold: float = 0.6) -> Dict[str, ScenarioStats]:
        """Return scenarios whose modal-verdict share is below *threshold*.

        A scenario is *fragile* when the judge's severity verdict disagrees
        across ``n_repetitions`` runs — i.e. the modal verdict is held by less
        than *threshold* of the runs.  These are the scenarios most likely to
        flip under perturbation (see arXiv:2608.12645, *Jagged Judges*).

        Example::

            stab = results.stability("my-model")
            fragile = stab.fragile(threshold=0.6)
            for name, stats in fragile.items():
                print(f"{name}: agreement={stats.agreement_rate:.2f}")
        """
        return {
            name: stats
            for name, stats in self.per_scenario.items()
            if stats.agreement_rate < threshold
        }

    def to_dict(self) -> Dict:
        return {
            "model": self.model,
            "n_runs": self.n_runs,
            "scores": self.scores,
            "mean_score": self.mean_score,
            "std_score": self.std_score,
            "min_score": self.min_score,
            "max_score": self.max_score,
            "cv": self.cv,
            "per_scenario": {k: v.to_dict() for k, v in self.per_scenario.items()},
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_model_aggregate(runs: List[AuditResults]) -> Dict:
    """Compute aggregate stats (mean ± std, total) across runs for one model."""
    n = len(runs)

    def stats(values: List[float]) -> Dict:
        mean = statistics.mean(values)
        std = statistics.stdev(values) if n >= 2 else 0.0
        return {"mean": round(mean, 2), "std": round(std, 2), "total": sum(values)}

    scores = [r.score for r in runs]
    score_mean = statistics.mean(scores)
    score_std = statistics.stdev(scores) if n >= 2 else 0.0

    all_severities: set = set()
    for r in runs:
        all_severities.update(r.severity_distribution.keys())

    token_keys = ["auditor_input", "auditor_output", "judge_input", "judge_output", "target_input", "target_output", "total"]

    return {
        "n_runs": n,
        "score": {"mean": round(score_mean, 1), "std": round(score_std, 2)},
        "passed": stats([r.passed for r in runs]),
        "failed": stats([r.failed for r in runs]),
        "severity_distribution": {
            sev: stats([r.severity_distribution.get(sev, 0) for r in runs])
            for sev in sorted(all_severities)
        },
        "token_usage": {
            k: stats([r.token_usage[k] for r in runs])
            for k in token_keys
        },
    }


def _index_by_name(audit_results: AuditResults) -> Dict[str, AuditResult]:
    return {r.scenario_name: r for r in audit_results}


def _build_stability_report(model: str, runs: List[AuditResults]) -> ModelStabilityReport:
    scores = [r.score for r in runs]
    mean = statistics.mean(scores)
    std = statistics.stdev(scores) if len(scores) >= 2 else 0.0
    cv = (std / mean * 100) if mean != 0.0 else 0.0

    # Collect scenario names across ALL runs (first-seen order). Using only
    # run 0 would silently drop stats for any scenario that failed to appear
    # in that particular run.
    per_scenario: Dict[str, ScenarioStats] = {}
    if runs:
        scenario_names = list(dict.fromkeys(
            r.scenario_name for run in runs for r in run
        ))
        duplicated: Dict[str, int] = {}
        for run in runs:
            for n, c in Counter(r.scenario_name for r in run).items():
                if c > 1:
                    duplicated[n] = max(duplicated.get(n, 0), c)
        if duplicated:
            warnings.warn(
                f"Model {model!r}: duplicate scenario names {sorted(duplicated)} — "
                "per-scenario stability statistics are keyed by name, so these "
                "entries are collapsed and their aggregates may be misleading. "
                "Give each scenario a unique 'name'.",
                stacklevel=2,
            )
        for scenario_name in scenario_names:
            severities = []
            for run in runs:
                indexed = _index_by_name(run)
                if scenario_name in indexed:
                    severities.append(indexed[scenario_name].severity)
            if not severities:
                continue
            dist = dict(Counter(severities))
            mode_sev = Counter(severities).most_common(1)[0][0]
            n = len(severities)

            # Normalised Shannon entropy over the severity distribution.
            # 0.0 = all runs agree (perfectly stable); 1.0 = uniform spread.
            counts = [dist[s] for s in dist]
            total = sum(counts)
            log_base = math.log(len(dist)) if len(dist) > 1 else 1.0
            raw_entropy = -sum(
                (c / total) * math.log(c / total) for c in counts if c > 0
            )
            entropy = raw_entropy / log_base if log_base > 0 else 0.0

            # Ordinal spread: std of severity positions on the 0–4 scale.
            positions = [
                SEVERITY_ORDER.index(s) if s in SEVERITY_ORDER else 0
                for s in severities
            ]
            ordinal_spread = statistics.stdev(positions) if n >= 2 else 0.0

            per_scenario[scenario_name] = ScenarioStats(
                pass_rate=severities.count("pass") / n,
                severity_distribution=dist,
                most_common_severity=mode_sev,
                agreement_rate=severities.count(mode_sev) / n,
                entropy=round(entropy, 4),
                ordinal_spread=round(ordinal_spread, 4),
            )

    return ModelStabilityReport(
        model=model,
        n_runs=len(runs),
        scores=scores,
        mean_score=round(mean, 1),
        std_score=round(std, 2),
        min_score=round(min(scores), 1),
        max_score=round(max(scores), 1),
        cv=round(cv, 1),
        per_scenario=per_scenario,
    )


# ---------------------------------------------------------------------------
# Main container
# ---------------------------------------------------------------------------

class RepeatedExperimentResults:
    """
    Results from running AuditExperiment with n_repetitions > 1.

    Provides:
    - Backward-compatible dict interface (returns first run's AuditResults)
    - .runs(model) — all runs for a model, in execution order
    - .stability(model) — mean/std/CV and per-scenario pass rates
    - .summary() — prints stability reports for all models
    - .save() / .load() — JSON serialization
    """

    def __init__(self, runs_by_model: Dict[str, List[AuditResults]], judge: Optional[Dict] = None) -> None:
        self._runs: Dict[str, List[AuditResults]] = runs_by_model
        self._judge: Optional[Dict] = judge

    # ------------------------------------------------------------------
    # Backward-compatible dict interface
    # ------------------------------------------------------------------

    def __getitem__(self, key: Union[str, Tuple[str, int]]) -> AuditResults:
        """Return AuditResults for the given model.

        A plain string key returns the first run (backward compat).
        A ``(model, run_index)`` tuple returns the specific run, e.g.
        ``results["gpt-4o", 1]`` is the second run of "gpt-4o".
        """
        if isinstance(key, tuple):
            model, run_index = key
            if model not in self._runs:
                raise KeyError(model)
            runs = self._runs[model]
            if not 0 <= run_index < len(runs):
                raise IndexError(
                    f"run index {run_index} out of range for model {model!r} ({len(runs)} runs)"
                )
            return runs[run_index]
        if key not in self._runs:
            raise KeyError(key)
        return self._runs[key][0]

    def runs(self, model_name: str) -> List[AuditResults]:
        """Return all runs for the given model, in execution order."""
        if model_name not in self._runs:
            raise KeyError(model_name)
        return list(self._runs[model_name])

    def __iter__(self) -> Iterator[str]:
        return iter(self._runs)

    def __len__(self) -> int:
        return len(self._runs)

    def __contains__(self, key: object) -> bool:
        return key in self._runs

    def keys(self):
        return self._runs.keys()

    def values(self):
        """Return the first run for each model (backward compat).

        Use :meth:`runs` to access all runs for a specific model.
        """
        return [runs[0] for runs in self._runs.values()]

    def items(self) -> List[Tuple[str, AuditResults]]:
        """Return (model, first_run) pairs (backward compat).

        Use :meth:`runs` to access all runs for a specific model.
        """
        return [(label, runs[0]) for label, runs in self._runs.items()]

    def all_runs(self) -> Dict[str, List[AuditResults]]:
        """Return a dict mapping each model to its full list of runs.

        Unlike :meth:`values` / :meth:`items` (which only expose run 0
        for backward compatibility), this gives access to every run.

        Example::

            for model, runs in results.all_runs().items():
                for i, run in enumerate(runs):
                    print(f"{model} run {i}: {run.summary()}")
        """
        return {label: list(runs) for label, runs in self._runs.items()}

    # ------------------------------------------------------------------
    # Statistical methods
    # ------------------------------------------------------------------

    def stability(self, model_name: str) -> ModelStabilityReport:
        """Compute stability statistics for a single model across N runs."""
        if model_name not in self._runs:
            available = list(self._runs.keys())
            raise KeyError(f"No model '{model_name}' in results. Available: {available}")
        return _build_stability_report(model_name, self._runs[model_name])

    def summary(self) -> None:
        """Print stability reports for all models."""
        for model_name in self._runs:
            self.stability(model_name).summary()

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> Dict:
        n_reps = len(next(iter(self._runs.values()))) if self._runs else 0
        return {
            "version": "1.0",
            "n_repetitions": n_reps,
            "models": list(self._runs.keys()),
            "judge": self._judge,
            "aggregate": {
                label: _build_model_aggregate(runs)
                for label, runs in self._runs.items()
            },
            "runs": {
                label: [run.to_dict() for run in runs]
                for label, runs in self._runs.items()
            },
        }

    def save(self, filepath: str) -> None:
        """Save all runs to a JSON file (atomically, so interrupts can't corrupt it)."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_dump(self.to_dict(), filepath)
        print(f"✓ Repeated experiment results saved to {filepath}")

    @classmethod
    def load(cls, filepath: str) -> "RepeatedExperimentResults":
        """Load repeated experiment results from a JSON file."""
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)

        runs_by_model: Dict[str, List[AuditResults]] = {}
        for label, run_list in data["runs"].items():
            reconstructed = []
            for run_data in run_list:
                results = [AuditResult(**r) for r in run_data["results"]]
                instance = AuditResults(results)
                instance.timestamp = run_data.get("timestamp", instance.timestamp)
                reconstructed.append(instance)
            runs_by_model[label] = reconstructed

        return cls(runs_by_model, judge=data.get("judge"))
