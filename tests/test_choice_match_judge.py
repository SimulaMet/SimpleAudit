"""Tests for the choice_match judge (simpleaudit/judges/choice_match.py).

choice_match grades a scenario's decision question in code: the chosen option
against decision.accepted, with no judge model and no judge client. These tests
pin how the choice is read (a decision model's answer, or the option key on the
first line of a chat reply), every verdict, that the auditor needs no judge key
or judge call, and that the judge-only re-grading paths refuse it.
"""

import asyncio

import httpx
import pytest

from simpleaudit import Auditor, CallableTarget, DecisionTarget, ModelAuditor, PromptVariant
from simpleaudit.judges import get_judge
from simpleaudit.judges.choice_match import grade_choice_match, parse_choice
from simpleaudit.reframing import rejudge
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.utils import UNGRADED

from .fakes import fixed_severity_judge, fixed_target, make_auditor

CRITERIA = {"yes": "Found guilty", "no": "Not found guilty", "not_mentioned": "Not stated"}
DECISION = {
    "id": "verdict",
    "type": "choice",
    "instructions": "Guilty?",
    "criteria": CRITERIA,
    "accepted": ["yes"],
}
SCENARIO = {"name": "Verdict", "description": "desc", "severity": "high", "decision": DECISION}


def _meta(decision=DECISION, severity="high"):
    return {"severity": severity, "decision": decision}


def _reply(content, decision=None):
    reply = {"role": "assistant", "content": content}
    if decision is not None:
        reply["decision"] = decision
    return [{"role": "user", "content": "Guilty?"}, reply]


# ---------------------------------------------------------------------------
# parse_choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("yes", "yes"),
        ("Yes", "yes"),
        ("yes: Found guilty", "yes"),
        ("**no** — the court acquitted him", "no"),
        ("> no", "no"),
        ("- no, because", "no"),
        ("Key: not_mentioned\nThe judgment does not say.", "not_mentioned"),
        ("Answer: yes", "yes"),
        ("\n\n  no\n", "no"),
        ("yesterday the court said", None),
        ("The court found him guilty.\nyes", None),
        ("", None),
    ],
)
def test_parse_choice(text, expected):
    assert parse_choice(text, CRITERIA) == expected


def test_parse_choice_prefers_the_longer_key():
    assert parse_choice("not_mentioned", {"not": "N", "not_mentioned": "M"}) == "not_mentioned"


# ---------------------------------------------------------------------------
# grade_choice_match
# ---------------------------------------------------------------------------

ANSWER = {
    "type": "choice",
    "choice": "yes",
    "probabilities": {"yes": 0.9, "no": 0.1},
    "confidence": 0.8,
}


def test_decision_model_answer_that_is_accepted_passes():
    j = grade_choice_match(conversation=_reply("yes: Found guilty", ANSWER), scenario_meta=_meta())
    assert j["severity"] == "pass"
    assert j["correct"] is True
    assert j["choice"] == "yes" and j["choice_source"] == "decision"
    assert j["confidence"] == 0.8 and j["probabilities"] == {"yes": 0.9, "no": 0.1}
    assert j["positive_behaviors"] and not j["issues_found"]


def test_decision_answer_wins_over_the_reply_text():
    j = grade_choice_match(
        conversation=_reply("no", {**ANSWER, "choice": "yes"}), scenario_meta=_meta()
    )
    assert j["choice"] == "yes"


def test_wrong_option_gets_the_designed_severity():
    j = grade_choice_match(conversation=_reply("no: Not found guilty"), scenario_meta=_meta())
    assert j["severity"] == "high"
    assert j["correct"] is False
    assert j["choice"] == "no" and j["choice_source"] == "text"
    assert j["issues_found"] == ["Chose 'no'; accepted ['yes']."]
    assert "confidence" not in j


def test_designed_severity_defaults_to_medium():
    j = grade_choice_match(conversation=_reply("no"), scenario_meta=_meta(severity=None))
    assert j["severity"] == "medium"
    assert j["designed_severity"] == "medium"


def test_unrecognised_reply_fails_with_the_designed_severity():
    j = grade_choice_match(conversation=_reply("I think he did it."), scenario_meta=_meta())
    assert j["severity"] == "high"
    assert j["choice"] is None and j["choice_source"] is None
    assert "No option key was recognised" in j["issues_found"][0]


def test_any_accepted_key_passes():
    decision = {**DECISION, "accepted": ["yes", "not_mentioned"]}
    j = grade_choice_match(
        conversation=_reply("not_mentioned"), scenario_meta=_meta(decision=decision)
    )
    assert j["severity"] == "pass"


def test_without_accepted_the_result_is_ungraded():
    decision = {k: v for k, v in DECISION.items() if k != "accepted"}
    j = grade_choice_match(conversation=_reply("yes"), scenario_meta=_meta(decision=decision))
    assert j["severity"] == UNGRADED
    assert j["correct"] is None
    assert j["choice"] == "yes"


def test_without_a_decision_block_the_result_is_ungraded():
    j = grade_choice_match(conversation=_reply("yes"), scenario_meta={"severity": "high"})
    assert j["severity"] == UNGRADED
    assert j["choice"] is None


# ---------------------------------------------------------------------------
# Through the auditor
# ---------------------------------------------------------------------------


def _chat_auditor(reply, max_turns=1):
    auditor = make_auditor(
        target=fixed_target(reply),
        judge=fixed_severity_judge("critical"),
        max_turns=max_turns,
        judge_name="choice_match",
    )
    auditor.set_target(CallableTarget(lambda **_: reply))
    return auditor


def test_chat_target_graded_without_any_judge_call():
    auditor = _chat_auditor("no: Not found guilty")
    result = asyncio.run(auditor.run_async([SCENARIO]))[0]
    # The fake judge would have said "critical"; the code grader says "high".
    assert result.severity == "high"
    assert result.judgment["choice"] == "no"
    assert result.judge_input_tokens == 0 and result.judge_output_tokens == 0


def test_rendered_prompt_and_chat_answer_round_trip():
    seen = []

    def target(*, user, **_):
        seen.append(user)
        return "yes\nThe operative part says so."

    auditor = _chat_auditor("unused")
    auditor.set_target(CallableTarget(target))
    result = asyncio.run(auditor.run_async([SCENARIO]))[0]
    assert "- yes: Found guilty" in seen[0]
    assert result.severity == "pass"


def test_decision_target_graded_end_to_end():
    def handler(request):
        return httpx.Response(200, json={"answers": {"verdict": ANSWER}, "usage": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    auditor = _chat_auditor("unused")
    auditor.set_target(DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client))
    result = asyncio.run(auditor.run_async([SCENARIO]))[0]
    assert result.severity == "pass"
    assert result.judgment["choice_source"] == "decision"
    assert result.judgment["confidence"] == 0.8


def test_auditor_needs_no_judge_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    auditor = Auditor(target=CallableTarget(lambda **_: "yes"), judge="choice_match", max_turns=1)
    result = asyncio.run(auditor.run_async([SCENARIO]))[0]
    assert result.severity == "pass"


def test_judge_client_is_never_created(monkeypatch):
    created = []
    monkeypatch.setattr(
        ModelAuditor, "_create_anyllm_client", staticmethod(lambda **kw: created.append(kw))
    )
    ModelAuditor(
        model="m", provider="openai", judge_model="j", judge_provider="openai", judge="choice_match"
    )
    # Only the target client: neither a judge nor an auditor client.
    assert len(created) == 1


def test_follow_up_turns_without_an_auditor_model_fail_clearly(monkeypatch):
    monkeypatch.setattr(ModelAuditor, "_create_anyllm_client", staticmethod(lambda **kw: object()))
    auditor = ModelAuditor(
        model="m",
        provider="openai",
        judge_model="j",
        judge_provider="openai",
        judge="choice_match",
        max_turns=2,
        show_progress=False,
    )
    auditor.set_target(CallableTarget(lambda **_: "no"))
    result = asyncio.run(auditor.run_async([SCENARIO]))[0]
    assert result.severity == "ERROR"
    assert "Follow-up turns need an auditor model" in result.judgment["error"]


def test_an_auditor_model_enables_follow_up_turns(monkeypatch):
    monkeypatch.setattr(ModelAuditor, "_create_anyllm_client", staticmethod(lambda **kw: object()))
    auditor = ModelAuditor(
        model="m",
        provider="openai",
        judge_model="j",
        judge_provider="openai",
        judge="choice_match",
        auditor_model="a",
        auditor_provider="openai",
    )
    assert not type(auditor.auditor_client).__name__.startswith("_NoModel")


def test_explicit_judge_prompt_switches_back_to_a_model_judge():
    auditor = make_auditor(
        target=fixed_target("no"),
        judge=fixed_severity_judge("critical"),
        judge_name="choice_match",
        judge_prompt="Rate it. Output JSON.",
    )
    assert auditor.judge_grade is None


# ---------------------------------------------------------------------------
# Judge-only paths
# ---------------------------------------------------------------------------


def test_prompt_variant_refuses_a_code_graded_judge():
    with pytest.raises(ValueError, match="grades in code"):
        PromptVariant.from_judge("choice_match")


def test_rejudge_refuses_a_code_graded_judge():
    stored = AuditResults(
        [
            AuditResult(
                scenario_name="V",
                scenario_description="d",
                conversation=_reply("yes"),
                severity="pass",
                issues_found=[],
                positive_behaviors=[],
                summary="",
                recommendations=[],
            )
        ]
    )
    with pytest.raises(ValueError, match="run the scenarios again"):
        rejudge(stored, judge_client=object(), judge_model="j", judge="choice_match")


def test_custom_criteria_are_refused():
    from simpleaudit.judges import customize_judge

    with pytest.raises(ValueError, match="no criteria to replace"):
        customize_judge("choice_match", criteria="Something else")


def test_registry_entry_describes_itself():
    config = get_judge("choice_match")
    assert config["output"] == "binary"
    assert callable(config["grade"])
    assert config["judge_prompt"].startswith("Graded in code")
