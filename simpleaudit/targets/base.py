"""
Core Target protocol and shared data types.

This module defines the contract the audit engine depends on. It imports
nothing from the rest of the package so that any target implementation
(model, HTTP app, callable, future agent runtime) can satisfy it without
circular imports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Union, runtime_checkable


@dataclass
class TargetResponse:
    """Normalized result of sending a message to a target.

    ``input_tokens`` / ``output_tokens`` are ``None`` when the target does not
    report usage (e.g. a black-box HTTP app). Callers must treat ``None`` as
    "unknown", never as zero, so audit records do not fabricate token counts.
    """

    content: str
    raw: Any = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    # A decision model's full answer to the scenario's decision question
    # (choice, probabilities, confidence). None for every other target. The
    # auditor stores it beside the reply in the transcript.
    decision: Optional[Dict[str, Any]] = None
    # What an agent did while producing this reply, for targets that can see
    # it (an agent harness). JSON-serialisable dicts, in order:
    #   {"type": "tool_call", "name": str, "arguments": Any, "id": str}
    #   {"type": "tool_result", "name": str, "result": Any, "error": bool, "id": str}
    # ("id" is optional and pairs a result with its call). The auditor stores
    # the list beside the reply and shows it to the judge only: the probe
    # model and provider APIs never receive it.
    tool_events: Optional[List[Dict[str, Any]]] = None
    # True when the system under test ended the conversation (an agent that
    # finished its episode). The auditor stops the turn loop after this reply
    # and judges the transcript so far.
    end_conversation: bool = False


@dataclass
class TargetContext:
    """Per-turn context passed to a target.

    Carries the audit correlation identifiers and (optionally) the W3C trace
    context so a target can propagate it to the system under test. All fields
    are optional so existing call sites that do not care about tracing can
    pass a bare context or ``None``.
    """

    audit_run_id: Optional[str] = None
    scenario_run_id: Optional[str] = None
    turn_id: Optional[str] = None
    # W3C trace-context headers to forward, e.g. {"traceparent": "00-...-...-01"}.
    trace_headers: Dict[str, str] = field(default_factory=dict)
    # Free-form extra metadata a target may use (e.g. custom correlation ids).
    extra: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Target(Protocol):
    """Anything the auditor can send a message to and get a response from.

    Implementations:
        - ``ModelTarget``    (LLM endpoint via AnyLLM)
        - ``HTTPAppTarget``  (external application over HTTP)
        - ``CallableTarget`` (in-process Python callable)
        - ``DecisionTarget`` (decision model over a System One endpoint)

    The signature mirrors the historical ``ModelAuditor._call_async`` inputs so
    that ``ModelTarget`` can delegate to the existing client path with no
    behavior change. New targets may ignore the model-specific keyword
    arguments (``response_format``, ``file_uri``, ``documents``) when they do
    not apply.

    A target that holds per-scenario state (an agent session, a sandbox) may
    also define ``async start_scenario(context)`` and ``async
    end_scenario(context)``. ``ModelAuditor.run_scenario`` awaits the first
    before turn 0 and the second after the last turn, before judging; the
    context carries ``scenario_run_id`` and ``extra["scenario_name"]``. Both
    are optional and are not part of this protocol. ``SingleTurnAuditor``
    does not call them.
    """

    async def send(
        self,
        *,
        system: Optional[str] = None,
        user: str,
        history: Optional[List[Dict[str, Any]]] = None,
        file_uri: Optional[Union[str, List[str]]] = None,
        documents: Optional[List[Union[str, Dict[str, Any]]]] = None,
        response_format: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        context: Optional[TargetContext] = None,
    ) -> TargetResponse:
        """Send one message and return the normalized response."""
        ...
