"""Tests for DecisionTarget (simpleaudit/targets/decision.py).

A decision model reads a state and typed questions and returns a choice with
probabilities. These tests run DecisionTarget against an in-process httpx mock
of a System One endpoint, so no network is needed. They pin the request it
builds, how it maps the answer back, every limit and error path, and the
auditor behaviour it relies on: one turn, and the full answer stored beside the
reply in the transcript.
"""

import asyncio
import json
import warnings

import httpx
import pytest

from simpleaudit import DecisionTarget, TargetContext
from simpleaudit.decision import public_decision, validate_decision

from .fakes import fixed_severity_judge, fixed_target, make_auditor

DECISION = validate_decision(
    {
        "id": "verdict",
        "instructions": "Did the court find the defendant guilty?",
        "criteria": {"yes": "Found guilty", "no": "Not found guilty", "unclear": None},
        "accepted": ["yes"],
        "state": {"jurisdiction": "Kosovo"},
    }
)

ANSWER = {
    "type": "choice",
    "choice": "yes",
    "probabilities": {"yes": 0.96, "no": 0.03, "unclear": 0.01},
    "confidence": 0.9,
}


def _endpoint(answer=ANSWER, status=200, body=None):
    """Mock System One endpoint. Records every request it receives."""
    seen = []

    def handler(request):
        seen.append(request)
        if body is not None:
            return httpx.Response(status, json=body)
        return httpx.Response(
            status,
            json={
                "model": "clef",
                "answers": {"verdict": answer},
                "usage": {"input_tokens": 149, "output_tokens": 0},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


def _context(decision=DECISION):
    return TargetContext(
        turn_id="t1",
        trace_headers={"traceparent": "00-abc-def-01"},
        extra={"decision": public_decision(decision)} if decision is not None else {},
    )


def _send(target, user="Was he convicted?", history=None, context=None, **kwargs):
    return asyncio.run(
        target.send(user=user, history=history, context=context or _context(), **kwargs)
    )


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


def test_request_carries_the_question_without_the_answer_key():
    client, seen = _endpoint()
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    _send(target)
    body = json.loads(seen[0].content)
    assert seen[0].url == "http://decisions.test/v1/systemone"
    assert body["model"] == "clef"
    assert body["questions"] == {
        "verdict": {
            "type": "choice",
            "instructions": "Did the court find the defendant guilty?",
            "criteria": {"yes": "Found guilty", "no": "Not found guilty", "unclear": None},
        }
    }
    assert "accepted" not in seen[0].content.decode()


def test_state_is_decision_state_plus_document_text_only():
    client, seen = _endpoint()
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    history = [
        {
            "role": "user",
            "content": "Q",
            "documents": [
                "Gjykata e shpall të pandehurin fajtor.",
                {"text": "Second document.", "relevant": False, "source": "SENTINEL-SOURCE"},
            ],
        }
    ]
    _send(target, history=history)
    body = json.loads(seen[0].content)
    assert body["state"] == {
        "jurisdiction": "Kosovo",
        "documents": ["Gjykata e shpall të pandehurin fajtor.", "Second document."],
    }
    assert "SENTINEL-SOURCE" not in seen[0].content.decode()


def test_documents_argument_is_used_when_history_has_none():
    client, seen = _endpoint()
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    _send(target, documents=["Plain text."])
    assert json.loads(seen[0].content)["state"]["documents"] == ["Plain text."]


def test_state_falls_back_to_the_prompt():
    client, seen = _endpoint()
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    decision = validate_decision({k: v for k, v in DECISION.items() if k != "state"})
    _send(target, user="The court found him guilty. Guilty?", context=_context(decision))
    assert json.loads(seen[0].content)["state"] == "The court found him guilty. Guilty?"


def test_body_is_compact_utf8_and_includes_extra_body():
    client, seen = _endpoint()
    target = DecisionTarget(
        "http://decisions.test/v1/systemone",
        "clef",
        client=client,
        extra_body={"keep_alive": "10m", "provider": {"zdr": True}},
    )
    _send(target, history=[{"role": "user", "content": "Q", "documents": ["fajtor ë"]}])
    raw = seen[0].content.decode("utf-8")
    assert "ë" in raw and "\\u00eb" not in raw
    assert ", " not in raw and ": " not in raw.replace("Found guilty", "")
    body = json.loads(raw)
    assert body["keep_alive"] == "10m"
    assert body["provider"] == {"zdr": True}


def test_headers_carry_key_and_trace_context():
    client, seen = _endpoint()
    target = DecisionTarget(
        "http://or/decisions", "jev", api_key="sk-test", headers={"X-Title": "t"}, client=client
    )
    _send(target)
    headers = seen[0].headers
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["x-title"] == "t"
    assert headers["content-type"] == "application/json"
    assert headers["traceparent"] == "00-abc-def-01"


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


def test_answer_maps_to_content_decision_and_tokens():
    client, _ = _endpoint()
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    resp = _send(target)
    assert resp.content == "yes: Found guilty"
    assert resp.decision == ANSWER
    assert resp.raw["answers"]["verdict"] == ANSWER
    assert resp.input_tokens == 149
    assert resp.output_tokens == 0


def test_option_without_description_is_named_by_key():
    client, _ = _endpoint(answer={**ANSWER, "choice": "unclear"})
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    assert _send(target).content == "unclear"


# ---------------------------------------------------------------------------
# Limits and errors
# ---------------------------------------------------------------------------


def test_scenario_without_a_decision_block_is_an_error():
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=_endpoint()[0])
    with pytest.raises(ValueError, match="needs a scenario with a 'decision' block"):
        _send(target, context=_context(decision=None))


def test_too_many_options_raise_before_any_request():
    client, seen = _endpoint()
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    many = validate_decision(
        {"id": "verdict", "instructions": "Pick", "criteria": {f"o{i}": None for i in range(27)}}
    )
    with pytest.raises(ValueError, match="27 options; this endpoint accepts 2–26"):
        _send(target, context=_context(many))
    assert seen == []


def test_option_limit_can_be_lifted():
    client, seen = _endpoint(answer={**ANSWER, "choice": "o0"})
    target = DecisionTarget("http://x", "m", client=client, max_options=None)
    many = validate_decision(
        {"id": "verdict", "instructions": "Pick", "criteria": {f"o{i}": None for i in range(40)}}
    )
    assert _send(target, context=_context(many)).content == "o0"


def test_oversized_body_raises_before_any_request_and_is_not_shortened():
    client, seen = _endpoint()
    target = DecisionTarget.ollama("clef", base_url="http://decisions.test", client=client)
    history = [{"role": "user", "content": "Q", "documents": ["x" * (64 * 1024)]}]
    with pytest.raises(ValueError, match="over this endpoint's 65536-byte limit"):
        _send(target, history=history)
    assert seen == []


def test_http_error_reports_the_endpoint_message():
    client, _ = _endpoint(status=400, body={"error": "decision prompt exceeds the model context"})
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    with pytest.raises(RuntimeError, match="HTTP 400: .*exceeds the model context"):
        _send(target)


def test_missing_answer_is_an_error():
    client, _ = _endpoint(body={"answers": {}})
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    with pytest.raises(RuntimeError, match="no answer for 'verdict'"):
        _send(target)


def test_choice_outside_the_options_is_an_error():
    client, _ = _endpoint(answer={**ANSWER, "choice": "maybe"})
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    with pytest.raises(RuntimeError, match="chose 'maybe'"):
        _send(target)


def test_without_a_client_one_is_created_per_request_and_closed(monkeypatch):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"answers": {"verdict": ANSWER}})
    )
    real_client = httpx.AsyncClient
    created = []

    def make_client(**kwargs):
        client = real_client(transport=transport)
        created.append((kwargs, client))
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", timeout=12.5)
    assert _send(target).content == "yes: Found guilty"
    assert len(created) == 1
    kwargs, client = created[0]
    assert kwargs == {"timeout": 12.5}
    assert client.is_closed


# ---------------------------------------------------------------------------
# Client reuse and retries
# ---------------------------------------------------------------------------


def _count_clients(monkeypatch, handler):
    """Patch httpx.AsyncClient to a mock-backed one; returns the list of clients made."""
    real_client = httpx.AsyncClient
    created = []

    def make_client(**kwargs):
        client = real_client(transport=httpx.MockTransport(handler))
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    return created


def _ok(request):
    return httpx.Response(200, json={"answers": {"verdict": ANSWER}})


def test_reuse_client_serves_every_request_with_one_client(monkeypatch):
    created = _count_clients(monkeypatch, _ok)
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", reuse_client=True)

    async def three_requests():
        for _ in range(3):
            await target.send(user="Was he convicted?", context=_context())
        assert not created[0].is_closed
        await target.aclose()

    asyncio.run(three_requests())
    assert len(created) == 1
    assert created[0].is_closed


def test_reuse_client_replaces_a_client_from_an_earlier_event_loop(monkeypatch):
    created = _count_clients(monkeypatch, _ok)
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", reuse_client=True)
    _send(target)
    _send(target)
    assert len(created) == 2


def test_reuse_client_as_context_manager_closes_on_exit(monkeypatch):
    created = _count_clients(monkeypatch, _ok)

    async def run():
        async with DecisionTarget(
            "http://decisions.test/v1/systemone", "clef", reuse_client=True
        ) as target:
            await target.send(user="Was he convicted?", context=_context())
            await target.send(user="Was he convicted?", context=_context())

    asyncio.run(run())
    assert len(created) == 1
    assert created[0].is_closed


def test_aclose_leaves_an_injected_client_open():
    client, _ = _endpoint()
    target = DecisionTarget(
        "http://decisions.test/v1/systemone", "clef", client=client, reuse_client=True
    )
    _send(target)

    async def close():
        await target.aclose()
        return client.is_closed

    assert asyncio.run(close()) is False


def _flaky(statuses):
    """Handler answering with ``statuses`` in turn, then with a good answer.

    An entry is a status or a ``(status, headers)`` pair.
    """
    seen = []

    def handler(request):
        seen.append(request)
        if len(seen) <= len(statuses):
            status = statuses[len(seen) - 1]
            status, headers = status if isinstance(status, tuple) else (status, {})
            return httpx.Response(status, headers=headers, json={"error": "later"})
        return _ok(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), seen


@pytest.fixture
def sleeps(monkeypatch):
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr("simpleaudit.targets.decision.asyncio.sleep", fake_sleep)
    return waited


def test_default_sends_once_and_does_not_retry(sleeps):
    client, seen = _flaky([503])
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        _send(target)
    assert len(seen) == 1
    assert sleeps == []


def test_transient_status_is_retried_with_doubling_backoff(sleeps):
    client, seen = _flaky([503, 429])
    target = DecisionTarget(
        "http://decisions.test/v1/systemone", "clef", client=client, max_retries=3, retry_backoff=0.5
    )
    assert _send(target).content == "yes: Found guilty"
    assert len(seen) == 3
    assert sleeps == [0.5, 1.0]


def test_default_backoff_matches_model_target():
    target = DecisionTarget("http://decisions.test/v1/systemone", "clef")
    assert (target.max_retries, target.retry_backoff) == (0, 0.5)


def _retrying(statuses, **kwargs):
    client, seen = _flaky(statuses)
    target = DecisionTarget(
        "http://decisions.test/v1/systemone", "clef", client=client, max_retries=2, **kwargs
    )
    return target, seen


def test_retry_after_replaces_the_backoff_on_429_and_503(sleeps):
    target, _ = _retrying(
        [(429, {"Retry-After": "7"}), (503, {"Retry-After": "3"})], retry_backoff=10
    )
    assert _send(target).content == "yes: Found guilty"
    assert sleeps == [7, 3]


def test_retry_after_is_used_unchanged_up_to_the_cap(sleeps):
    client, _ = _flaky([(429, {"Retry-After": "30"})])
    target = DecisionTarget(
        "http://decisions.test/v1/systemone", "clef", client=client, max_retries=1
    )
    _send(target)
    assert sleeps == [30]


def test_retry_after_http_date_is_read_as_a_delay(sleeps):
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    target, _ = _retrying([(429, {"Retry-After": when})], retry_backoff=100)
    _send(target)
    assert 25 <= sleeps[0] <= 30


def test_retry_after_in_the_past_waits_no_time(sleeps):
    target, _ = _retrying([(503, {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})], retry_backoff=10)
    _send(target)
    assert sleeps == [0.0]


def test_retry_after_is_capped_at_60_seconds(sleeps):
    client, _ = _flaky([(429, {"Retry-After": "120"})])
    target = DecisionTarget(
        "http://decisions.test/v1/systemone", "clef", client=client, max_retries=1
    )
    _send(target)
    assert sleeps == [60.0]


def test_unreadable_retry_after_and_other_statuses_use_the_backoff(sleeps):
    target, _ = _retrying(
        [(429, {"Retry-After": "soon"}), (502, {"Retry-After": "1"})], retry_backoff=4
    )
    _send(target)
    assert sleeps == [4, 8]


def test_retries_stop_at_the_cap_and_report_the_last_status(sleeps):
    client, seen = _flaky([502, 502, 502, 502])
    target = DecisionTarget(
        "http://decisions.test/v1/systemone", "clef", client=client, max_retries=2
    )
    with pytest.raises(RuntimeError, match="HTTP 502"):
        _send(target)
    assert len(seen) == 3
    assert len(sleeps) == 2


def test_a_client_error_is_not_retried(sleeps):
    client, seen = _flaky([400])
    target = DecisionTarget(
        "http://decisions.test/v1/systemone", "clef", client=client, max_retries=3
    )
    with pytest.raises(RuntimeError, match="HTTP 400"):
        _send(target)
    assert len(seen) == 1


def test_transport_errors_are_retried_then_raised(sleeps):
    attempts = []

    def handler(request):
        attempts.append(request)
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    target = DecisionTarget(
        "http://decisions.test/v1/systemone", "clef", client=client, max_retries=2
    )
    with pytest.raises(httpx.ConnectError):
        _send(target)
    assert len(attempts) == 3


def test_retry_settings_are_validated():
    with pytest.raises(ValueError, match="max_retries"):
        DecisionTarget("http://decisions.test/v1/systemone", "clef", max_retries=-1)
    with pytest.raises(ValueError, match="retry_backoff"):
        DecisionTarget("http://decisions.test/v1/systemone", "clef", retry_backoff=-1)


# ---------------------------------------------------------------------------
# Constructors
# ---------------------------------------------------------------------------


def test_ollama_constructor():
    target = DecisionTarget.ollama("clef", base_url="http://localhost:11434/")
    assert target.url == "http://localhost:11434/v1/systemone"
    assert target.max_body_bytes == 64 * 1024
    assert target.max_turns == 1


def test_openrouter_constructor_reads_the_key_from_the_environment(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-env")
    target = DecisionTarget.openrouter("typesafe/jev-1.13", extra_body={"provider": {"zdr": True}})
    assert target.url == "https://openrouter.ai/api/v1/systemone"
    assert target.headers["Authorization"] == "Bearer sk-env"
    assert target.max_body_bytes is None
    assert target.extra_body == {"provider": {"zdr": True}}


# ---------------------------------------------------------------------------
# Through the auditor
# ---------------------------------------------------------------------------


def _auditor_with(target, max_turns=1):
    auditor = make_auditor(
        target=fixed_target("unused"), judge=fixed_severity_judge("pass"), max_turns=max_turns
    )
    auditor.set_target(target)
    return auditor


SCENARIO = {"name": "Verdict", "description": "desc", "decision": DECISION}


def test_auditor_stores_the_answer_beside_the_reply():
    client, _ = _endpoint()
    auditor = _auditor_with(
        DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    )
    result = asyncio.run(auditor.run_async([SCENARIO]))[0]
    assert result.severity == "pass"
    reply = result.conversation[-1]
    assert reply == {"role": "assistant", "content": "yes: Found guilty", "decision": ANSWER}
    assert result.target_input_tokens == 149


def test_auditor_runs_one_turn_and_warns_once():
    client, seen = _endpoint()
    auditor = _auditor_with(
        DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client), max_turns=3
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        results = asyncio.run(auditor.run_async([SCENARIO, {**SCENARIO, "name": "Second"}]))
    assert len(seen) == 2
    assert all(len(r.conversation) == 2 for r in results)
    turn_warnings = [w for w in caught if "answers at most 1 turn" in str(w.message)]
    assert len(turn_warnings) == 1


def test_endpoint_failure_is_recorded_as_a_scenario_error():
    client, _ = _endpoint(status=400, body={"error": "boom"})
    auditor = _auditor_with(
        DecisionTarget("http://decisions.test/v1/systemone", "clef", client=client)
    )
    result = asyncio.run(auditor.run_async([SCENARIO]))[0]
    assert result.severity == "ERROR"
    assert "HTTP 400" in result.judgment["error"]


def test_chat_targets_are_not_capped():
    auditor = make_auditor(
        target=fixed_target("hello"), judge=fixed_severity_judge("pass"), max_turns=2
    )
    assert auditor._turns_for_target(2) == 2
