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
