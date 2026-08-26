"""
Repeated-run results for SimpleAudit.

Holds results from running an AuditExperiment multiple times and provides
stability statistics across runs.

Stability is reported at two levels. The aggregate level — mean, std and CV
over whole-run scores — answers whether a model's overall score has settled.
The per-scenario level answers which individual verdicts have settled, and
that is a different question: a scenario swinging between pass and critical
can sit inside a perfectly steady mean, and a single-run "critical" on it
should not be read the same way as one on a scenario that never moves.

The per-scenario statistics here — modal share, normalised entropy and
ordinal spread — are the reproducibility leg of the validity chain pushed
down to the scenario. They cost nothing to compute: the verdicts were
already produced by the runs the experiment paid for. The choice to derive
fragility from existing disagreement rather than from new perturbation runs
follows Zhao et al., *Jagged Judges: Epistemic Stability Under Silence,
Pressure, and Persistence* (2026), https://arxiv.org/abs/2608.12645, which
identifies baseline jury majority strength as the most effective single-shot
signal for anticipating which items wiggle. Modal share is that quantity as
it already exists here, so the disagreement across ``n_repetitions`` is a
signal the runs have paid for whether or not anyone reads it.

The complementary check, which varies the judge prompt rather than resampling
it, lives in :mod:`simpleaudit.reframing`.
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


#: Default modal-verdict share below which a scenario counts as fragile.
#: Used by both ``ModelStabilityReport.fragile()`` and the flag in
#: ``summary()``, so the printed report and the queryable accessor can never
#: disagree about which scenarios are fragile.
FRAGILE_THRESHOLD_DEFAULT = 0.6


# ---------------------------------------------------------------------------
# Fragility statistics over a scenario's severity verdicts
# ---------------------------------------------------------------------------

def _normalised_entropy(severities: List[str]) -> float:
    """Shannon entropy of the severity distribution, scaled to 0.0–1.0.

    0.0 means every run returned the same verdict. Higher means the runs
    spread across more levels, more evenly.

    The denominator is ``log(len(SEVERITY_ORDER))`` — the full canonical
    ladder — and not the number of levels actually observed. Normalising
    against the observed count would rescale every scenario to its own
    maximum, so a scenario split evenly between two severities and one split
    evenly between five would both read 1.0 despite being very different
    kinds of unstable. The fixed denominator keeps the number comparable
    across scenarios, at a cost worth stating plainly: 1.0 needs the verdicts
    spread evenly over all five levels, which few run counts allow. Below five
    runs the ceiling is ``log(n) / log(5)`` — a two-run scenario tops out at
    0.43 even when its two verdicts disagree completely. At five or more it is
    exactly 1.0 only when n is a multiple of five, and a little under otherwise
    (0.97 at n=6), because n verdicts cannot be split evenly across five levels
    unless five divides n. Read it as a position on a fixed scale, not as a
    percentage of the disagreement that was achievable in this many runs.

    Verdicts outside ``SEVERITY_ORDER`` (e.g. "ERROR", or a custom judge
    vocabulary) are counted as their own levels rather than dropped, since
    a run that errored is a run that did not agree. A distribution wider
    than the canonical ladder is clamped to 1.0.
    """
    if not severities:
        return 0.0
    total = len(severities)
    entropy = 0.0
    for count in Counter(severities).values():
        p = count / total
        entropy -= p * math.log(p)
    return min(entropy / math.log(len(SEVERITY_ORDER)), 1.0)


def _ordinal_spread(severities: List[str]) -> Optional[float]:
    """Population standard deviation of the verdicts on the 0–4 severity scale.

    Severities are mapped to their index in ``SEVERITY_ORDER`` ("pass" = 0 …
    "critical" = 4) and the spread of those indices is returned, so a scenario
    flipping pass/critical scores higher than one flipping high/critical. The
    maximum is 2.0, from an even split between the two ends of the ladder, so
    it is only attainable when n is even; at odd n the ceiling is a little
    lower (1.886 at n=3).

    This is not a duplicate of ``ModelStabilityReport.std_score``. That is the
    sample standard deviation of the whole-run *score* (0–100, one value per
    run, across every scenario); this is the spread of one scenario's
    *severity verdicts*. A model can hold a steady overall score while one
    scenario swings between pass and critical, and only this number sees it.
    The population form is used rather than the sample form because the N runs
    are the whole set being described, not a sample drawn from a larger one.

    Returns None when any verdict is off the canonical ladder, matching
    ``severity_direction()``: an "ERROR" verdict has no position on a 0–4
    scale, and returning 0.0 would report it as perfect agreement.
    """
    if not severities:
        return 0.0
    if any(s not in SEVERITY_ORDER for s in severities):
        return None
    indices = [SEVERITY_ORDER.index(s) for s in severities]
    if len(indices) < 2:
        return 0.0
    return statistics.pstdev(indices)


# ---------------------------------------------------------------------------
# Per-scenario stability stats (one model, N runs)
# ---------------------------------------------------------------------------

@dataclass
class ScenarioStats:
    pass_rate: float                        # fraction of runs where severity == "pass"
    severity_distribution: Dict[str, int]  # raw counts across all N runs
    most_common_severity: str
    agreement_rate: float                  # fraction of runs matching the mode
    normalised_entropy: float = 0.0        # 0.0 = unanimous; see _normalised_entropy
    ordinal_spread: Optional[float] = 0.0  # std on the 0-4 ladder; None if off-ladder
    n_observations: int = 0                # runs this scenario actually appeared in

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
            header = (
                f"  {'Scenario':<35} {'Pass Rate':>9}   {'Agreement':>9}"
                f"   {'Entropy':>7}   Mode"
            )
            print(header)
            print("  " + "-" * (len(header) - 2))
            for name, stats in self.per_scenario.items():
                short = name[:34]
                # One observation cannot disagree with itself, so the
                # fragility numbers are structurally 0.0 rather than measured.
                # Printing them would read as "perfectly stable" when the
                # truth is "not measured". Keyed on the scenario's own count,
                # not the report's: a scenario that errored out of four of
                # five runs sits in an n_runs=5 report having been seen once.
                measured = stats.n_observations > 1
                entropy = f"{stats.normalised_entropy:>7.2f}" if measured else f"{'—':>7}"
                flag = "  (fragile)" if stats.agreement_rate < FRAGILE_THRESHOLD_DEFAULT else ""
                print(
                    f"  {short:<35} {stats.pass_rate * 100:>8.0f}%"
                    f"   {stats.agreement_rate * 100:>8.0f}%"
                    f"   {entropy}"
                    f"   {stats.most_common_severity}{flag}"
                )
            # Guarded on n_runs so printing a single-run report does not
            # emit the "needs at least 2 runs" warning: that belongs to a
            # caller who asked for fragility, not to one who asked for a
            # summary.
            fragile = self.fragile() if self.n_runs > 1 else {}
            if fragile:
                print()
                print(
                    f"  {len(fragile)} of {len(self.per_scenario)} scenarios fragile "
                    f"(modal share < {FRAGILE_THRESHOLD_DEFAULT:.0%}): "
                    f"{', '.join(sorted(fragile))}"
                )
                print("  Single-run verdicts on these scenarios should not be read as settled.")
        print()

    def fragile(self, threshold: float = FRAGILE_THRESHOLD_DEFAULT) -> Dict[str, "ScenarioStats"]:
        """Scenarios whose modal-verdict share falls below *threshold*.

        The threshold is on the modal share (``agreement_rate``): the fraction
        of runs that returned the most common severity. The comparison is
        strict, so ``fragile(threshold=0.6)`` returns scenarios where fewer
        than 60% of runs agreed, and a scenario sitting exactly on 0.6 is not
        fragile. Entropy and ordinal spread are reported per scenario but do
        not gate this accessor — modal share is the statistic the paper
        identifies as the single-shot predictor of verdict instability.

        One reading to be aware of: a scenario where the judge failed in every
        run has a modal share of 1.0 and is *not* returned here, because the
        runs did agree — on "ERROR". That is consistent agreement on a
        non-verdict, not a stable verdict. ``most_common_severity`` shows it in
        the summary table and ``ordinal_spread`` is None for it, so both
        surfaces carry the signal; this accessor deliberately does not
        reinterpret it, since ``agreement_rate`` predates this change and
        callers already depend on its meaning.

        Returns
        -------
        dict
            ``{scenario_name: ScenarioStats}`` for the fragile scenarios,
            keeping the shape of ``per_scenario`` so the same reporting code
            reads both. Empty when nothing is fragile.

        Raises
        ------
        ValueError
            If *threshold* is outside 0.0–1.0, which would otherwise silently
            return everything or nothing.

        Warns
        -----
        UserWarning
            When called on a single-run report. One run always agrees with
            itself, so every scenario reads as stable; the empty result means
            "not measured", not "nothing is fragile".
        """
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(
                f"threshold must be between 0.0 and 1.0, got {threshold}. "
                "It is compared against the modal-verdict share, a fraction."
            )
        if self.n_runs < 2:
            warnings.warn(
                f"Model {self.model!r}: fragility needs at least 2 runs to mean anything — "
                f"this report has {self.n_runs}. Every scenario will read as stable because "
                "a single run cannot disagree with itself. Re-run with n_repetitions > 1.",
                stacklevel=2,
            )
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
            spread = _ordinal_spread(severities)
            per_scenario[scenario_name] = ScenarioStats(
                pass_rate=severities.count("pass") / len(severities),
                severity_distribution=dist,
                most_common_severity=mode_sev,
                agreement_rate=severities.count(mode_sev) / len(severities),
                normalised_entropy=round(_normalised_entropy(severities), 4),
                ordinal_spread=None if spread is None else round(spread, 4),
                n_observations=len(severities),
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
    - .stability(model).fragile(threshold=...) — scenarios whose verdict
      disagrees across runs
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

    def _stability_reports(self) -> Dict[str, ModelStabilityReport]:
        """Stability reports for every model, without re-raising the warnings.

        ``_build_stability_report`` warns about duplicate scenario names. That
        warning belongs to a caller who asked for stability, and it is already
        raised by :meth:`stability`. Letting it through here would make
        ``save()`` warn — and, under ``-W error``, raise — on files it wrote
        without complaint before this key existed.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return {
                label: _build_stability_report(label, runs)
                for label, runs in self._runs.items()
            }

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
            # Per-scenario fragility travels with the saved run, so a reader
            # who only has the JSON can tell which verdicts were unstable
            # without re-deriving them. Additive: load() ignores this key.
            "stability": {
                label: report.to_dict()
                for label, report in self._stability_reports().items()
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
