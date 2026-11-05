"""
Tests for the Target abstraction (core refactor).

Covers:
    - TargetResponse / TargetContext data types
    - ModelTarget delegating to a client (byte-identical to legacy path)
    - ModelTarget transport mode
    - CallableTarget (sync + async, str + TargetResponse)
    - HTTPAppTarget (response_path, token_paths, correlation headers)
    - ModelAuditor.target property (lazy wrap of target_client)
    - ModelAuditor.set_target override
    - Auditor(target=...) generic entry point
"""

from pathlib import Path

import pytest

from simpleaudit import (
    Auditor,
    CallableTarget,
    HTTPAppTarget,
    ModelAuditor,
    ModelTarget,
    TargetContext,
    TargetResponse,
)
from simpleaudit.targets.http import _resolve_path

# ---------------------------------------------------------------------------
# TargetResponse / TargetContext
# ---------------------------------------------------------------------------

def test_target_response_defaults():
    r = TargetResponse(content="hi")
    assert r.content == "hi"
    assert r.raw is None
    assert r.input_tokens is None
    assert r.output_tokens is None


def test_target_context_defaults():
    c = TargetContext()
    assert c.audit_run_id is None
    assert c.trace_headers == {}
    assert c.extra == {}


# ---------------------------------------------------------------------------
# ModelTarget
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, content="hello", in_tok=5, out_tok=7):
        self.content = content
        self.in_tok = in_tok
        self.out_tok = out_tok
        self.calls = []

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        return type("R", (), {
            "choices": [type("C", (), {"message": type("M", (), {"content": self.content})})],
            "usage": type("U", (), {"prompt_tokens": self.in_tok, "completion_tokens": self.out_tok}),
        })()


@pytest.mark.asyncio
async def test_model_target_client_mode():
    client = _FakeClient()
    t = ModelTarget(client=client, model="m")
    r = await t.send(user="hi")
    assert r.content == "hello"
    assert r.input_tokens == 5
    assert r.output_tokens == 7
    assert client.calls[0]["model"] == "m"


@pytest.mark.asyncio
async def test_model_target_client_mode_forwards_trace_headers():
    """trace_headers on the per-turn context reach the wire as extra_headers."""
    client = _FakeClient()
    t = ModelTarget(client=client, model="m")
    ctx = TargetContext(trace_headers={"traceparent": "00-11223344556677889900112233445566-abcdef0123456789-01"})
    await t.send(user="hi", context=ctx)
    assert client.calls[0]["extra_headers"] == {
        "traceparent": "00-11223344556677889900112233445566-abcdef0123456789-01"
    }


@pytest.mark.asyncio
async def test_model_target_trace_headers_merge_with_user_params():
    """User-supplied extra_headers and the framework's trace headers coexist."""
    client = _FakeClient()
    t = ModelTarget(client=client, model="m")
    ctx = TargetContext(trace_headers={"traceparent": "00-11223344556677889900112233445566-abcdef0123456789-01"})
    await t.send(user="hi", params={"extra_headers": {"X-Custom": "1"}}, context=ctx)
    assert client.calls[0]["extra_headers"] == {
        "X-Custom": "1",
        "traceparent": "00-11223344556677889900112233445566-abcdef0123456789-01",
    }


@pytest.mark.asyncio
async def test_model_target_no_context_no_extra_headers():
    """Without a context the call is byte-identical to the legacy path."""
    client = _FakeClient()
    t = ModelTarget(client=client, model="m")
    await t.send(user="hi")
    assert "extra_headers" not in client.calls[0]


@pytest.mark.asyncio
async def test_model_target_transport_mode():
    async def transport(**kw):
        return TargetResponse(content="from-transport", input_tokens=1, output_tokens=2)

    t = ModelTarget(transport=transport)
    r = await t.send(user="x")
    assert r.content == "from-transport"
    assert r.input_tokens == 1


def test_model_target_requires_client_or_transport():
    with pytest.raises(ValueError):
        ModelTarget()


# ---------------------------------------------------------------------------
# CallableTarget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_callable_target_sync_str():
    t = CallableTarget(lambda **kw: "echo:" + kw["user"])
    r = await t.send(user="hi")
    assert r.content == "echo:hi"
    assert r.input_tokens is None


@pytest.mark.asyncio
async def test_callable_target_async_response():
    async def fn(**kw):
        return TargetResponse(content="async", input_tokens=3)
    t = CallableTarget(fn)
    r = await t.send(user="x")
    assert r.content == "async"
    assert r.input_tokens == 3


# ---------------------------------------------------------------------------
# HTTPAppTarget
# ---------------------------------------------------------------------------

def test_resolve_path_dotted():
    data = {"choices": [{"message": {"content": "x"}}]}
    assert _resolve_path(data, "choices.0.message.content") == "x"


def test_resolve_path_missing():
    assert _resolve_path({"a": 1}, "b.c") is None


class _FakeHTTPResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


class _FakeAsyncClient:
    def __init__(self, payload):
        self._payload = payload
        self.last_headers = None
        self.last_body = None

    async def request(self, method, url, json=None, headers=None):
        self.last_headers = headers
        self.last_body = json
        return _FakeHTTPResponse(self._payload)

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_http_target_response_path():
    client = _FakeAsyncClient({"answer": "the answer", "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    t = HTTPAppTarget(
        url="http://x/chat",
        response_path="answer",
        token_paths=("usage.prompt_tokens", "usage.completion_tokens"),
        client=client,
    )
    r = await t.send(user="q")
    assert r.content == "the answer"
    assert r.input_tokens == 10
    assert r.output_tokens == 20
    assert client.last_body["message"] == "q"


@pytest.mark.asyncio
async def test_http_target_forwards_correlation_headers():
    client = _FakeAsyncClient({"ok": 1})
    t = HTTPAppTarget(url="http://x/chat", response_path="ok", client=client)
    ctx = TargetContext(
        audit_run_id="run_1",
        scenario_run_id="scen_1",
        turn_id="turn_1",
        trace_headers={"traceparent": "00-abc-def-01"},
    )
    await t.send(user="q", context=ctx)
    h = client.last_headers
    assert h["traceparent"] == "00-abc-def-01"
    assert h["X-SimpleAudit-Turn-ID"] == "turn_1"
    assert h["X-SimpleAudit-Scenario-Run-ID"] == "scen_1"
    assert h["X-SimpleAudit-Run-ID"] == "run_1"


@pytest.mark.asyncio
async def test_http_target_no_tokens_when_absent():
    client = _FakeAsyncClient({"answer": "no usage here"})
    t = HTTPAppTarget(url="http://x/chat", response_path="answer", client=client)
    r = await t.send(user="q")
    assert r.input_tokens is None
    assert r.output_tokens is None


def test_http_target_openai_style_messages_body():
    """message_field='messages' produces an OpenAI-style messages list."""
    t = HTTPAppTarget(
        url="http://x/api/v1/chat/completions",
        request_template={"model": "stm-radgiver"},
        message_field="messages",
    )
    body = t._build_body("What is the capital of France?", None)
    assert body["model"] == "stm-radgiver"
    assert body["messages"] == [{"role": "user", "content": "What is the capital of France?"}]


def test_http_target_openai_style_messages_with_history():
    t = HTTPAppTarget(url="http://x", message_field="messages")
    history = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    body = t._build_body("follow-up", history)
    assert body["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "follow-up"},
    ]


# HTTPAppTarget inside an audit (issue #124): run_scenario passes the whole
# conversation, current turn included, with SimpleAudit's markers beside the
# text. The app must get each turn once and no marker.

MARKED_DOCUMENT = {
    "text": "Deadline is six weeks.",
    "relevant": True,
    "true": False,
    "authority": "guidance",
    "source": "old-guide",
}


class _RecordingHTTPClient(_FakeAsyncClient):
    """Keeps a copy of every request body, as it was when sent."""

    def __init__(self, payload):
        super().__init__(payload)
        self.bodies = []

    async def request(self, method, url, json=None, headers=None):
        import json as _json

        self.bodies.append(_json.loads(_json.dumps(json)))
        return await super().request(method, url, json=json, headers=headers)


def _audit_http(scenario, max_turns=2, **target_kwargs):
    import asyncio

    from tests.fakes import fixed_probe_auditor, fixed_severity_judge, fixed_target, make_auditor

    client = _RecordingHTTPClient({"answer": "ok"})
    auditor = make_auditor(
        fixed_target("unused"), fixed_severity_judge("pass"), fixed_probe_auditor("Second question?"), max_turns=max_turns
    )
    auditor.set_target(HTTPAppTarget(url="http://app/chat", response_path="answer", client=client, **target_kwargs))
    result = asyncio.run(auditor.run_async([scenario]))[0]
    return result, client.bodies


PLAIN_SCENARIO = {"name": "S", "description": "d", "test_prompt": "First question?"}


def test_http_audit_messages_body_sends_each_turn_once():
    result, bodies = _audit_http(PLAIN_SCENARIO, message_field="messages")
    assert result.severity == "pass"
    assert [b["messages"] for b in bodies] == [
        [{"role": "user", "content": "First question?"}],
        [
            {"role": "user", "content": "First question?"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "Second question?"},
        ],
    ]


def test_http_audit_message_body_keeps_history_to_earlier_turns():
    _, bodies = _audit_http(PLAIN_SCENARIO)
    assert bodies == [
        {"message": "First question?", "history": []},
        {
            "message": "Second question?",
            "history": [
                {"role": "user", "content": "First question?"},
                {"role": "assistant", "content": "ok"},
            ],
        },
    ]


def test_http_audit_sends_documents_as_text_and_never_their_marks():
    scenario = {**PLAIN_SCENARIO, "documents": ["Appeals go to NAV.", MARKED_DOCUMENT]}
    result, bodies = _audit_http(scenario, message_field="messages", documents_field="context")
    assert result.severity == "pass"
    assert [b["context"] for b in bodies] == [["Appeals go to NAV.", "Deadline is six weeks."]] * 2
    sent = str(bodies)
    for mark in ("relevant", "authority", "old-guide", "documents"):
        assert mark not in sent


def test_http_audit_with_documents_needs_a_documents_field():
    scenario = {**PLAIN_SCENARIO, "documents": [MARKED_DOCUMENT]}
    result, bodies = _audit_http(scenario, message_field="messages")
    assert result.severity == "ERROR"
    assert "documents_field" in result.judgment["error"]
    assert bodies == []


def test_http_body_drops_markers_and_copies_history():
    t = HTTPAppTarget(url="http://x")
    history = [
        {"role": "user", "content": "Is it guilty?", "file_uri": "/tmp/scan.png"},
        {"role": "assistant", "content": "yes", "decision": {"choice": "yes"}, "tool_events": [{"type": "tool_call"}]},
    ]
    body = t._build_body("And the sentence?", history)
    assert body["history"] == [
        {"role": "user", "content": "Is it guilty?"},
        {"role": "assistant", "content": "yes"},
    ]
    assert body["history"] is not history
    assert "file_uri" in history[0]  # the caller's transcript is untouched


# ---------------------------------------------------------------------------
# ModelAuditor.target property + set_target
# ---------------------------------------------------------------------------

def _make_auditor_with_fake(target_client):
    ma = ModelAuditor.__new__(ModelAuditor)
    ma._target_override = None
    ma.target_client = target_client
    ma.target_model = "m"
    ma.max_retries = 0
    ma.retry_backoff = 0.5
    return ma


def test_target_property_wraps_client():
    ma = _make_auditor_with_fake(_FakeClient())
    assert isinstance(ma.target, ModelTarget)
    assert ma.target.model == "m"


def test_set_target_override():
    ma = _make_auditor_with_fake(_FakeClient())
    custom = CallableTarget(lambda **kw: "custom")
    ma.set_target(custom)
    assert ma.target is custom


# ---------------------------------------------------------------------------
# Auditor (generic entry point)
# ---------------------------------------------------------------------------

def test_auditor_requires_target():
    with pytest.raises(ValueError):
        Auditor(target=None)


def test_auditor_delegates_to_engine():
    t = CallableTarget(lambda **kw: "x")
    a = Auditor(target=t, judge_model="gpt-4o", judge_provider="openai", judge_api_key="sk-test-dummy")
    assert a.target is t
    assert isinstance(a.engine, ModelAuditor)
    # run_async should be delegated
    assert hasattr(a, "run_async")
    # The unused model target client should be the noop placeholder
    assert type(a.engine.target_client).__name__ == "_NoopTargetClient"


# ---------------------------------------------------------------------------
# Trace-context correlation (engine -> target)
# ---------------------------------------------------------------------------

def test_engine_forwards_traceparent_and_records_correlation():
    """The engine generates a W3C traceparent per turn and records it."""
    import asyncio

    from simpleaudit.tracing.context import TraceCorrelation
    from tests.fakes import fixed_probe_auditor, fixed_severity_judge, fixed_target, make_auditor

    captured: dict = {}

    class _CapturingTarget:
        async def send(self, *, user, history=None, context=None, **kw):
            captured["context"] = context
            return TargetResponse(content="ok")

    auditor = make_auditor(
        target=fixed_target("ok"),
        judge=fixed_severity_judge("pass"),
        auditor=fixed_probe_auditor("probe"),
        max_turns=2,
        show_progress=False,
    )
    # Override the target with one that captures the context.
    auditor.set_target(_CapturingTarget())

    correlation = TraceCorrelation(audit_run_id="audit_test")
    scenarios = [{"name": "Corr", "description": "correlation test"}]
    asyncio.run(
        auditor.run_async(
            scenarios=scenarios,
            max_turns=2,
            audit_run_id="audit_test",
            trace_correlation=correlation,
        )
    )

    ctx = captured["context"]
    assert ctx is not None
    assert ctx.audit_run_id == "audit_test"
    assert ctx.scenario_run_id
    assert ctx.turn_id
    # traceparent is a valid W3C header: 00-<32hex>-<16hex>-01
    tp = ctx.trace_headers["traceparent"]
    parts = tp.split("-")
    assert parts[0] == "00"
    assert len(parts[1]) == 32
    assert len(parts[2]) == 16
    assert parts[3] == "01"
    # The correlation recorded at least one turn -> trace link.
    assert correlation.all_trace_ids()


def test_engine_traceparent_reaches_wire_on_default_target():
    """With the default ModelTarget (no override), the scenario's traceparent
    is forwarded on the target's outgoing acompletion as extra_headers."""
    import asyncio

    from simpleaudit.tracing.context import TraceCorrelation
    from tests.fakes import fixed_probe_auditor, fixed_severity_judge, make_auditor

    client = _FakeClient()
    auditor = make_auditor(
        target=client,
        judge=fixed_severity_judge("pass"),
        auditor=fixed_probe_auditor("probe"),
        max_turns=2,
        show_progress=False,
    )

    correlation = TraceCorrelation(audit_run_id="audit_test")
    scenarios = [{"name": "Wire", "description": "wire traceparent test"}]
    asyncio.run(
        auditor.run_async(
            scenarios=scenarios,
            max_turns=2,
            audit_run_id="audit_test",
            trace_correlation=correlation,
        )
    )

    assert client.calls
    last = client.calls[-1]
    tp = last["extra_headers"]["traceparent"]
    # Same trace id the correlation recorded, not a fresh unrelated one.
    assert tp.split("-")[1] in correlation.all_trace_ids()


def test_http_body_keeps_other_fields_of_direct_callers():
    t = HTTPAppTarget(url="http://x", message_field="messages")
    history = [
        {"role": "user", "content": "What's 2+2?"},
        {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "add"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "add", "content": "4"},
        {"role": "assistant", "content": "4."},
    ]
    body = t._build_body("And 3+3?", history)
    assert body["messages"] == [*history, {"role": "user", "content": "And 3+3?"}]


def test_http_documents_are_parsed_and_keep_their_positions():
    t = HTTPAppTarget(url="http://x", documents_field="context")
    assert t._build_body("q", None, ["", "B"])["context"] == ["", "B"]
    assert "context" not in t._build_body("q", [{"role": "user", "content": "q", "documents": ["A"]}], [])
    with pytest.raises(ValueError, match="text"):
        t._build_body("q", None, [{"content": "Deadline is six weeks."}])


def test_http_audit_with_attached_files_fails_instead_of_dropping_them():
    result, bodies = _audit_http({**PLAIN_SCENARIO, "file_uri": "/tmp/scan.png"}, message_field="messages")
    assert result.severity == "ERROR" and "cannot send attached files" in result.judgment["error"]
    assert bodies == []


# Issues #126–#128: what CallableTarget and HTTPAppTarget pass on.


def test_callable_gets_plain_history_and_text_documents():
    import asyncio

    from tests.fakes import fixed_probe_auditor, fixed_severity_judge, fixed_target, make_auditor

    seen = []
    auditor = make_auditor(
        fixed_target("x"), fixed_severity_judge("pass"), fixed_probe_auditor("Second question?"), max_turns=2
    )
    auditor.set_target(CallableTarget(lambda **kwargs: seen.append(kwargs) or "ok"))
    image = str(Path(__file__).resolve().parent.parent / "simpleaudit" / "scenarios" / "images" / "product_a.png")
    scenario = {**PLAIN_SCENARIO, "documents": ["Appeals go to NAV.", MARKED_DOCUMENT], "file_uri": image}
    result = asyncio.run(auditor.run_async([scenario]))[0]
    assert result.severity == "pass"
    first, second = seen
    # The file stays on the turn that attached it, and file_uri is set on that turn only.
    assert first["history"] == [{"role": "user", "content": "First question?", "file_uri": image}]
    assert second["history"] == [
        {"role": "user", "content": "First question?", "file_uri": image},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "Second question?"},
    ]
    assert (first["file_uri"], second["file_uri"]) == (image, None)
    assert first["documents"] == second["documents"] == ["Appeals go to NAV.", "Deadline is six weeks."]
    assert "documents" in result.conversation[0]  # the stored transcript keeps its markers


@pytest.mark.asyncio
async def test_callable_passes_given_documents_and_no_history_through():
    seen = []
    await CallableTarget(lambda **kwargs: seen.append(kwargs) or "ok").send(user="q", documents=[MARKED_DOCUMENT])
    assert seen[0]["history"] is None and seen[0]["file_uri"] is None
    assert seen[0]["documents"] == ["Deadline is six weeks."]


def test_http_messages_body_sends_the_system_prompt_and_fixed_messages():
    t = HTTPAppTarget(
        url="http://x",
        message_field="messages",
        request_template={"model": "m", "messages": [{"role": "system", "content": "Fixed app prompt."}]},
    )
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "more"}]
    body = t._build_body("more", history, system="You are careful.")
    assert body["messages"] == [
        {"role": "system", "content": "Fixed app prompt.\n\nYou are careful."},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more"},
    ]
    assert t.request_template["messages"] == [{"role": "system", "content": "Fixed app prompt."}]


def test_http_message_body_sends_the_system_prompt_to_system_field_or_warns():
    import warnings

    assert HTTPAppTarget(url="http://x", system_field="instructions")._build_body("q", None, system="Be careful.") == {
        "message": "q",
        "instructions": "Be careful.",
    }
    t = HTTPAppTarget(url="http://x")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        t._build_body("q", None, system="Be careful.")
        t._build_body("q", None, system="Be careful.")
    assert len([w for w in caught if "system_field" in str(w.message)]) == 1
    # With warnings as errors, every call fails, not just the first.
    strict = HTTPAppTarget(url="http://x")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        for _ in range(2):
            with pytest.raises(UserWarning):
                strict._build_body("q", None, system="Be careful.")


def test_http_template_history_comes_before_the_conversation():
    _, bodies = _audit_http(PLAIN_SCENARIO, request_template={"history": []})
    assert bodies[1]["history"] == [
        {"role": "user", "content": "First question?"},
        {"role": "assistant", "content": "ok"},
    ]
    fixed = [{"role": "user", "content": "Earlier session."}, {"role": "assistant", "content": "Noted."}]
    _, bodies = _audit_http(PLAIN_SCENARIO, request_template={"history": fixed})
    assert bodies[0]["history"] == fixed
    assert bodies[1]["history"] == [*fixed, {"role": "user", "content": "First question?"}, {"role": "assistant", "content": "ok"}]



def test_http_system_field_wins_in_messages_mode_and_joins_the_template_value():
    t = HTTPAppTarget(url="http://x", message_field="messages", system_field="system")
    assert t._build_body("q", None, system="Be careful.") == {
        "system": "Be careful.",
        "messages": [{"role": "user", "content": "q"}],
    }
    joined = HTTPAppTarget(url="http://x", system_field="instructions", request_template={"instructions": "Fixed."})
    assert joined._build_body("q", None, system="Be careful.")["instructions"] == "Fixed.\n\nBe careful."
    blocks = HTTPAppTarget(
        url="http://x", message_field="messages", system_field="system",
        request_template={"system": [{"type": "text", "text": "Fixed."}]},
    )
    assert blocks._build_body("q", None, system="Be careful.")["system"] == [
        {"type": "text", "text": "Fixed."},
        {"type": "text", "text": "Be careful."},
    ]


def test_http_template_messages_without_a_system_prompt_are_kept_as_they_are():
    fixed = [{"role": "user", "content": "Example question"}, {"role": "assistant", "content": "Example answer"}]
    t = HTTPAppTarget(url="http://x", message_field="messages", request_template={"messages": fixed})
    assert t._build_body("q", None)["messages"] == [*fixed, {"role": "user", "content": "q"}]
    assert t._build_body("q", None, system="Be careful.")["messages"] == [
        {"role": "system", "content": "Be careful."},
        *fixed,
        {"role": "user", "content": "q"},
    ]


def test_http_template_history_that_is_not_a_list_is_an_error():
    t = HTTPAppTarget(url="http://x", request_template={"history": {"session": "abc"}})
    with pytest.raises(ValueError, match="must be a list"):
        t._build_body("q", [{"role": "user", "content": "q"}])
