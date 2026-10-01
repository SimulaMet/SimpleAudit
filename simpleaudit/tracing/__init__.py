"""
Tracing / observability layer for SimpleAudit.

Provides:
    - W3C trace-context generation and audit↔trace correlation (``context``)
    - an in-memory span store with kind/trace/attribute queries (``store``)
    - OTLP/HTTP JSON ingestion (``otlp``)
    - span selection for judge-over-spans evidence (``selection``)

This layer is optional: black-box auditing works with no tracing at all.
Tracing adds richer evidence (retrieved docs, tool calls, agent reasoning)
when the target is instrumented.
"""

from .context import (
    TraceCorrelation,
    TurnTraceLink,
    make_traceparent,
    new_span_id,
    new_trace_id,
)
from .otlp import OTLPTraceReceiver, parse_otlp_json
from .selection import (
    DEFAULT_EVIDENCE_KINDS,
    DEFAULT_NOISE_KINDS,
    SelectionResult,
    evidence_spans_for_turn,
    select_spans,
    summarize_for_judge,
)
from .store import SpanStore, normalize_span

__all__ = [
    "new_trace_id",
    "new_span_id",
    "make_traceparent",
    "TurnTraceLink",
    "TraceCorrelation",
    "SpanStore",
    "normalize_span",
    "parse_otlp_json",
    "OTLPTraceReceiver",
    "SelectionResult",
    "select_spans",
    "evidence_spans_for_turn",
    "summarize_for_judge",
    "DEFAULT_EVIDENCE_KINDS",
    "DEFAULT_NOISE_KINDS",
]
