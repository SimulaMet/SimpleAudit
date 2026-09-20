"""
Offline end-to-end run of the judge-robustness checks over a real stored file.

Uses the nav_aap transcripts shipped in examples/ and a fake judge, so the
loader, the Norwegian perturbations, the engine, the accessors and rejudge are
exercised together on real data with no network.
"""

import json
from pathlib import Path

import pytest

from simpleaudit import (
    AuditResults,
    PromptVariant,
    RepeatedExperimentResults,
    compare_judges,
    get_judge,
    load_stored_records,
    perturbation_variants,
    reframing_check,
    rejudge,
)
from simpleaudit.perturbations import _TEXTS
from tests.fakes import FakeClient, _make_judge_json

STORED = Path(__file__).resolve().parent.parent / "examples" / "nav_aap" / "nav_aap_sonnet_4_6.json"

pytestmark = pytest.mark.skipif(not STORED.exists(), reason="stored nav_aap run not present")


def _artifact_sensitive_judge():
    """A judge that grades lenient when it sees an apology and strict on an authority claim.

    Stands in for the artifact sensitivity the literature reports, so the
    pipeline has something to detect.
    """
    apology = _TEXTS["apologetic_opener"]["Norwegian"][0].strip()
    authority = _TEXTS["authority_claim"]["Norwegian"][0].strip()

    def respond(**kwargs):
        rendered = kwargs["messages"][1]["content"]
        if apology in rendered:
            return _make_judge_json("pass")
        if authority in rendered:
            return _make_judge_json("high")
        return _make_judge_json("low")

    return FakeClient(respond)


def test_pipeline_over_the_stored_nav_aap_run():
    records = load_stored_records(STORED)
    assert len(records) == 15
    assert all(r.expected_behavior for r in records)

    base = get_judge("safety")["judge_prompt"]
    judge = _artifact_sensitive_judge()

    # D. perturbations in the transcript's language
    results = reframing_check(
        judge, "fake-judge", records, perturbation_variants(base, language="Norwegian"),
        max_concurrency=4,
    )
    effects = results.effects()
    assert effects["apologetic_opener"].flip_rate == 1.0
    assert effects["apologetic_opener"].net == "lenient"
    assert effects["authority_claim"].flip_rate == 1.0
    assert effects["authority_claim"].net == "stricter"
    assert effects["hedging_disclaimer"].flip_rate == 0.0
    assert effects["verbose_padding"].flip_rate == 0.0
    assert effects["self_certification"].flip_rate == 0.0
    assert results.per_scenario[records[0].scenario_name]["baseline"] == "low"
    json.dumps(results.to_dict())

    # C. resampling: a deterministic judge is perfectly stable
    resample = reframing_check(judge, "fake-judge", records, [PromptVariant("safety", base)], k=3)
    assert resample.fragile() == {}
    assert all(cell["safety"].agreement_rate == 1.0 for cell in resample.stability().values())

    # E. rejudge the saved run and compare with the stored verdicts
    original = AuditResults.load(str(STORED))
    rejudged = rejudge(original, judge, "fake-judge", judge_prompt=base)
    assert [r.scenario_name for r in rejudged] == [r.scenario_name for r in original]
    assert all(r.severity == "low" for r in rejudged)
    assert all(a.conversation == b.conversation for a, b in zip(original, rejudged, strict=True))
    comparison = compare_judges(
        RepeatedExperimentResults({"s": [original]}),
        RepeatedExperimentResults({"s": [rejudged]}),
        subject_label="s",
    )
    assert comparison["n_total"] == 15
