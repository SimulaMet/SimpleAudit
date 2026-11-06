"""
OTLP ingestion — accept OpenTelemetry trace exports and normalize to spans.

SimpleAudit can act as an OTLP/HTTP trace receiver so instrumented targets
(OpenInference, OpenLLMetry, MLflow, raw OTel) can push their spans directly.

Two entry points:

    - :func:`parse_otlp_json` — parse an OTLP/HTTP JSON ``ExportTraceServiceRequest``
      payload into normalized spans (transport-agnostic, easy to test).
    - :class:`OTLPTraceReceiver` — a minimal async receiver that accepts a
      POSTed OTLP JSON body, stores the spans, and returns the OTLP ack.

The HTTP receivers also accept ``http/protobuf`` bodies (by Content-Type,
decoded with the ``opentelemetry-proto`` package from the ``tracing`` extra);
:class:`EphemeralOTLPGRPCReceiver` takes gRPC. The normalization target is
the same for every transport.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .auth import AuthResult, OTLPAuth, tag_spans
from .store import SpanStore

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from .auth import Authenticator


def _run_authenticator(authenticator: Any, authorization: Optional[str]) -> AuthResult:
    """Call a custom authenticator, failing closed.

    ``None`` from ``_authenticate`` means "no gate configured", so an
    authenticator that returns ``None`` (or a bool, or raises) on a bad
    credential must not be read as success: anything other than an
    authenticated ``AuthResult`` is a 401.
    """
    try:
        result = authenticator(authorization)
    except Exception:
        return AuthResult(ok=False, reason="authenticator_error")
    if getattr(result, "authenticated", False) is True:
        return result
    return AuthResult(ok=False, reason="invalid_credentials")


def _check_auth_args(auth: Any, authenticator: Any) -> None:
    """Refuse both auth gates at once.

    ``auth`` takes precedence when set, so passing a mode-``none``
    ``OTLPAuth()`` alongside a custom ``authenticator`` would silently open
    the receiver: every request would pass.
    """
    if auth is not None and authenticator is not None:
        raise ValueError("Pass either auth= or authenticator=, not both")


def _proto_ts_to_unix(ns: Any) -> Optional[float]:
    if ns is None:
        return None
    # ``MessageToDict`` renders int64 nanosecond timestamps as strings.
    return int(ns) / 1e9


def _lower_id(value: Any) -> Any:
    """A hex id in lowercase; anything else unchanged, for the caller to reject."""
    return value.lower() if isinstance(value, str) else value


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
                        # OTLP/JSON ids are case-insensitive hex; SimpleAudit
                        # generates and looks them up in lowercase.
                        "trace_id": _lower_id(span.get("traceId")) or "",
                        "span_id": _lower_id(span.get("spanId")) or "",
                        "parent_span_id": _lower_id(span.get("parentSpanId")) or None,
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
    # The proto3 JSON mapping also allows the enum name ("STATUS_CODE_ERROR").
    if isinstance(code, str):
        code = {"STATUS_CODE_ERROR": 2, "ERROR": 2, "2": 2}.get(code.strip().upper(), 0)
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
        auth: Optional[OTLPAuth] = None,
    ) -> None:
        self._default_store = store is None
        # `is None`, not `or`: an empty SpanStore is falsy (len 0).
        self.store = store if store is not None else SpanStore()
        # Optional auth gate. When set, handle() calls it with the request's
        # Authorization header and rejects the export (401) on failure. When
        # None the receiver is open — the historical, backward-compatible
        # behaviour for the local, single-audit ephemeral case.
        _check_auth_args(auth, authenticator)
        self.authenticator = authenticator
        self.auth = auth

    async def handle(self, body: Any, authorization: Optional[str] = None) -> Dict[str, Any]:
        """Ingest an OTLP JSON export body; return the OTLP ack payload.

        When an ``authenticator`` was provided, *authorization* (the raw
        ``Authorization`` header) is checked first; a failed check returns a
        401 ``{"error": ...}`` payload and no spans are stored.
        """
        result = self._authenticate(authorization)
        if result is not None and not result.authenticated:
            # "unknown username" is reported as "invalid credentials": telling
            # the two apart would let a caller enumerate usernames. The exact
            # reason stays on the AuthResult for server-side logging.
            reason = getattr(result, "reason", None)
            return {
                "status": 401,
                "authenticated": False,
                "reason": "invalid_credentials" if reason == "unknown_username" else reason,
                "error": {"code": "unauthorized", "message": "Invalid or missing OTLP credentials."},
            }
        try:
            raw_spans = parse_otlp_json(body)
            if result is not None:
                tag_spans(raw_spans, result.identity)
            self.store.add_many(raw_spans)
        except (ValueError, TypeError, AttributeError, RecursionError, ArithmeticError):
            return {
                "status": 400,
                "error": {"code": "bad_request", "message": "Malformed OTLP JSON export."},
            }
        # OTLP ack: partialSuccess with the number of rejected spans (0 here).
        ack = {"partialSuccess": {"rejectedSpans": 0}}
        if self.auth is not None or self._default_store:
            ack["authenticated"] = True
        return ack

    def _authenticate(self, authorization: Optional[str]) -> Any:
        if self.auth is not None:
            return self.auth.verify(authorization)
        if self.authenticator is not None:
            return _run_authenticator(self.authenticator, authorization)
        return None

    def trace_ids(self) -> List[str]:
        seen: List[str] = []
        for s in self.store.all():
            if s["trace_id"] and s["trace_id"] not in seen:
                seen.append(s["trace_id"])
        return seen


#: Largest OTLP/HTTP request body accepted (the OTel Collector's order of
#: magnitude). Credentials are checked before the body is read.
DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024


class EphemeralOTLPReceiver:
    """A self-contained OTLP/HTTP trace receiver that lives for one audit.

    Promptfoo-style: the auditor opens this receiver when an audit starts,
    points the (controlled) target's ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` at
    :attr:`endpoint` (or ``OTEL_EXPORTER_OTLP_ENDPOINT`` at :attr:`base_url`:
    exporters append ``/v1/traces`` to that one, so pointing it at
    :attr:`endpoint` makes them POST to ``/v1/traces/v1/traces`` and get 404),
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
            # set the target's OTEL_EXPORTER_OTLP_TRACES_ENDPOINT to rx.endpoint
            # (or OTEL_EXPORTER_OTLP_ENDPOINT to rx.base_url)
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
        auth: Optional[OTLPAuth] = None,
        max_body_bytes: Optional[int] = None,
    ) -> None:
        self.host = host
        self.port = port
        # aiohttp's own default (1 MiB) answers 413 to an ordinary batch of
        # LLM spans carrying prompts or RAG context; exporters do not split a
        # rejected batch, so the audit would run with no evidence and no error.
        self.max_body_bytes = max_body_bytes or DEFAULT_MAX_BODY_BYTES
        # `is None`, not `or`: an empty SpanStore is falsy (len 0).
        self.store = store if store is not None else SpanStore()
        # Optional auth gate (see OTLPTraceReceiver). When set, each POST is
        # checked against the request's Authorization header and rejected with
        # 401 on failure. None = open receiver (default).
        _check_auth_args(auth, authenticator)
        self.authenticator = authenticator
        self.auth = auth
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
        """The OTLP/HTTP traces URL: the value for ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT``."""
        return f"http://{self.host}:{self._actual_port}/v1/traces"

    @property
    def base_url(self) -> str:
        """The receiver's base URL: the value for ``OTEL_EXPORTER_OTLP_ENDPOINT``,
        to which OTLP/HTTP exporters append ``/v1/traces`` themselves."""
        return f"http://{self.host}:{self._actual_port}"

    @property
    def actual_port(self) -> int:
        return self._actual_port

    async def _handle_traces(self, request: Any) -> Any:
        from aiohttp import web

        result = self._authenticate(request.headers.get("Authorization"))
        if result is not None and not result.authenticated:
            return web.json_response(
                {"authenticated": False, "error": {"code": "unauthorized", "message": "Invalid or missing OTLP credentials."}},
                status=401,
            )

        content_type = request.headers.get("Content-Type", "")
        body_bytes = await request.read()
        try:
            if "protobuf" in content_type:
                raw_spans = _parse_otlp_http_protobuf(body_bytes)
            else:
                raw_spans = parse_otlp_json(body_bytes.decode("utf-8"))
            if result is not None:
                tag_spans(raw_spans, result.identity)
            self.store.add_many(raw_spans)
        except Exception:
            # Never fail the export; ack with a rejection count so the target
            # doesn't retry-loop. The audit continues regardless.
            return web.json_response({"partialSuccess": {"rejectedSpans": 1}}, status=200)
        return web.json_response({"partialSuccess": {"rejectedSpans": 0}, "authenticated": True}, status=200)

    def _authenticate(self, authorization: Optional[str]) -> Any:
        if self.auth is not None:
            return self.auth.verify(authorization)
        if self.authenticator is not None:
            return _run_authenticator(self.authenticator, authorization)
        return None

    def _serve(self, loop: Any) -> None:

        try:
            from aiohttp import web

            app = web.Application(client_max_size=self.max_body_bytes)
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
            self._closed = False
            self._loop = asyncio.new_event_loop()
            self._ready = threading.Event()
            self._start_error = None
            self._thread = threading.Thread(target=self._serve, args=(self._loop,), daemon=True)
            self._thread.start()
            if not self._ready.wait(timeout=10):
                # Thread is stuck; tear it down and retry.
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=2)
                self._discard_loop(self._thread)
                self._thread = None
                last_error = RuntimeError("EphemeralOTLPReceiver failed to start within 10s")
                continue
            if self._start_error is not None:
                self._thread.join(timeout=2)
                self._discard_loop(self._thread)
                self._thread = None
                last_error = self._start_error
                continue
            return self
        self._loop = None
        raise RuntimeError(
            "EphemeralOTLPReceiver failed to start"
        ) from last_error

    def stop(self) -> None:
        """Stop the server and discard the in-memory spans."""
        if self._closed or self._loop is None:
            return
        self._closed = True
        self._loop.call_soon_threadsafe(self._loop.stop)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            self._thread = None
        self._discard_loop(thread)
        # Discard spans: this receiver is ephemeral by design.
        self.store = SpanStore()

    def _discard_loop(self, thread: Any) -> None:
        """Close the serve loop once its thread has exited.

        ``loop.close()`` raises on a loop that is still running, so a thread
        that outlived its join timeout keeps its loop (it is a daemon thread
        and the loop is dropped with it).
        """
        loop, self._loop = self._loop, None
        if loop is not None and (thread is None or not thread.is_alive()):
            loop.close()

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

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        store: Optional[SpanStore] = None,
        authenticator: Optional["Authenticator"] = None,
        auth: Optional[OTLPAuth] = None,
        max_body_bytes: Optional[int] = None,
    ) -> None:
        self.host = host
        self.port = port
        # gRPC's own 4 MiB message limit drops ordinary LLM span batches
        # (RESOURCE_EXHAUSTED); same default as the HTTP receiver.
        self.max_body_bytes = max_body_bytes or DEFAULT_MAX_BODY_BYTES
        # `is None`, not `or`: an empty SpanStore is falsy (len 0).
        self.store = store if store is not None else SpanStore()
        # Same optional auth gate as the HTTP receiver, read from the
        # ``authorization`` gRPC metadata. None = open receiver (default).
        _check_auth_args(auth, authenticator)
        self.authenticator = authenticator
        self.auth = auth
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

        receiver = self

        class _TraceServicer(trace_service_pb2_grpc.TraceServiceServicer):
            def Export(self, request, context):  # noqa: N802 - gRPC method name
                import grpc

                metadata = dict(context.invocation_metadata() or ())
                result = receiver._authenticate(metadata.get("authorization"))
                if result is not None and not result.authenticated:
                    context.abort(
                        grpc.StatusCode.UNAUTHENTICATED,
                        "Invalid or missing OTLP credentials.",
                    )
                raw_spans = _parse_otlp_grpc(request)
                if result is not None:
                    tag_spans(raw_spans, result.identity)
                receiver.store.add_many(raw_spans)
                return trace_service_pb2.ExportTraceServiceResponse(
                    partial_success=trace_service_pb2.ExportTracePartialSuccess(rejected_spans=0)
                )

        return _TraceServicer()

    def _authenticate(self, authorization: Optional[str]) -> Any:
        if self.auth is not None:
            return self.auth.verify(authorization)
        if self.authenticator is not None:
            return _run_authenticator(self.authenticator, authorization)
        return None

    def _serve(self) -> None:
        import grpc
        from concurrent import futures
        from opentelemetry.proto.collector.trace.v1 import trace_service_pb2_grpc

        try:
            # so_reuseport off: with gRPC's default a second receiver on a busy
            # port starts and steals the first audit's spans (the HTTP receiver
            # refuses the bind; this one now does too).
            server = grpc.server(
                futures.ThreadPoolExecutor(max_workers=4),
                options=[
                    ("grpc.so_reuseport", 0),
                    ("grpc.max_receive_message_length", self.max_body_bytes),
                ],
            )
            trace_service_pb2_grpc.add_TraceServiceServicer_to_server(self._make_servicer(), server)
            self._actual_port = server.add_insecure_port(f"{self.host}:{self.port}")
            if not self._actual_port:
                raise OSError(f"could not bind {self.host}:{self.port}")
            server.start()
        except Exception as exc:  # surface the real cause (port in use, ...) to start()
            self._start_error = exc
            self._ready.set()
            return
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
        self._closed = False
        self._start_error: Optional[BaseException] = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10) or self._start_error is not None:
            # Not running: forget the thread so a later start() really retries
            # instead of reporting a receiver with no port as started.
            error, self._start_error = self._start_error, None
            self._thread = None
            raise RuntimeError("EphemeralOTLPGRPCReceiver failed to start") from error
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
