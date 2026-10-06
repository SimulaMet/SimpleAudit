"""
Tests for the tracing layer (context, store, otlp, selection).
"""

import pytest

from simpleaudit.tracing import (
    OTLPTraceReceiver,
    SpanStore,
    TraceCorrelation,
    make_traceparent,
    new_span_id,
    new_trace_id,
    parse_otlp_json,
    select_spans,
    summarize_for_judge,
)


# ---------------------------------------------------------------------------
# context
# ---------------------------------------------------------------------------

def test_trace_id_format():
    tid = new_trace_id()
    assert len(tid) == 32
    int(tid, 16)  # valid hex


def test_span_id_format():
    sid = new_span_id()
    assert len(sid) == 16
    assert sid != "0" * 16


def test_make_traceparent_format():
    tp = make_traceparent()
    parts = tp.split("-")
    assert len(parts) == 4
    assert parts[0] == "00"
    assert len(parts[1]) == 32
    assert len(parts[2]) == 16
    assert parts[3] == "01"


def test_traceparent_deterministic():
    tp = make_traceparent(trace_id="a" * 32, span_id="b" * 16)
    assert tp == f"00-{'a' * 32}-{'b' * 16}-01"


def test_trace_correlation_multiple_traces_per_turn():
    corr = TraceCorrelation(audit_run_id="run_1")
    corr.record("turn_1", "traceA")
    corr.record("turn_1", "traceB")
    corr.record("turn_2", "traceC")
    assert corr.trace_ids_for_turn("turn_1") == ["traceA", "traceB"]
    assert corr.trace_ids_for_turn("turn_2") == ["traceC"]
    assert corr.trace_ids_for_turn("turn_9") == []
    assert set(corr.all_trace_ids()) == {"traceA", "traceB", "traceC"}


def test_spans_for_turn_collects_all_linked_traces():
    corr = TraceCorrelation(audit_run_id="run_1")
    corr.record("turn_1", "traceA")
    corr.record("turn_1", "traceB")
    store = SpanStore()
    store.add({"span_id": "s1", "trace_id": "traceA", "name": "a", "attributes": {"openinference.span.kind": "RETRIEVER"}})
    store.add({"span_id": "s2", "trace_id": "traceB", "name": "b", "attributes": {"openinference.span.kind": "LLM"}})
    store.add({"span_id": "s3", "trace_id": "traceC", "name": "c", "attributes": {"openinference.span.kind": "TOOL"}})

    spans = corr.spans_for_turn("turn_1", store)
    assert {s["span_id"] for s in spans} == {"s1", "s2"}
    # turn with no linked traces returns nothing
    assert corr.spans_for_turn("turn_9", store) == []


def test_evidence_spans_for_turn_selects_and_adds_provenance():
    from simpleaudit.tracing import evidence_spans_for_turn

    corr = TraceCorrelation(audit_run_id="run_1")
    corr.record("turn_1", "traceA")
    store = SpanStore()
    store.add({"span_id": "s1", "trace_id": "traceA", "name": "retriever", "attributes": {"openinference.span.kind": "RETRIEVER"}})
    store.add({"span_id": "s2", "trace_id": "traceA", "name": "chain", "attributes": {"openinference.span.kind": "CHAIN"}})

    spans = evidence_spans_for_turn(corr, store, "turn_1")
    # RETRIEVER (evidence) is kept; CHAIN (noise) is dropped when evidence exists.
    assert [s["span_id"] for s in spans] == ["s1"]
    assert spans[0]["provenance"]["trace_id"] == "traceA"
    assert spans[0]["provenance"]["span_id"] == "s1"


def test_evidence_spans_for_turn_empty_when_no_traces():
    from simpleaudit.tracing import evidence_spans_for_turn

    corr = TraceCorrelation(audit_run_id="run_1")
    store = SpanStore()
    store.add({"span_id": "s1", "trace_id": "other", "name": "x", "attributes": {"openinference.span.kind": "LLM"}})
    assert evidence_spans_for_turn(corr, store, "turn_1") == []


# ---------------------------------------------------------------------------
# EphemeralOTLPReceiver (Promptfoo-style built-in receiver)
# ---------------------------------------------------------------------------

def _otlp_http_payload(trace_id: str = "a" * 32, span_id: str = "b" * 16, name: str = "POST /chat") -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "open-webui"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": name,
                                "kind": 2,
                                "startTimeUnixNano": 1_000_000_000,
                                "endTimeUnixNano": 2_000_000_000,
                                "attributes": [
                                    {"key": "http.url", "value": {"stringValue": "http://x/chat"}},
                                    {"key": "http.method", "value": {"stringValue": "POST"}},
                                ],
                                "status": {"code": 1},
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_ephemeral_receiver_binds_and_serves():
    import asyncio
    import httpx

    from simpleaudit.tracing import EphemeralOTLPReceiver

    async def _run():
        rx = EphemeralOTLPReceiver().start()
        try:
            assert rx.endpoint.startswith("http://127.0.0.1:")
            assert rx.actual_port > 0
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(rx.endpoint, json=_otlp_http_payload())
                assert r.status_code == 200
                assert r.json() == {"partialSuccess": {"rejectedSpans": 0}}
            assert len(rx.store) == 1
            spans = rx.store.by_trace("a" * 32)
            assert len(spans) == 1
            assert spans[0]["name"] == "POST /chat"
            assert spans[0]["attributes"]["http.url"] == "http://x/chat"
        finally:
            rx.stop()
        # Spans are discarded on stop (ephemeral by design).
        assert len(rx.store) == 0

    asyncio.run(_run())


def test_ephemeral_receiver_context_manager():
    import asyncio
    import httpx

    from simpleaudit.tracing import EphemeralOTLPReceiver

    async def _run():
        async with EphemeralOTLPReceiver() as rx:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(rx.endpoint, json=_otlp_http_payload())
                assert r.status_code == 200
            assert len(rx.store) == 1
        # After the context exits, the store is cleared.
        assert len(rx.store) == 0

    asyncio.run(_run())


def test_ephemeral_receiver_malformed_body_acks_rejection():
    import asyncio
    import httpx

    from simpleaudit.tracing import EphemeralOTLPReceiver

    async def _run():
        rx = EphemeralOTLPReceiver().start()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Not valid JSON -> parse fails -> rejectedSpans: 1, but HTTP 200.
                r = await client.post(rx.endpoint, content=b"not json", headers={"Content-Type": "application/json"})
                assert r.status_code == 200
                assert r.json() == {"partialSuccess": {"rejectedSpans": 1}}
            assert len(rx.store) == 0
        finally:
            rx.stop()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# TraceProvider (BuiltinOTLP / ExternalTraceProvider)
# ---------------------------------------------------------------------------

def test_builtin_otlp_provider_lifecycle():
    from simpleaudit.tracing import BuiltinOTLP

    provider = BuiltinOTLP()
    assert provider.endpoint is None  # not started
    provider.start()
    try:
        assert provider.endpoint.startswith("http://127.0.0.1:")
        assert provider.fetch("nope") == []
    finally:
        provider.stop()
    assert provider.endpoint is None


def test_external_trace_provider_requires_fetch():
    from simpleaudit.tracing import ExternalTraceProvider

    class _Stub(ExternalTraceProvider):
        def _fetch_remote(self, trace_id):
            return [{"span_id": "s", "trace_id": trace_id, "name": "x", "attributes": {}}]

    p = _Stub()
    assert p.fetch("t1") == [{"span_id": "s", "trace_id": "t1", "name": "x", "attributes": {}}]

    class _Unimpl(ExternalTraceProvider):
        pass

    try:
        _Unimpl().fetch("t1")
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass


# ---------------------------------------------------------------------------
# audit_with_tracing (Promptfoo-style one-call flow)
# ---------------------------------------------------------------------------

def test_audit_with_tracing_runs_and_attaches_evidence():
    import asyncio

    from simpleaudit.tracing import BuiltinOTLP, audit_with_tracing
    from tests.fakes import fixed_probe_auditor, fixed_severity_judge, fixed_target, make_auditor

    auditor = make_auditor(
        target=fixed_target("ok"),
        judge=fixed_severity_judge("pass"),
        auditor=fixed_probe_auditor("probe"),
        max_turns=1,
        show_progress=False,
    )

    async def _run():
        results = await audit_with_tracing(auditor, "safety", max_workers=2)
        assert len(results) == 8
        # The fake target emits no spans, so evidence_spans is not attached.
        for r in results.results:
            assert "evidence_spans" not in (r.judgment or {})

    asyncio.run(_run())


def test_audit_with_tracing_attaches_spans_when_target_emits():
    import asyncio
    import httpx

    from simpleaudit.tracing import BuiltinOTLP, audit_with_tracing
    from tests.fakes import fixed_probe_auditor, fixed_severity_judge, make_auditor

    # A target that, on each send, POSTs an OTLP span to the provider endpoint.
    class _EmittingTarget:
        def __init__(self, endpoint: str, trace_id: str):
            self.endpoint = endpoint
            self.trace_id = trace_id

        async def send(self, *, user, history=None, context=None, **kw):
            from simpleaudit.targets.base import TargetResponse

            # Emit a span for the trace the engine assigned to this turn.
            tp = (context.trace_headers.get("traceparent") if context else "") or ""
            tid = tp.split("-")[1] if tp and len(tp.split("-")) >= 2 else self.trace_id
            payload = _otlp_http_payload(trace_id=tid, span_id="c" * 16, name="LLM call")
            import json as _json

            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(self.endpoint, json=payload)
            return TargetResponse(content="ok")

    async def _run():
        provider = BuiltinOTLP()
        provider.start()
        try:
            target = _EmittingTarget(provider.endpoint, "d" * 32)
            auditor = make_auditor(
                target=_fake_client_wrapper(target),
                judge=fixed_severity_judge("pass"),
                auditor=fixed_probe_auditor("probe"),
                max_turns=1,
                show_progress=False,
            )
            # Override the engine's target with our emitting target.
            auditor.set_target(target)
            results = await audit_with_tracing(auditor, "safety", provider=provider, max_workers=1)
            # At least one result should have evidence_spans attached.
            with_evidence = [r for r in results.results if (r.judgment or {}).get("evidence_spans")]
            assert len(with_evidence) >= 1
            # The attached spans carry provenance.
            sample = with_evidence[0].judgment["evidence_spans"][0]
            assert "provenance" in sample
            assert sample["provenance"]["trace_id"]
        finally:
            provider.stop()

    asyncio.run(_run())


def _fake_client_wrapper(target):
    """Wrap a Target in a minimal FakeClient-shaped object for make_auditor."""
    from tests.fakes import FakeClient

    class _Wrap(FakeClient):
        def __init__(self, target):
            super().__init__(lambda **kw: "ok")
            self._target = target

        async def acompletion(self, **kwargs):
            resp = await self._target.send(user=kwargs.get("messages", [""])[-1] if kwargs.get("messages") else "")
            return resp.content

    return _Wrap(target)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def test_span_store_by_kind_and_trace():
    store = SpanStore()
    store.add({"span_id": "s1", "trace_id": "t1", "name": "retriever", "attributes": {"openinference.span.kind": "RETRIEVER"}})
    store.add({"span_id": "s2", "trace_id": "t1", "name": "llm", "attributes": {"openinference.span.kind": "LLM"}})
    store.add({"span_id": "s3", "trace_id": "t2", "name": "tool", "attributes": {"openinference.span.kind": "TOOL"}})

    assert len(store) == 3
    assert len(store.by_trace("t1")) == 2
    assert [s["span_id"] for s in store.by_kind("RETRIEVER")] == ["s1"]
    assert [s["span_id"] for s in store.by_kind("tool")] == ["s3"]  # case-insensitive


def test_normalize_span_maps_otlp_int_kind_to_name():
    from simpleaudit.tracing.store import normalize_span

    # OTLP gRPC/proto sends kind as a SpanKind int; normalize to its name.
    assert normalize_span({"span_id": "s", "trace_id": "t", "kind": 2})["kind"] == "SERVER"
    assert normalize_span({"span_id": "s", "trace_id": "t", "kind": 3})["kind"] == "CLIENT"
    # A string kind (OpenInference) is preserved as-is.
    assert normalize_span({"span_id": "s", "trace_id": "t", "kind": "RETRIEVER"})["kind"] == "RETRIEVER"
    # Unknown int falls back to its string form.
    assert normalize_span({"span_id": "s", "trace_id": "t", "kind": 99})["kind"] == "99"


def test_span_store_by_attribute():
    store = SpanStore()
    store.add({"span_id": "s1", "trace_id": "t1", "attributes": {"simpleaudit.turn_id": "turn_5"}})
    store.add({"span_id": "s2", "trace_id": "t1", "attributes": {"simpleaudit.turn_id": "turn_6"}})
    assert [s["span_id"] for s in store.by_attribute("simpleaudit.turn_id", "turn_5")] == ["s1"]


# ---------------------------------------------------------------------------
# otlp
# ---------------------------------------------------------------------------

def _otlp_payload():
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "agent-app"}}]},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "a" * 32,
                                "spanId": "b" * 16,
                                "name": "retriever",
                                "kind": 1,
                                "startTimeUnixNano": 1_000_000_000,
                                "endTimeUnixNano": 2_000_000_000,
                                "attributes": [
                                    {"key": "openinference.span.kind", "value": {"stringValue": "RETRIEVER"}},
                                    {"key": "docs", "value": {"stringValue": "doc1,doc2"}},
                                ],
                                "status": {"code": 1},
                            }
                        ]
                    }
                ],
            }
        ]
    }


def test_parse_otlp_json():
    spans = parse_otlp_json(_otlp_payload())
    assert len(spans) == 1
    s = spans[0]
    assert s["trace_id"] == "a" * 32
    assert s["span_id"] == "b" * 16
    assert s["attributes"]["openinference.span.kind"] == "RETRIEVER"
    assert s["attributes"]["service.name"] == "agent-app"  # resource attr merged
    assert s["start_time"] == 1.0
    assert s["end_time"] == 2.0


def test_parse_otlp_json_string_body():
    import json

    spans = parse_otlp_json(json.dumps(_otlp_payload()))
    assert len(spans) == 1


def test_parse_otlp_json_preserves_genai_content_attributes():
    payload = _otlp_http_payload()
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].extend(
        [
            {"key": "gen_ai.input.messages", "value": {"stringValue": "user prompt"}},
            {"key": "gen_ai.output.messages", "value": {"stringValue": "assistant output"}},
            {"key": "gen_ai.tool.call.arguments", "value": {"stringValue": '{"x":1}'}},
            {
                "key": "gen_ai.retrieval.documents",
                "value": {"arrayValue": {"values": [{"stringValue": "document text"}]}},
            },
        ]
    )
    span = parse_otlp_json(payload)[0]
    assert span["attributes"]["gen_ai.input.messages"] == "user prompt"
    assert span["attributes"]["gen_ai.output.messages"] == "assistant output"
    assert span["attributes"]["gen_ai.tool.call.arguments"] == '{"x":1}'
    assert span["attributes"]["gen_ai.retrieval.documents"] == ["document text"]


@pytest.mark.asyncio
async def test_otlp_receiver_ingests():
    receiver = OTLPTraceReceiver()
    ack = await receiver.handle(_otlp_payload())
    assert ack["partialSuccess"]["rejectedSpans"] == 0
    assert len(receiver.store) == 1
    assert receiver.trace_ids() == ["a" * 32]


# ---------------------------------------------------------------------------
# selection
# ---------------------------------------------------------------------------

def _span(sid, kind, size_text="x" * 100):
    return {
        "span_id": sid,
        "trace_id": "t1",
        "name": kind.lower(),
        "kind": kind,
        "attributes": {"data": size_text},
    }


def test_select_spans_prefers_evidence_kinds():
    spans = [_span("s1", "RETRIEVER"), _span("s2", "CHAIN"), _span("s3", "TOOL")]
    result = select_spans(spans)
    selected_ids = [s["span_id"] for s in result.selected]
    # CHAIN is noise, dropped; RETRIEVER + TOOL kept
    assert "s2" not in selected_ids
    assert "s1" in selected_ids and "s3" in selected_ids


def test_select_spans_token_budget_elides():
    # Each span ~100+ chars; budget 250 should keep ~2 and elide the rest.
    spans = [_span(f"s{i}", "TOOL") for i in range(5)]
    result = select_spans(spans, token_budget=250)
    assert len(result.selected) < 5
    assert result.elided_count > 0
    assert result.budget_used <= 250


def test_select_spans_no_evidence_falls_back_to_noise():
    spans = [_span("s1", "CHAIN")]
    result = select_spans(spans)
    assert [s["span_id"] for s in result.selected] == ["s1"]


def test_summarize_for_judge_includes_provenance():
    spans = [_span("s1", "RETRIEVER")]
    result = select_spans(spans)
    text = summarize_for_judge(result)
    assert "RETRIEVER" in text
    assert "trace=" in text


def test_summarize_for_judge_empty():
    result = select_spans([])
    assert "no evidence spans" in summarize_for_judge(result)


# ---------------------------------------------------------------------------
# trace-aware judge (evidence block reaches the judge prompt)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_receives_evidence_spans():
    from tests.fakes import make_auditor, fixed_target, fixed_severity_judge

    captured = {}

    def judge_fn(**kw):
        # Record the user message the judge saw.
        for m in kw.get("messages", []):
            if m.get("role") == "user":
                captured["user"] = m["content"]
        return '{"severity": "pass", "issues_found": [], "summary": "ok", "recommendations": []}'

    auditor = make_auditor(
        target=fixed_target("I cannot help with that."),
        judge=fixed_severity_judge("pass"),
    )
    # Replace the judge client with one that captures the prompt.
    from tests.fakes import FakeClient

    auditor.judge_client = FakeClient(judge_fn)

    spans = [
        {
            "span_id": "s1",
            "trace_id": "t1",
            "name": "retriever",
            "kind": "RETRIEVER",
            "attributes": {"openinference.span.kind": "RETRIEVER", "documents": "secret-doc"},
        }
    ]
    result = await auditor.run_scenario(
        name="Test",
        description="desc",
        evidence_spans=spans,
    )
    assert "OBSERVED INTERNAL TRACES" in captured["user"]
    assert "RETRIEVER" in captured["user"]
    assert "secret-doc" in captured["user"]


@pytest.mark.asyncio
async def test_judge_without_evidence_has_no_traces_block():
    from tests.fakes import make_auditor, fixed_target, fixed_severity_judge, FakeClient

    captured = {}

    def judge_fn(**kw):
        for m in kw.get("messages", []):
            if m.get("role") == "user":
                captured["user"] = m["content"]
        return '{"severity": "pass", "issues_found": [], "summary": "ok", "recommendations": []}'

    auditor = make_auditor(
        target=fixed_target("I cannot help with that."),
        judge=fixed_severity_judge("pass"),
    )
    auditor.judge_client = FakeClient(judge_fn)
    await auditor.run_scenario(name="Test", description="desc")
    assert "OBSERVED INTERNAL TRACES" not in captured["user"]


# ---------------------------------------------------------------------------
# SharedOTLPReceiver + TraceSessionManager
# ---------------------------------------------------------------------------

def test_trace_session_lifecycle():
    from simpleaudit.tracing import TraceSession

    s = TraceSession(audit_id="a1", ttl=10)
    assert not s.expired
    s.add_many([
        {"trace_id": "t1", "span_id": "s1", "name": "span1"},
        {"trace_id": "t1", "span_id": "s2", "name": "span2"},
    ])
    assert len(s) == 2
    assert len(s.spans_for_trace("t1")) == 2
    assert len(s.spans_for_trace("t2")) == 0
    s.close()
    assert s.expired
    assert len(s) == 0
    assert s.spans_for_trace("t1") == []


def test_trace_session_ttl_expiry():
    import time
    from simpleaudit.tracing import TraceSession

    s = TraceSession(audit_id="a1", ttl=0.1)
    s.add_many([{"trace_id": "t1", "span_id": "s1", "name": "x"}])
    assert len(s) == 1
    time.sleep(0.15)
    assert s.expired
    assert len(s) == 0


def test_session_manager_routing():
    from simpleaudit.tracing import TraceSessionManager

    mgr = TraceSessionManager()
    s1 = mgr.create("audit_1", ttl=60)
    s2 = mgr.create("audit_2", ttl=60)
    mgr.register_trace("audit_1", "trace_A")
    mgr.register_trace("audit_2", "trace_B")

    # Route spans — each goes to the right session.
    mgr.route_spans([
        {"trace_id": "trace_A", "span_id": "s1", "name": "span_A"},
        {"trace_id": "trace_B", "span_id": "s2", "name": "span_B"},
        {"trace_id": "trace_UNKNOWN", "span_id": "s3", "name": "dropped"},
    ])
    assert len(s1) == 1
    assert len(s2) == 1
    assert s1.spans_for_trace("trace_A")[0]["name"] == "span_A"
    assert s2.spans_for_trace("trace_B")[0]["name"] == "span_B"

    # Close audit_1 — its spans are discarded.
    mgr.close("audit_1")
    assert len(s1) == 0
    assert mgr.get("audit_1") is None
    # audit_2 still has its span.
    assert len(s2) == 1


def test_session_manager_sweep():
    import time
    from simpleaudit.tracing import TraceSessionManager

    mgr = TraceSessionManager()
    s1 = mgr.create("audit_1", ttl=0.1)
    s2 = mgr.create("audit_2", ttl=60)
    s1.add_many([{"trace_id": "t1", "span_id": "s1", "name": "x"}])
    s2.add_many([{"trace_id": "t2", "span_id": "s2", "name": "y"}])
    time.sleep(0.15)
    closed = mgr.sweep()
    assert closed == 1
    assert mgr.get("audit_1") is None
    assert mgr.get("audit_2") is not None


def test_shared_receiver_routes_spans():
    import asyncio
    import httpx
    from simpleaudit.tracing import SharedOTLPReceiver

    async def _run():
        shared = SharedOTLPReceiver(host="127.0.0.1", port=0).start()
        try:
            session = shared.sessions.create("audit_1", ttl=60)
            shared.sessions.register_trace("audit_1", "a" * 32)

            # Send a span via HTTP to the shared receiver.
            payload = {
                "resourceSpans": [{
                    "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "test"}}]},
                    "scopeSpans": [{"spans": [{
                        "traceId": "a" * 32, "spanId": "b" * 16, "name": "routed-span",
                        "kind": 2, "startTimeUnixNano": 1000000000, "endTimeUnixNano": 2000000000,
                        "attributes": [], "status": {"code": 1},
                    }]}],
                }]
            }
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(shared.endpoint, json=payload)
                assert r.status_code == 200

            # The span should be in the session.
            spans = session.spans_for_trace("a" * 32)
            assert len(spans) == 1
            assert spans[0]["name"] == "routed-span"

            # A span for an unregistered trace is dropped.
            payload2 = dict(payload)
            payload2["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"] = "c" * 32
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(shared.endpoint, json=payload2)
                assert r.status_code == 200
            assert len(session) == 1  # still just one span

            shared.sessions.close("audit_1")
            assert len(session) == 0
        finally:
            shared.stop()

    asyncio.run(_run())


def test_shared_receiver_parallel_audits():
    """Two concurrent audit sessions on the same shared receiver."""
    import asyncio
    import httpx
    from simpleaudit.tracing import SharedOTLPReceiver

    async def _run():
        shared = SharedOTLPReceiver(host="127.0.0.1", port=0).start()
        try:
            s1 = shared.sessions.create("audit_1", ttl=60)
            s2 = shared.sessions.create("audit_2", ttl=60)
            shared.sessions.register_trace("audit_1", "a" * 32)
            shared.sessions.register_trace("audit_2", "b" * 32)

            async def send_span(trace_id: str, span_id: str, name: str):
                payload = {
                    "resourceSpans": [{
                        "resource": {"attributes": []},
                        "scopeSpans": [{"spans": [{
                            "traceId": trace_id, "spanId": span_id, "name": name,
                            "kind": 2, "startTimeUnixNano": 1000000000, "endTimeUnixNano": 2000000000,
                            "attributes": [], "status": {"code": 1},
                        }]}],
                    }]
                }
                async with httpx.AsyncClient(timeout=10) as client:
                    r = await client.post(shared.endpoint, json=payload)
                    assert r.status_code == 200

            # Send spans for both audits concurrently.
            await asyncio.gather(
                send_span("a" * 32, "s1", "audit1-span"),
                send_span("b" * 32, "s2", "audit2-span"),
            )

            assert len(s1) == 1
            assert len(s2) == 1
            assert s1.spans_for_trace("a" * 32)[0]["name"] == "audit1-span"
            assert s2.spans_for_trace("b" * 32)[0]["name"] == "audit2-span"

            shared.sessions.close("audit_1")
            shared.sessions.close("audit_2")
        finally:
            shared.stop()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Heavy-traffic protections
# ---------------------------------------------------------------------------

def test_session_max_spans_cap():
    from simpleaudit.tracing import TraceSession

    s = TraceSession(audit_id="a1", ttl=60, max_spans=5)
    stored = s.add_many([{"trace_id": "t1", "span_id": f"s{i}", "name": f"span{i}"} for i in range(10)])
    assert stored == 5
    assert len(s) == 5
    assert s.full
    assert s.dropped == 5
    # More spans are still dropped.
    stored2 = s.add_many([{"trace_id": "t1", "span_id": "s99", "name": "extra"}])
    assert stored2 == 0
    assert s.dropped == 6


def test_manager_early_rejection_no_sessions():
    from simpleaudit.tracing import TraceSessionManager

    mgr = TraceSessionManager()
    # No sessions → spans are dropped immediately.
    mgr.route_spans([{"trace_id": "t1", "span_id": "s1", "name": "x"}] * 100)
    assert mgr.dropped_no_session == 100
    assert mgr.total_spans == 0


def test_manager_global_cap():
    from simpleaudit.tracing import TraceSessionManager

    mgr = TraceSessionManager(max_total_spans=10)
    s1 = mgr.create("a1", ttl=60, max_spans=100)
    s2 = mgr.create("a2", ttl=60, max_spans=100)
    mgr.register_trace("a1", "t1")
    mgr.register_trace("a2", "t2")

    # Fill up to the global cap.
    mgr.route_spans([{"trace_id": "t1", "span_id": f"s{i}", "name": f"x{i}"} for i in range(10)])
    assert mgr.total_spans == 10
    assert mgr.dropped_global_cap == 0

    # More spans → dropped by global cap.
    mgr.route_spans([{"trace_id": "t2", "span_id": "s99", "name": "y"}])
    assert mgr.total_spans == 10
    assert mgr.dropped_global_cap == 1


def test_shared_receiver_stats():
    import asyncio
    import httpx
    from simpleaudit.tracing import SharedOTLPReceiver

    async def _run():
        shared = SharedOTLPReceiver(host="127.0.0.1", port=0, max_total_spans=5).start()
        try:
            # No session → early rejection.
            payload = {
                "resourceSpans": [{
                    "resource": {"attributes": []},
                    "scopeSpans": [{"spans": [{
                        "traceId": "a" * 32, "spanId": "b" * 16, "name": "orphan",
                        "kind": 2, "startTimeUnixNano": 1000000000, "endTimeUnixNano": 2000000000,
                        "attributes": [], "status": {"code": 1},
                    }]}],
                }]
            }
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(shared.endpoint, json=payload)
                assert r.status_code == 200
            stats = shared.stats
            assert stats["dropped_no_session"] >= 1
            assert stats["total_spans"] == 0

            # Create a session and send spans up to the cap.
            session = shared.create_session("audit_1")
            shared.sessions.register_trace("audit_1", "a" * 32)
            for i in range(10):
                p = dict(payload)
                p["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["spanId"] = f"{'c' * 14}{i:02x}"
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(shared.endpoint, json=p)
            stats = shared.stats
            assert stats["total_spans"] == 5  # capped at max_total_spans=5
            assert stats["dropped_global_cap"] >= 5
        finally:
            shared.stop()

    asyncio.run(_run())
