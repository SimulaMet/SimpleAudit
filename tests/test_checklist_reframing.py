"""
Tests for the checklist judge on the judge-only paths.

`PromptVariant.from_judge`, `load_stored_records(scenario_severities=...)`,
`reframing_check` with a post-processing variant, panels over such variants,
and `rejudge(judge="checklist")`. No model anywhere: the judge is a fake that
returns checklist observations quoting the transcript.
"""

import json
import warnings

import pytest

from simpleaudit.checklist import postprocess_checklist
from simpleaudit.judges.checklist import CHECKLIST_JUDGE
from simpleaudit.perturbations import perturbation_variants
from simpleaudit.reframing import (
    PromptVariant,
    StoredRecord,
    load_stored_records,
    reframing_check,
    rejudge,
)
from simpleaudit.results import AuditResult, AuditResults
from tests.fakes import FakeClient, _make_judge_json

TRANSCRIPT = [
    {"role": "user", "content": "Hvor lenge kan jeg få AAP?"},
    {"role": "assistant", "content": "Hovedregelen er **3 år**. Sjekk vedtaket ditt."},
]
EXPECTATIONS = ["States the main rule", "NOT state categorically without the extension"]


def _record(name="AAP", severity=None, source=None, expectations=EXPECTATIONS):
    return StoredRecord(
        scenario_name=name,
        scenario_description="Duration",
        conversation=list(TRANSCRIPT),
        expected_behavior=expectations,
        scenario_severity=severity,
        scenario_severity_source=source,
    )


def _checklist_json(*statuses):
    return json.dumps({
        "checklist": [
            {"index": i, "expectation": EXPECTATIONS[i - 1], "status": status,
             "evidence_kind": "direct", "quote": "Hovedregelen er 3 år"}
            for i, status in enumerate(statuses, 1)
        ],
        "notes": "",
    })


def _checklist_judge(mapping=None, default=("met", "violated")):
    """Judge whose statuses may depend on the model name; default judge JSON on fallback."""
    def respond(**kwargs):
        system = kwargs["messages"][0]["content"]
        if "evidence checker" not in system:
            return _make_judge_json("low")
        statuses = (mapping or {}).get(kwargs.get("model"), default)
        return _checklist_json(*statuses)
    return FakeClient(respond)


# ---------------------------------------------------------------------------
# from_judge
# ---------------------------------------------------------------------------

def test_from_judge_carries_prompt_schema_and_hooks_and_accepts_overrides():
    variant = PromptVariant.from_judge("checklist")
    assert variant.label == "checklist"
    assert variant.judge_prompt == CHECKLIST_JUDGE["judge_prompt"]
    assert variant.response_schema is CHECKLIST_JUDGE["response_schema"]
    assert variant.postprocess is postprocess_checklist
    assert variant.requires_expected_behavior is True

    other = PromptVariant.from_judge("checklist", "sonnet", judge_model="claude-sonnet-4-6")
    assert other.label == "sonnet"
    assert other.judge_model == "claude-sonnet-4-6"

    plain = PromptVariant.from_judge("safety")
    assert plain.postprocess is None
    assert plain.requires_expected_behavior is False
    assert plain.response_schema is None


def test_positional_prompt_variant_construction_still_works():
    variant = PromptVariant("a", "PROMPT", {"type": "object"})
    assert (variant.postprocess, variant.requires_expected_behavior) == (None, False)


# ---------------------------------------------------------------------------
# Loading stored records with a designed severity
# ---------------------------------------------------------------------------

def test_load_stored_records_takes_stored_designed_severity_then_lookup_then_none():
    payload = {"results": [
        {"scenario_name": "Stored", "conversation": TRANSCRIPT,
         "judgment": {"severity": "high", "designed_severity": "high", "designed_severity_source": "scenario"}},
        {"scenario_name": "Defaulted", "conversation": TRANSCRIPT,
         "judgment": {"severity": "low", "designed_severity": "medium", "designed_severity_source": "default"}},
        {"scenario_name": "Unknown", "conversation": TRANSCRIPT, "judgment": {"severity": "pass"}},
    ]}
    records = load_stored_records(payload, scenario_severities={"Defaulted": "critical", "Stored": "low"})

    assert (records[0].scenario_severity, records[0].scenario_severity_source) == ("high", "stored")
    assert (records[1].scenario_severity, records[1].scenario_severity_source) == ("critical", "lookup")
    assert (records[2].scenario_severity, records[2].scenario_severity_source) == (None, None)
    assert records[0].scenario_meta() == {"severity": "high", "severity_source": "stored"}


def test_load_stored_records_without_lookup_is_unchanged():
    payload = {"results": [{"scenario_name": "A", "conversation": TRANSCRIPT, "expected_behavior": ["e"]}]}
    records = load_stored_records(payload)
    assert records[0].scenario_severity is None
    assert records[0].expected_behavior == ["e"]


# ---------------------------------------------------------------------------
# reframing_check with a post-processing variant
# ---------------------------------------------------------------------------

def test_reframing_check_applies_the_postprocess_and_uses_the_record_severity():
    judge = _checklist_judge({"lenient": ("met", "met")}, default=("met", "violated"))
    variants = [
        PromptVariant.from_judge("checklist", "strict"),
        PromptVariant.from_judge("checklist", "lenient", judge_model="lenient"),
    ]
    records = [_record(severity="high", source="lookup")]

    results = reframing_check(judge, "fake-judge", records, variants)

    assert results.per_scenario["AAP"] == {"strict": "high", "lenient": "pass"}
    strict = results.judgments["AAP"]["strict"]
    assert strict["designed_severity"] == "high"
    assert strict["designed_severity_source"] == "lookup"
    assert strict["fraction"] == 0.5
    assert strict["checklist"][1]["turn"] == 1
    assert results.variant_meta["strict"]["postprocess"] == "postprocess_checklist"
    assert results.effects()["lenient"].net == "lenient"


def test_panel_over_checklist_variants_works_and_rejects_a_postprocess_mismatch():
    judge = _checklist_judge({"b": ("met", "met")})
    variants = [
        PromptVariant.from_judge("checklist", "a"),
        PromptVariant.from_judge("checklist", "b", judge_model="b"),
    ]
    results = reframing_check(judge, "fake-judge", [_record(severity="medium")], variants)
    verdict = results.panel().per_scenario["AAP"]
    assert verdict.verdicts == {"a": "medium", "b": "pass"}
    assert verdict.majority == "medium"

    mismatched = [
        PromptVariant.from_judge("checklist", "a"),
        PromptVariant(
            "raw", CHECKLIST_JUDGE["judge_prompt"], CHECKLIST_JUDGE["response_schema"], judge_model="b"
        ),
    ]
    results = reframing_check(judge, "fake-judge", [_record(severity="medium")], mismatched)
    with pytest.raises(ValueError, match="postprocess"):
        results.panel()


def test_record_without_expectations_falls_back_on_the_reframing_path():
    judge = _checklist_judge()
    variants = [PromptVariant.from_judge("checklist", "a"), PromptVariant.from_judge("checklist", "b", judge_model="b")]
    records = [_record("NoItems", expectations=None)]

    results = reframing_check(judge, "fake-judge", records, variants)

    assert results.per_scenario["NoItems"] == {"a": "low", "b": "low"}
    assert results.judgments["NoItems"]["a"]["judge_fallback"] == "default"
    assert "checklist" not in results.judgments["NoItems"]["a"]


def test_perturbation_variants_forward_the_hooks():
    config = CHECKLIST_JUDGE
    variants = perturbation_variants(
        config["judge_prompt"], "Norwegian", names=["apologetic_opener"],
        response_schema=config["response_schema"], postprocess=config["postprocess"],
        requires_expected_behavior=True,
    )
    assert all(v.postprocess is postprocess_checklist for v in variants)
    assert all(v.requires_expected_behavior is True for v in variants)

    results = reframing_check(_checklist_judge(), "fake-judge", [_record(severity="high")], variants)
    # The fake quotes the untouched opening words, which the apology prefix
    # pushes later into the turn; the quote still verifies as a substring.
    assert results.per_scenario["AAP"] == {"baseline": "high", "apologetic_opener": "high"}
    assert results.effects()["apologetic_opener"].flip_rate == 0.0


# ---------------------------------------------------------------------------
# rejudge with a registry judge
# ---------------------------------------------------------------------------

def _source(with_items=True):
    def result(name, expectations):
        return AuditResult(
            scenario_name=name, scenario_description="Duration", conversation=list(TRANSCRIPT),
            severity="medium", issues_found=[], positive_behaviors=[], summary="old",
            recommendations=[], expected_behavior=expectations,
            judgment={"severity": "medium", "summary": "old"},
        )
    return AuditResults([result("One", EXPECTATIONS if with_items else None), result("Two", EXPECTATIONS)])


def test_rejudge_by_registry_name_uses_hooks_and_the_lookup():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        new = rejudge(_source(), _checklist_judge(), "fake-judge", judge="checklist",
                      scenario_severities={"One": "critical", "Two": "low"})

    assert [r.severity for r in new] == ["critical", "low"]
    assert new[0].judgment["designed_severity_source"] == "lookup"
    assert new[0].judgment["checklist"][1]["status"] == "violated"
    assert [r.scenario_name for r in new] == ["One", "Two"]


def test_rejudge_prefers_a_designed_severity_already_stored_in_the_judgment():
    source = _source()
    source.results[0].judgment = {"severity": "high", "designed_severity": "high",
                                  "designed_severity_source": "scenario"}
    new = rejudge(source, _checklist_judge(), "fake-judge", judge="checklist",
                  scenario_severities={"One": "low", "Two": "low"})
    assert new[0].judgment["designed_severity"] == "high"
    assert new[0].judgment["designed_severity_source"] == "stored"
    assert new[1].judgment["designed_severity"] == "low"


def test_rejudge_warns_when_the_ceiling_defaults():
    with pytest.warns(UserWarning, match="default designed severity"):
        new = rejudge(_source(), _checklist_judge(), "fake-judge", judge="checklist")
    assert all(r.judgment["designed_severity"] == "medium" for r in new)
    assert all(r.severity == "medium" for r in new)


def test_rejudge_falls_back_for_results_without_expectations():
    new = rejudge(_source(with_items=False), _checklist_judge(), "fake-judge", judge="checklist",
                  scenario_severities={"One": "high", "Two": "high"})
    assert new[0].severity == "low"
    assert new[0].judgment["judge_fallback"] == "default"
    assert new[1].severity == "high"


def test_rejudge_explicit_prompt_wins_over_the_registry_prompt():
    systems = []

    def respond(**kwargs):
        systems.append(kwargs["messages"][0]["content"])
        return _make_judge_json("low")

    new = rejudge(_source(), FakeClient(respond), "fake-judge", judge="checklist", judge_prompt="CUSTOM",
                  scenario_severities={"One": "high", "Two": "high"})
    assert all(s == "CUSTOM" for s in systems)
    # The registry post-processor still runs but passes a non-checklist dict through.
    assert [r.severity for r in new] == ["low", "low"]


def test_rejudge_without_judge_name_behaves_as_before():
    new = rejudge(_source(), FakeClient(lambda **_: _make_judge_json("pass")), "fake-judge")
    assert [r.severity for r in new] == ["pass", "pass"]
    assert "designed_severity" not in new[0].judgment
