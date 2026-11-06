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
import numbers
from typing import Any, Dict, Optional, Sequence, Tuple

#: z-value for a two-sided 95% interval / test (the common default).
DEFAULT_Z = 1.96


def _check_counts(k: int, n: int) -> None:
    # Successes must lie within the trials (an empty group: k == 0).
    if n < 0 or not 0 <= k <= n:
        raise ValueError(f"need 0 <= successes <= trials, got k={k}, n={n}")


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
    _check_counts(k, n)
    # A non-positive z would invert the interval (lower > upper) or collapse it.
    # Any real number type (numpy, Decimal) is taken as float; bools and
    # strings are not z values.
    try:
        z_value = float(z) if isinstance(z, numbers.Number) and not isinstance(z, (bool, complex)) else None
    except (TypeError, ValueError):
        z_value = None
    if z_value is None or not math.isfinite(z_value) or z_value <= 0:
        raise ValueError(f"z must be a finite number > 0, got {z!r}")
    z = z_value
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
        return None  # an empty group: undefined (pinned contract)
    _check_counts(k1, n1)
    _check_counts(k2, n2)
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
    with missing ratings, and has a metric for ordinal scales, which Cohen's
    kappa (two raters, nominal, no gaps) does not. Like every kappa-type
    coefficient it drops when one category dominates, even at near-perfect
    agreement; see :func:`gwet_ac` for the companion that does not.

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


def gwet_ac(
    units: Sequence[Sequence[Any]],
    weights: str = "identity",
    values: Optional[Sequence[Any]] = None,
) -> Optional[float]:
    """Gwet's AC1 (identity weights) or AC2 (weighted): agreement that stays
    readable when one category dominates.

    Kappa-type coefficients, Krippendorff's alpha among them, correct for
    chance using the observed category frequencies. When nearly every item
    sits in one category that correction eats the agreement: two raters who
    match on 97 of 100 items, 98 of which are ``pass``, can score an alpha
    below 0.4 (Feinstein & Cicchetti 1990). Gwet's coefficient estimates the
    chance of agreement from how evenly the categories are used rather than
    from their marginals, so it reads 0.9 or more in that case (Gwet 2008).
    Report it next to alpha, not instead of it: alpha is the stricter
    figure when the categories are balanced.

    Args:
        units: as in :func:`krippendorff_alpha`: one inner sequence per item,
            one entry per rater, ``None`` for a missing rating.
        weights: ``"identity"`` (AC1: same or different), ``"linear"`` or
            ``"quadratic"`` (AC2: partial credit for a near miss, by distance
            on the ordered *values*).
        values: the ordered category list; required for the weighted forms
            when the categories are strings. Defaults to the sorted observed set.

    Returns:
        The coefficient, or ``None`` when no item has two ratings or the
        expected agreement is 1 (every rating is the same value).
    """
    if weights not in ("identity", "linear", "quadratic"):
        raise ValueError(f"weights must be identity, linear or quadratic, got {weights!r}")
    rated = [[v for v in unit if v is not None] for unit in units]
    rated = [unit for unit in rated if unit]
    if not rated:
        return None
    if values is None:
        values = sorted({v for unit in rated for v in unit})
    values = list(values)
    q = len(values)
    index = {c: i for i, c in enumerate(values)}
    unknown = {v for unit in rated for v in unit if v not in index}
    if unknown:
        raise ValueError(f"ratings {sorted(map(str, unknown))} are not in values {values}")

    def weight(k: int, m: int) -> float:
        if weights == "identity":
            return 1.0 if k == m else 0.0
        if q < 2:
            return 1.0
        gap = abs(k - m) / (q - 1)
        return 1.0 - gap if weights == "linear" else 1.0 - gap * gap

    w = [[weight(k, m) for m in range(q)] for k in range(q)]
    total_weight = sum(sum(row) for row in w)

    pi = [0.0] * q
    p_a_sum = 0.0
    n_pairable = 0
    for unit in rated:
        r = len(unit)
        counts = [0] * q
        for v in unit:
            counts[index[v]] += 1
        for k in range(q):
            pi[k] += counts[k] / r
        if r < 2:
            continue
        n_pairable += 1
        weighted = [sum(w[k][m] * counts[m] for m in range(q)) for k in range(q)]
        p_a_sum += sum(counts[k] * (weighted[k] - 1) for k in range(q)) / (r * (r - 1))
    if n_pairable == 0:
        return None
    p_a = p_a_sum / n_pairable
    pi = [x / len(rated) for x in pi]
    p_e = total_weight / (q * (q - 1)) * sum(x * (1 - x) for x in pi) if q > 1 else 1.0
    if p_e >= 1:
        return None
    return (p_a - p_e) / (1 - p_e)
