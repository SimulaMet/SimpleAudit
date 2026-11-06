"""
Shared OTLP receiver + per-audit trace session routing.

The receiver is a **long-lived service** (one per deployment). Trace data is
**ephemeral per audit** — spans are routed to a ``TraceSession`` by
``trace_id`` and expire after a TTL.

Architecture::

    Target A ─┐
    Target B ─┤  OTLP
    Target C ─┘
         │
         ▼
    SharedOTLPReceiver          ← permanent, one per process
         │
    TraceSessionManager         ← routes by trace_id
         │
    ┌────┼────────┐
    ▼    ▼        ▼
    session_1  session_2  session_3   ← ephemeral, per audit
    spans[]    spans[]    spans[]
         │
         ▼
    judge / trajectory evaluation
         │
    TTL expiry → spans discarded

Usage::

    # Start once (e.g. at app boot). Binds loopback by default; to accept
    # spans from other hosts, bind 0.0.0.0 *and* pass auth=OTLPAuth(...).
    shared = SharedOTLPReceiver(port=4317).start()

    # Per audit
    session = shared.sessions.create(audit_id="audit_101", ttl=300)
    # ... run audit with traceparent correlation ...
    spans = session.spans_for_trace(trace_id)
    session.close()   # discard spans
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .auth import TARGET_ID_ATTR as _TARGET_ID_ATTR
from .auth import OTLPAuth
from .otlp import EphemeralOTLPReceiver
from .store import SpanStore

if TYPE_CHECKING:  # pragma: no cover - annotation only
    from .auth import Authenticator


@dataclass
class TraceSession:
    """Ephemeral trace buffer for one audit run.

    Spans are routed here by ``trace_id``. The session expires after
    ``ttl`` seconds without activity (checked lazily on access) or when
    :meth:`close` is called explicitly. Registering a trace or receiving a
    span counts as activity, so an audit that runs longer than ``ttl`` keeps
    its evidence as long as it is still producing traces.

    ``max_spans`` caps the number of spans retained for this session to
    prevent a single audit from consuming unbounded memory under heavy
    OTLP traffic. When the cap is hit, new spans are dropped (and counted
    in :attr:`dropped`).
    """

    audit_id: str
    execution_id: str = ""
    target_id: str = ""
    created_at: float = field(default_factory=time.time)
    ttl: float = 300.0  # seconds
    max_spans: int = 10_000  # per-session cap
    _store: SpanStore = field(default_factory=SpanStore, repr=False)
    _closed: bool = field(default=False, repr=False)
    _dropped: int = field(default=0, repr=False)
    _last_active: float = field(default=0.0, repr=False)

    @property
    def expired(self) -> bool:
        idle_since = max(self.created_at, self._last_active)
        return self._closed or (time.time() - idle_since) > self.ttl

    def touch(self) -> None:
        """Record activity, pushing back the idle expiry."""
        if not self.expired:
            self._last_active = time.time()

    @property
    def store(self) -> SpanStore:
        return self._store

    @property
    def dropped(self) -> int:
        """Number of spans dropped due to the per-session cap."""
        return self._dropped

    @property
    def full(self) -> bool:
        return len(self._store) >= self.max_spans

    def add_many(self, spans: List[Dict[str, Any]]) -> int:
        """Add spans; returns the number actually stored (vs dropped)."""
        if self.expired:
            return 0
        stored = 0
        for s in spans:
            if self.full:
                self._dropped += 1
                continue
            self._store.add(s)
            stored += 1
        if stored:
            self.touch()
        return stored

    def spans_for_trace(self, trace_id: str) -> List[Dict[str, Any]]:
        if self.expired:
            return []
        return self._store.by_trace(trace_id)

    def all_spans(self) -> List[Dict[str, Any]]:
        if self.expired:
            return []
        return self._store.all()

    def close(self) -> None:
        """Discard all spans for this session."""
        self._closed = True
        self._store = SpanStore()

    def __len__(self) -> int:
        if self.expired:
            return 0
        return len(self._store)


class TraceSessionManager:
    """Routes incoming spans to the correct :class:`TraceSession`.

    The primary correlation mechanism is ``trace_id`` (from the OTLP span
    itself). The manager keeps a ``trace_id → session`` index so that when a
    span arrives, it can be routed to the right audit's buffer.

    Sessions are cleaned up lazily (on access) and via :meth:`sweep`.

    Heavy-traffic protections:
    - **Early rejection**: if no sessions are active, :meth:`route_spans`
      returns immediately without touching the spans (zero-cost drop).
    - **Per-session cap**: each :class:`TraceSession` has a ``max_spans``
      limit; excess spans are dropped and counted.
    - **Global cap**: ``max_total_spans`` limits the total spans across all
      active sessions; when hit, new spans are dropped globally.
    """

    def __init__(self, *, max_total_spans: int = 200_000) -> None:
        self._sessions: Dict[str, TraceSession] = {}  # audit_id → session
        self._trace_index: Dict[str, str] = {}  # trace_id → audit_id
        self._lock = threading.Lock()
        self._max_total_spans = max_total_spans
        self._dropped_no_session: int = 0
        self._dropped_global_cap: int = 0

    @property
    def dropped_no_session(self) -> int:
        """Spans dropped because no session was active (early rejection)."""
        return self._dropped_no_session

    @property
    def dropped_global_cap(self) -> int:
        """Spans dropped because the global span cap was hit."""
        return self._dropped_global_cap

    @property
    def total_spans(self) -> int:
        """Total spans across all active sessions."""
        with self._lock:
            return sum(len(s) for s in self._sessions.values() if not s.expired)

    @property
    def has_active_sessions(self) -> bool:
        """Quick check: is there at least one non-expired session?"""
        with self._lock:
            return any(not s.expired for s in self._sessions.values())

    def create(
        self,
        audit_id: str,
        *,
        execution_id: str = "",
        target_id: str = "",
        ttl: float = 300.0,
        max_spans: int = 10_000,
    ) -> TraceSession:
        """Create a new trace session for an audit run.

        Raises ``ValueError`` when a live session already has this
        ``audit_id``: replacing it would route its traces to the newcomer,
        and closing either would discard the other's spans.
        """
        session = TraceSession(
            audit_id=audit_id,
            execution_id=execution_id,
            target_id=target_id,
            ttl=ttl,
            max_spans=max_spans,
        )
        with self._lock:
            existing = self._sessions.get(audit_id)
            if existing is not None and not existing.expired:
                raise ValueError(f"A trace session for audit_id {audit_id!r} is already active")
            if existing is not None:
                # Its trace routes would deliver the old audit's late spans
                # into the new one.
                for tid in [t for t, a in self._trace_index.items() if a == audit_id]:
                    del self._trace_index[tid]
                existing.close()
            self._sessions[audit_id] = session
        return session

    def register_trace(self, audit_id: str, trace_id: str) -> None:
        """Map a ``trace_id`` to an audit session (called by the correlation layer)."""
        with self._lock:
            self._trace_index[trace_id] = audit_id
            session = self._sessions.get(audit_id)
            if session is not None:
                session.touch()

    def route_spans(self, spans: List[Dict[str, Any]]) -> None:
        """Route incoming spans to the session that owns their ``trace_id``.

        Spans whose ``trace_id`` is not registered are dropped (they belong
        to a non-audit flow or an expired session).

        Heavy-traffic optimizations:
        - If no sessions are active, returns immediately (zero-cost drop).
        - Global span cap prevents unbounded memory growth.
        """
        if not spans:
            return
        # Early rejection: no active sessions → drop everything.
        if not self.has_active_sessions:
            self._dropped_no_session += len(spans)
            return

        # Group spans by trace_id for efficient lookup.
        by_trace: Dict[str, List[Dict[str, Any]]] = {}
        for s in spans:
            tid = s.get("trace_id", "")
            if tid:
                by_trace.setdefault(tid, []).append(s)

        with self._lock:
            current_total = sum(len(s) for s in self._sessions.values() if not s.expired)
            for tid, trace_spans in by_trace.items():
                audit_id = self._trace_index.get(tid)
                if audit_id is None:
                    continue
                session = self._sessions.get(audit_id)
                if session is None or session.expired:
                    continue
                if session.target_id:
                    # A session bound to a target accepts only spans the
                    # receiver attributed to that target (or unattributed
                    # spans, when the receiver runs without auth): another
                    # authenticated target that learned the trace id must not
                    # add evidence or fill the session's cap.
                    trace_spans = [
                        s for s in trace_spans
                        if (s.get("attributes") or {}).get(_TARGET_ID_ATTR) in (None, session.target_id)
                    ]
                    if not trace_spans:
                        continue
                # Global cap check.
                if current_total >= self._max_total_spans:
                    self._dropped_global_cap += len(trace_spans)
                    continue
                stored = session.add_many(trace_spans)
                current_total += stored

    def get(self, audit_id: str) -> Optional[TraceSession]:
        with self._lock:
            session = self._sessions.get(audit_id)
            if session is not None and session.expired:
                del self._sessions[audit_id]
                for tid in [t for t, a in self._trace_index.items() if a == audit_id]:
                    del self._trace_index[tid]
                session.close()
                return None
            return session

    def close(self, audit_id: str, session: Optional["TraceSession"] = None) -> None:
        """Close a session and discard its spans.

        With *session*, only that session is closed: a provider whose own
        session expired must not close a newer one that reused the audit_id.
        """
        with self._lock:
            if session is not None and self._sessions.get(audit_id) is not session:
                session.close()
                return
            session = self._sessions.pop(audit_id, None)
            # Remove trace index entries for this session.
            stale_traces = [tid for tid, aid in self._trace_index.items() if aid == audit_id]
            for tid in stale_traces:
                del self._trace_index[tid]
        if session is not None:
            session.close()

    def sweep(self) -> int:
        """Close all expired sessions. Returns the number closed."""
        closed = 0
        with self._lock:
            expired = [aid for aid, s in self._sessions.items() if s.expired]
            for aid in expired:
                s = self._sessions.pop(aid)
                s.close()
                stale = [tid for tid, a in self._trace_index.items() if a == aid]
                for tid in stale:
                    del self._trace_index[tid]
                closed += 1
        return closed

    @property
    def active_sessions(self) -> List[TraceSession]:
        with self._lock:
            return [s for s in self._sessions.values() if not s.expired]

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


class SharedOTLPReceiver:
    """A long-lived OTLP receiver that routes spans to per-audit trace sessions.

    One instance per process/deployment. Multiple targets (Open WebUI, agent
    SDKs, HTTP apps) all export to the same endpoint. Spans are routed to the
    correct :class:`TraceSession` by ``trace_id``.

    The receiver handles both OTLP/HTTP JSON and OTLP/HTTP protobuf.

    Binds ``127.0.0.1`` by default. A receiver reachable from other hosts
    should require credentials: pass ``auth=OTLPAuth("bearer")`` (or an
    ``authenticator``), as anyone who can post spans with a registered
    ``trace_id`` can otherwise inject evidence into a live audit.

    It speaks OTLP over **HTTP** (``POST /v1/traces``). The default port,
    4317, is kept for compatibility but is the gRPC convention: an exporter
    left on its defaults (gRPC to 4317, or HTTP to 4318) will not reach it.
    Configure the target with ``OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf``
    (or ``http/json``) and ``OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`` set to
    ``http://<host>:<port>/v1/traces``, or pass ``port=4318``.

    Usage::

        # Boot (once)
        shared = SharedOTLPReceiver(port=4317).start()

        # Per audit
        session = shared.sessions.create(audit_id="audit_101", ttl=300)
        # Engine records traceparent → call:
        shared.sessions.register_trace("audit_101", trace_id)
        # ... run audit ...
        spans = session.spans_for_trace(trace_id)
        shared.sessions.close("audit_101")

        # Shutdown
        shared.stop()
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 4317,
        *,
        session_ttl: float = 300.0,
        sweep_interval: float = 60.0,
        max_total_spans: int = 200_000,
        max_spans_per_session: int = 10_000,
        auth: Optional[OTLPAuth] = None,
        authenticator: Optional["Authenticator"] = None,
        max_body_bytes: Optional[int] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.max_body_bytes = max_body_bytes
        self.auth = auth
        self.authenticator = authenticator
        self.session_ttl = session_ttl
        self.sessions = TraceSessionManager(max_total_spans=max_total_spans)
        self.max_spans_per_session = max_spans_per_session
        self._receiver: Optional[EphemeralOTLPReceiver] = None
        self._sweep_interval = sweep_interval
        self._sweep_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def endpoint(self) -> str:
        """The OTLP/HTTP traces URL for targets to export to."""
        if self._receiver:
            return self._receiver.endpoint
        return f"http://{self.host}:{self.port}/v1/traces"

    @property
    def actual_port(self) -> int:
        return self._receiver.actual_port if self._receiver else self.port

    @property
    def stats(self) -> Dict[str, int]:
        """Drop counters and totals for observability."""
        return {
            "active_sessions": len(self.sessions),
            "total_spans": self.sessions.total_spans,
            "dropped_no_session": self.sessions.dropped_no_session,
            "dropped_global_cap": self.sessions.dropped_global_cap,
        }

    def create_session(
        self,
        audit_id: str,
        *,
        execution_id: str = "",
        target_id: str = "",
        ttl: Optional[float] = None,
    ) -> TraceSession:
        """Create a trace session with the receiver's configured defaults."""
        return self.sessions.create(
            audit_id,
            execution_id=execution_id,
            target_id=target_id,
            ttl=ttl if ttl is not None else self.session_ttl,
            max_spans=self.max_spans_per_session,
        )

    def _on_spans(self, spans: List[Dict[str, Any]]) -> None:
        """Callback invoked by the receiver when spans arrive."""
        self.sessions.route_spans(spans)

    def _sweep_loop(self) -> None:
        """Background thread that periodically closes expired sessions."""
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=self._sweep_interval)
            if self._stop_event.is_set():
                break
            self.sessions.sweep()

    def start(self) -> "SharedOTLPReceiver":
        """Start the shared receiver and the session sweep thread."""
        if self._receiver is not None:
            return self
        self._receiver = EphemeralOTLPReceiver(
            host=self.host, port=self.port,
            auth=self.auth, authenticator=self.authenticator,
            max_body_bytes=self.max_body_bytes,
        ).start()
        # Hook the receiver's store: instead of a single SpanStore, we route
        # spans through the session manager. We do this by wrapping the
        # receiver's internal store with a routing store.
        self._install_routing()
        # Start the sweep thread.
        self._stop_event.clear()
        self._sweep_thread = threading.Thread(target=self._sweep_loop, daemon=True)
        self._sweep_thread.start()
        return self

    def _install_routing(self) -> None:
        """Replace the receiver's store with a routing SpanStore."""
        if self._receiver is None:
            return
        self._receiver.store = _RoutingSpanStore(self.sessions)

    def stop(self) -> None:
        """Stop the receiver and close all sessions."""
        self._stop_event.set()
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=5)
            self._sweep_thread = None
        if self._receiver is not None:
            self._receiver.stop()
            self._receiver = None
        # Close all remaining sessions, with their trace routes.
        for session in self.sessions.active_sessions:
            self.sessions.close(session.audit_id)

    def __enter__(self) -> "SharedOTLPReceiver":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class SharedOTLP:
    """A :class:`TraceProvider` that plugs into a :class:`SharedOTLPReceiver`.

    Use this with :func:`audit_with_tracing` when the target exports to a
    shared, long-lived OTLP receiver (e.g. the Studio's global endpoint)
    rather than an ephemeral per-audit receiver.

    The provider creates a :class:`TraceSession` on :meth:`start`, registers
    trace ids as the engine generates them, and discards the session on
    :meth:`stop`.

    Usage::

        shared = SharedOTLPReceiver(port=4317).start()
        provider = SharedOTLP(shared, audit_id="audit_101")
        results = await audit_with_tracing(auditor, "safety", provider=provider)
    """

    def __init__(
        self,
        shared: "SharedOTLPReceiver",
        *,
        audit_id: str = "",
        ttl: Optional[float] = None,
        target_id: str = "",
    ) -> None:
        # target_id binds the session to one authenticated target: spans the
        # receiver attributed to another target are dropped (see route_spans).
        self._target_id = target_id
        # ttl None: the receiver's session_ttl. Sessions are created through
        # the receiver so its per-session span cap applies too.
        self._shared = shared
        self._audit_id = audit_id
        self._ttl = ttl
        self._session: Optional[TraceSession] = None

    def start(self) -> "SharedOTLP":
        from .context import new_trace_id

        if self._session is None:
            self._audit_id = self._audit_id or f"audit_{new_trace_id()[:12]}"
            self._session = self._shared.create_session(
                self._audit_id, ttl=self._ttl, target_id=self._target_id
            )
        return self

    def stop(self) -> None:
        if self._session is not None:
            self._shared.sessions.close(self._audit_id, self._session)
            self._session = None

    @property
    def endpoint(self) -> Optional[str]:
        return self._shared.endpoint

    @property
    def audit_id(self) -> str:
        return self._audit_id

    def register_trace(self, trace_id: str) -> None:
        """Register a trace_id with this session (called by the correlation layer)."""
        # Only while this provider's own session is the live one for the id:
        # otherwise its traces would be routed into another audit's session.
        if self._session is not None and not self._session.expired and \
                self._shared.sessions._sessions.get(self._audit_id) is self._session:
            self._shared.sessions.register_trace(self._audit_id, trace_id)

    def fetch(self, trace_id: str) -> List[Dict[str, Any]]:
        if self._session is None:
            return []
        return self._session.spans_for_trace(trace_id)

    def __enter__(self) -> "SharedOTLP":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()


class _RoutingSpanStore:
    """A SpanStore-compatible facade that routes spans to TraceSessions.

    Implements the same interface as ``SpanStore`` (``add``, ``add_many``,
    ``by_trace``, ``all``, ``__len__``) but delegates to the
    ``TraceSessionManager`` for routing.
    """

    def __init__(self, manager: TraceSessionManager) -> None:
        self._manager = manager

    def add(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        self._manager.route_spans([raw])
        return raw

    def add_many(self, spans: List[Dict[str, Any]]) -> None:
        self._manager.route_spans(spans)

    def by_trace(self, trace_id: str) -> List[Dict[str, Any]]:
        # Aggregate across all active sessions.
        results: List[Dict[str, Any]] = []
        for session in self._manager.active_sessions:
            results.extend(session.spans_for_trace(trace_id))
        return results

    def all(self) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for session in self._manager.active_sessions:
            results.extend(session.all_spans())
        return results

    def __len__(self) -> int:
        return sum(len(s) for s in self._manager.active_sessions)
