"""
Repeated-run results for SimpleAudit.

Holds results from running an AuditExperiment multiple times and provides
stability statistics across runs.
"""

import json
import statistics
from collections import Counter
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from simpleaudit.results import AuditResult, AuditResults


# ---------------------------------------------------------------------------
# Per-scenario stability stats (one model, N runs)
# ---------------------------------------------------------------------------

@dataclass
class ScenarioStats:
    pass_rate: float                        # fraction of runs where severity == "pass"
    severity_distribution: Dict[str, int]  # raw counts across all N runs
    most_common_severity: str
    agreement_rate: float                  # fraction of runs matching the mode

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
            header = f"  {'Scenario':<35} {'Pass Rate':>9}   {'Agreement':>9}   Mode"
            print(header)
            print("  " + "-" * (len(header) - 2))
            for name, stats in self.per_scenario.items():
                short = name[:34]
                print(
                    f"  {short:<35} {stats.pass_rate * 100:>8.0f}%"
                    f"   {stats.agreement_rate * 100:>8.0f}%"
                    f"   {stats.most_common_severity}"
                )
        print()

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

    # Collect scenario names from the first run
    per_scenario: Dict[str, ScenarioStats] = {}
    if runs:
        for scenario_name in [r.scenario_name for r in runs[0]]:
            severities = []
            for run in runs:
                indexed = _index_by_name(run)
                if scenario_name in indexed:
                    severities.append(indexed[scenario_name].severity)
            if not severities:
                continue
            dist = dict(Counter(severities))
            mode_sev = Counter(severities).most_common(1)[0][0]
            per_scenario[scenario_name] = ScenarioStats(
                pass_rate=severities.count("pass") / len(severities),
                severity_distribution=dist,
                most_common_severity=mode_sev,
                agreement_rate=severities.count(mode_sev) / len(severities),
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

    def __getitem__(self, key: str) -> AuditResults:
        return self._runs[key][0]

    def __iter__(self) -> Iterator[str]:
        return iter(self._runs)

    def __len__(self) -> int:
        return len(self._runs)

    def __contains__(self, key: object) -> bool:
        return key in self._runs

    def keys(self):
        return self._runs.keys()

    def values(self):
        return [runs[0] for runs in self._runs.values()]

    def items(self) -> List[Tuple[str, AuditResults]]:
        return [(label, runs[0]) for label, runs in self._runs.items()]

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
        """Save all runs to a JSON file."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
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

        return cls(runs_by_model)
