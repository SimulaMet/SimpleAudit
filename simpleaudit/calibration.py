"""
Calibrate a judge against a reference: human labels, or a judge from another
model family.

Every other judge check in SimpleAudit compares the judge with itself
(resampling, reframing, perturbations) or with another model (judge swap,
panel). None of them says whether the judge is *right*. This module scores
the verdicts a judge gave on stored transcripts against a reference that is
independent of it, and reports agreement in a form that is honest about how
few labels there are.

Two references are supported, in order of quality:

- **Human labels.** A dict of ``{scenario_name: severity}`` for some or all
  of the transcripts in a results file. A few labels are better than none;
  every number carries its n and an interval, and the report says when the
  count is too low to be more than indicative.
- **A judge from another model family.** The same transcripts re-graded by
  a model whose training lineage differs from the subject judge's (Claude
  versus GPT, Gemini versus Qwen, ...). Two judges from the same family
  share blind spots, so their agreement says little; the call refuses that
  pairing. Cross-family agreement is weak but real evidence, and it costs
  judge tokens only.

The three calls::

    from simpleaudit.calibration import label, calibrate

    labels = label("results/nav_aap.json")            # interactive, saves as it goes
    calibrate("results/nav_aap.json", labels).summary()

    ref = rejudge("results/nav_aap.json", other_client, "gpt-5.2")
    calibrate("results/nav_aap.json", ref, judge_model="claude-haiku-4-5").summary()

Nothing here calls a model. The reference judge is produced by
:func:`simpleaudit.reframing.rejudge` or :func:`simpleaudit.reframing.reframing_check`.
"""

from __future__ import annotations

import json
import random
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .results import AuditResults, _atomic_json_dump
from .stats import krippendorff_alpha, wilson_interval
from .utils import SEVERITY_ORDER, UNGRADED, normalize_severity

#: Below this many compared items the report prints "indicative only" and
#: withholds chance-corrected agreement, which is noise at that size.
MIN_ITEMS_FOR_ALPHA = 10

#: Value a rater writes to leave an item out without deleting the row.
UNSURE = "unsure"

LABELS_FORMAT_VERSION = "1.0"
REPORT_FORMAT_VERSION = "1.0"

_LADDER_INDEX = {s: i for i, s in enumerate(SEVERITY_ORDER)}
_SHORTCUTS = {"p": "pass", "l": "low", "m": "medium", "h": "high", "c": "critical"}


# ---------------------------------------------------------------------------
# Model families
# ---------------------------------------------------------------------------

#: (family, regex on the lowercased model id with any provider prefix removed).
#: Order matters only where a name could match twice; the first wins.
_FAMILY_PATTERNS: List[Tuple[str, str]] = [
    ("anthropic", r"claude"),
    ("openai", r"(^|[^a-z])(gpt|chatgpt|o[1-9](-|$)|davinci|text-embedding)"),
    ("google", r"gemini|gemma|palm|bison"),
    ("meta", r"(^|[^a-z])llama"),   # not "ollama"
    ("mistral", r"mistral|mixtral|codestral|ministral|magistral|pixtral"),
    ("qwen", r"qwen|qwq"),
    ("deepseek", r"deepseek"),
    ("microsoft", r"(^|[^a-z])phi[-_ ]?[0-9]"),
    ("cohere", r"(^|[^a-z])command(-|$)|aya"),
    ("xai", r"grok"),
    ("amazon", r"(^|[^a-z])nova(-|$)|titan"),
    ("nvidia", r"nemotron"),
    ("ai21", r"jamba"),
]


def model_family(model: Optional[str]) -> Optional[str]:
    """Training lineage of a model id, or ``None`` when it is not recognised.

    The whole id is searched, so a provider prefix or a size tag
    (``anthropic/claude-...``, ``openrouter/google/gemini-...``,
    ``ollama:qwen3:8b``) does not hide the family. An unrecognised id gets
    ``None``: the caller decides whether to warn or refuse.
    """
    if not model:
        return None
    text = str(model).strip().lower()
    for family, pattern in _FAMILY_PATTERNS:
        if re.search(pattern, text):
            return family
    return None


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def _normalize_label(value: Any, scenario: str, source: str) -> Optional[str]:
    """One reference rating onto the ladder; ``None`` means "no rating".

    Humans write ``unsure`` or leave a blank to skip an item. A judge writes
    ``ERROR`` or ``ungraded`` when it did not grade one. Both drop out of the
    comparison; anything else off the ladder is a typo and raises, because a
    silently dropped label would make the report look better than it is.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", UNSURE, "skip", "error", UNGRADED):
        return None
    text = _SHORTCUTS.get(text, text)
    if text in _LADDER_INDEX:
        return text
    raise ValueError(
        f"{source}: label {value!r} for {scenario!r} is not a severity. "
        f"Use one of {SEVERITY_ORDER} or {UNSURE!r} to leave the item out."
    )


def load_labels(path: Union[str, Path]) -> Dict[str, str]:
    """Read a labels file written by :func:`label` (or by hand).

    The file is JSON, either a bare ``{scenario_name: severity}`` mapping or
    the object :func:`label` writes, which holds that mapping under
    ``"labels"``. Returns the mapping; ``unsure`` entries are kept so the
    report can count them.
    """
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "labels" in data and isinstance(data["labels"], dict):
        return dict(data["labels"])
    if isinstance(data, dict) and all(isinstance(v, (str, type(None))) for v in data.values()):
        return dict(data)
    raise ValueError(
        f"{path}: expected a {{scenario_name: severity}} mapping or a labels file "
        "with a 'labels' key."
    )


def save_labels(
    labels: Mapping[str, Any],
    path: Union[str, Path],
    *,
    source: Optional[str] = None,
    rater: Optional[str] = None,
) -> None:
    """Write labels as the JSON object :func:`load_labels` reads back."""
    payload: Dict[str, Any] = {
        "version": LABELS_FORMAT_VERSION,
        "source": source,
        "rater": rater,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "labels": dict(labels),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_dump(payload, str(path))


def _render_transcript(result: Any) -> str:
    lines: List[str] = []
    for turn in result.conversation:
        role = str(turn.get("role", "?")).upper()
        content = turn.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        lines.append(f"[{role}]\n{content}\n")
    return "\n".join(lines)


def label(
    results_or_path: Union[AuditResults, str, Path],
    out: Optional[Union[str, Path]] = None,
    *,
    rater: Optional[str] = None,
    relabel: bool = False,
    show_expected: bool = True,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> Dict[str, str]:
    """Label stored transcripts one at a time in the terminal.

    Prints the scenario, what the target was expected to do, and the full
    transcript, then asks for a severity. The judge's own verdict is never
    shown, so the label is blind. Each answer is written to *out* at once,
    so stopping after six items and picking up tomorrow loses nothing.

    Answers: ``pass``, ``low``, ``medium``, ``high``, ``critical`` (or the
    first letter), ``unsure`` to leave the item out, an empty line to skip
    it for now, and ``q`` to stop.

    Args:
        results_or_path: a results file or object. With an object, *out*
            is required.
        out: where to save; defaults to ``<results stem>.labels.json`` next
            to the results file.
        rater: a name recorded in the file, for when several people label.
        relabel: ask again for items the file already holds.
        show_expected: print the scenario's ``expected_behavior`` list.
        input_fn, print_fn: injectable for tests and for other front ends.

    Returns:
        The labels now in the file, ``{scenario_name: severity}``.
    """
    if isinstance(results_or_path, AuditResults):
        if out is None:
            raise ValueError("label() needs out= when given an AuditResults object.")
        results = results_or_path
        source = None
    else:
        source = str(results_or_path)
        results = AuditResults.load(source)
        if out is None:
            p = Path(source)
            out = p.with_name(p.stem + ".labels.json")
    out = Path(out)
    labels: Dict[str, str] = load_labels(out) if out.exists() else {}

    total = len(results.results)
    print_fn(f"Labelling {total} transcripts -> {out}")
    print_fn("Answer pass / low / medium / high / critical (or p l m h c), "
             "'unsure' to leave out, Enter to skip, q to stop.\n")
    for i, result in enumerate(results.results, 1):
        name = result.scenario_name
        if name in labels and not relabel:
            continue
        print_fn("=" * 72)
        print_fn(f"[{i}/{total}] {name}")
        if result.scenario_description:
            print_fn(f"Scenario: {result.scenario_description}")
        if show_expected and result.expected_behavior:
            print_fn("Expected:")
            for item in result.expected_behavior:
                print_fn(f"  - {item}")
        print_fn("-" * 72)
        print_fn(_render_transcript(result))
        while True:
            answer = input_fn("severity> ").strip().lower()
            if answer == "q":
                print_fn(f"Stopped. {len(labels)} labels in {out}")
                return labels
            if answer == "":
                break
            try:
                value = _normalize_label(answer, name, "label()")
            except ValueError as exc:
                print_fn(str(exc))
                continue
            labels[name] = UNSURE if value is None else value
            save_labels(labels, out, source=source, rater=rater)
            break
    print_fn(f"Done. {len(labels)} labels in {out}")
    return labels


# ---------------------------------------------------------------------------
# References and subjects
# ---------------------------------------------------------------------------

@dataclass
class Rater:
    """One source of reference verdicts: a person or a cross-family judge."""

    name: str
    kind: str                                  # "human" | "model"
    ratings: Dict[str, Optional[str]]          # scenario -> ladder value or None
    model: Optional[str] = None
    family: Optional[str] = None

    @property
    def n_rated(self) -> int:
        return sum(1 for v in self.ratings.values() if v is not None)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "model": self.model,
                "family": self.family, "n_rated": self.n_rated}


def _is_results_file(path: Path) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return isinstance(data, dict) and isinstance(data.get("results"), list)


def _verdicts_of(results: AuditResults, source: str) -> Dict[str, str]:
    """``{scenario_name: normalised severity}`` for a results object.

    Off-ladder values (``ERROR``, ``ungraded``, a custom vocabulary) are kept
    as they are so coverage can count them. Duplicate names (a repeated run
    saved flat) warn, and the last one wins, as elsewhere in the library.
    """
    out: Dict[str, str] = {}
    dupes = set()
    for r in results.results:
        if r.scenario_name in out:
            dupes.add(r.scenario_name)
        out[r.scenario_name] = normalize_severity(r.severity)
    if dupes:
        warnings.warn(
            f"{source}: duplicate scenario names {sorted(dupes)}; the last verdict for "
            "each is used. Labels are keyed by scenario name, so label one run per file.",
            stacklevel=3,
        )
    return out


def _rater_from(ref: Any, index: int, *, reference_model: Optional[str]) -> Rater:
    """Turn one reference argument into a :class:`Rater`."""
    if isinstance(ref, Rater):
        return ref
    if isinstance(ref, AuditResults):
        model = reference_model or ref.meta.get("judge_model")
        verdicts = _verdicts_of(ref, f"reference {index}")
        return Rater(
            name=f"model:{model or 'unknown'}",
            kind="model",
            ratings={k: (v if v in _LADDER_INDEX else None) for k, v in verdicts.items()},
            model=model,
            family=model_family(model),
        )
    if isinstance(ref, (str, Path)):
        path = Path(ref)
        if _is_results_file(path):
            return _rater_from(AuditResults.load(str(path)), index, reference_model=reference_model)
        mapping = load_labels(path)
        return Rater(
            name=path.stem,
            kind="human",
            ratings={k: _normalize_label(v, k, str(path)) for k, v in mapping.items()},
        )
    if isinstance(ref, Mapping):
        return Rater(
            name=f"rater_{index + 1}",
            kind="human",
            ratings={k: _normalize_label(v, k, "labels") for k, v in ref.items()},
        )
    raise TypeError(
        f"reference must be a labels dict, a labels file, a results file or an "
        f"AuditResults, got {type(ref).__name__}"
    )


def _consensus(values: Sequence[str]) -> str:
    """Median on the ladder; an even split resolves to the stricter middle.

    Same conservative tie-break as :func:`simpleaudit.aggregate_severities`:
    when two raters say ``low`` and ``medium``, the item is ``medium``.
    """
    ranks = sorted(_LADDER_INDEX[v] for v in values)
    mid = len(ranks) // 2
    return SEVERITY_ORDER[ranks[mid]]


@dataclass
class Reference:
    """The consensus the judge is scored against, built from one or more raters."""

    raters: List[Rater]
    consensus: Dict[str, str]                  # items with at least one rating
    split: Dict[str, bool]                     # item -> raters disagreed
    n_unsure: int                              # entries present but unrated by every rater

    @property
    def kind(self) -> str:
        kinds = {r.kind for r in self.raters}
        return kinds.pop() if len(kinds) == 1 else "mixed"

    def human_ceiling(self) -> Optional[float]:
        """Ordinal alpha among the raters; ``None`` with one rater or too few items."""
        if len(self.raters) < 2:
            return None
        units = [[r.ratings.get(name) for r in self.raters] for name in self.consensus]
        units = [u for u in units if sum(v is not None for v in u) >= 2]
        if len(units) < MIN_ITEMS_FOR_ALPHA:
            return None
        return krippendorff_alpha(units, "ordinal", SEVERITY_ORDER)


def _build_reference(refs: Sequence[Any], reference_model: Optional[str]) -> Reference:
    raters = [_rater_from(ref, i, reference_model=reference_model) for i, ref in enumerate(refs)]
    names: List[str] = []
    for r in raters:
        for name in r.ratings:
            if name not in names:
                names.append(name)
    consensus: Dict[str, str] = {}
    split: Dict[str, bool] = {}
    n_unsure = 0
    for name in names:
        values = [r.ratings[name] for r in raters if r.ratings.get(name) is not None]
        if not values:
            n_unsure += 1
            continue
        consensus[name] = _consensus(values)
        split[name] = len(set(values)) > 1
    return Reference(raters=raters, consensus=consensus, split=split, n_unsure=n_unsure)


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------

def _bootstrap(
    stat: Callable[[List[Any]], Optional[float]],
    items: List[Any],
    n_resamples: int = 1000,
    seed: int = 0,
) -> Optional[Tuple[float, float]]:
    """Percentile bootstrap over items; ``None`` when the statistic is undefined."""
    if len(items) < 2:
        return None
    rng = random.Random(seed)
    draws: List[float] = []
    for _ in range(n_resamples):
        sample = [items[rng.randrange(len(items))] for _ in items]
        value = stat(sample)
        if value is not None:
            draws.append(value)
    if len(draws) < n_resamples // 2:
        return None
    draws.sort()
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[int(0.975 * (len(draws) - 1))]
    return lo, hi


@dataclass
class Agreement:
    """How one judge's verdicts compare with the reference consensus.

    Counts are kept raw; intervals are computed on request so the dataclass
    stays a plain record. ``n_compared`` is the denominator for everything
    except the pass/fail rates, which use the human-fail and human-pass
    subsets.
    """

    label: str
    judge_model: Optional[str]
    fail_at: str
    n_reference: int            # items with a consensus
    n_compared: int             # ... that the judge put on the ladder
    n_error: int                # judge said ERROR
    n_ungraded: int             # judge said ungraded / off-ladder
    n_unjudged: int             # reference item not in the judge's output
    exact: int
    within_one: int
    n_stricter: int
    n_lenient: int
    bias_mean: Optional[float]
    bias_interval: Optional[Tuple[float, float]]
    alpha: Optional[float]
    alpha_interval: Optional[Tuple[float, float]]
    sensitivity: Tuple[int, int]        # judge fail | reference fail
    specificity: Tuple[int, int]        # judge pass | reference pass
    severe_misses: Tuple[int, int]      # judge pass/low | reference high/critical
    confusion: Dict[str, Dict[str, int]]
    disagreements: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def indicative_only(self) -> bool:
        return self.n_compared < MIN_ITEMS_FOR_ALPHA

    def rate(self, k: int, n: int) -> Dict[str, Any]:
        lo, hi = wilson_interval(k, n)
        return {"k": k, "n": n, "value": (k / n) if n else None, "interval": [lo, hi]}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "judge_model": self.judge_model,
            "fail_at": self.fail_at,
            "indicative_only": self.indicative_only,
            "coverage": {
                "reference": self.n_reference, "compared": self.n_compared,
                "error": self.n_error, "ungraded": self.n_ungraded, "unjudged": self.n_unjudged,
            },
            "exact": self.rate(self.exact, self.n_compared),
            "within_one": self.rate(self.within_one, self.n_compared),
            "bias": {
                "mean": self.bias_mean, "interval": list(self.bias_interval) if self.bias_interval else None,
                "stricter": self.n_stricter, "lenient": self.n_lenient,
            },
            "alpha": {"value": self.alpha, "interval": list(self.alpha_interval) if self.alpha_interval else None},
            "sensitivity": self.rate(*self.sensitivity),
            "specificity": self.rate(*self.specificity),
            "severe_misses": self.rate(*self.severe_misses),
            "confusion": self.confusion,
            "disagreements": self.disagreements,
        }


def _agreement(
    label: str,
    judge_model: Optional[str],
    verdicts: Mapping[str, str],
    consensus: Mapping[str, str],
    fail_at: str,
) -> Agreement:
    fail_rank = _LADDER_INDEX[fail_at]
    n_error = n_ungraded = n_unjudged = 0
    pairs: List[Tuple[str, str, str]] = []     # (scenario, reference, judge)
    confusion: Dict[str, Dict[str, int]] = {s: {c: 0 for c in SEVERITY_ORDER + ["ERROR"]} for s in SEVERITY_ORDER}
    for name, ref in consensus.items():
        if name not in verdicts:
            n_unjudged += 1
            continue
        v = verdicts[name]
        if v == "ERROR":
            n_error += 1
            confusion[ref]["ERROR"] += 1
        elif v not in _LADDER_INDEX:
            n_ungraded += 1
        else:
            pairs.append((name, ref, v))
            confusion[ref][v] += 1

    gaps = [_LADDER_INDEX[v] - _LADDER_INDEX[ref] for _, ref, v in pairs]
    n = len(pairs)
    ref_fail = [(ref, v) for _, ref, v in pairs if _LADDER_INDEX[ref] >= fail_rank]
    ref_pass = [(ref, v) for _, ref, v in pairs if _LADDER_INDEX[ref] < fail_rank]
    ref_severe = [(ref, v) for _, ref, v in pairs if _LADDER_INDEX[ref] >= _LADDER_INDEX["high"]]

    def alpha_of(sample: List[Tuple[str, str, str]]) -> Optional[float]:
        return krippendorff_alpha([[ref, v] for _, ref, v in sample], "ordinal", SEVERITY_ORDER)

    def bias_of(sample: List[Tuple[str, str, str]]) -> Optional[float]:
        return sum(_LADDER_INDEX[v] - _LADDER_INDEX[ref] for _, ref, v in sample) / len(sample) if sample else None

    alpha = alpha_of(pairs) if n >= MIN_ITEMS_FOR_ALPHA else None
    return Agreement(
        label=label,
        judge_model=judge_model,
        fail_at=fail_at,
        n_reference=len(consensus),
        n_compared=n,
        n_error=n_error,
        n_ungraded=n_ungraded,
        n_unjudged=n_unjudged,
        exact=sum(1 for g in gaps if g == 0),
        within_one=sum(1 for g in gaps if abs(g) <= 1),
        n_stricter=sum(1 for g in gaps if g > 0),
        n_lenient=sum(1 for g in gaps if g < 0),
        bias_mean=(sum(gaps) / n) if n else None,
        bias_interval=_bootstrap(bias_of, pairs) if n else None,
        alpha=alpha,
        alpha_interval=_bootstrap(alpha_of, pairs) if alpha is not None else None,
        sensitivity=(sum(1 for _, v in ref_fail if _LADDER_INDEX[v] >= fail_rank), len(ref_fail)),
        specificity=(sum(1 for _, v in ref_pass if _LADDER_INDEX[v] < fail_rank), len(ref_pass)),
        severe_misses=(sum(1 for _, v in ref_severe if _LADDER_INDEX[v] <= _LADDER_INDEX["low"]), len(ref_severe)),
        confusion=confusion,
        disagreements=[
            {"scenario": name, "reference": ref, "judge": v, "gap": _LADDER_INDEX[v] - _LADDER_INDEX[ref]}
            for name, ref, v in pairs if ref != v
        ],
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt_rate(k: int, n: int) -> str:
    if n == 0:
        return "n/a (0 items)"
    lo, hi = wilson_interval(k, n)
    return f"{k}/{n} = {k / n:.2f}  [{lo:.2f}, {hi:.2f}]"


def _fmt_interval(value: Optional[float], interval: Optional[Tuple[float, float]], signed: bool = False) -> str:
    if value is None:
        return "n/a"
    text = f"{value:+.2f}" if signed else f"{value:.2f}"
    if interval:
        text += f"  [{interval[0]:{'+' if signed else ''}.2f}, {interval[1]:{'+' if signed else ''}.2f}]"
    return text


@dataclass
class CalibrationReport:
    """What :func:`calibrate` returns: one :class:`Agreement` per judge, plus
    the reference it was scored against and the unanimous/split strata."""

    subject: str
    reference: Reference
    fail_at: str
    judges: Dict[str, Agreement]
    strata: Dict[str, Dict[str, Agreement]]    # judge label -> {"unanimous": .., "split": ..}
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    # -- access ---------------------------------------------------------------

    def __getitem__(self, label: str) -> Agreement:
        return self.judges[label]

    @property
    def n_items(self) -> int:
        return len(self.reference.consensus)

    @property
    def n_split(self) -> int:
        return sum(1 for v in self.reference.split.values() if v)

    def leaderboard(self) -> List[Agreement]:
        """Judges ordered best first: alpha when every judge has one, else exact agreement."""
        rows = list(self.judges.values())
        if rows and all(r.alpha is not None for r in rows):
            return sorted(rows, key=lambda r: (r.alpha, r.exact / max(r.n_compared, 1)), reverse=True)
        return sorted(rows, key=lambda r: (r.exact / max(r.n_compared, 1), r.n_compared), reverse=True)

    # -- output ---------------------------------------------------------------

    def reference_line(self) -> str:
        parts = []
        for r in self.reference.raters:
            if r.kind == "model":
                parts.append(f"{r.model or 'unknown model'} ({r.family or 'unknown family'})")
            else:
                parts.append(r.name)
        kind = self.reference.kind
        return f"{kind}, {len(parts)} rater{'s' if len(parts) != 1 else ''}: " + ", ".join(parts)

    def summary(self, print_fn: Callable[[str], None] = print) -> None:
        """Print the report. Every rate shows its n and a 95% interval."""
        out: List[str] = []
        out.append(f"Calibration: {self.subject}")
        out.append(f"  reference: {self.reference_line()}")
        line = f"  items with a reference label: {self.n_items}"
        if self.reference.n_unsure:
            line += f"   (+{self.reference.n_unsure} unsure, left out)"
        if len(self.reference.raters) > 1:
            line += f"   split between raters: {self.n_split}"
        out.append(line)
        ceiling = self.reference.human_ceiling()
        if ceiling is not None:
            out.append(f"  agreement among raters (ordinal alpha): {ceiling:.2f}   <- the ceiling to read the judge against")
        for a in self.leaderboard():
            out.append("")
            head = f"  judge '{a.label}'"
            if a.judge_model:
                head += f"  ({a.judge_model}, {model_family(a.judge_model) or 'unknown family'})"
            out.append(head)
            cov = f"    compared: {a.n_compared} of {a.n_reference}"
            extras = [f"{a.n_error} ERROR" if a.n_error else "", f"{a.n_ungraded} ungraded" if a.n_ungraded else "",
                      f"{a.n_unjudged} not judged" if a.n_unjudged else ""]
            extras = [e for e in extras if e]
            if extras:
                cov += "   (" + ", ".join(extras) + ")"
            if a.indicative_only:
                cov += f"   INDICATIVE ONLY (fewer than {MIN_ITEMS_FOR_ALPHA})"
            out.append(cov)
            if a.n_compared == 0:
                continue
            out.append(f"    exact agreement     {_fmt_rate(a.exact, a.n_compared)}")
            out.append(f"    within one level    {_fmt_rate(a.within_one, a.n_compared)}")
            out.append(f"    bias (judge - ref)  mean {_fmt_interval(a.bias_mean, a.bias_interval, signed=True)}"
                       f"   stricter {a.n_stricter}  lenient {a.n_lenient}")
            if a.alpha is not None:
                out.append(f"    ordinal alpha       {_fmt_interval(a.alpha, a.alpha_interval)}")
            out.append(f"    pass/fail at '{a.fail_at}':  sensitivity {_fmt_rate(*a.sensitivity)}"
                       f"   specificity {_fmt_rate(*a.specificity)}")
            out.append(f"    severe misses (ref high/critical -> judge pass/low): {_fmt_rate(*a.severe_misses)}")
            strata = self.strata.get(a.label, {})
            if len(self.reference.raters) > 1 and strata:
                for key in ("unanimous", "split"):
                    s = strata.get(key)
                    if s and s.n_compared:
                        out.append(f"    {key:<9} items   exact {_fmt_rate(s.exact, s.n_compared)}")
            out.append("    confusion (rows reference, cols judge)   " + "".join(f"{c:>6}" for c in ("pass", "low", "med", "high", "crit", "ERR")))
            for ref in SEVERITY_ORDER:
                row = a.confusion[ref]
                if any(row.values()):
                    out.append(f"      {ref:<36}" + "".join(f"{row[c]:>6}" for c in SEVERITY_ORDER + ["ERROR"]))
            if a.disagreements:
                out.append("    disagreements (read these transcripts):")
                for d in sorted(a.disagreements, key=lambda d: -abs(d["gap"]))[:10]:
                    out.append(f"      {d['scenario']}: reference {d['reference']}, judge {d['judge']}")
                if len(a.disagreements) > 10:
                    out.append(f"      ... and {len(a.disagreements) - 10} more")
        print_fn("\n".join(out))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": REPORT_FORMAT_VERSION,
            "created_at": self.created_at,
            "subject": self.subject,
            "fail_at": self.fail_at,
            "reference": {
                "kind": self.reference.kind,
                "raters": [r.to_dict() for r in self.reference.raters],
                "n_items": self.n_items,
                "n_split": self.n_split,
                "n_unsure": self.reference.n_unsure,
                "human_ceiling_alpha": self.reference.human_ceiling(),
                "consensus": dict(self.reference.consensus),
            },
            "judges": {label: a.to_dict() for label, a in self.judges.items()},
            "strata": {
                label: {key: s.to_dict() for key, s in by_key.items()}
                for label, by_key in self.strata.items()
            },
        }

    def default_path(self, directory: Union[str, Path] = "results/calibration") -> Path:
        stem = Path(self.subject).stem if self.subject else "calibration"
        ref = self.reference.kind
        if ref == "model" and self.reference.raters:
            ref = "model-" + re.sub(r"[^a-z0-9]+", "-", (self.reference.raters[0].model or "unknown").lower()).strip("-")
        date = self.created_at[:10].replace("-", "")
        return Path(directory) / f"{stem}__{ref}__{date}.json"

    def save(self, path: Optional[Union[str, Path]] = None, directory: Union[str, Path] = "results/calibration") -> Path:
        """Write the report as JSON. Defaults to ``results/calibration/<subject>__<reference>__<date>.json``."""
        target = Path(path) if path else self.default_path(directory)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_dump(self.to_dict(), str(target))
        return target


def list_calibrations(directory: Union[str, Path] = "results/calibration") -> List[Dict[str, Any]]:
    """One row per saved report in *directory*: subject, reference, judges and their headline numbers."""
    rows: List[Dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "judges" not in data or "reference" not in data:
            continue
        for label, a in data["judges"].items():
            rows.append({
                "path": str(path),
                "created_at": data.get("created_at"),
                "subject": data.get("subject"),
                "reference": data["reference"].get("kind"),
                "raters": [r.get("name") for r in data["reference"].get("raters", [])],
                "judge": label,
                "judge_model": a.get("judge_model"),
                "n_compared": a.get("coverage", {}).get("compared"),
                "exact": a.get("exact", {}).get("value"),
                "alpha": a.get("alpha", {}).get("value"),
                "indicative_only": a.get("indicative_only"),
            })
    return rows


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------

def _subjects(
    subject: Any, judge_model: Optional[str]
) -> Tuple[str, Dict[str, Tuple[Optional[str], Dict[str, str]]]]:
    """``(description, {label: (judge_model, verdicts)})`` for whatever was passed."""
    # ReframingResults: one judge per variant. Imported by duck type so this
    # module never imports reframing (which pulls in the whole auditor).
    if hasattr(subject, "variant_labels") and hasattr(subject, "per_scenario"):
        out: Dict[str, Tuple[Optional[str], Dict[str, str]]] = {}
        for lab in subject.variant_labels:
            meta = getattr(subject, "variant_meta", {}).get(lab, {})
            model = meta.get("judge_model") or judge_model
            verdicts = {
                name: normalize_severity(by_variant[lab])
                for name, by_variant in subject.per_scenario.items() if lab in by_variant
            }
            out[lab] = (model, verdicts)
        return "reframing_check results", out
    if isinstance(subject, (str, Path)):
        results = AuditResults.load(str(subject))
        desc = str(subject)
    elif isinstance(subject, AuditResults):
        results = subject
        desc = "AuditResults"
    else:
        raise TypeError(f"subject must be a results file, AuditResults or ReframingResults, got {type(subject).__name__}")
    model = judge_model or results.meta.get("judge_model")
    label_ = results.meta.get("judge_name") or "stored"
    return desc, {label_: (model, _verdicts_of(results, desc))}


def _check_families(
    judges: Mapping[str, Tuple[Optional[str], Dict[str, str]]],
    reference: Reference,
    allow_same_family: bool,
) -> None:
    """Refuse a model reference from the subject judge's own family."""
    model_raters = [r for r in reference.raters if r.kind == "model"]
    if not model_raters:
        return
    for rater in model_raters:
        if rater.family is None:
            warnings.warn(
                f"reference judge {rater.model!r} is not a recognised model family; "
                "cannot check that it differs from the subject judge's. Pass a model id "
                "with a known family, or check by hand.",
                stacklevel=3,
            )
    seen: Dict[str, str] = {}
    for rater in model_raters:
        if rater.family and rater.family in seen and not allow_same_family:
            raise ValueError(
                f"reference judges {seen[rater.family]!r} and {rater.model!r} are both "
                f"{rater.family}; references must come from different model families."
            )
        if rater.family:
            seen[rater.family] = rater.model or ""
    for label, (model, _) in judges.items():
        if model is None:
            warnings.warn(
                f"subject judge '{label}' has no judge_model recorded (results saved before "
                "0.5.1 do not carry it); pass judge_model= to check its family against the reference.",
                stacklevel=3,
            )
            continue
        family = model_family(model)
        if family is None:
            warnings.warn(f"subject judge model {model!r} is not a recognised model family; family check skipped.", stacklevel=3)
            continue
        for rater in model_raters:
            if rater.family == family and not allow_same_family:
                raise ValueError(
                    f"subject judge {model!r} and reference judge {rater.model!r} are both "
                    f"{family}. A judge from the same family shares its blind spots, so their "
                    "agreement is not evidence; use a human reference or a judge from another "
                    "family (pass allow_same_family=True to override for an experiment)."
                )


def calibrate(
    subject: Any,
    reference: Any,
    *,
    judge_model: Optional[str] = None,
    reference_model: Optional[str] = None,
    fail_at: str = "low",
    allow_same_family: bool = False,
) -> CalibrationReport:
    """Score a judge's stored verdicts against a reference.

    Args:
        subject: what the judge produced: a results file or :class:`AuditResults`
            (its stored ``severity`` per scenario), or a ``ReframingResults``
            from :func:`simpleaudit.reframing_check` (one judge per variant,
            the modal verdict at ``k > 1``).
        reference: human labels as ``{scenario_name: severity}``, a labels
            file from :func:`label`, a results file or :class:`AuditResults`
            graded by a judge from another model family, or a list mixing any
            of these. With several raters the reference for an item is the
            median on the ladder, stricter middle on an even split, and items
            where raters disagreed are reported as a separate stratum.
        judge_model: the subject judge's model id, when the results do not
            record it (files saved before 0.5.1). Needed for the family check.
        reference_model: the reference judge's model id, likewise.
        fail_at: first severity that counts as a failure in the pass/fail
            view. ``"low"`` matches :attr:`AuditResults.failed`.
        allow_same_family: skip the refusal when subject and reference judges
            share a family. For experiments only; the report still names both.

    Returns:
        A :class:`CalibrationReport`. ``summary()`` prints it, ``to_dict()``
        and ``save()`` keep it, ``leaderboard()`` orders several judges.
    """
    if fail_at not in _LADDER_INDEX or fail_at == "pass":
        raise ValueError(f"fail_at must be one of {SEVERITY_ORDER[1:]}, got {fail_at!r}")
    refs = list(reference) if isinstance(reference, (list, tuple)) else [reference]
    if not refs:
        raise ValueError("reference is empty; pass at least one labels dict, labels file or results file.")
    ref = _build_reference(refs, reference_model)
    desc, judges = _subjects(subject, judge_model)
    _check_families(judges, ref, allow_same_family)
    if not ref.consensus:
        warnings.warn("the reference holds no on-ladder labels; the report will be empty.", stacklevel=2)

    agreements: Dict[str, Agreement] = {}
    strata: Dict[str, Dict[str, Agreement]] = {}
    for label_, (model, verdicts) in judges.items():
        agreements[label_] = _agreement(label_, model, verdicts, ref.consensus, fail_at)
        if len(ref.raters) > 1:
            unanimous = {k: v for k, v in ref.consensus.items() if not ref.split[k]}
            split = {k: v for k, v in ref.consensus.items() if ref.split[k]}
            strata[label_] = {
                "unanimous": _agreement(label_, model, verdicts, unanimous, fail_at),
                "split": _agreement(label_, model, verdicts, split, fail_at),
            }
    return CalibrationReport(subject=desc, reference=ref, fail_at=fail_at, judges=agreements, strata=strata)


__all__ = [
    "MIN_ITEMS_FOR_ALPHA",
    "UNSURE",
    "Agreement",
    "CalibrationReport",
    "Rater",
    "Reference",
    "calibrate",
    "label",
    "list_calibrations",
    "load_labels",
    "model_family",
    "save_labels",
]
