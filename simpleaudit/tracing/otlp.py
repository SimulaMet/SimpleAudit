"""
OTLP ingestion — accept OpenTelemetry trace exports and normalize to spans.

SimpleAudit can act as an OTLP/HTTP trace receiver so instrumented targets
(OpenInference, OpenLLMetry, MLflow, raw OTel) can push their spans directly.

Two entry points:

    - :func:`parse_otlp_json` — parse an OTLP/HTTP JSON ``ExportTraceServiceRequest``
      payload into normalized spans (transport-agnostic, easy to test).
    - :class:`OTLPTraceReceiver` — a minimal async receiver that accepts a
      POSTed OTLP JSON body, stores the spans, and returns the OTLP ack.

The protobuf wire format is also standard OTLP; if you need it, run an OTel
Collector in front and have it forward JSON, or extend :func:`parse_otlp_json`
with a protobuf decoder. The normalization target is the same either way.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .store import SpanStore, normalize_span

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from .auth import Authenticator


def _proto_ts_to_unix(ns: Any) -> Optional[float]:
    if ns is None:
        return None
    # ``MessageToDict`` renders int64 nanosecond timestamps as strings.
    return int(ns) / 1e9


def parse_otlp_json(payload: Any) -> List[Dict[str, Any]]:
    """Parse an OTLP/HTTP JSON ExportTraceServiceRequest into raw spans.

    Handles the standard shape::

        {"resourceSpans": [
            {"resource": {"attributes": [...]},
             "scopeSpans": [
                {"spans": [
                    {"traceId": "...", "spanId": "...", "name": "...",
                     "kind": 1, "startTimeUnixNano": ..., "endTimeUnixNano": ...,
                     "attributes": [{"key": "...", "value": {"stringValue": "..."}}],
                     "status": {"code": 1}}
                ]}
            ]}
        ]}

    ``traceId`` / ``spanId`` are hex strings in OTLP JSON. Attribute values use
    the OTLP ``AnyValue`` oneof (stringValue, intValue, doubleValue, boolValue,
    arrayValue, kvlistValue).
    """
    if isinstance(payload, (str, bytes)):
        payload = json.loads(payload)

    spans: List[Dict[str, Any]] = []
    for rs in payload.get("resourceSpans") or []:
        resource_attrs = _decode_attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or []:
            for span in ss.get("spans") or []:
                attrs = dict(resource_attrs)
                attrs.update(_decode_attrs(span.get("attributes")))
                spans.append(
                    {
                        "trace_id": span.get("traceId") or "",
                        "span_id": span.get("spanId") or "",
                        "parent_span_id": span.get("parentSpanId") or None,
                        "name": span.get("name") or "span",
                        "kind": span.get("kind"),
                        "start_time": _proto_ts_to_unix(span.get("startTimeUnixNano")),
                        "end_time": _proto_ts_to_unix(span.get("endTimeUnixNano")),
                        "status": _status_code(span.get("status")),
                        "attributes": attrs,
                    }
                )
    return spans


def _decode_attrs(attrs: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for a in attrs or []:
        key = a.get("key")
        out[key] = _decode_any_value((a.get("value") or {}))
    return out


def _decode_any_value(v: Dict[str, Any]) -> Any:
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])
    if "doubleValue" in v:
        return float(v["doubleValue"])
    if "boolValue" in v:
        return bool(v["boolValue"])
    if "arrayValue" in v:
        return [_decode_any_value(x) for x in (v["arrayValue"].get("values") or [])]
    if "kvlistValue" in v:
        return _decode_attrs(v["kvlistValue"].get("values"))
    # Unknown / empty
    return None


def _status_code(status: Optional[Dict[str, Any]]) -> str:
    if not status:
        return "OK"
    code = status.get("code", 0)
    return {0: "OK", 1: "OK", 2: "ERROR"}.get(code, "OK")


class OTLPTraceReceiver:
    """Minimal async OTLP/HTTP JSON trace receiver.

    Usage (e.g. with any ASGI framework)::

        receiver = OTLPTraceReceiver()
        # POST /v1/traces  ->  await receiver.handle(request_body_bytes)

    Or standalone::

        store = SpanStore()
        receiver = OTLPTraceReceiver(store=store)
        await receiver.handle(open("export.json").read())
    """

    def __init__(
        self,
        store: Optional[SpanStore] = None,
        authenticator: Optional["Authenticator"] = None,
    ) -> None:
        self.store = store or SpanStore()
        # Optional auth gate. When set, handle() calls it with the request's
        # Authorization header and rejects the export (401) on failure. When
        # None the receiver is open — the historical, backward-compatible
        # behaviour for the local, single-audit ephemeral case.
        self.authenticator = authenticator

    async def handle(self, body: Any, authorization: Optional[str] = None) -> Dict[str, Any]:
        """Ingest an OTLP JSON export body; return the OTLP ack payload.

        When an ``authenticator`` was provided, *authorization* (the raw
        ``Authorization`` header) is checked first; a failed check returns a
        401 ``{"error": ...}`` payload and no spans are stored.
        """
        if self.authenticator is not None:
            result = self.authenticator(authorization)
            if not result.authenticated:
                return {"status": 401, "error": {"code": "unauthorized", "message": "Invalid or missing OTLP credentials."}}
        raw_spans = parse_otlp_json(body)
        self.store.add_many(raw_spans)
        # OTLP ack: partialSuccess with the number of rejected spans (0 here).
        return {"partialSuccess": {"rejectedSpans": 0}}

    def trace_ids(self) -> List[str]:
        seen: List[str] = []
        for s in self.store.all():
            if s["trace_id"] and s["trace_id"] not in seen:
                seen.append(s["trace_id"])
        return seen


class EphemeralOTLPReceiver:
    """A self-contained OTLP/HTTP trace receiver that lives for one audit.

    Promptfoo-style: the auditor opens this receiver when an audit starts,
    points the (controlled) target's ``OTEL_EXPORTER_OTLP_ENDPOINT`` at it,
    collects the spans the target emits during the run, and closes it when the
    audit ends. No external collector, no persistent store — the spans are
    held in an in-memory :class:`SpanStore` and discarded on :meth:`close`.

    It speaks the **OTLP/HTTP JSON** protocol (``POST /v1/traces``), which is
    what ``OTEL_EXPORTER_OTLP_PROTOCOL=http/json`` selects. For the default
    ``grpc`` protocol, run an OTel Collector in front that forwards JSON, or
    point the target at this receiver's HTTP endpoint.

    Usage::

        async with EphemeralOTLPReceiver() as rx:
            # rx.endpoint == "http://127.0.0.1:<port>/v1/traces"
            # set the target's OTEL_EXPORTER_OTLP_ENDPOINT to rx.endpoint
            results = await auditor.run_async("safety", trace_correlation=corr)
            spans = rx.store.by_trace(trace_id)   # inspect / feed the judge

    The server runs on a background thread (aiohttp) so it works from both
    sync and async callers. Port 0 binds an ephemeral port.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        store: Optional[SpanStore] = None,
        authenticator: Optional["Authenticator"] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.store = store or SpanStore()
        # Optional auth gate (see OTLPTraceReceiver). When set, each POST is
        # checked against the request's Authorization header and rejected with
        # 401 on failure. None = open receiver (default).
        self.authenticator = authenticator
        self._runner: Optional[Any] = None
        self._site: Optional[Any] = None
        self._thread: Optional[Any] = None
        self._loop: Optional[Any] = None
        self._ready: Optional[Any] = None
        self._actual_port: Optional[int] = None
        self._closed = False
        self._start_error: Optional[BaseException] = None

    @property
    def endpoint(self) -> str:
        """The OTLP/HTTP traces URL to configure the target's exporter to."""
        return f"http://{self.host}:{self._actual_port}/v1/traces"

    @property
    def actual_port(self) -> int:
        return self._actual_port

    async def _handle_traces(self, request: Any) -> Any:
        from aiohttp import web

        if self.authenticator is not None:
            result = self.authenticator(request.headers.get("Authorization"))
            if not result.authenticated:
                return web.json_response(
                    {"error": {"code": "unauthorized", "message": "Invalid or missing OTLP credentials."}},
                    status=401,
                )

        content_type = request.headers.get("Content-Type", "")
        body_bytes = await request.read()
        try:
            if "protobuf" in content_type:
                raw_spans = _parse_otlp_http_protobuf(body_bytes)
            else:
                raw_spans = parse_otlp_json(body_bytes.decode("utf-8"))
            self.store.add_many(raw_spans)
        except Exception:
            # Never fail the export; ack with a rejection count so the target
            # doesn't retry-loop. The audit continues regardless.
            return web.json_response({"partialSuccess": {"rejectedSpans": 1}}, status=200)
        return web.json_response({"partialSuccess": {"rejectedSpans": 0}}, status=200)

    def _serve(self, loop: Any) -> None:
        import asyncio

        from aiohttp import web

        try:
            app = web.Application()
            app.router.add_post("/v1/traces", self._handle_traces)
            runner = web.AppRunner(app)
            loop.run_until_complete(runner.setup())
            site = web.TCPSite(runner, self.host, self.port)
            loop.run_until_complete(site.start())
            self._runner = runner
            self._site = site
            self._actual_port = site._server.sockets[0].getsockname()[1]
            self._ready.set()
            loop.run_forever()
        except Exception as exc:  # surface the real cause to the caller
            self._start_error = exc
            self._ready.set()
        finally:
            if self._runner is not None:
                try:
                    loop.run_until_complete(self._runner.cleanup())
                except Exception:
                    pass

    def start(self) -> "EphemeralOTLPReceiver":
        """Start the receiver on a background thread; bind an ephemeral port.

        Bounded retry: under load (e.g. CI) the first bind can be slow, so we
        give the serve thread a few attempts before giving up.
        """
        import asyncio
        import threading

        if self._thread is not None:
            return self
        last_error: Optional[BaseException] = None
        for _ in range(3):
            self._loop = asyncio.new_event_loop()
            self._ready = threading.Event()
            self._start_error = None
            self._thread = threading.Thread(target=self._serve, args=(self._loop,), daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=10):
                # Thread is stuck; tear it down and retry.
                self._closed = True
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=2)
                self._thread = None
                last_error = RuntimeError("EphemeralOTLPReceiver failed to start within 10s")
                continue
            if self._start_error is not None:
                self._thread = None
                last_error = self._start_error
                continue
            return self
        raise RuntimeError(
            "EphemeralOTLPReceiver failed to start"
        ) from last_error

    def stop(self) -> None:
        """Stop the server and discard the in-memory spans."""
        if self._closed or self._loop is None:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._loop.close()
        self._loop = None
        # Discard spans: this receiver is ephemeral by design.
        self.store = SpanStore()

    def __enter__(self) -> "EphemeralOTLPReceiver":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    async def __aenter__(self) -> "EphemeralOTLPReceiver":
        return self.start()

    async def __aexit__(self, *exc: Any) -> None:
        self.stop()


class EphemeralOTLPGRPCReceiver:
    """A self-contained OTLP/gRPC trace receiver that lives for one audit.

    Speaks the **OTLP/gRPC** protocol (``TraceService/Export``), which is
    what ``OTEL_EXPORTER_OTLP_PROTOCOL=grpc`` (the default) selects on port
    4317. Useful when the target (e.g. Open WebUI) exports over gRPC and you
    want to capture spans without running a full OTel Collector.

    Usage::

        rx = EphemeralOTLPGRPCReceiver(port=4317).start()
        # target's OTEL_EXPORTER_OTLP_ENDPOINT = http://127.0.0.1:4317
        # ... run audit ...
        rx.stop()

    The gRPC server runs on a background thread. Port 0 binds an ephemeral
    port. Spans are discarded on :meth:`stop` (ephemeral by design).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0, store: Optional[SpanStore] = None) -> None:
        self.host = host
        self.port = port
        self.store = store or SpanStore()
        self._server: Optional[Any] = None
        self._thread: Optional[Any] = None
        self._ready: Optional[Any] = None
        self._actual_port: Optional[int] = None
        self._closed = False

    @property
    def endpoint(self) -> str:
        """The OTLP/gRPC endpoint to configure the target's exporter to."""
        return f"http://{self.host}:{self._actual_port}"

    @property
    def actual_port(self) -> int:
        return self._actual_port

    def _make_servicer(self) -> Any:
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

        store = self.store

        class _TraceServicer(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, request, context):
                raw_spans = _parse_otlp_grpc(request)
                store.add_many(raw_spans)
                return trace_service_pb2.ExportTraceServiceResponse(
                    partial_success=trace_service_pb2.ExportTracePartialSuccess(rejected_spans=0)
                )

        return _TraceServicer()

    def _serve(self) -> None:
        import grpc
        from concurrent import futures
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        trace_service_pb2_grpc.add_TraceServiceServicer_to_server(self._make_servicer(), server)
        self._actual_port = server.add_insecure_port(f"{self.host}:{self.port}")
        server.start()
        self._server = server
        self._ready.set()
        try:
            server.wait_for_termination()
        except Exception:
            pass

    def start(self) -> "EphemeralOTLPGRPCReceiver":
        """Start the gRPC server on a background thread."""
        import threading

        if self._thread is not None:
            return self
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("EphemeralOTLPGRPCReceiver failed to start within 10s")
        return self

    def stop(self) -> None:
        """Stop the gRPC server and discard the in-memory spans."""
        if self._closed:
            return
        self._closed = True
        if self._server is not None:
            self._server.stop(grace=2)
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        # Discard spans: ephemeral by design.
        self.store = SpanStore()

    def __enter__(self) -> "EphemeralOTLPGRPCReceiver":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    async def __aenter__(self) -> "EphemeralOTLPGRPCReceiver":
        return self.start()

    async def __aexit__(self, *exc: Any) -> None:
        self.stop()


def _parse_otlp_grpc(request: Any) -> List[Dict[str, Any]]:
    """Parse an OTLP/gRPC ``ExportTraceServiceRequest`` into raw span dicts.

    Delegates the wire-format decoding to ``opentelemetry-proto`` (the
    canonical OTLP protobuf definitions) via ``json_format.MessageToDict``,
    then maps the resulting dict to the store's raw-span schema. This keeps
    the parsing spec-correct and avoids hand-rolling the protobuf decoding.

    Returns raw dicts (pre-normalization) so the caller can use
    ``SpanStore.add_many`` which normalizes internally.
    """
    from google.protobuf.json_format import MessageToDict

    data = MessageToDict(request, preserving_proto_field_name=True)
    return _spans_from_otlp_dict(data)


def _parse_otlp_http_protobuf(body: bytes) -> List[Dict[str, Any]]:
    """Parse an OTLP/HTTP protobuf ``ExportTraceServiceRequest`` body."""
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2

    req = trace_service_pb2.ExportTraceServiceRequest()
    req.ParseFromString(body)
    return _parse_otlp_grpc(req)


def _spans_from_otlp_dict(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map an OTLP ``ExportTraceServiceRequest`` dict to raw span dicts.

    ``data`` is the ``MessageToDict(..., preserving_proto_field_name=True)``
    shape: snake_case field names, ``trace_id``/``span_id`` as base64 strings,
    ``start_time_unix_nano``/``end_time_unix_nano`` as string nanoseconds,
    ``kind`` as an enum name (e.g. ``SPAN_KIND_SERVER``), and ``attributes``
    as a list of ``{"key": ..., "value": AnyValue}`` entries.
    """
    spans: List[Dict[str, Any]] = []
    for rs in data.get("resource_spans") or []:
        resource_attrs = _attrs_from_list((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scope_spans") or []:
            for span in ss.get("spans") or []:
                attrs = dict(resource_attrs)
                attrs.update(_attrs_from_list(span.get("attributes")))
                spans.append(
                    {
                        "trace_id": _b64_to_hex(span.get("trace_id")),
                        "span_id": _b64_to_hex(span.get("span_id")),
                        "parent_span_id": _b64_to_hex(span.get("parent_span_id")) or None,
                        "name": span.get("name") or "span",
                        "kind": span.get("kind"),
                        "start_time": _proto_ts_to_unix(span.get("start_time_unix_nano")),
                        "end_time": _proto_ts_to_unix(span.get("end_time_unix_nano")),
                        "status": _status_from_dict(span.get("status")),
                        "attributes": attrs,
                    }
                )
    return spans


def _status_from_dict(status: Optional[Dict[str, Any]]) -> str:
    """Map a ``MessageToDict`` status (code is an enum-name string) to OK/ERROR."""
    if not status:
        return "OK"
    code = status.get("code")
    if isinstance(code, str):
        return "ERROR" if "ERROR" in code.upper() else "OK"
    return _status_code(status)


def _b64_to_hex(b64: Optional[str]) -> str:
    """Decode a base64-encoded OTLP id (``MessageToDict`` form) to hex."""
    if not b64:
        return ""
    import base64

    try:
        return base64.b64decode(b64).hex()
    except Exception:
        return b64


def _attrs_from_list(attr_list: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Convert an OTLP attribute list (``[{"key", "value"}]``) to plain values."""
    out: Dict[str, Any] = {}
    for a in attr_list or []:
        out[a.get("key")] = _any_value(a.get("value"))
    return out


def _any_value(v: Any) -> Any:
    """Convert an OTLP ``AnyValue`` (snake_case dict form) to a Python scalar."""
    if not isinstance(v, dict):
        return v
    if "string_value" in v:
        return v["string_value"]
    if "int_value" in v:
        return int(v["int_value"])
    if "double_value" in v:
        return float(v["double_value"])
    if "bool_value" in v:
        return bool(v["bool_value"])
    if "array_value" in v:
        return [_any_value(x) for x in (v["array_value"].get("values") or [])]
    if "kvlist_value" in v:
        return _attrs_from_list(v["kvlist_value"].get("values"))
    return None
