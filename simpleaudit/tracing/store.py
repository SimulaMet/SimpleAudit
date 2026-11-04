"""
Span store — in-memory persistence and query for ingested trace spans.

This is deliberately transport-agnostic: spans are plain dicts normalized to a
small schema (see :func:`normalize_span`). The store supports the queries the
audit engine needs:

    - by trace_id
    - by span kind (RETRIEVER / TOOL / AGENT / GUARDRAIL / EVALUATOR / LLM / ...)
    - by attribute (e.g. a correlation id)

It is in-memory by default so the core has no storage dependency; a
persistence backend can be added later without changing the query API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# OTLP SpanKind proto enum values -> names (opentelemetry.proto.trace.v1).
# Used to render an int kind (as sent over gRPC/proto) as its name.
_OTLP_SPAN_KIND_NAMES: Dict[int, str] = {
    0: "UNSPECIFIED",
    1: "INTERNAL",
    2: "SERVER",
    3: "CLIENT",
    4: "PRODUCER",
    5: "CONSUMER",
}


def normalize_span(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a raw span (OTel/OpenInference-shaped) to the store schema.

    Accepts the common OpenInference/OTel attribute keys and produces a flat
    dict with: span_id, trace_id, name, kind, attributes, parent_span_id,
    start_time, end_time, status.
    """
    attrs = raw.get("attributes") or {}

    def _attr(*keys: str) -> Any:
        for k in keys:
            if k in attrs:
                return attrs[k]
        return None

    # OTLP carries span kind as a proto int (1=INTERNAL, 2=SERVER, ...); the
    # OpenInference/OTel attribute carries a string kind (RETRIEVER, TOOL, ...).
    # Prefer the string attribute; fall back to the OTLP kind mapped to its
    # SpanKind name (or coerced to a string) so downstream code (e.g.
    # select_spans) can always call .upper().
    kind = _attr("openinference.span.kind", "span.kind") or raw.get("kind")
    if isinstance(kind, int):
        kind = _OTLP_SPAN_KIND_NAMES.get(kind, str(kind))
    kind = str(kind) if kind is not None and kind != "" else "CHAIN"
    # Protobuf/gRPC ingest yields enum names ("SPAN_KIND_INTERNAL"); JSON
    # yields the mapped short name ("INTERNAL"). Store one form.
    if kind.startswith("SPAN_KIND_"):
        kind = kind[len("SPAN_KIND_"):]

    return {
        "span_id": raw.get("span_id") or _attr("span_id") or "",
        "trace_id": raw.get("trace_id") or _attr("trace_id") or "",
        "name": raw.get("name") or _attr("openinference.span.kind", "span.name") or "span",
        "kind": kind,
        "parent_span_id": raw.get("parent_span_id") or _attr("parent_span_id") or None,
        "attributes": attrs,
        "start_time": raw.get("start_time"),
        "end_time": raw.get("end_time"),
        "status": raw.get("status") or "OK",
    }


# Mirrors simpleaudit.tracing.auth.TARGET_ID_ATTR (not imported, to keep this
# module free of the auth layer).
_TARGET_ID_ATTR = "simpleaudit.target_id"


def _owner(span: Dict[str, Any]) -> Any:
    return (span.get("attributes") or {}).get(_TARGET_ID_ATTR)


@dataclass
class SpanStore:
    """In-memory span store with kind/trace/attribute queries.

    Spans are keyed by ``(trace_id, span_id)``: span ids are only unique
    within a trace, and a target with deterministic ids (or a replayed
    export) must not overwrite another trace's span.
    """

    _spans: Dict[Tuple[str, str], Dict[str, Any]] = field(default_factory=dict)
    _by_trace: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    _by_span_id: Dict[str, Tuple[str, str]] = field(default_factory=dict)  # latest key per span_id

    def add(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        span = normalize_span(raw)
        if not span["span_id"]:
            import uuid

            span["span_id"] = uuid.uuid4().hex
        key = (span["trace_id"] or "", span["span_id"])
        existing = self._spans.get(key)
        if existing is not None and _owner(existing) != _owner(span):
            # A span already stored for another authenticated target: keep
            # the original. Replacing it would let one target overwrite
            # another target's evidence.
            return existing
        self._spans[key] = span
        self._by_span_id[span["span_id"]] = key
        if span["trace_id"]:
            keys = self._by_trace.setdefault(span["trace_id"], [])
            if key not in keys:
                keys.append(key)
        return span

    def add_many(self, raws: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.add(r) for r in raws]

    def get(self, span_id: str) -> Optional[Dict[str, Any]]:
        key = self._by_span_id.get(span_id)
        return self._spans.get(key) if key is not None else None

    def by_trace(self, trace_id: str) -> List[Dict[str, Any]]:
        spans = self._spans
        return [spans[k] for k in list(self._by_trace.get(trace_id, [])) if k in spans]

    def by_kind(self, kind: str) -> List[Dict[str, Any]]:
        k = kind.upper()
        # Snapshot first: the receiver thread may add spans meanwhile.
        return [s for s in list(self._spans.values()) if (s.get("kind") or "").upper() == k]

    def by_attribute(self, key: str, value: Any) -> List[Dict[str, Any]]:
        return [s for s in list(self._spans.values()) if (s.get("attributes") or {}).get(key) == value]

    def all(self) -> List[Dict[str, Any]]:
        return list(self._spans.values())

    def __len__(self) -> int:
        return len(self._spans)
