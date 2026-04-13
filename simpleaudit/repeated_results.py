"""
Repeated-run results for SimpleAudit.

Holds results from running an AuditExperiment multiple times and provides
stability statistics and pairwise significance testing between models.
"""

import json
import math
import statistics
from collections import Counter
from dataclasses import dataclass, asdict
from itertools import combinations
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
# Pairwise model comparison report
# ---------------------------------------------------------------------------

@dataclass
class ModelComparisonReport:
    model_a: str
    model_b: str
    n_runs: int
    mean_a: float
    mean_b: float
    t_statistic: Optional[float]           # None when scipy unavailable
    p_value: Optional[float]               # None when scipy unavailable
    df: Optional[float]                    # Welch–Satterthwaite; None when scipy unavailable
    effect_size_cohens_d: Optional[float]  # None when scipy unavailable
    alpha: float
    significant: Optional[bool]            # None when scipy unavailable
    winner: Optional[str]                  # model name or None (tie / not significant)
    scipy_available: bool

    # Qualitative label for Cohen's d magnitude
    @staticmethod
    def _d_label(d: float) -> str:
        d = abs(d)
        if d < 0.2:
            return "negligible"
        if d < 0.5:
            return "small"
        if d < 0.8:
            return "medium"
        if d < 1.2:
            return "large"
        return "very large"

    def summary(self) -> None:
        print()
        print("=" * 60)
        print(f"COMPARISON: {self.model_a}  vs  {self.model_b}  ({self.n_runs} runs each)")
        print("=" * 60)
        diff = self.mean_a - self.mean_b
        favour = self.model_a if diff >= 0 else self.model_b
        print(f"Mean Scores :  {self.model_a} = {self.mean_a:.1f}   {self.model_b} = {self.mean_b:.1f}")
        print(f"Difference  :  {abs(diff):+.1f} in favour of {favour}")

        if not self.scipy_available:
            print()
            print("t-statistic :  SCIPY MISSING")
            print("p-value     :  SCIPY MISSING")
            print("Effect size :  SCIPY MISSING")
            print("Result      :  SCIPY MISSING — install scipy for significance testing")
        else:
            t_str = "∞" if self.t_statistic == math.inf else f"{self.t_statistic:.3f}"
            if math.isinf(self.effect_size_cohens_d):
                d_str = "∞ (perfect separation)"
            else:
                d_str = f"{self.effect_size_cohens_d:.2f}  ({self._d_label(self.effect_size_cohens_d)})"
            print(f"t-statistic :  {t_str}   df = {self.df:.1f}   p = {self.p_value:.4f}")
            print(f"Effect size :  Cohen's d = {d_str}")
            print(f"Alpha       :  {self.alpha}")
            if self.significant and self.winner:
                print(f"Result      :  SIGNIFICANT — {self.winner} performs better")
            else:
                print("Result      :  NOT SIGNIFICANT — no reliable difference detected")
        print()

    def to_dict(self) -> Dict:
        return asdict(self)


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


def _compute_comparison(
    model_a: str,
    scores_a: List[float],
    model_b: str,
    scores_b: List[float],
    alpha: float,
) -> ModelComparisonReport:
    n_runs = len(scores_a)
    mean_a = statistics.mean(scores_a)
    mean_b = statistics.mean(scores_b)

    try:
        from scipy.stats import ttest_ind
    except ImportError:
        return ModelComparisonReport(
            model_a=model_a,
            model_b=model_b,
            n_runs=n_runs,
            mean_a=round(mean_a, 1),
            mean_b=round(mean_b, 1),
            t_statistic=None,
            p_value=None,
            df=None,
            effect_size_cohens_d=None,
            alpha=alpha,
            significant=None,
            winner=None,
            scipy_available=False,
        )

    var_a = statistics.variance(scores_a)
    var_b = statistics.variance(scores_b)
    n_a = len(scores_a)
    n_b = len(scores_b)

    pooled_std = math.sqrt((var_a + var_b) / 2)
    if pooled_std > 0:
        d = (mean_a - mean_b) / pooled_std
    elif mean_a != mean_b:
        d = math.inf  # perfectly separated groups
    else:
        d = 0.0

    # Welch–Satterthwaite degrees of freedom and t-statistic
    term_a = var_a / n_a
    term_b = var_b / n_b
    se = math.sqrt(term_a + term_b)

    if se == 0.0:
        # Zero within-group variance: scores are perfectly consistent.
        # If means differ the difference is certain; if same there is no effect.
        if mean_a == mean_b:
            t, p_value, df = 0.0, 1.0, float(n_a + n_b - 2)
        else:
            t, p_value, df = math.inf, 0.0, float(n_a + n_b - 2)
    else:
        t = (mean_a - mean_b) / se
        denom_df = term_a + term_b
        df = denom_df ** 2 / (term_a ** 2 / (n_a - 1) + term_b ** 2 / (n_b - 1))
        _, p_value = ttest_ind(scores_a, scores_b, equal_var=False)

    significant = p_value < alpha
    if significant:
        winner = model_a if mean_a > mean_b else model_b
    else:
        winner = None

    return ModelComparisonReport(
        model_a=model_a,
        model_b=model_b,
        n_runs=n_runs,
        mean_a=round(mean_a, 1),
        mean_b=round(mean_b, 1),
        t_statistic=round(t, 3) if math.isfinite(t) else t,
        p_value=round(p_value, 4),
        df=round(df, 1),
        effect_size_cohens_d=round(d, 2) if math.isfinite(d) else d,
        alpha=alpha,
        significant=significant,
        winner=winner,
        scipy_available=True,
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
    - .compare(model_a, model_b) — Welch's t-test + Cohen's d
    - .summary() — prints all stability reports and pairwise comparisons
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

    def compare(
        self,
        model_a: str,
        model_b: str,
        alpha: float = 0.05,
    ) -> ModelComparisonReport:
        """
        Welch's two-sample t-test comparing scores of model_a vs model_b.
        Requires n_repetitions >= 2 and scipy installed for p-values.
        """
        for name in (model_a, model_b):
            if name not in self._runs:
                raise KeyError(f"No model '{name}' in results. Available: {list(self._runs.keys())}")

        n = len(self._runs[model_a])
        if n < 2:
            raise ValueError(
                "compare() requires n_repetitions >= 2. "
                "Re-run AuditExperiment with n_repetitions=2 or more."
            )

        scores_a = [r.score for r in self._runs[model_a]]
        scores_b = [r.score for r in self._runs[model_b]]
        return _compute_comparison(model_a, scores_a, model_b, scores_b, alpha)

    def summary(self) -> None:
        """Print stability reports for all models and all pairwise comparisons."""
        for model_name in self._runs:
            self.stability(model_name).summary()

        n_reps = len(next(iter(self._runs.values())))
        model_names = list(self._runs.keys())
        if n_reps >= 2 and len(model_names) >= 2:
            for a, b in combinations(model_names, 2):
                self.compare(a, b).summary()

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
