"""
Pure statistics over audit verdicts.

These are the small, dependency-free building blocks that turn a count of
passing trials into something interpretable: a confidence interval on a pass
rate, and a test of whether two pass rates differ. They operate on plain
counts (``k`` successes out of ``n`` trials) — no runs, results, or storage —
so they can be shared by the in-process engine, a CLI, and out-of-process
callers (e.g. a web studio that tracks a recurring audit over time) without
each re-implementing the formula.

They are deliberately the *statistics only*. Deciding which trials count as
pass / fail / error, and how to group runs into a baseline, is the caller's
domain logic and stays out of here.
"""

import math
from typing import Any, Dict, Optional, Sequence, Tuple

#: z-value for a two-sided 95% interval / test (the common default).
DEFAULT_Z = 1.96


def wilson_interval(k: int, n: int, z: float = DEFAULT_Z) -> Tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    The Wilson interval is preferred over the naive Wald interval for small
    ``n`` or proportions near 0 or 1, where the Wald interval is badly
    calibrated. This is the standard form: a centre pulled toward 0.5 by the
    ``z^2/(2n)`` term, with a half-width that shrinks as ``n`` grows.

    Args:
        k: number of successes (passing trials).
        n: total number of trials.
        z: critical value (default 1.96 ≈ 95%).

    Returns:
        ``(lower, upper)`` clamped to ``[0, 1]``. When ``n == 0`` there is no
        evidence, so the full range ``(0.0, 1.0)`` is returned rather than a
        degenerate point.
    """
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def two_proportion_z(k1: int, n1: int, k2: int, n2: int) -> Optional[float]:
    """Pooled two-proportion z statistic for ``p2 - p1``.

    Tests whether the second proportion differs from the first, pooling the
    two samples under the null hypothesis that they share one proportion. A
    large magnitude (|z| >= 1.96 for 95%) indicates a real difference rather
    than sampling noise.

    Args:
        k1, n1: successes and trials for the first (baseline) group.
        k2, n2: successes and trials for the second group.

    Returns:
        The z statistic, or ``None`` when it is undefined — either group has
        no trials, or the pooled standard error is zero (both groups agree
        perfectly, so there is no variance to test against).
    """
    if n1 == 0 or n2 == 0:
        return None
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return None
    return (k2 / n2 - k1 / n1) / se


def krippendorff_alpha(
    units: Sequence[Sequence[Any]],
    metric: str = "ordinal",
    values: Optional[Sequence[Any]] = None,
) -> Optional[float]:
    """Krippendorff's alpha: chance-corrected agreement for any number of raters.

    ``alpha = 1 - D_o / D_e``: observed disagreement over the disagreement
    expected by chance. 1 is perfect agreement, 0 is what chance would give,
    negative is systematic disagreement. It takes any number of raters, copes
    with missing ratings, and has a metric for ordinal scales, which is why it
    is the headline figure for judge-versus-human agreement rather than
    percent agreement or Cohen's kappa (two raters, nominal, no gaps).

    Args:
        units: one inner sequence per item, holding each rater's value for
            that item; ``None`` marks a missing rating. Items with fewer than
            two ratings carry no information about agreement and are skipped.
        metric: ``"nominal"`` (same or different), ``"ordinal"`` (ranks, the
            default: right for a severity ladder) or ``"interval"`` (equally
            spaced numeric codes, by position in *values*).
        values: the ordered category list for ordinal and interval metrics.
            Defaults to the sorted set of observed values, which is right for
            numbers and wrong for a ladder like ``["pass", "low", ...]``, so
            pass it whenever the categories are strings.

    Returns:
        The coefficient, or ``None`` when it is undefined: no item has two
        ratings, or every rating is the same value (nothing to disagree
        about, so expected disagreement is zero).
    """
    if metric not in ("nominal", "ordinal", "interval"):
        raise ValueError(f"metric must be nominal, ordinal or interval, got {metric!r}")
    pairable = [[v for v in unit if v is not None] for unit in units]
    pairable = [unit for unit in pairable if len(unit) >= 2]
    if not pairable:
        return None
    if values is None:
        values = sorted({v for unit in pairable for v in unit})
    values = list(values)
    index = {c: i for i, c in enumerate(values)}
    unknown = {v for unit in pairable for v in unit if v not in index}
    if unknown:
        raise ValueError(f"ratings {sorted(map(str, unknown))} are not in values {values}")

    # Coincidence matrix: each ordered pair of ratings within an item counts
    # 1 / (m - 1), so every item contributes its number of ratings in total.
    coincidence: Dict[Tuple[Any, Any], float] = {}
    for unit in pairable:
        m = len(unit)
        for i in range(m):
            for j in range(m):
                if i != j:
                    key = (unit[i], unit[j])
                    coincidence[key] = coincidence.get(key, 0.0) + 1.0 / (m - 1)
    n_c = {c: sum(coincidence.get((c, k), 0.0) for k in values) for c in values}
    n = sum(n_c.values())

    def delta(c: Any, k: Any) -> float:
        if metric == "nominal":
            return 0.0 if c == k else 1.0
        if metric == "interval":
            return float((index[c] - index[k]) ** 2)
        lo, hi = sorted((index[c], index[k]))
        between = sum(n_c[values[g]] for g in range(lo, hi + 1))
        return (between - (n_c[c] + n_c[k]) / 2) ** 2

    d_o = sum(coincidence.get((c, k), 0.0) * delta(c, k) for c in values for k in values)
    d_e = sum(n_c[c] * n_c[k] * delta(c, k) for c in values for k in values) / (n - 1)
    if d_e == 0:
        return None
    return 1 - d_o / d_e
