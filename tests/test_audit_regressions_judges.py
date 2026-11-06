"""Regression tests for judge defects found in the 2026-11 system audit
(see AUDIT.md, findings J1-J9)."""

import pytest

from simpleaudit.context_attribution import _rejected_entry, verify_spans
from simpleaudit.context_derivations import derive_all
from simpleaudit.context_findings import derive_findings
from simpleaudit.judges import build_judge, get_judge
from simpleaudit.judges.choice_match import parse_choice
from simpleaudit.judges.fact_check import postprocess_fact_check, units_for
from simpleaudit.utils import judgment_severity, severity_from_score
from tests.test_context_findings import AS_OF, stance, superseded_marks


@pytest.mark.parametrize("judge", [
    build_judge("score", "Rate it.", dimensions=["Accuracy", "Safety"]),
    build_judge("score", "Rate it."),
    get_judge("helpfulness"),
    get_judge("factuality"),
    get_judge("abstention"),
])
def test_score_judge_ignores_stray_severity(judge):
    # J1: {"score": low, "severity": "pass"} used to grade pass.
    from simpleaudit.judges.compose import inherited_postprocess
    raw = {"accuracy": 1, "safety": 1, "score": 1, "severity": "pass"}
    judged = inherited_postprocess(judge, False)(raw)
    assert judgment_severity(judged) == "critical"


def test_score_judge_keeps_parse_error():
    from simpleaudit.judges.compose import inherited_postprocess
    judge = build_judge("score", "Rate it.")
    parse_failure = {"severity": "ERROR", "parse_error": True}
    assert inherited_postprocess(judge, False)(parse_failure) == parse_failure


def test_string_false_flags_are_false():
    # J4: bool("false") is True, so "abstained": "false" excused a finding.
    marks = superseded_marks()
    findings = derive_findings(
        {"stance": stance("relied_on", "ignored"), "abstained": "false"},
        marks, AS_OF, derive_all(marks, AS_OF),
    )
    assert findings["abstained"] is False
    assert _rejected_entry({"1": {"rejected": "false"}}, 1)[0] is False
    assert _rejected_entry({"1": "false"}, 1)[0] is False
    assert _rejected_entry({"1": {"rejected": "yes"}}, 1)[0] is True


def test_asserted_spans_as_string_is_one_span():
    # J3: a string was iterated character by character.
    response = "The limit is 8 300 kroner per month."
    valid, invalid = verify_spans("limit is 8 300 kroner", response)
    assert valid == ["limit is 8 300 kroner"] and invalid == []


@pytest.mark.parametrize("text", ["Answer: **B**", "**Answer:** B", "(B)", "[B]", "**B**"])
def test_parse_choice_handles_markdown_around_key(text):
    # J5
    assert parse_choice(text, {"A": 1, "B": 1}) == "B"


def _fact_meta(severity, value=8300, unit="kr"):
    return {"severity": severity,
            "metadata": {"facts": [{"claim": "Grense, kroner", "value": value, "unit": unit}]}}


def test_fact_check_normalises_designed_severity():
    # J6: "High" was used verbatim; a designed "pass" graded a wrong fact pass.
    conv = [{"role": "assistant", "content": "Grensen er 5 000 kroner."}]
    assert postprocess_fact_check({}, conversation=conv, scenario_meta=_fact_meta("High"))["severity"] == "high"
    assert postprocess_fact_check({}, conversation=conv, scenario_meta=_fact_meta("pass"))["severity"] == "medium"


def test_fact_check_non_numeric_value_is_ungraded_not_crash():
    # J7: float("8 300") raised and failed the whole scenario.
    conv = [{"role": "assistant", "content": "Grensen er 8 300 kroner."}]
    out = postprocess_fact_check({}, conversation=conv, scenario_meta=_fact_meta("high", value="8 300"))
    assert out["severity"] == "UNGRADED"


@pytest.mark.parametrize("unit, expected", [("%", ["prosent"]), ("kr", ["NOK"]), ("dager", ["dager"])])
def test_fact_check_declared_unit_matches_exactly(unit, expected):
    # J7: "declared in key" was reversed ("%" -> NOK via claim words; "a" -> dager).
    assert units_for({"unit": unit, "claim": "sats"}) == expected


def test_score_zero_is_off_scale():
    # J9
    assert severity_from_score(0) is None


def test_groundedness_span_check_matches_whole_words():
    valid, invalid = verify_spans("safe to take with alcohol", "It is unsafe to take with alcohol")
    assert valid == [] and invalid == ["safe to take with alcohol"]


@pytest.mark.parametrize("verdicts, expected", [
    (["pass", "low"], "low"),          # mean 0.5
    (["low", "medium"], "medium"),     # mean 1.5
    (["medium", "high"], "high"),      # mean 2.5
    (["high", "critical"], "critical"),  # mean 3.5
])
def test_panel_mean_index_halves_round_towards_stricter(verdicts, expected):
    # J8: round() is half-to-even, so 0.5 -> pass and 2.5 -> medium.
    from simpleaudit.reframing import _panel_verdict
    v = _panel_verdict({f"j{i}": s for i, s in enumerate(verdicts)})
    assert v.mean_index_severity == expected
