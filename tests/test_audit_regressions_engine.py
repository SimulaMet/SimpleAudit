"""Regression tests for engine defects found in the 2026-11 system audit
(see AUDIT.md, findings C1-C9). SingleTurnAuditor must apply the same judge
contract as ModelAuditor."""

import copy
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.auditor import Auditor
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.single_turn import SingleTurnAuditor
from simpleaudit.targets import CallableTarget
from tests.fakes import FakeClient


def _make(cls, judge, **kw):
    return cls(
        model="t", provider="openai", judge_model="j", judge_provider="openai",
        api_key="x", judge_api_key="x", judge=judge, show_progress=False,
        max_retries=0, **kw,
    )


CHECKLIST_VIOLATED = json.dumps({
    "checklist": [{"index": 1, "expectation": "Refuses", "status": "violated",
                   "evidence_kind": "direct", "quote": "Sure, here is how"}],
    "notes": "",
})
SCENARIO = [{"name": "s", "description": "d", "test_prompt": "how?",
             "expected_behavior": ["Refuses"], "severity": "critical"}]


@pytest.mark.parametrize("cls", [ModelAuditor, SingleTurnAuditor])
def test_checklist_postprocess_applied(cls):
    # C1: SingleTurnAuditor skipped the judge postprocess -> ERROR.
    a = _make(cls, "checklist")
    a.target_client = FakeClient(lambda **k: "Sure, here is how to do it.")
    a.judge_client = a.auditor_client = FakeClient(lambda **k: CHECKLIST_VIOLATED)
    assert a.run(SCENARIO, max_turns=1)[0].severity == "critical"


@pytest.mark.parametrize("cls", [ModelAuditor, SingleTurnAuditor])
def test_code_graded_judge(cls):
    # C2: SingleTurnAuditor called the placeholder judge client -> ERROR.
    a = _make(cls, "choice_match")
    a.target_client = FakeClient(lambda **k: "B")
    sc = [{"name": "s", "description": "d", "test_prompt": "Pick A or B",
           "expected_behavior": ["B"],
           "metadata": {"correct_choice": "B", "choices": ["A", "B"]}}]
    assert a.run(sc, max_turns=1)[0].severity != "ERROR"


def test_judge_notes_reach_single_turn_judge():
    seen = []

    def judge(**kw):
        seen.append(json.dumps(kw.get("messages"), ensure_ascii=False))
        return json.dumps({"severity": "pass", "summary": "ok"})

    a = _make(SingleTurnAuditor, None)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(judge)
    sc = [{"name": "s", "description": "d", "test_prompt": "q",
           "metadata": {"judge_notes": ["UNIQUE-NOTE-123"]}}]
    a.run(sc, max_turns=1)
    assert "UNIQUE-NOTE-123" in seen[-1]


def test_single_turn_empty_custom_target_reply_is_error():
    # C3: an empty reply from a custom target was graded (often as pass).
    a = _make(SingleTurnAuditor, None)
    a.set_target(CallableTarget(lambda **k: ""))
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    assert a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)[0].severity == "ERROR"


def test_single_turn_coerces_judgment_fields():
    # C4: a string issues_found was kept as a string (split into chars later).
    a = _make(SingleTurnAuditor, None)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps(
        {"severity": "high", "issues_found": "leaked data", "summary": None}))
    r = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1)[0]
    assert r.issues_found == ["leaked data"]
    assert r.summary == ""


def test_single_turn_evidence_resolver_dict():
    # C5: a dict return from evidence_resolver crashed the judge (ERROR).
    a = _make(SingleTurnAuditor, None)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    r = a.run(
        [{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=1,
        evidence_resolver=lambda ex: {"selected_spans": [], "agentic_evaluation": {"ok": True}},
    )[0]
    assert r.severity == "pass"


def test_auditor_construction_does_not_leak_noop_target_to_other_threads():
    # C6: Auditor toggled a class-wide flag; a concurrent ModelAuditor saw it.
    real_create = ModelAuditor._create_anyllm_client
    built = {}

    def slow_create(self, *a, **kw):
        time.sleep(0.05)
        return MagicMock()

    with patch.object(ModelAuditor, "_create_anyllm_client", new=slow_create):
        t = threading.Thread(target=lambda: Auditor(target=CallableTarget(lambda **k: "x"),
                                                    judge_api_key="x"))
        t.start()
        time.sleep(0.01)
        built["m"] = ModelAuditor(model="t", provider="openai", api_key="x",
                                  judge_model="j", judge_provider="openai", judge_api_key="x",
                                  show_progress=False)
        t.join()
    assert type(built["m"].target_client).__name__ != "_NoopTargetClient"
    assert real_create is ModelAuditor._create_anyllm_client


def test_auditor_copy_does_not_recurse():
    # C9: __getattr__ recursed forever when _engine was not yet set.
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        a = Auditor(target=CallableTarget(lambda **k: "x"))
    copy.copy(a)


def test_run_meta_names_explicit_target_and_real_turns():
    # C9: meta said target_model "unused" with no hint of the real target,
    # and SingleTurnAuditor reported the requested max_turns, not 1.
    a = _make(SingleTurnAuditor, None)
    a.set_target(CallableTarget(lambda **k: "answer"))
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    res = a.run([{"name": "s", "description": "d", "test_prompt": "q"}], max_turns=5)
    assert res.meta["target_type"] == "CallableTarget"
    assert res.meta["max_turns"] == 1


def test_per_call_judge_params_reach_auditor_like_constructor_ones():
    # C7: run(judge_params=...) did not reach the auditor; the constructor did.
    seen = []

    def auditor(**kw):
        seen.append(kw.get("temperature"))
        return "probe"

    a = _make(ModelAuditor, None)
    a.target_client = FakeClient(lambda **k: "answer")
    a.judge_client = FakeClient(lambda **k: json.dumps({"severity": "pass"}))
    a.auditor_client = FakeClient(auditor)
    a.run([{"name": "s", "description": "d"}], max_turns=1, judge_params={"temperature": 0})
    assert seen and seen[0] == 0


async def test_tokens_of_retried_empty_responses_are_counted():
    # C8: only the final response's usage was reported.
    from tests.fakes import _make_response

    responses = iter([_make_response("", 1000, 4000), _make_response("ok", 10, 5)])

    class Client:
        async def acompletion(self, **kw):
            return next(responses)

    content, tin, tout = await ModelAuditor._call_async(
        Client(), "m", None, "hi", max_retries=1, retry_backoff=0, require_content=True,
    )
    assert content == "ok"
    assert (tin, tout) == (1010, 4005)
