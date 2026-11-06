"""Regression tests for tracing/auth defects found in the 2026-11 system audit
(see AUDIT.md, findings T1-T9)."""

import json
import threading
import time

import pytest

from simpleaudit.tracing import OTLPAuth, OTLPTraceReceiver
from simpleaudit.tracing.auth import TARGET_ID_ATTR
from simpleaudit.tracing.shared import SharedOTLPReceiver, TraceSession, TraceSessionManager
from simpleaudit.tracing.store import SpanStore


def _export(span_id="b" * 16, attrs=None, output="hello"):
    attributes = [{"key": "output.value", "value": {"stringValue": output}}]
    for k, v in (attrs or {}).items():
        attributes.append({"key": k, "value": {"stringValue": v}})
    return json.dumps({"resourceSpans": [{"resource": {"attributes": []}, "scopeSpans": [{"spans": [{
        "traceId": "a" * 32, "spanId": span_id, "name": "chat", "kind": 1,
        "startTimeUnixNano": 0, "endTimeUnixNano": 1, "attributes": attributes,
        "status": {"code": 0},
    }]}]}]})


def _auth():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("token-A", target_id="target_A")
    auth.add_bearer("token-B", target_id="target_B")
    return auth


async def test_client_cannot_forge_target_id():
    # T1: tag_spans used setdefault, so a client-supplied target id survived.
    rx = OTLPTraceReceiver(auth=_auth())
    await rx.handle(_export(attrs={TARGET_ID_ATTR: "target_B"}), authorization="Bearer token-A")
    assert rx.store.by_attribute(TARGET_ID_ATTR, "target_B") == []
    assert len(rx.store.by_attribute(TARGET_ID_ATTR, "target_A")) == 1


async def test_target_cannot_overwrite_another_targets_span():
    # T2: spans were keyed by span_id alone, so A could replace B's evidence.
    rx = OTLPTraceReceiver(auth=_auth())
    await rx.handle(_export(output="genuine"), authorization="Bearer token-B")
    await rx.handle(_export(output="forged"), authorization="Bearer token-A")
    [span] = rx.store.all()
    assert span["attributes"][TARGET_ID_ATTR] == "target_B"
    assert span["attributes"]["output.value"] == "genuine"


def test_same_target_can_still_resend_a_span():
    store = SpanStore()
    store.add({"span_id": "s", "trace_id": "t", "attributes": {TARGET_ID_ATTR: "A", "v": 1}})
    store.add({"span_id": "s", "trace_id": "t", "attributes": {TARGET_ID_ATTR: "A", "v": 2}})
    assert store.get("s")["attributes"]["v"] == 2


def test_shared_receiver_defaults_to_loopback_and_accepts_auth():
    # T3: bound 0.0.0.0 by default with no way to require auth.
    auth = _auth()
    shared = SharedOTLPReceiver(port=0, auth=auth)
    assert shared.host == "127.0.0.1"
    shared.start()
    try:
        assert shared._receiver.auth is auth
    finally:
        shared.stop()


def test_grpc_receiver_accepts_auth():
    # T4: the gRPC receiver had no auth at all.
    pytest.importorskip("grpc")
    pytest.importorskip("opentelemetry.proto")
    from simpleaudit.tracing.otlp import EphemeralOTLPGRPCReceiver

    rx = EphemeralOTLPGRPCReceiver(auth=_auth())
    servicer = rx._make_servicer()

    class Aborted(Exception):
        pass

    class Ctx:
        def __init__(self, md):
            self.md = md

        def invocation_metadata(self):
            return self.md

        def abort(self, code, msg):
            raise Aborted(code)

    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2
    with pytest.raises(Aborted):
        servicer.Export(trace_service_pb2.ExportTraceServiceRequest(), Ctx([]))
    servicer.Export(
        trace_service_pb2.ExportTraceServiceRequest(), Ctx([("authorization", "Bearer token-A")])
    )


def test_active_session_outlives_ttl():
    # T5: expiry counted from creation, so audits longer than ttl lost evidence.
    mgr = TraceSessionManager()
    session = mgr.create("a", ttl=0.3)
    mgr.register_trace("a", "t1")
    for _ in range(4):
        time.sleep(0.1)
        mgr.route_spans([{"span_id": f"s{_}", "trace_id": "t1"}])
    assert not session.expired
    assert len(session.spans_for_trace("t1")) == 4
    time.sleep(0.4)
    assert session.expired


def test_auth_is_thread_safe_under_concurrent_mutation():
    # T6: verify() iterated dicts that other threads were mutating.
    auth = OTLPAuth(mode="bearer")
    for i in range(50):
        auth.add_bearer(f"t{i}", target_id=f"x{i}")
    errors = []
    stop = threading.Event()

    def churn():
        i = 1000
        while not stop.is_set():
            auth.add_bearer(f"t{i}", target_id="y")
            i += 1

    th = threading.Thread(target=churn)
    th.start()
    try:
        for _ in range(300):
            try:
                auth.verify("Bearer nope")
            except RuntimeError as exc:
                errors.append(exc)
    finally:
        stop.set()
        th.join()
    assert errors == []


async def test_receiver_does_not_reveal_which_usernames_exist():
    # T7: unknown_username vs invalid_credentials enumerated users.
    auth = OTLPAuth(mode="basic")
    auth.add_basic("alice", "pw", target_id="a")
    rx = OTLPTraceReceiver(auth=auth)
    import base64

    def hdr(u, p):
        return "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()

    unknown = await rx.handle(_export(), authorization=hdr("bob", "x"))
    wrong = await rx.handle(_export(), authorization=hdr("alice", "x"))
    assert unknown == wrong


async def test_receiver_handle_rejects_malformed_body():
    # T12: handle() raised on malformed payloads instead of returning 400.
    rx = OTLPTraceReceiver()
    assert (await rx.handle("[]"))["status"] == 400
    assert (await rx.handle("{not json"))["status"] == 400


def test_empty_expired_session_is_removed_and_closed():
    # T9: `if session` treated an empty session (len 0) as missing.
    mgr = TraceSessionManager()
    mgr.create("a", ttl=0.01)
    time.sleep(0.05)
    assert mgr.get("a") is None
    assert "a" not in mgr._sessions
    s2 = mgr.create("b", ttl=60)
    mgr.close("b")
    assert s2._closed is True


def test_trace_session_close_flag_default():
    assert TraceSession(audit_id="x").expired is False
