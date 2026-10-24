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
import warnings
from typing import Any, Callable, Dict, List, Optional, Union

from ..context_marks import parse_documents
from .base import (
    Target,
    TargetContext,
    TargetResponse,
    history_documents,
    history_file_uri,
    plain_history,
)


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


def _earlier_turns(user: str, history: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """The conversation before the current turn, without the engine's markers.

    Under ``run_scenario`` the history ends with the current user turn, which
    the body adds itself; a direct caller may pass the earlier turns only.
    Fields other than the markers (``tool_calls``, ``tool_call_id``, ...) stay.
    """
    turns = plain_history(history)
    if turns and turns[-1].get("role") == "user" and turns[-1].get("content") == user:
        turns.pop()
    return turns


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
        # Where a context-grounding run places its retrieval documents (text
        # only). When set, the document texts are written to this request
        # field. When unset and documents are supplied, the run fails loudly:
        # an HTTP app must never be probed without its intended context.
        documents_field: Optional[str] = None,
        # Optional: extract token usage from the response, e.g.
        # ("usage.prompt_tokens", "usage.completion_tokens").
        token_paths: Optional[tuple] = None,
        # Where the audit's system prompt goes when message_field is not
        # "messages" (in "messages" mode it is a leading system message).
        system_field: Optional[str] = None,
    ) -> None:
        self.url = url
        self.method = method.upper()
        self.headers = dict(headers or {})
        self.request_template = dict(request_template or {})
        self.message_field = message_field
        self.response_path = response_path
        self.timeout = timeout
        self._client = client
        self.documents_field = documents_field
        self.token_paths = token_paths
        self.system_field = system_field
        self._warned_system = False

    def _build_body(
        self,
        user: str,
        history: Optional[List[Dict[str, Any]]],
        documents: Optional[List[Union[str, Dict[str, Any]]]] = None,
        system: Optional[str] = None,
    ) -> Dict[str, Any]:
        body = json.loads(json.dumps(self.request_template))  # deep copy
        # Retrieval context: text only, so no evaluation-only mark ever
        # reaches the target. A context-grounding run passes the documents
        # directly; a multi-turn run carries them on the history. Every
        # document keeps its position, matching the judge's numbering. Fail
        # loudly when documents were supplied but this body has nowhere to
        # carry them — a silent drop would grade a response the app never saw
        # the context for.
        if documents is None:
            documents = history_documents(history)
        doc_texts = [mark.text for mark in parse_documents(documents)]
        if doc_texts:
            if self.documents_field is None:
                raise ValueError(
                    "HTTPAppTarget received the scenario's documents but has no way to "
                    "send them: pass documents_field= to name the request field that "
                    "carries the retrieval context."
                )
            body[self.documents_field] = doc_texts
        earlier = _earlier_turns(user, history)
        if self.message_field == "messages":
            # OpenAI-style chat body: the audit's system prompt, any messages
            # the request template fixes, then the conversation.
            fixed = body.get("messages") if isinstance(body.get("messages"), list) else []
            system_turn = [{"role": "system", "content": system}] if system else []
            body["messages"] = [*system_turn, *fixed, *earlier, {"role": "user", "content": user}]
        else:
            body[self.message_field] = user
            if system:
                if self.system_field:
                    body[self.system_field] = system
                elif not self._warned_system:
                    self._warned_system = True
                    warnings.warn(
                        "HTTPAppTarget has nowhere to send the system prompt: pass "
                        "system_field= to name the request field for it.",
                        stacklevel=3,
                    )
            if history is not None:
                # A history list in the template comes before the conversation.
                fixed = body.get("history") if isinstance(body.get("history"), list) else []
                body["history"] = [*fixed, *earlier]
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
        # Like documents, an attachment the app never receives must not be
        # graded as if it had.
        if file_uri or history_file_uri(history):
            raise ValueError("HTTPAppTarget cannot send attached files; this scenario attaches file_uri")
        headers = dict(self.headers)
        body = self._build_body(user, history, documents, system)

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
