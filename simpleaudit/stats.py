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
from typing import Optional, Tuple

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
