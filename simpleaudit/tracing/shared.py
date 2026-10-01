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

    # Start once (e.g. at app boot)
    shared = SharedOTLPReceiver(host="0.0.0.0", port=4317).start()

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
from typing import Any, Dict, List, Optional

from .otlp import EphemeralOTLPReceiver
from .store import SpanStore


@dataclass
class TraceSession:
    """Ephemeral trace buffer for one audit run.

    Spans are routed here by ``trace_id``. The session expires after
    ``ttl`` seconds (checked lazily on access) or when :meth:`close` is
    called explicitly.
    """

    audit_id: str
    execution_id: str = ""
    target_id: str = ""
    created_at: float = field(default_factory=time.time)
    ttl: float = 300.0  # seconds
    _store: SpanStore = field(default_factory=SpanStore, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def expired(self) -> bool:
        return self._closed or (time.time() - self.created_at) > self.ttl

    @property
    def store(self) -> SpanStore:
        return self._store

    def add_many(self, spans: List[Dict[str, Any]]) -> None:
        if not self.expired:
            self._store.add_many(spans)

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
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, TraceSession] = {}  # audit_id → session
        self._trace_index: Dict[str, str] = {}  # trace_id → audit_id
        self._lock = threading.Lock()

    def create(
        self,
        audit_id: str,
        *,
        execution_id: str = "",
        target_id: str = "",
        ttl: float = 300.0,
    ) -> TraceSession:
        """Create a new trace session for an audit run."""
        session = TraceSession(
            audit_id=audit_id,
            execution_id=execution_id,
            target_id=target_id,
            ttl=ttl,
        )
        with self._lock:
            self._sessions[audit_id] = session
        return session

    def register_trace(self, audit_id: str, trace_id: str) -> None:
        """Map a ``trace_id`` to an audit session (called by the correlation layer)."""
        with self._lock:
            self._trace_index[trace_id] = audit_id

    def route_spans(self, spans: List[Dict[str, Any]]) -> None:
        """Route incoming spans to the session that owns their ``trace_id``.

        Spans whose ``trace_id`` is not registered are dropped (they belong
        to a non-audit flow or an expired session).
        """
        if not spans:
            return
        # Group spans by trace_id for efficient lookup.
        by_trace: Dict[str, List[Dict[str, Any]]] = {}
        for s in spans:
            tid = s.get("trace_id", "")
            if tid:
                by_trace.setdefault(tid, []).append(s)

        with self._lock:
            for tid, trace_spans in by_trace.items():
                audit_id = self._trace_index.get(tid)
                if audit_id is None:
                    continue
                session = self._sessions.get(audit_id)
                if session is None or session.expired:
                    continue
                session.add_many(trace_spans)

    def get(self, audit_id: str) -> Optional[TraceSession]:
        with self._lock:
            session = self._sessions.get(audit_id)
            if session and session.expired:
                del self._sessions[audit_id]
                return None
            return session

    def close(self, audit_id: str) -> None:
        """Close a session and discard its spans."""
        with self._lock:
            session = self._sessions.pop(audit_id, None)
            # Remove trace index entries for this session.
            stale_traces = [tid for tid, aid in self._trace_index.items() if aid == audit_id]
            for tid in stale_traces:
                del self._trace_index[tid]
        if session:
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

    Usage::

        # Boot (once)
        shared = SharedOTLPReceiver(host="0.0.0.0", port=4317).start()

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
        host: str = "0.0.0.0",
        port: int = 4317,
        *,
        session_ttl: float = 300.0,
        sweep_interval: float = 60.0,
    ) -> None:
        self.host = host
        self.port = port
        self.session_ttl = session_ttl
        self.sessions = TraceSessionManager()
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
        self._receiver = EphemeralOTLPReceiver(host=self.host, port=self.port).start()
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
        # Close all remaining sessions.
        for session in self.sessions.active_sessions:
            session.close()

    def __enter__(self) -> "SharedOTLPReceiver":
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
