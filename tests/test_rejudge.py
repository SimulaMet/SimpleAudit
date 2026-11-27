"""
Tests for re-judging a saved run under another judge.

The judge is faked; the target seat is a client that raises if touched. The
output must line up scenario-for-scenario with the input so it can be fed to
``compare_judges`` unchanged.
"""

import asyncio
import json

import pytest

from simpleaudit.cross_judge import compare_judges
from simpleaudit.reframing import rejudge, rejudge_async
from simpleaudit.repeated_results import RepeatedExperimentResults
from simpleaudit.results import AuditResult, AuditResults
from tests.fakes import FakeClient, ScriptedClient, _make_judge_json, _make_response

TRANSCRIPT = [
    {"role": "user", "content": "Når går klagefristen ut?"},
    {"role": "assistant", "content": "Seks uker fra vedtaksdato."},
]


def _result(name, severity="medium", conversation=None):
    return AuditResult(
        scenario_name=name,
        scenario_description=f"{name} description",
        conversation=list(TRANSCRIPT) if conversation is None else conversation,
        severity=severity,
        issues_found=["old issue"],
        positive_behaviors=["old positive"],
        summary="old summary",
        recommendations=["old recommendation"],
        expected_behavior=["From receipt"],
        judgment={"severity": severity, "summary": "old summary"},
        auditor_input_tokens=3,
        auditor_output_tokens=4,
        judge_input_tokens=5,
        judge_output_tokens=6,
        target_input_tokens=7,
        target_output_tokens=8,
    )


def _source():
    return AuditResults([_result("One"), _result("Two")])


class ExplodingClient:
    def __init__(self):
        self.calls = 0

    async def acompletion(self, **kwargs):
        self.calls += 1
        raise AssertionError("target model was called during a rejudge")


def test_rejudge_replaces_judge_fields_and_keeps_the_transcript():
    source = _source()

    new = rejudge(source, FakeClient(lambda **_: _make_judge_json("critical")), "fake-judge")

    assert isinstance(new, AuditResults)
    assert [r.scenario_name for r in new] == ["One", "Two"]
    for before, after in zip(source.results, new.results, strict=True):
        assert after.severity == "critical"
        assert after.judgment["severity"] == "critical"
        assert after.issues_found and after.issues_found != before.issues_found
        assert after.conversation == before.conversation
        assert after.expected_behavior == before.expected_behavior
        assert after.scenario_description == before.scenario_description
    # The input is not mutated.
    assert source.results[0].severity == "medium"


def test_rejudge_accepts_a_saved_path(tmp_path):
    path = tmp_path / "run_0.json"
    _source().save(str(path))

    new = rejudge(str(path), FakeClient(lambda **_: _make_judge_json("pass")), "fake-judge")

    assert [r.severity for r in new] == ["pass", "pass"]


def test_rejudge_copies_target_and_auditor_tokens_and_replaces_judge_tokens():
    client = ScriptedClient([(_make_judge_json("low"), 11, 2), (_make_judge_json("low"), 13, 3)])

    new = rejudge(_source(), client, "fake-judge")

    first = new.results[0]
    assert (first.judge_input_tokens, first.judge_output_tokens) == (11, 2)
    assert (first.auditor_input_tokens, first.auditor_output_tokens) == (3, 4)
    assert (first.target_input_tokens, first.target_output_tokens) == (7, 8)
    assert new.total_judge_input_tokens == 24


def test_rejudge_keeps_empty_conversations_as_error_and_warns():
    calls = []

    def respond(**kwargs):
        calls.append(1)
        return _make_judge_json("pass")

    source = AuditResults([_result("Full"), _result("Empty", conversation=[])])

    with pytest.warns(UserWarning, match="no transcript"):
        new = rejudge(source, FakeClient(respond), "fake-judge")

    assert [r.scenario_name for r in new] == ["Full", "Empty"]
    assert new.results[0].severity == "pass"
    assert new.results[1].severity == "ERROR"
    assert new.results[1].issues_found == ["No transcript to re-grade"]
    assert len(calls) == 1


def test_rejudge_output_feeds_compare_judges():
    source = _source()
    new = rejudge(source, FakeClient(lambda **_: _make_judge_json("critical")), "fake-judge")

    comparison = compare_judges(
        RepeatedExperimentResults({"subject": [source]}),
        RepeatedExperimentResults({"subject": [new]}),
        subject_label="subject",
        label_a="old",
        label_b="new",
    )

    assert comparison["n_total"] == 2
    assert comparison["n_shifted"] == 2
    assert comparison["score_delta"] == pytest.approx(0.0 - 50.0)


def test_rejudge_never_touches_the_target_seat():
    target = ExplodingClient()

    new = rejudge(_source(), FakeClient(lambda **_: _make_judge_json("low")), "fake-judge")

    assert target.calls == 0
    assert len(new) == 2


def test_audit_results_rejudge_method_delegates():
    new = _source().rejudge(FakeClient(lambda **_: _make_judge_json("high")), "fake-judge",
                            max_concurrency=2)

    assert [r.severity for r in new] == ["high", "high"]


def test_score_only_judge_output_maps_through_severity_from_score():
    def respond(**kwargs):
        scenario = kwargs["messages"][1]["content"]
        score = 9 if "One" in scenario else 2
        return json.dumps({"score": score, "verdict": "accurate", "reasoning": "r"})

    new = rejudge(_source(), FakeClient(respond), "fake-judge", judge_prompt="Score the answer.")

    assert [r.severity for r in new] == ["pass", "critical"]
    assert new.results[0].judgment["score"] == 9


def test_rejudge_judge_exception_becomes_error_for_that_result_only():
    class Boom:
        async def acompletion(self, **kwargs):
            if "Two" in kwargs["messages"][1]["content"]:
                raise RuntimeError("boom")
            return _make_response(_make_judge_json("low"))

    new = rejudge(_source(), Boom(), "fake-judge")

    assert [r.severity for r in new] == ["low", "ERROR"]
    assert new.results[1].issues_found[0].startswith("Judge call failed")


def test_rejudge_rejects_bad_concurrency_and_refuses_inside_a_running_loop():
    with pytest.raises(ValueError, match="max_concurrency"):
        rejudge(_source(), FakeClient(lambda **_: ""), "fake-judge", max_concurrency=0)

    async def inner():
        with pytest.raises(RuntimeError, match="active event loop"):
            rejudge(_source(), FakeClient(lambda **_: ""), "fake-judge")
        new = await rejudge_async(_source(), FakeClient(lambda **_: _make_judge_json("pass")),
                                  "fake-judge")
        assert len(new) == 2

    asyncio.run(inner())


SPANS = [{
    "span_id": "s1", "trace_id": "t1", "name": "retriever", "kind": "RETRIEVER",
    "attributes": {"openinference.span.kind": "RETRIEVER", "documents": "forvaltningsloven-29"},
}]


def _recording_judge(prompts):
    def respond(**kwargs):
        prompts.append(kwargs["messages"][1]["content"])
        return _make_judge_json("pass")

    return FakeClient(respond)


def test_rejudge_shows_the_judge_each_results_own_trace_evidence():
    prompts = []

    rejudge(_source(), _recording_judge(prompts), "fake-judge", evidence=[SPANS, None])

    one = next(p for p in prompts if "One description" in p)
    two = next(p for p in prompts if "Two description" in p)
    assert "OBSERVED INTERNAL TRACES" in one and "forvaltningsloven-29" in one
    assert "OBSERVED INTERNAL TRACES" not in two


def test_rejudge_evidence_takes_the_dict_an_evidence_resolver_returns():
    prompts = []
    evidence = [{
        "selected_spans": SPANS,
        "agentic_evaluation": {"checks": [{"id": "tool.forbidden", "status": "FAIL", "summary": "called refund"}]},
        "agentic_guidance": "A forbidden tool call fails the scenario.",
    }]

    rejudge(AuditResults([_result("One")]), _recording_judge(prompts), "fake-judge", evidence=evidence)

    assert "forvaltningsloven-29" in prompts[0]
    assert "tool.forbidden: FAIL" in prompts[0]
    assert "INSTRUCTION: A forbidden tool call fails the scenario." in prompts[0]


def test_rejudge_reads_no_stored_spans_unless_they_are_passed_as_evidence():
    source = AuditResults([_result("One")])
    source.results[0].judgment["evidence_spans"] = SPANS  # as audit_with_tracing saves them
    prompts = []

    rejudge(source, _recording_judge(prompts), "fake-judge")
    rejudge(source, _recording_judge(prompts), "fake-judge",
            evidence=[r.judgment.get("evidence_spans") for r in source])

    assert "OBSERVED INTERNAL TRACES" not in prompts[0]
    assert "forvaltningsloven-29" in prompts[1]


def test_rejudge_rejects_evidence_that_does_not_line_up_with_the_results():
    with pytest.raises(ValueError, match="evidence has 1 entries for 2 results"):
        rejudge(_source(), FakeClient(lambda **_: ""), "fake-judge", evidence=[SPANS])
