"""judgment.failure: which role failed, and how (additive; see simpleaudit/failures.py)."""

import asyncio
import json

import pytest

from simpleaudit.failures import build_failure
from simpleaudit.targets import CallableTarget
from simpleaudit.single_turn import SingleTurnAuditor
from tests.fakes import FakeClient, make_auditor


class RateLimitError(Exception):
    status_code = 429


class InternalServerError(Exception):
    status_code = 503


class UnauthorizedError(Exception):
    def __init__(self):
        super().__init__("bad key")
        self.response = type("R", (), {"status_code": 401})()


CASES = [
    (lambda: RateLimitError("slow down"), 429, "rate_limit", "RateLimitError"),
    (
        lambda: InternalServerError("provider unavailable"),
        503,
        "unavailable",
        "InternalServerError",
    ),
    (lambda: TimeoutError("timed out"), None, "timeout", "TimeoutError"),
    (lambda: ConnectionError("refused"), None, "unavailable", "ConnectionError"),
]
IDS = ["429", "5xx", "timeout", "connection"]

OK = FakeClient(lambda **kw: json.dumps({"severity": "pass", "issues_found": [], "summary": "ok"}))


def _boom(make):
    def fn(**kw):
        raise make()

    return FakeClient(fn)


def _run(auditor, **kw):
    return asyncio.run(auditor.run_scenario(name="S", description="d", **kw))


def _assert_failure(result, role, status, kind, exc_name, phase):
    j = result.judgment
    f = j["failure"]
    assert f["role"] == role
    assert f["kind"] == kind
    assert f["exception"] == exc_name
    assert f["message"] == j["error"]
    assert f.get("http_status") == status
    assert j["error_phase"] == phase
    assert result.severity == "ERROR"
    return f


@pytest.mark.parametrize("make,status,kind,name", CASES, ids=IDS)
def test_target_failure(make, status, kind, name):
    def fn(**kw):
        raise make()

    a = make_auditor(target=OK, judge=OK, auditor=OK, max_turns=2)
    a.set_target(CallableTarget(fn))
    f = _assert_failure(_run(a), "target", status, kind, name, "conversation")
    assert f["turn"] == 1


@pytest.mark.parametrize("make,status,kind,name", CASES, ids=IDS)
def test_auditor_failure(make, status, kind, name):
    a = make_auditor(target=OK, judge=OK, auditor=_boom(make), max_turns=2)
    f = _assert_failure(_run(a), "auditor", status, kind, name, "conversation")
    assert f["turn"] == 1
    assert f["model"] == "fake-judge"  # the configured auditor model


@pytest.mark.parametrize("make,status,kind,name", CASES, ids=IDS)
def test_judge_failure(make, status, kind, name):
    a = make_auditor(target=OK, judge=_boom(make), auditor=OK, max_turns=1)
    f = _assert_failure(_run(a), "judge", status, kind, name, "judge")
    assert "turn" not in f


def test_auditor_failure_on_later_turn_is_turn_two():
    calls = {"n": 0}

    def fn(**kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RateLimitError("429")
        return "next probe"

    a = make_auditor(target=OK, judge=OK, auditor=FakeClient(fn), max_turns=3)
    f = _assert_failure(_run(a), "auditor", 429, "rate_limit", "RateLimitError", "conversation")
    assert f["turn"] == 2


def test_target_failure_on_later_turn():
    calls = {"n": 0}

    def fn(**kw):
        calls["n"] += 1
        if calls["n"] > 1:
            raise InternalServerError("503")
        return "answer"

    a = make_auditor(target=OK, judge=OK, auditor=OK, max_turns=3)
    a.set_target(CallableTarget(fn))
    f = _assert_failure(
        _run(a), "target", 503, "unavailable", "InternalServerError", "conversation"
    )
    assert f["turn"] == 2


def _single(st):
    scenario = {"name": "S", "description": "d", "test_prompt": "hi"}
    return asyncio.run(st.run_async([scenario])).results[0]


@pytest.mark.parametrize("make,status,kind,name", CASES, ids=IDS)
def test_single_turn_roles(make, status, kind, name):
    from unittest.mock import MagicMock, patch

    def build(judge):
        with patch.object(SingleTurnAuditor, "_create_anyllm_client", return_value=MagicMock()):
            st = SingleTurnAuditor(
                model="m",
                provider="openai",
                judge_model="j",
                judge_provider="openai",
                max_retries=0,
                show_progress=False,
            )
        st.judge_client = judge
        return st

    def fn(**kw):
        raise make()

    st = build(OK)
    st.set_target(CallableTarget(fn))
    res = _single(st)
    f = _assert_failure(res, "target", status, kind, name, "conversation")
    assert f["turn"] == 1

    st = build(_boom(make))
    st.set_target(CallableTarget(lambda **kw: "fine"))
    res = _single(st)
    _assert_failure(res, "judge", status, kind, name, "judge")


def test_existing_fields_unchanged():
    a = make_auditor(target=OK, judge=OK, auditor=OK, max_turns=1)
    a.set_target(CallableTarget(lambda **kw: (_ for _ in ()).throw(RateLimitError("x"))))
    j = _run(a).judgment
    assert j["error"] == "RateLimitError: x"
    assert j["error_phase"] == "conversation"
    assert j["severity"] == "ERROR"
    assert {"issues_found", "summary", "recommendations", "traceback"} <= set(j)
    json.dumps(j)  # still serialisable


def test_success_has_no_failure():
    a = make_auditor(target=OK, judge=OK, auditor=OK, max_turns=1)
    assert "failure" not in _run(a).judgment


def test_error_like_answer_without_exception_is_not_flagged():
    a = make_auditor(target=OK, judge=OK, auditor=OK, max_turns=1)
    a.set_target(
        CallableTarget(lambda **kw: "APIError: The language model provider is unavailable (429)")
    )
    res = _run(a)
    assert res.severity != "ERROR"
    assert "failure" not in res.judgment


def test_judge_cannot_forge_failure_key():
    forged = FakeClient(
        lambda **kw: json.dumps(
            {"severity": "pass", "issues_found": [], "summary": "ok", "failure": {"role": "target"}}
        )
    )
    a = make_auditor(target=OK, judge=forged, auditor=OK, max_turns=1)
    assert "failure" not in _run(a).judgment


def test_unknown_stays_unknown_and_optional_fields_omitted():
    f = build_failure("target", ValueError("odd"))
    assert f == {
        "role": "target",
        "kind": "unknown",
        "message": "ValueError: odd",
        "exception": "ValueError",
    }


def test_status_from_cause_chain_and_endpoint_redacted():
    inner = RateLimitError("429")
    inner.request = type(
        "Rq", (), {"url": "https://user:pw@host.example/v1/chat?api_key=abc&x=1"}
    )()
    try:
        try:
            raise inner
        except RateLimitError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError as outer:
        f = build_failure("auditor", outer, turn=2)
    assert f["http_status"] == 429 and f["kind"] == "rate_limit" and f["turn"] == 2
    assert "pw" not in f["endpoint"] and "user" not in f["endpoint"] and "abc" not in f["endpoint"]
    assert f["endpoint"].startswith("https://host.example/v1/chat")


def test_unknown_role_becomes_system_and_http_kinds():
    assert build_failure("bogus", ValueError("x"))["role"] == "system"
    for status, kind in (
        (401, "auth"),
        (403, "auth"),
        (504, "timeout"),
        (500, "unavailable"),
        (404, "config"),
    ):
        e = Exception("x")
        e.status_code = status
        assert build_failure("target", e)["kind"] == kind


def test_experiment_reps_carry_failure():
    from unittest.mock import MagicMock, patch
    from simpleaudit.model_auditor import ModelAuditor

    async def go():
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            a = ModelAuditor(
                model="m",
                provider="openai",
                judge_model="j",
                judge_provider="openai",
                max_retries=0,
                show_progress=False,
                max_turns=1,
            )
        a.judge_client = OK
        a.auditor_client = OK
        a.set_target(CallableTarget(lambda **kw: (_ for _ in ()).throw(RateLimitError("429"))))
        return await a.run_async([{"name": "S", "description": "d"}] * 2)

    res = asyncio.run(go())
    assert len(res) == 2
    for r in res:
        assert r.judgment["failure"]["role"] == "target"
