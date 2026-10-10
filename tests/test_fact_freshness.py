"""stale_facts: dated facts in metadata.facts, checked against a fixed date."""

from datetime import date, datetime

import pytest

from simpleaudit.scenarios import SCENARIO_PACKS, stale_facts

FACT_KEYS = {"claim", "value", "valid_from", "verified_at", "review_by", "source_url", "source_quote"}
OPTIONAL_FACT_KEYS = {"anchors"}


def _fact(claim, review_by, **extra):
    return {"claim": claim, "value": 1, "valid_from": None, "verified_at": "2026-10-07",
            "review_by": review_by, "source_url": "https://example.org", "source_quote": "q", **extra}


def _packs(*facts):
    return {"p": [{"name": "S - One", "metadata": {"facts": list(facts)}}]}


PACKS = _packs(_fact("early", "2027-01-01"), _fact("late", "2027-05-01"))


def test_nothing_is_stale_before_every_review_by():
    assert stale_facts(PACKS, "2026-12-31") == []


def test_a_fact_is_not_stale_on_its_review_by_date():
    assert stale_facts(PACKS, "2027-01-01") == []


def test_only_the_fact_past_its_review_by_is_returned():
    out = stale_facts(PACKS, "2027-01-02")
    assert [f["claim"] for f in out] == ["early"]
    assert out[0]["pack"] == "p" and out[0]["scenario"] == "S - One"


def test_as_of_accepts_date_and_datetime():
    assert stale_facts(PACKS, date(2027, 5, 2)) == stale_facts(PACKS, "2027-05-02")
    assert len(stale_facts(PACKS, datetime(2027, 5, 2, 12, 0))) == 2


def test_scenarios_without_facts_are_ignored():
    packs = {"q": [{"name": "A - No metadata"}, {"name": "B - Empty", "metadata": {}},
                   {"name": "C - None", "metadata": None}], **PACKS}
    assert [f["claim"] for f in stale_facts(packs, "2030-01-01")] == ["early", "late"]


def test_review_by_none_is_never_stale():
    assert stale_facts(_packs(_fact("statute", None)), "2100-01-01") == []


def test_a_scenario_in_several_packs_is_reported_once_under_the_first():
    scenarios = _packs(_fact("x", "2027-01-01"))["p"]
    out = stale_facts({"own": scenarios, "all": scenarios}, "2028-01-01")
    assert [(f["pack"], f["claim"]) for f in out] == [("own", "x")]


@pytest.mark.parametrize("bad", ["2027-13-01", "2027-02-30", "01.05.2027", "2027-5-1", "", 20270501])
def test_an_invalid_date_in_a_fact_is_a_clear_error(bad):
    with pytest.raises(ValueError, match=r"p / S - One / facts\[0\]\.review_by: .* is not a valid YYYY-MM-DD date"):
        stale_facts(_packs(_fact("x", bad)), "2027-01-01")


def test_an_invalid_valid_from_is_an_error_even_when_not_stale():
    with pytest.raises(ValueError, match=r"facts\[0\]\.valid_from"):
        stale_facts(_packs(_fact("x", "2099-01-01", valid_from="1 May 2026")), "2027-01-01")


def test_an_invalid_as_of_is_a_clear_error():
    with pytest.raises(ValueError, match=r"as_of: '2027-02-29' is not a valid YYYY-MM-DD date"):
        stale_facts(PACKS, "2027-02-29")


def test_a_missing_review_by_is_an_error_not_never_stale():
    fact = _fact("x", "2027-01-01")
    del fact["review_by"]
    with pytest.raises(ValueError, match="no review_by"):
        stale_facts(_packs(fact), "2027-01-01")


def test_built_in_facts_are_complete_and_current_on_their_verification_date():
    facts = [f for sc in SCENARIO_PACKS.values() for s in sc
             for f in (s.get("metadata") or {}).get("facts") or []]
    assert facts
    for f in facts:
        assert FACT_KEYS <= set(f) <= FACT_KEYS | OPTIONAL_FACT_KEYS, f
        assert all(isinstance(a, str) and a.strip() for a in f.get("anchors", [])), f
        assert f["source_url"].startswith("https://"), f
    assert stale_facts(SCENARIO_PACKS, "2026-10-07") == []


def test_the_g_derived_facts_in_nav_aap_are_stale_after_the_next_regulation():
    out = stale_facts(SCENARIO_PACKS, "2027-05-02")
    g = {f["value"] for f in out if f["pack"] == "nav_aap" and f["review_by"] == "2027-05-01"}
    assert g == {136549, 819294, 278697, 185798}
