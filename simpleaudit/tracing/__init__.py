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

from .auth import (
    AuthResult,
    Authenticator,
    OTLPAuth,
    TARGET_ID_ATTR,
    hash_secret,
    make_basic_bearer_authenticator,
    new_salt,
    parse_basic_header,
    parse_bearer_header,
    tag_spans,
    token_lookup_prefix,
    verify_secret,
)
from .context import (
    TraceCorrelation,
    TurnTraceLink,
    make_traceparent,
    new_span_id,
    new_trace_id,
)
from .otlp import EphemeralOTLPGRPCReceiver, EphemeralOTLPReceiver, OTLPTraceReceiver, parse_otlp_json
from .provider import BuiltinOTLP, ExternalTraceProvider, TraceProvider, audit_with_tracing
from .shared import SharedOTLP, SharedOTLPReceiver, TraceSession, TraceSessionManager
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
    "EphemeralOTLPReceiver",
    "EphemeralOTLPGRPCReceiver",
    "SharedOTLP",
    "SharedOTLPReceiver",
    "TraceSession",
    "TraceSessionManager",
    "TraceProvider",
    "BuiltinOTLP",
    "ExternalTraceProvider",
    "audit_with_tracing",
    "SelectionResult",
    "select_spans",
    "evidence_spans_for_turn",
    "summarize_for_judge",
    "DEFAULT_EVIDENCE_KINDS",
    "DEFAULT_NOISE_KINDS",
    "AuthResult",
    "Authenticator",
    "OTLPAuth",
    "TARGET_ID_ATTR",
    "tag_spans",
    "hash_secret",
    "new_salt",
    "verify_secret",
    "parse_basic_header",
    "parse_bearer_header",
    "token_lookup_prefix",
    "make_basic_bearer_authenticator",
]
