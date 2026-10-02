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

from .base import Target, TargetContext, TargetResponse

# A callable target may return a string or a TargetResponse.
TargetCallable = Callable[..., Union[str, TargetResponse, Awaitable[Union[str, TargetResponse]]]]


class CallableTarget:
    """Wrap an in-process callable as a :class:`Target`."""

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
        result = self._fn(
            system=system,
            user=user,
            history=history,
            file_uri=file_uri,
            documents=documents,
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
