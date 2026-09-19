"""
Offline end-to-end run of the checklist judge over a real stored file.

Uses the nav_aap transcripts shipped in examples/ and a fake checklist judge
that quotes real spans from the rendered transcript. Exercises the loader with
the designed-severity lookup, prompt rendering, quote verification, turn
localisation, derivation, flattening, perturbations, rejudge and save/load
together, with no network.
"""

import json
import re
from pathlib import Path

import pytest

from simpleaudit import (
    AuditResults,
    PromptVariant,
    get_judge,
    get_scenarios,
    load_stored_records,
    perturbation_variants,
    reframing_check,
    rejudge,
    severity_by_name,
)
from simpleaudit.checklist import is_optional, step_below
from tests.fakes import FakeClient

STORED = Path(__file__).resolve().parent.parent / "examples" / "nav_aap" / "nav_aap_sonnet_4_6.json"

pytestmark = pytest.mark.skipif(not STORED.exists(), reason="stored nav_aap run not present")

_EXPECTATION_LINE = re.compile(r"^(\d+)\. (.+)$", re.MULTILINE)
_ASSISTANT_SEGMENT = re.compile(r"ASSISTANT:\n(.*?)(?=\n\nUSER:\n|\n\nSCENARIO EXPECTATIONS:|\Z)", re.DOTALL)
_NEGATIVE = re.compile(r"^\s*NOT\b")


def _fake_checklist_judge():
    """Met for every item except NOT items, which it marks violated.

    Quotes are the first eight words of a real assistant segment in the
    rendered transcript, so they verify against whatever the judge was shown,
    perturbed or not.
    """
    def respond(**kwargs):
        user = kwargs["messages"][1]["content"]
        expectations_block = user.split("SCENARIO EXPECTATIONS:", 1)[1]
        expectations = _EXPECTATION_LINE.findall(expectations_block)
        segments = _ASSISTANT_SEGMENT.findall(user)
        first_words = " ".join(segments[0].split()[:8])
        last_words = " ".join(segments[-1].split()[:8])
        items = []
        for number, text in expectations:
            negative = _NEGATIVE.match(text) is not None
            items.append({
                "index": int(number),
                "expectation": text,
                "status": "violated" if negative else "met",
                "evidence_kind": "direct",
                "quote": first_words if negative else last_words,
            })
        return json.dumps({"checklist": items, "notes": "fake"})

    return FakeClient(respond)


def _expected_severity(expectations, designed):
    required = [e for e in expectations if not is_optional(e)]
    violated = [e for e in required if _NEGATIVE.match(e)]
    if not violated:
        return "pass"
    fraction = len(violated) / len(required)
    return designed if fraction >= 0.5 else step_below(designed)


def test_checklist_pipeline_over_the_stored_nav_aap_run(tmp_path):
    lookup = severity_by_name(get_scenarios("nav_aap"))
    records = load_stored_records(STORED, scenario_severities=lookup)
    assert len(records) == 15
    assert all(r.scenario_severity in ("low", "medium", "high", "critical") for r in records)
    assert all(r.scenario_severity_source == "lookup" for r in records)

    config = get_judge("checklist")
    judge = _fake_checklist_judge()

    # Perturbations with the checklist hooks: every quote verifies, nothing flips.
    variants = perturbation_variants(
        config["judge_prompt"], language="Norwegian", response_schema=config["response_schema"],
        postprocess=config["postprocess"], requires_expected_behavior=True,
    )
    results = reframing_check(judge, "fake-judge", records, variants, max_concurrency=4)

    for record in records:
        expected = _expected_severity(record.expected_behavior, record.scenario_severity)
        assert results.per_scenario[record.scenario_name]["baseline"] == expected, record.scenario_name
    baseline_items = [
        item
        for judgment in (results.judgments[r.scenario_name]["baseline"] for r in records)
        for item in judgment["checklist"]
    ]
    assert baseline_items
    assert all(item["verified"] is True and item["turn"] is not None for item in baseline_items)
    assert all(results.judgments[r.scenario_name]["baseline"]["evidence_complete"] for r in records)
    assert all(effect.flip_rate == 0.0 for effect in results.effects().values())
    json.dumps(results.to_dict())

    # Resampling a deterministic judge is stable.
    resample = reframing_check(judge, "fake-judge", records, [PromptVariant.from_judge("checklist")], k=2)
    assert resample.fragile() == {}

    # Re-judge the saved run under the checklist judge; alignment and format hold.
    original = AuditResults.load(str(STORED))
    rejudged = rejudge(original, judge, "fake-judge", judge="checklist", scenario_severities=lookup)
    assert [r.scenario_name for r in rejudged] == [r.scenario_name for r in original]
    assert all(r.judgment["designed_severity_source"] == "lookup" for r in rejudged)
    assert all(a.conversation == b.conversation for a, b in zip(original, rejudged, strict=True))
    saved = tmp_path / "rejudged.json"
    rejudged.save(str(saved))
    loaded = AuditResults.load(str(saved))
    assert set(loaded.to_dict()["results"][0]) == set(original.to_dict()["results"][0])
    assert loaded[0].judgment["checklist"][0]["verified"] is True
