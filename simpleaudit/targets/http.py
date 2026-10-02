"""
HTTPAppTarget — audit an external application over HTTP (black-box).

This is the target that lets SimpleAudit audit systems that are *not* a bare
LLM endpoint: an agent service, a RAG app, an Open WebUI instance, a company
API, etc. SimpleAudit sends the probe as an HTTP request and reads the answer
out of the response using ``response_path``.

Design notes:
    - **No tracing required.** Works with zero instrumentation on the target.
    - **Correlation is optional.** When a :class:`TargetContext` carries
      ``trace_headers`` (e.g. a W3C ``traceparent``) or correlation ids, they
      are forwarded as request headers so an instrumented target can link its
      OTel spans back to this audit turn.
    - **Tokens are nullable.** External apps rarely report usage, so
      ``input_tokens`` / ``output_tokens`` stay ``None`` unless the response
      explicitly provides them.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional, Union

from .base import Target, TargetContext, TargetResponse


def _resolve_path(data: Any, path: Optional[str]) -> Any:
    """Resolve a dotted path (e.g. ``"choices.0.message.content"``) in a dict/list."""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


class HTTPAppTarget:
    """Send audit probes to an external HTTP application and parse the answer."""

    def __init__(
        self,
        url: str,
        *,
        method: str = "POST",
        headers: Optional[Dict[str, str]] = None,
        request_template: Optional[Dict[str, Any]] = None,
        message_field: str = "message",
        response_path: Optional[str] = None,
        timeout: float = 60.0,
        client: Optional[Any] = None,
        # Optional: extract token usage from the response, e.g.
        # ("usage.prompt_tokens", "usage.completion_tokens").
        token_paths: Optional[tuple] = None,
    ) -> None:
        self.url = url
        self.method = method.upper()
        self.headers = dict(headers or {})
        self.request_template = dict(request_template or {})
        self.message_field = message_field
        self.response_path = response_path
        self.timeout = timeout
        self._client = client
        self.token_paths = token_paths

    def _build_body(self, user: str, history: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
        body = json.loads(json.dumps(self.request_template))  # deep copy
        if self.message_field == "messages":
            # OpenAI-style chat body: ``messages`` is a list of
            # ``{"role", "content"}`` dicts. Append the current user turn,
            # optionally preceded by prior conversation history.
            messages: List[Dict[str, str]] = []
            if history:
                messages.extend(history)
            messages.append({"role": "user", "content": user})
            body["messages"] = messages
        else:
            body[self.message_field] = user
            if history is not None:
                body.setdefault("history", history)
        return body

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
        headers = dict(self.headers)
        body = self._build_body(user, history)

        # Forward correlation / trace context when available.
        if context is not None:
            headers.update(context.trace_headers)
            if context.turn_id:
                headers.setdefault("X-SimpleAudit-Turn-ID", context.turn_id)
            if context.scenario_run_id:
                headers.setdefault("X-SimpleAudit-Scenario-Run-ID", context.scenario_run_id)
            if context.audit_run_id:
                headers.setdefault("X-SimpleAudit-Run-ID", context.audit_run_id)

        owns_client = self._client is None
        if client := self._client:
            pass
        else:
            import httpx

            client = httpx.AsyncClient(timeout=self.timeout)
        try:
            resp = await client.request(self.method, self.url, json=body, headers=headers)
            resp.raise_for_status()
            try:
                data = resp.json()
            except ValueError:
                data = resp.text
        finally:
            if owns_client:
                await client.aclose()

        content = _resolve_path(data, self.response_path) if self.response_path else data
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)

        in_tok = out_tok = None
        if self.token_paths and isinstance(data, (dict, list)):
            in_tok = _resolve_path(data, self.token_paths[0])
            out_tok = _resolve_path(data, self.token_paths[1])

        return TargetResponse(
            content=content,
            raw=data,
            input_tokens=int(in_tok) if in_tok is not None else None,
            output_tokens=int(out_tok) if out_tok is not None else None,
        )
