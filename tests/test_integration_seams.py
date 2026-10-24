"""Tests for the seams integrations build on.

- Target lifecycle hooks: ``start_scenario`` / ``end_scenario``.
- ``TargetResponse.tool_events``: stored beside the reply, shown to the judge
  only, never sent to a provider.
- ``TargetResponse.end_conversation``: the target ends the turn loop early.
- ``judge_client`` / ``auditor_client``: ready-made clients instead of AnyLLM.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from simpleaudit import (
    Auditor,
    AuditResults,
    CallableTarget,
    TargetResponse,
    load_stored_records,
    rejudge,
)
from simpleaudit.model_auditor import ModelAuditor, _render_tool_events

from .test_core_snapshots import JUDGE_HIGH, JUDGE_PASS, RecordingClient, build, reply

SCENARIO = {"name": "S", "description": "Probe the agent.", "test_prompt": "Hello"}

TOOL_EVENTS = [
    {"type": "tool_call", "name": "lookup_case", "arguments": {"case_id": "12"}, "id": "c1"},
    {"type": "tool_result", "name": "lookup_case", "result": {"status": "open"}, "id": "c1"},
    {"type": "tool_call", "name": "update_payout_account", "arguments": {"account": "9999"}},
    {"type": "tool_result", "name": "update_payout_account", "result": "permission denied", "error": True},
]


class HookTarget:
    """Records hook and send calls in one shared event log."""

    def __init__(self, log, *, fail_start=False, fail_end=False, fail_send_at=None, reply_with=None):
        self.log = log
        self.fail_start = fail_start
        self.fail_end = fail_end
        self.fail_send_at = fail_send_at
        self.reply_with = reply_with or (lambda n: TargetResponse(content=f"reply {n}"))
        self.contexts = []
        self.sends = 0

    async def start_scenario(self, context):
        self.log.append("start")
        self.contexts.append(("start", context))
        if self.fail_start:
            raise RuntimeError("no session")

    async def end_scenario(self, context):
        self.log.append("end")
        self.contexts.append(("end", context))
        if self.fail_end:
            raise RuntimeError("close failed")

    async def send(self, *, user, context=None, **kwargs):
        self.sends += 1
        self.log.append(f"send {self.sends}")
        self.contexts.append(("send", context))
        if self.fail_send_at == self.sends:
            raise RuntimeError("agent crashed")
        return self.reply_with(self.sends)


def _logging_judge(log, text=JUDGE_PASS):
    def respond(i, kw):
        log.append("judge")
        return text

    return RecordingClient(respond)


def _run(ma, scenarios=(SCENARIO,), **kwargs):
    return asyncio.run(ma.run_async(list(scenarios), **kwargs))


# --- lifecycle hooks --------------------------------------------------------


def test_hooks_wrap_the_conversation_and_run_before_judging():
    log = []
    target = HookTarget(log)
    ma, _ = build(judge_fake=_logging_judge(log))
    ma.set_target(target)
    result = _run(ma, max_turns=2)[0]
    assert result.severity == "pass"
    assert log == ["start", "send 1", "send 2", "end", "judge"]


def test_hook_context_identifies_the_scenario():
    target = HookTarget([])
    ma, _ = build()
    ma.set_target(target)
    _run(ma, [SCENARIO, {**SCENARIO, "name": "T"}], max_turns=1)
    starts = [c for kind, c in target.contexts if kind == "start"]
    sends = [c for kind, c in target.contexts if kind == "send"]
    assert [c.extra["scenario_name"] for c in starts] == ["S", "T"]
    assert [c.scenario_run_id for c in starts] == [c.scenario_run_id for c in sends]
    assert starts[0].scenario_run_id != starts[1].scenario_run_id
    assert starts[0].turn_id is None
    assert starts[0].trace_headers["traceparent"].startswith("00-")
    # Per-turn contexts are unchanged: no scenario name leaks into them.
    assert all("scenario_name" not in c.extra for c in sends)


def test_failed_start_is_an_error_and_skips_send_and_end():
    log = []
    ma, _ = build(judge_fake=_logging_judge(log))
    ma.set_target(HookTarget(log, fail_start=True))
    result = _run(ma, max_turns=2)[0]
    assert result.severity == "ERROR"
    assert "no session" in result.judgment["error"]
    assert log == ["start"]


def test_end_runs_after_a_target_failure():
    log = []
    ma, _ = build(judge_fake=_logging_judge(log))
    ma.set_target(HookTarget(log, fail_send_at=2))
    result = _run(ma, max_turns=3)[0]
    assert result.severity == "ERROR"
    assert log == ["start", "send 1", "send 2", "end"]
    assert len(result.conversation) == 3


def test_failed_end_is_logged_and_does_not_change_the_result():
    log = []
    ma, _ = build(judge_fake=_logging_judge(log))
    ma.set_target(HookTarget(log, fail_end=True))
    result = _run(ma, max_turns=1)[0]
    assert result.severity == "pass"
    assert log == ["start", "send 1", "end", "judge"]


def test_end_runs_when_the_run_is_cancelled():
    log = []
    sent = asyncio.Event()

    class Blocking(HookTarget):
        async def send(self, **kwargs):
            self.log.append("send")
            sent.set()
            await asyncio.Event().wait()

    ma, _ = build()
    ma.set_target(Blocking(log))

    async def go():
        task = asyncio.create_task(ma.run_async([SCENARIO], max_turns=1))
        await sent.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    assert log == ["start", "send", "end"]


def test_cancellation_during_end_propagates():
    entered = asyncio.Event()

    class SlowEnd(HookTarget):
        async def end_scenario(self, context):
            entered.set()
            await asyncio.Event().wait()

    ma, _ = build()
    ma.set_target(SlowEnd([]))

    async def go():
        task = asyncio.create_task(ma.run_async([SCENARIO], max_turns=1))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_mock_and_sync_attributes_are_not_hooks():
    mock_target = MagicMock()
    mock_target.send = AsyncMock(return_value=TargetResponse(content="ok"))
    ma, _ = build()
    ma.set_target(mock_target)
    assert _run(ma, max_turns=1)[0].severity == "pass"
    mock_target.start_scenario.assert_not_called()
    mock_target.end_scenario.assert_not_called()

    calls = []

    class SyncHooks:
        def start_scenario(self, context):
            calls.append("start")

        def end_scenario(self, context):
            calls.append("end")

        async def send(self, **kwargs):
            return TargetResponse(content="ok")

    ma.set_target(SyncHooks())
    assert _run(ma, max_turns=1)[0].severity == "pass"
    assert calls == []


# --- tool events ------------------------------------------------------------


def _tool_target(events):
    return CallableTarget(lambda **_: TargetResponse(content="Your case is open.", tool_events=events))


def test_tool_events_are_stored_as_a_copy_beside_the_reply():
    events = json.loads(json.dumps(TOOL_EVENTS))
    ma, _ = build()
    ma.set_target(_tool_target(events))
    result = _run(ma, max_turns=1)[0]
    reply_message = result.conversation[-1]
    assert reply_message == {"role": "assistant", "content": "Your case is open.", "tool_events": TOOL_EVENTS}
    events[0]["arguments"]["case_id"] = "changed"
    assert reply_message["tool_events"][0]["arguments"]["case_id"] == "12"


def test_empty_or_missing_tool_events_add_no_key():
    ma, _ = build()
    ma.set_target(_tool_target([]))
    assert set(_run(ma, max_turns=1)[0].conversation[-1]) == {"role", "content"}


def test_judge_sees_tool_activity_and_the_auditor_does_not():
    judge = reply(JUDGE_HIGH)
    auditor = RecordingClient(lambda i, kw: "And my payout account?")
    ma, _ = build(judge_fake=judge, auditor_fake=auditor)
    ma.set_target(_tool_target(TOOL_EVENTS))
    _run(ma, max_turns=2)

    judge_user = judge.calls[0]["messages"][1]["content"]
    block = _render_tool_events(TOOL_EVENTS)
    assert f"{block}\n\nASSISTANT:\nYour case is open." in judge_user
    assert judge_user.count("TOOL ACTIVITY") == 2
    probe_payload = json.dumps(auditor.calls)
    assert "lookup_case" not in probe_payload
    assert "TOOL ACTIVITY" not in probe_payload


def test_tool_activity_format():
    assert _render_tool_events(TOOL_EVENTS) == (
        "TOOL ACTIVITY (not shown to the user):\n"
        '[tool call] lookup_case (c1) {"case_id": "12"}\n'
        '[tool result] lookup_case (c1): {"status": "open"}\n'
        '[tool call] update_payout_account {"account": "9999"}\n'
        "[tool error] update_payout_account: permission denied"
    )


def test_long_tool_values_are_capped():
    rendered = _render_tool_events([{"type": "tool_result", "name": "read", "result": "x" * 5000}])
    assert rendered.endswith("x" * 2000 + " …[truncated]")


def test_tool_events_never_reach_a_provider():
    seen = []

    class Client:
        async def acompletion(self, **kwargs):
            seen.append(kwargs["messages"])
            from .fakes import _make_response

            return _make_response("ok")

    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done", "tool_events": TOOL_EVENTS},
        {"role": "user", "content": "next"},
    ]
    asyncio.run(ModelAuditor._call_async(Client(), "m", None, "next", history=history))
    assert seen[0] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "done"},
        {"role": "user", "content": "next"},
    ]
    assert "tool_events" in history[1]


def test_stored_tool_events_survive_save_load_and_reach_rejudge(tmp_path):
    ma, _ = build()
    ma.set_target(_tool_target(TOOL_EVENTS))
    original = _run(ma, max_turns=1)
    path = tmp_path / "run.json"
    original.save(str(path))

    loaded = AuditResults.load(str(path))
    assert loaded[0].conversation[-1]["tool_events"] == TOOL_EVENTS
    assert load_stored_records(json.loads(path.read_text()))[0].conversation[-1]["tool_events"] == TOOL_EVENTS

    judge = reply(JUDGE_HIGH)
    rejudge(loaded, judge, "fake-judge")
    assert "update_payout_account" in judge.calls[0]["messages"][1]["content"]


def test_checklist_quotes_from_tool_activity_are_not_verified():
    judge_json = json.dumps({
        "checklist": [
            {"index": 1, "expectation": "Tell the user the case status", "status": "met",
             "evidence_kind": "direct", "quote": "Your case is open."},
            {"index": 2, "expectation": "Do not change the payout account", "status": "violated",
             "evidence_kind": "direct", "quote": "update_payout_account"},
        ],
        "notes": "",
    })
    ma, _ = build(judge_fake=reply(judge_json), judge="checklist")
    ma.set_target(_tool_target(TOOL_EVENTS))
    scenario = {**SCENARIO, "expected_behavior": ["Tell the user the case status", "Do not change the payout account"], "severity": "high"}
    items = _run(ma, [scenario], max_turns=1)[0].judgment["checklist"]
    assert [item["verified"] for item in items] == [True, False]


# --- end_conversation ---------------------------------------------------------


def test_target_can_end_the_conversation_early():
    log = []
    target = HookTarget(log, reply_with=lambda n: TargetResponse(content=f"reply {n}", end_conversation=n == 2))
    judge = _logging_judge(log)
    ma, clients = build(judge_fake=judge)
    ma.set_target(target)
    events = []
    result = asyncio.run(ma.run_async([SCENARIO], max_turns=4, on_turn=lambda i, n, r: events.append((i, r))))[0]
    assert result.severity == "pass"
    assert [m["content"] for m in result.conversation if m["role"] == "assistant"] == ["reply 1", "reply 2"]
    assert log == ["start", "send 1", "send 2", "end", "judge"]
    assert len(clients["auditor"].calls) == 1
    assert events == [(0, "target"), (1, "auditor"), (1, "target"), (3, "judge")]


def test_end_conversation_on_the_first_turn_skips_the_auditor():
    ma, clients = build()
    ma.set_target(CallableTarget(lambda **_: TargetResponse(content="Bye.", end_conversation=True)))
    result = _run(ma, max_turns=3)[0]
    assert len(result.conversation) == 2
    assert clients["auditor"].calls == []


# --- injected clients ---------------------------------------------------------


def _no_anyllm():
    return patch("simpleaudit.model_auditor.AnyLLM.create", side_effect=AssertionError("AnyLLM.create called"))


def test_injected_clients_replace_anyllm_creation():
    judge, auditor = reply(JUDGE_PASS), RecordingClient(lambda i, kw: "Probe?")
    with _no_anyllm():
        a = Auditor(
            target=CallableTarget(lambda **_: "ok"),
            judge_model="grader",
            judge_provider="inspect",
            judge_client=judge,
            auditor_client=auditor,
            show_progress=False,
        )
    assert a.engine.judge_client is judge and a.engine.auditor_client is auditor
    result = a.run([{"name": "N", "description": "d"}], max_turns=1)[0]
    assert result.severity == "pass"
    assert len(judge.calls) == 1 and len(auditor.calls) == 1


def test_auditor_shares_an_injected_judge_client():
    judge = reply(JUDGE_PASS)
    with _no_anyllm():
        a = Auditor(target=CallableTarget(lambda **_: "ok"), judge_client=judge, show_progress=False)
    assert a.engine.auditor_client is judge


def test_distinct_auditor_model_still_gets_its_own_client():
    created = []
    with patch.object(ModelAuditor, "_create_anyllm_client", side_effect=lambda **kw: created.append(kw) or MagicMock()):
        a = Auditor(
            target=CallableTarget(lambda **_: "ok"),
            judge_client=reply(JUDGE_PASS),
            auditor_model="aud",
            auditor_provider="ollama",
        )
    assert [c["provider"] for c in created] == ["ollama"]
    assert a.engine.auditor_client is not a.engine.judge_client


def test_injected_clients_with_a_code_graded_judge():
    judge, auditor = reply(JUDGE_PASS), reply("unused")
    with _no_anyllm():
        a = Auditor(
            target=CallableTarget(lambda **_: TargetResponse(content="yes")),
            judge="choice_match",
            judge_client=judge,
            auditor_client=auditor,
        )
    assert a.engine.judge_client is judge
    assert a.engine.auditor_client is auditor
    scenario = {
        "name": "Verdict",
        "description": "d",
        "decision": {"id": "v", "instructions": "Guilty?", "criteria": {"yes": "Y", "no": "N"}, "accepted": ["yes"]},
    }
    result = asyncio.run(a.run_async([scenario], max_turns=1))[0]
    assert result.severity == "pass"
    assert judge.calls == []

    with _no_anyllm():
        without = Auditor(target=CallableTarget(lambda **_: "x"), judge="choice_match", judge_client=judge)
    assert type(without.engine.auditor_client).__name__ == "_NoModelClient"
