"""
Tests for the pure verdict statistics in :mod:`simpleaudit.stats`.

Nothing here reaches a model or a result store — the functions take plain
counts. The values are pinned against the standard closed forms so a
regression in the formula is caught, not just a regression in the plumbing.
"""

import math

import pytest

from simpleaudit.stats import DEFAULT_Z, two_proportion_z, wilson_interval


# ---------------------------------------------------------------------------
# wilson_interval
# ---------------------------------------------------------------------------

def test_wilson_zero_trials_is_the_full_range():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_wilson_all_pass_is_tight_near_one():
    lo, hi = wilson_interval(10, 10)
    assert lo > 0.7
    assert hi == 1.0


def test_wilson_all_fail_is_tight_near_zero():
    lo, hi = wilson_interval(0, 10)
    assert lo == 0.0
    assert hi < 0.3


def test_wilson_matches_closed_form():
    k, n, z = 7, 20, 1.96
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    assert wilson_interval(k, n, z) == (max(0.0, centre - half), min(1.0, centre + half))


def test_wilson_wider_z_gives_wider_interval():
    lo90, hi90 = wilson_interval(5, 10, z=1.645)
    lo95, hi95 = wilson_interval(5, 10, z=1.96)
    assert (hi95 - lo95) > (hi90 - lo90)


def test_wilson_default_z_is_95():
    assert DEFAULT_Z == pytest.approx(1.96)
    assert wilson_interval(5, 10) == wilson_interval(5, 10, z=1.96)


def test_wilson_interval_is_ordered_and_bounded():
    for k in range(11):
        lo, hi = wilson_interval(k, 10)
        assert 0.0 <= lo <= hi <= 1.0


# ---------------------------------------------------------------------------
# two_proportion_z
# ---------------------------------------------------------------------------

def test_two_proportion_z_zero_group_is_none():
    assert two_proportion_z(3, 0, 4, 10) is None
    assert two_proportion_z(3, 10, 4, 0) is None


def test_two_proportion_z_identical_groups_is_zero():
    assert two_proportion_z(5, 10, 5, 10) == pytest.approx(0.0)


def test_two_proportion_z_perfect_agreement_is_none():
    """Both groups unanimous in the same direction -> pooled SE is 0 -> None."""
    assert two_proportion_z(10, 10, 10, 10) is None


def test_two_proportion_z_sign_follows_direction():
    # p2 > p1 -> positive; p2 < p1 -> negative.
    assert two_proportion_z(2, 10, 8, 10) > 0
    assert two_proportion_z(8, 10, 2, 10) < 0


def test_two_proportion_z_matches_closed_form():
    k1, n1, k2, n2 = 2, 10, 8, 10
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    expected = (k2 / n2 - k1 / n1) / se
    assert two_proportion_z(k1, n1, k2, n2) == pytest.approx(expected)


# ---------------------------------------------------------------------------
# krippendorff_alpha
# ---------------------------------------------------------------------------

from simpleaudit.stats import krippendorff_alpha  # noqa: E402

# Krippendorff (2011), "Computing Krippendorff's Alpha-Reliability": four
# observers, twelve units, with missing values. The published coefficients
# are the fixture.
_A = [1, 2, 3, 3, 2, 1, 4, 1, 2, None, None, None]
_B = [1, 2, 3, 3, 2, 2, 4, 1, 2, 5, None, 3]
_C = [None, 3, 3, 3, 2, 3, 4, 2, 2, 5, 1, None]
_D = [1, 2, 3, 3, 2, 4, 4, 1, 2, 5, 1, None]
_UNITS = [list(t) for t in zip(_A, _B, _C, _D)]


@pytest.mark.parametrize("metric, expected", [("nominal", 0.743), ("ordinal", 0.815), ("interval", 0.849)])
def test_alpha_reproduces_the_published_example(metric, expected):
    assert krippendorff_alpha(_UNITS, metric, values=[1, 2, 3, 4, 5]) == pytest.approx(expected, abs=0.0005)


def test_alpha_default_values_are_the_sorted_observed_set():
    assert krippendorff_alpha(_UNITS, "ordinal") == pytest.approx(0.815, abs=0.0005)


def test_alpha_on_a_string_ladder_uses_the_given_order():
    ladder = ["pass", "low", "medium", "high", "critical"]
    units = [["pass", "low"], ["high", "critical"], ["medium", "medium"], ["low", "pass"], ["critical", "high"]]
    ordinal = krippendorff_alpha(units, "ordinal", ladder)
    nominal = krippendorff_alpha(units, "nominal", ladder)
    assert nominal < ordinal          # the disagreements are all one step apart
    with pytest.raises(ValueError, match="not in values"):
        krippendorff_alpha(units, "ordinal", ["pass", "fail"])


def test_alpha_perfect_agreement_is_one():
    units = [["a", "a"], ["b", "b"], ["c", "c", "c"]]
    assert krippendorff_alpha(units, "nominal") == pytest.approx(1.0)


def test_alpha_is_undefined_without_pairs_or_variation():
    assert krippendorff_alpha([[1], [2, None], []], "nominal") is None
    assert krippendorff_alpha([[1, 1], [1, 1]], "nominal") is None


def test_alpha_rejects_unknown_metric():
    with pytest.raises(ValueError):
        krippendorff_alpha(_UNITS, "ratio")


# ---------------------------------------------------------------------------
# gwet_ac
# ---------------------------------------------------------------------------

from simpleaudit.stats import gwet_ac  # noqa: E402

LADDER = ["pass", "low", "medium", "high", "critical"]


def test_gwet_two_raters_matches_the_closed_form():
    import random
    rng = random.Random(1)
    q = 5
    pairs = [(rng.randrange(q), rng.randrange(q)) for _ in range(200)]
    for weights in ("identity", "linear", "quadratic"):
        def w(k, m):
            if weights == "identity":
                return 1.0 if k == m else 0.0
            gap = abs(k - m) / (q - 1)
            return 1 - gap if weights == "linear" else 1 - gap * gap
        p_a = sum(w(a, b) for a, b in pairs) / len(pairs)
        pi = [(sum(a == k for a, _ in pairs) + sum(b == k for _, b in pairs)) / (2 * len(pairs)) for k in range(q)]
        t_w = sum(w(k, m) for k in range(q) for m in range(q))
        p_e = t_w / (q * (q - 1)) * sum(x * (1 - x) for x in pi)
        expected = (p_a - p_e) / (1 - p_e)
        assert gwet_ac([[a, b] for a, b in pairs], weights, list(range(q))) == pytest.approx(expected)


def test_gwet_ac1_with_uniform_marginals_is_bennetts_s():
    q = 5
    pairs = [(k, k) for k in range(q)] * 10 + [(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)]
    p_a = sum(a == b for a, b in pairs) / len(pairs)
    assert gwet_ac([[a, b] for a, b in pairs], "identity", list(range(q))) == pytest.approx((p_a - 1 / q) / (1 - 1 / q))


def test_gwet_stays_high_where_alpha_collapses_under_skew():
    # 98 pass / 2 low, judge matches 97: alpha 0.39 (Feinstein & Cicchetti's paradox), AC2 0.99
    ref = ["pass"] * 98 + ["low"] * 2
    judge = ["pass"] * 96 + ["low"] * 2 + ["pass"] + ["low"]
    units = [[r, j] for r, j in zip(ref, judge)]
    assert krippendorff_alpha(units, "ordinal", LADDER) == pytest.approx(0.388, abs=0.001)
    assert gwet_ac(units, "linear", LADDER) > 0.98
    assert gwet_ac(units, "identity", LADDER) == pytest.approx(0.97, abs=0.001)


def test_gwet_perfect_undefined_and_missing():
    assert gwet_ac([[1, 1], [2, 2], [3, 3]]) == pytest.approx(1.0)
    assert gwet_ac([[1, 1], [1, 1]]) is None            # one value everywhere
    assert gwet_ac([[1], [2, None]]) is None            # no item with two ratings
    three = gwet_ac([[1, 1, None], [2, 2, 2], [1, 2, None], [3, None, 3]], "linear", [1, 2, 3])
    assert 0 < three < 1
    with pytest.raises(ValueError):
        gwet_ac([[1, 2]], "ordinal")
    with pytest.raises(ValueError, match="not in values"):
        gwet_ac([["a", "b"]], "linear", ["a"])
