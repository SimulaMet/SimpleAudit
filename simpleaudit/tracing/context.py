"""
Trace-context generation and correlation for SimpleAudit.

SimpleAudit correlates its own audit hierarchy (AuditRun → ScenarioRun → Turn)
with the W3C trace context that instrumented targets propagate. The primary
mechanism is the standard ``traceparent`` header (W3C Trace Context), which
normal OpenTelemetry instrumentation already understands — no custom header
required.

Custom ``X-SimpleAudit-*`` headers are a *fallback* for targets that do not
yet propagate W3C context but can echo a correlation id back.

The correlation model is intentionally loose:

    scenario_run_id
        └── turn_id
              └── 0..N trace_ids

A single turn may fan out into multiple traces (background jobs, parallel
agents). SimpleAudit never forces one trace per scenario.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


def new_trace_id() -> str:
    """Generate a 32-hex-char W3C trace id."""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """Generate a 16-hex-char W3C span id (non-zero)."""
    sid = uuid.uuid4().hex[:16]
    return sid if sid != "0" * 16 else "0" * 15 + "1"


def make_traceparent(trace_id: Optional[str] = None, span_id: Optional[str] = None) -> str:
    """Build a W3C ``traceparent`` header value.

    Format: ``version-traceid-parentid-traceflags`` (e.g. ``00-<32hex>-<16hex>-01``).
    """
    tid = trace_id or new_trace_id()
    sid = span_id or new_span_id()
    return f"00-{tid}-{sid}-01"


@dataclass
class TurnTraceLink:
    """Links one audit turn to the trace(s) observed for it."""

    turn_id: str
    trace_ids: List[str] = field(default_factory=list)
    traceparent: Optional[str] = None


@dataclass
class TraceCorrelation:
    """Correlates an audit run's turns with observed traces.

    Call :meth:`record` as traces arrive; query with :meth:`trace_ids_for_turn`.

    Set :attr:`on_new_trace` to a callback that is invoked the first time each
    ``trace_id`` is recorded. Use this to register the trace with a
    :class:`~simpleaudit.tracing.shared.SharedOTLP` session so the shared
    receiver routes incoming spans to the right audit.
    """

    audit_run_id: str
    _turns: Dict[str, TurnTraceLink] = field(default_factory=dict)
    on_new_trace: Optional[Any] = field(default=None, repr=False)
    _seen_traces: set = field(default_factory=set, repr=False)

    def link_turn(self, turn_id: str, traceparent: Optional[str] = None) -> TurnTraceLink:
        if turn_id not in self._turns:
            self._turns[turn_id] = TurnTraceLink(turn_id=turn_id, traceparent=traceparent)
        else:
            if traceparent:
                self._turns[turn_id].traceparent = traceparent
        return self._turns[turn_id]

    def record(self, turn_id: str, trace_id: str) -> None:
        link = self.link_turn(turn_id)
        if trace_id not in link.trace_ids:
            link.trace_ids.append(trace_id)
        if self.on_new_trace is not None and trace_id not in self._seen_traces:
            self._seen_traces.add(trace_id)
            self.on_new_trace(trace_id)

    def trace_ids_for_turn(self, turn_id: str) -> List[str]:
        link = self._turns.get(turn_id)
        return list(link.trace_ids) if link else []

    def all_trace_ids(self) -> List[str]:
        seen: List[str] = []
        for link in self._turns.values():
            for tid in link.trace_ids:
                if tid not in seen:
                    seen.append(tid)
        return seen

    def turns(self) -> List[TurnTraceLink]:
        return list(self._turns.values())

    def spans_for_turn(
        self, turn_id: str, store: "Any"
    ) -> List[Dict[str, Any]]:
        """Return all spans in ``store`` belonging to the traces of ``turn_id``.

        ``store`` is anything with a ``by_trace(trace_id)`` method (e.g.
        :class:`simpleaudit.tracing.store.SpanStore`). A turn may map to 0..N
        traces (fan-out), so every linked trace's spans are collected.
        """
        spans: List[Dict[str, Any]] = []
        seen: set = set()
        for tid in self.trace_ids_for_turn(turn_id):
            for span in store.by_trace(tid):
                sid = span.get("span_id")
                if sid in seen:
                    continue
                seen.add(sid)
                spans.append(span)
        return spans
