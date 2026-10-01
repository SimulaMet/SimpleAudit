"""
TraceProvider — pluggable source of traces for an audit.

SimpleAudit supports two trace-acquisition modes (mirroring Promptfoo):

1. **BuiltinOTLP** — the auditor runs its own ephemeral OTLP receiver for the
   duration of the audit. The (controlled) target's
   ``OTEL_EXPORTER_OTLP_ENDPOINT`` is pointed at it. Best for CI, local apps,
   staging, and agent SDKs you control. No external collector required.

2. **ExternalTraceProvider** — the target already ships traces to its own
   observability backend (Tempo, Jaeger, an OTel Collector, etc.). SimpleAudit
   propagates the W3C ``traceparent`` and, after the run, *fetches* the matching
   trace by id from that backend. The customer never redirects telemetry.

Both expose the same minimal contract so the audit engine (and the
``evidence_spans_for_turn`` glue) does not care which mode is in use::

    provider.start()
    provider.endpoint          # only meaningful for BuiltinOTLP
    provider.fetch(trace_id)   # -> normalized spans for that trace
    provider.stop()

The contract is deliberately small; a Tempo/HTTP-JSON provider is a thin
subclass that implements :meth:`fetch` against the backend's trace API.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .otlp import EphemeralOTLPReceiver
from .store import SpanStore


class TraceProvider:
    """Base class for a trace source used during an audit."""

    def start(self) -> "TraceProvider":
        return self

    def stop(self) -> None:
        return None

    @property
    def endpoint(self) -> Optional[str]:
        """OTLP endpoint the target should export to, or None for fetch-based."""
        return None

    def fetch(self, trace_id: str) -> List[Dict[str, Any]]:
        """Return the normalized spans for ``trace_id`` (may be empty)."""
        return []

    def __enter__(self) -> "TraceProvider":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class BuiltinOTLP(TraceProvider):
    """Run an ephemeral OTLP receiver for the audit; collect spans in-memory.

    Point the target's ``OTEL_EXPORTER_OTLP_ENDPOINT`` at :attr:`endpoint`
    (with ``OTEL_EXPORTER_OTLP_PROTOCOL=http/json``) for the duration of the
    audit. Spans are discarded on :meth:`stop`.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self._host = host
        self._port = port
        self._receiver: Optional[EphemeralOTLPReceiver] = None

    def start(self) -> "BuiltinOTLP":
        if self._receiver is None:
            self._receiver = EphemeralOTLPReceiver(host=self._host, port=self._port).start()
        return self

    def stop(self) -> None:
        if self._receiver is not None:
            self._receiver.stop()
            self._receiver = None

    @property
    def endpoint(self) -> Optional[str]:
        return self._receiver.endpoint if self._receiver else None

    @property
    def store(self) -> SpanStore:
        return self._receiver.store if self._receiver else SpanStore()

    def fetch(self, trace_id: str) -> List[Dict[str, Any]]:
        return self.store.by_trace(trace_id) if self._receiver else []


class ExternalTraceProvider(TraceProvider):
    """Fetch traces from an existing observability backend by trace id.

    Subclass and implement :meth:`_fetch_remote` to query your backend
    (Tempo, Jaeger, an OTel Collector, etc.). SimpleAudit propagates the
    ``traceparent`` during the run; after the response, call :meth:`fetch`
    with the recorded trace id to retrieve the spans.

    Example (pseudo)::

        class TempoProvider(ExternalTraceProvider):
            def _fetch_remote(self, trace_id):
                # GET {base}/api/traces/{trace_id} -> OTLP JSON -> parse
                ...
    """

    def fetch(self, trace_id: str) -> List[Dict[str, Any]]:
        return self._fetch_remote(trace_id)

    def _fetch_remote(self, trace_id: str) -> List[Dict[str, Any]]:
        raise NotImplementedError(
            "ExternalTraceProvider subclasses must implement _fetch_remote(trace_id)"
        )


async def audit_with_tracing(
    auditor: Any,
    scenarios: Any,
    *,
    provider: Optional[TraceProvider] = None,
    audit_run_id: Optional[str] = None,
    token_budget: Optional[int] = None,
    **run_kwargs: Any,
) -> Any:
    """Run an audit with a trace provider and attach per-scenario evidence.

    This is the Promptfoo-style flow for a **controlled target** you can point
    at an OTLP endpoint:

    1. Start the provider (default: :class:`BuiltinOTLP` ephemeral receiver).
    2. **You** point the target's ``OTEL_EXPORTER_OTLP_ENDPOINT`` at
       ``provider.endpoint`` (with ``OTEL_EXPORTER_OTLP_PROTOCOL=http/json``)
       for the duration of the run. The helper exposes the endpoint via the
       returned object so you can configure the target before/around the run.
    3. Run the audit. The engine propagates a W3C ``traceparent`` per turn and
       records ``turn_id -> trace_id`` in a :class:`TraceCorrelation`.
    4. After the run, collect each scenario's spans (union of its turns'
       traces) and select evidence-relevant spans.
    5. Stop the provider (discarding spans for BuiltinOTLP).

    The selected spans are attached to each :class:`AuditResult` under
    ``result.judgment["evidence_spans"]`` (with provenance), ready to feed a
    trace-aware judge or a Finding's ``EvidenceRef``.

    Returns
    -------
    A :class:`~simpleaudit.results.AuditResults` whose per-result
    ``judgment["evidence_spans"]`` carry the selected spans (with provenance).

    Notes
    -----
    The helper does **not** mutate the target. For a target that reads its
    OTLP endpoint from the environment (e.g. Open WebUI), set
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` to ``provider.endpoint`` before the run.
    """
    from .context import TraceCorrelation, new_trace_id

    provider = provider or BuiltinOTLP()
    audit_run_id = audit_run_id or f"audit_{new_trace_id()[:12]}"
    correlation = TraceCorrelation(audit_run_id=audit_run_id)

    with provider:
        results = await auditor.run_async(
            scenarios,
            audit_run_id=audit_run_id,
            trace_correlation=correlation,
            **run_kwargs,
        )
        # Collect evidence spans while the provider is still alive.
        evidence = _scenario_evidence(correlation, provider, token_budget=token_budget)

    # Attach selected evidence spans to each result by scenario.
    if evidence:
        for result in results.results:
            judgment = result.judgment if isinstance(result.judgment, dict) else {}
            judgment["evidence_spans"] = evidence
            result.judgment = judgment

    return results


def _scenario_evidence(
    correlation: Any,
    provider: Any,
    *,
    token_budget: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Collect + select evidence spans across every trace the correlation saw.

    The correlation records ``turn_id -> trace_id``. We gather all spans for
    those traces from the provider, de-duplicate, and select the
    evidence-relevant kinds. Per-scenario attribution is approximate when
    ``max_workers>1`` (the correlation is shared across scenarios), but each
    span's provenance carries the exact ``trace_id``/``span_id`` for a
    Finding's ``EvidenceRef``.
    """
    from .selection import select_spans

    all_spans: List[Dict[str, Any]] = []
    seen: set = set()
    for turn in correlation.turns():
        for tid in turn.trace_ids:
            for span in provider.fetch(tid):
                sid = span.get("span_id")
                if sid in seen:
                    continue
                seen.add(sid)
                all_spans.append(span)
    if not all_spans:
        return []
    selected = select_spans(all_spans, token_budget=token_budget)
    return selected.selected
