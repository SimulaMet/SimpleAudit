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
from typing import Any, Dict, List, Optional


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
    # Prefer the string attribute; fall back to the OTLP kind coerced to a
    # string so downstream code (e.g. select_spans) can always call .upper().
    kind = _attr("openinference.span.kind", "span.kind") or raw.get("kind")
    kind = str(kind) if kind is not None and kind != "" else "CHAIN"

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


@dataclass
class SpanStore:
    """In-memory span store with kind/trace/attribute queries."""

    _spans: Dict[str, Dict[str, Any]] = field(default_factory=dict)  # span_id -> span
    _by_trace: Dict[str, List[str]] = field(default_factory=dict)  # trace_id -> [span_id]

    def add(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        span = normalize_span(raw)
        if not span["span_id"]:
            import uuid

            span["span_id"] = uuid.uuid4().hex
        self._spans[span["span_id"]] = span
        if span["trace_id"]:
            self._by_trace.setdefault(span["trace_id"], [])
            if span["span_id"] not in self._by_trace[span["trace_id"]]:
                self._by_trace[span["trace_id"]].append(span["span_id"])
        return span

    def add_many(self, raws: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [self.add(r) for r in raws]

    def get(self, span_id: str) -> Optional[Dict[str, Any]]:
        return self._spans.get(span_id)

    def by_trace(self, trace_id: str) -> List[Dict[str, Any]]:
        return [self._spans[sid] for sid in self._by_trace.get(trace_id, []) if sid in self._spans]

    def by_kind(self, kind: str) -> List[Dict[str, Any]]:
        k = kind.upper()
        return [s for s in self._spans.values() if (s.get("kind") or "").upper() == k]

    def by_attribute(self, key: str, value: Any) -> List[Dict[str, Any]]:
        return [s for s in self._spans.values() if (s.get("attributes") or {}).get(key) == value]

    def all(self) -> List[Dict[str, Any]]:
        return list(self._spans.values())

    def __len__(self) -> int:
        return len(self._spans)
