"""
CallableTarget — an in-process Python callable as an audit target.

Useful for:
    - unit/integration tests of the audit engine without a network
    - auditing a local function that wraps an agent, RAG pipeline, or tool
    - quick experiments before wiring up a real HTTP endpoint

The callable receives the same logical inputs as any Target and must return
either a plain string (treated as ``content``) or a :class:`TargetResponse`.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from ..context_marks import parse_documents
from .base import (
    Target,
    TargetContext,
    TargetResponse,
    history_documents,
    history_file_uri,
    plain_history,
)

# A callable target may return a string or a TargetResponse.
TargetCallable = Callable[..., Union[str, TargetResponse, Awaitable[Union[str, TargetResponse]]]]


class CallableTarget:
    """Wrap an in-process callable as a :class:`Target`.

    The callable gets what a system under test may see (see ``Target.send``):
    the history without the engine's markers, the scenario's documents as
    text through ``documents`` and its files through ``file_uri``. No
    evaluation-only document mark reaches it.
    """

    def __init__(self, fn: TargetCallable) -> None:
        self._fn = fn

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
        if documents is None:
            documents = history_documents(history)
        result = self._fn(
            system=system,
            user=user,
            history=plain_history(history) if history is not None else None,
            file_uri=file_uri if file_uri is not None else history_file_uri(history),
            documents=[mark.text for mark in parse_documents(documents)] or None,
            response_format=response_format,
            params=params,
            context=context,
        )
        # Support both sync and async callables.
        if hasattr(result, "__await__"):
            result = await result
        if isinstance(result, TargetResponse):
            return result
        return TargetResponse(content=str(result))
