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

from ..context_marks import document_texts
from ._http import make_client, warn_if_cleartext, with_endpoint
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


def _with_system_text(fixed: Any, system: str) -> Any:
    """The template's own system prompt at that field, if any, followed by the audit's."""
    if isinstance(fixed, str) and fixed:
        return f"{fixed}\n\n{system}"
    if isinstance(fixed, list):  # content blocks, e.g. Anthropic's native "system"
        return [*fixed, {"type": "text", "text": system}]
    return system


def _with_system_message(messages: List[Dict[str, Any]], system: str) -> List[Dict[str, Any]]:
    """One leading system message: the template's, if it starts with one, then the audit's prompt.

    Many chat templates reject two system messages in a row.
    """
    if messages and messages[0].get("role") == "system":
        first = messages[0]
        return [{**first, "content": _with_system_text(first.get("content"), system)}, *messages[1:]]
    return [{"role": "system", "content": system}, *messages]


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
        # The request field for the audit's system prompt. Without it, the
        # prompt is a leading system message in "messages" mode and cannot be
        # sent in other modes.
        system_field: Optional[str] = None,
    ) -> None:
        self.url = url
        warn_if_cleartext(url)
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
        doc_texts = document_texts(documents)
        if doc_texts:
            if self.documents_field is None:
                raise ValueError(
                    "HTTPAppTarget received the scenario's documents but has no way to "
                    "send them: pass documents_field= to name the request field that "
                    "carries the retrieval context."
                )
            body[self.documents_field] = doc_texts
        earlier = _earlier_turns(user, history)
        if system and self.system_field:
            body[self.system_field] = _with_system_text(body.get(self.system_field), system)
        if self.message_field == "messages":
            # OpenAI-style chat body: any messages the request template fixes,
            # then the conversation. Without a system_field, the audit's system
            # prompt joins the template's leading system message or becomes one.
            fixed = body.get("messages") if isinstance(body.get("messages"), list) else []
            if system and not self.system_field:
                fixed = _with_system_message(fixed, system)
            body["messages"] = [*fixed, *earlier, {"role": "user", "content": user}]
        else:
            body[self.message_field] = user
            if system and not self.system_field and not self._warned_system:
                warnings.warn(
                    "HTTPAppTarget has nowhere to send the system prompt: pass "
                    "system_field= to name the request field for it.",
                    stacklevel=3,
                )
                self._warned_system = True
            if history is not None:
                # A history list in the template comes before the conversation.
                fixed = body.get("history", [])
                if not isinstance(fixed, list):
                    raise ValueError(
                        "request_template's 'history' must be a list for the conversation to "
                        "follow it."
                    )
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
            client = make_client(self.timeout)
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
            raw=with_endpoint(data, self.url),
            input_tokens=int(in_tok) if in_tok is not None else None,
            output_tokens=int(out_tok) if out_tok is not None else None,
        )
