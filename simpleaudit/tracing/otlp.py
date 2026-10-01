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
from typing import Any, Dict, List, Optional

from .store import SpanStore, normalize_span


def _proto_ts_to_unix(ns: int) -> Optional[float]:
    if ns is None:
        return None
    return ns / 1e9


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

    def __init__(self, store: Optional[SpanStore] = None) -> None:
        self.store = store or SpanStore()

    async def handle(self, body: Any) -> Dict[str, Any]:
        """Ingest an OTLP JSON export body; return the OTLP ack payload."""
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
