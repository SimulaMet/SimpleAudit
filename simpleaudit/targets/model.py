"""
ModelTarget — an LLM endpoint target backed by AnyLLM.

This is the historical default target. It wraps an AnyLLM client (or a
pre-bound transport) so the core engine can call ``target.send(...)`` instead
of reaching into ``client.acompletion(...)`` directly.

Two construction modes:

1. **Client mode** — pass an AnyLLM client + model. ``send()`` builds the
   OpenAI-style message list and calls ``client.acompletion`` exactly like the
   legacy ``ModelAuditor._call_async`` path, so behavior is unchanged.

2. **Transport mode** — pass a ``transport`` async callable that already knows
   how to turn (system, user, history, ...) into (content, in_tok, out_tok).
   The engine uses this to keep a single source of truth for message building
   and retry logic while still routing through the ``Target`` protocol.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from .base import Target, TargetContext, TargetResponse

# A transport turns the logical send inputs into a normalized response.
Transport = Callable[..., Awaitable[TargetResponse]]


class ModelTarget:
    """A target that talks to an LLM endpoint via an AnyLLM client."""

    def __init__(
        self,
        client: Any = None,
        model: Optional[str] = None,
        *,
        transport: Optional[Transport] = None,
        max_retries: int = 0,
        retry_backoff: float = 0.5,
    ) -> None:
        if transport is None and client is None:
            raise ValueError("ModelTarget requires either a client or a transport")
        self._client = client
        self._model = model
        self._transport = transport
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    @property
    def client(self) -> Any:
        return self._client

    @property
    def model(self) -> Optional[str]:
        return self._model

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
        if self._transport is not None:
            return await self._transport(
                system=system,
                user=user,
                history=history,
                file_uri=file_uri,
                documents=documents,
                response_format=response_format,
                params=params,
                context=context,
            )
        return await _client_send(
            self._client,
            self._model,
            system=system,
            user=user,
            history=history,
            file_uri=file_uri,
            documents=documents,
            response_format=response_format,
            params=params,
            context=context,
            max_retries=self.max_retries,
            retry_backoff=self.retry_backoff,
        )


async def _client_send(
    client: Any,
    model: Optional[str],
    *,
    system: Optional[str],
    user: str,
    history: Optional[List[Dict[str, Any]]],
    file_uri: Optional[Union[str, List[str]]],
    documents: Optional[List[Union[str, Dict[str, Any]]]],
    response_format: Optional[Dict[str, Any]],
    params: Optional[Dict[str, Any]],
    context: Optional[TargetContext] = None,
    max_retries: int,
    retry_backoff: float,
) -> TargetResponse:
    """Delegate to the shared AnyLLM call path.

    Imported lazily so that ``simpleaudit.targets`` does not create an import
    cycle with ``model_auditor`` at package import time.

    When the per-turn context carries W3C trace headers (e.g. the engine's
    scenario traceparent), they are merged into ``extra_headers`` so the
    target's outgoing call propagates the trace context. This is what lets an
    instrumented target export spans under the same trace id the engine's
    TraceCorrelation records. User-supplied extra_headers are preserved.
    """
    from ..model_auditor import ModelAuditor

    if context is not None and context.trace_headers:
        headers = dict(params.get("extra_headers", {}) if params else {})
        headers.update(context.trace_headers)
        params = dict(params or {})
        params["extra_headers"] = headers

    content, input_tokens, output_tokens = await ModelAuditor._call_async(
        client,
        model,
        system,
        user,
        response_format=response_format,
        history=history,
        file_uri=file_uri,
        documents=documents,
        max_retries=max_retries,
        retry_backoff=retry_backoff,
        params=params,
    )
    return TargetResponse(
        content=content,
        input_tokens=input_tokens or None,
        output_tokens=output_tokens or None,
    )
