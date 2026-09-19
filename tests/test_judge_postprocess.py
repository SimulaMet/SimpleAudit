"""
Tests for the judge post-processing hook and the expectation-based fallback.

A judge config may declare `postprocess` (applied to the parsed judge output)
and `requires_expected_behavior` (scenarios without expectations go to the
default judge). Both must work identically on the audit path and on the
judge-only paths, and must leave every existing judge untouched.
"""

import asyncio
import dataclasses
import json
import warnings
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.checklist import postprocess_checklist
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult, AuditResults
from tests.fakes import (
    FakeClient,
    _make_judge_json,
    fixed_severity_judge,
    fixed_target,
    make_auditor,
)

CONVERSATION = [
    {"role": "user", "content": "Hei"},
    {"role": "assistant", "content": "Hovedregelen er 3 år. Sjekk vedtaket ditt."},
]


def _spy(returning=None):
    calls = []

    def postprocess(judgment, **kwargs):
        calls.append((judgment, kwargs))
        return returning if returning is not None else judgment

    postprocess.calls = calls
    return postprocess


def _judge_call(client, **kwargs):
    return asyncio.run(
        ModelAuditor._judge_conversation_async(client, "fake-judge", "desc", CONVERSATION, ["e1"], **kwargs)
    )


# ---------------------------------------------------------------------------
# The hook itself
# ---------------------------------------------------------------------------

def test_postprocess_receives_conversation_expectations_and_meta_on_the_custom_prompt_path():
    spy = _spy(returning={"severity": "high", "summary": "derived"})
    judgment, _, _ = _judge_call(
        FakeClient(lambda **_: _make_judge_json("low")),
        judge_prompt="CUSTOM", postprocess=spy, scenario_meta={"severity": "high"},
    )

    assert judgment == {"severity": "high", "summary": "derived"}
    (raw, kwargs), = spy.calls
    assert raw["severity"] == "low"
    assert kwargs == {"conversation": CONVERSATION, "expected_behavior": ["e1"],
                      "scenario_meta": {"severity": "high"}}


def test_postprocess_also_applies_on_the_default_judge_path():
    spy = _spy()
    judgment, _, _ = _judge_call(FakeClient(lambda **_: _make_judge_json("medium")), postprocess=spy)
    assert judgment["severity"] == "medium"
    assert len(spy.calls) == 1


def test_without_postprocess_the_output_is_unchanged():
    client = FakeClient(lambda **_: _make_judge_json("low"))
    plain, _, _ = _judge_call(client, judge_prompt="CUSTOM")
    assert plain == json.loads(_make_judge_json("low"))


def test_postprocess_sees_the_error_dict_on_parse_failure():
    spy = _spy()
    judgment, _, _ = _judge_call(FakeClient(lambda **_: "not json"), judge_prompt="CUSTOM", postprocess=spy)
    assert judgment["severity"] == "ERROR"
    assert spy.calls[0][0]["severity"] == "ERROR"


# ---------------------------------------------------------------------------
# ModelAuditor wiring
# ---------------------------------------------------------------------------

def test_auditor_stores_name_config_and_hooks_from_the_registry():
    auditor = make_auditor(fixed_target("x"), fixed_severity_judge("pass"), judge_name="checklist")
    assert auditor.judge_name == "checklist"
    assert auditor.judge_config["name"] == "Evidence-anchored Checklist"
    assert auditor.judge_postprocess is postprocess_checklist
    assert auditor.judge_requires_expected_behavior is True

    plain = make_auditor(fixed_target("x"), fixed_severity_judge("pass"))
    assert plain.judge_name is None
    assert plain.judge_config is None
    assert plain.judge_postprocess is None
    assert plain.judge_requires_expected_behavior is False


def test_explicit_judge_postprocess_overrides_the_config():
    spy = _spy()
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        auditor = ModelAuditor(model="m", provider="openai", judge_model="j", judge_provider="openai",
                               judge="checklist", judge_postprocess=spy)
    assert auditor.judge_postprocess is spy


def test_existing_judges_have_no_hooks():
    for name in ("safety", "abstention", "helpfulness", "factuality", "harm", "binary_abstention"):
        auditor = make_auditor(fixed_target("x"), fixed_severity_judge("pass"), judge_name=name)
        assert auditor.judge_postprocess is None
        assert auditor.judge_requires_expected_behavior is False


def _checklist_json(*statuses):
    return json.dumps({
        "checklist": [
            {"index": i, "expectation": f"e{i}", "status": status, "evidence_kind": "direct",
             "quote": "Hovedregelen er 3 år"}
            for i, status in enumerate(statuses, 1)
        ],
        "notes": "n",
    })


def test_run_async_threads_the_designed_severity_and_keeps_the_saved_format(tmp_path):
    auditor = make_auditor(
        fixed_target("Hovedregelen er 3 år. Sjekk vedtaket ditt."),
        FakeClient(lambda **_: _checklist_json("met", "violated")),
        judge_name="checklist",
    )
    scenario = {"name": "S", "description": "d", "test_prompt": "p",
                "expected_behavior": ["e1", "e2"], "severity": "high"}

    results = asyncio.run(auditor.run_async([scenario], max_turns=1))
    result = results[0]

    assert result.severity == "high"
    assert result.judgment["designed_severity"] == "high"
    assert result.judgment["designed_severity_source"] == "scenario"
    assert "judge_fallback" not in result.judgment

    payload = results.to_dict()
    saved_keys = set(payload["results"][0])
    assert saved_keys == {f.name for f in dataclasses.fields(AuditResult)}

    path = tmp_path / "run.json"
    results.save(str(path))
    loaded = AuditResults.load(str(path))
    assert loaded[0].judgment["checklist"][1]["status"] == "violated"
    assert loaded[0].judgment["designed_severity"] == "high"


def test_scenario_without_expectations_falls_back_to_the_default_judge_with_one_warning():
    prompts = []

    def judge(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _make_judge_json("low")

    auditor = make_auditor(fixed_target("svar"), FakeClient(judge), judge_name="checklist")
    # test_prompt keeps the probe generator (which shares the fake client) out
    # of the captured prompts, so every captured prompt is a judge prompt.
    scenarios = [{"name": "A", "description": "only a description", "test_prompt": "p"},
                 {"name": "B", "description": "another", "test_prompt": "p"}]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        results = asyncio.run(auditor.run_async(scenarios, max_turns=1))

    fallback_warnings = [w for w in caught if "graded by the default judge" in str(w.message)]
    assert len(fallback_warnings) == 1
    assert all(r.severity == "low" for r in results)
    assert all(r.judgment["judge_fallback"] == "default" for r in results)
    assert all("safety evaluator" in p and "evidence checker" not in p for p in prompts)


def test_scenario_with_expectations_is_not_a_fallback():
    prompts = []

    def judge(**kwargs):
        prompts.append(kwargs["messages"][0]["content"])
        return _checklist_json("met")

    auditor = make_auditor(fixed_target("Hovedregelen er 3 år"), FakeClient(judge), judge_name="checklist")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        results = asyncio.run(auditor.run_async(
            [{"name": "A", "description": "d", "test_prompt": "p", "expected_behavior": ["e1"]}], max_turns=1))
    assert "evidence checker" in prompts[0]
    assert results[0].severity == "pass"
    assert results[0].judgment["designed_severity_source"] == "default"


def test_resolve_judge_spec_only_falls_back_when_required_and_empty():
    spec = ModelAuditor._resolve_judge_spec("P", {"s": 1}, postprocess_checklist, True, None)
    assert spec == (None, None, None, True)
    spec = ModelAuditor._resolve_judge_spec("P", {"s": 1}, postprocess_checklist, True, ["e"])
    assert spec == ("P", {"s": 1}, postprocess_checklist, False)
    spec = ModelAuditor._resolve_judge_spec("P", None, None, False, None)
    assert spec == ("P", None, None, False)


@pytest.mark.parametrize("severity", ["pass", "low", "medium", "high", "critical"])
def test_default_judge_results_are_unchanged_by_the_hook_machinery(severity):
    auditor = make_auditor(fixed_target("x"), fixed_severity_judge(severity))
    results = asyncio.run(auditor.run_async(
        [{"name": "A", "description": "d", "test_prompt": "p", "severity": "high"}], max_turns=1))
    assert results[0].severity == severity
    assert "designed_severity" not in results[0].judgment
    assert "judge_fallback" not in results[0].judgment
