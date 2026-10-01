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
