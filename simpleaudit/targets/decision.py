"""
DecisionTarget — audit a decision model through a System One endpoint.

A decision model does not write prose. It reads a *state* (any text or JSON)
and a set of typed questions with fixed options, and returns the chosen option
for each question with a probability for every option. Examples are Clef
served by Ollama (``POST /v1/systemone``) and Jev on OpenRouter's Decisions
API. This target turns a scenario's ``decision`` block (see
``simpleaudit/decision.py``) into one such request:

    {"model": ..., "state": ..., "questions": {<id>: {"type": "choice",
                                                      "instructions": ...,
                                                      "criteria": {...}}}}

and the answer back into a :class:`TargetResponse` whose ``content`` names the
chosen option (``"yes: Found guilty"``), so judges, summaries and the viewer
work unchanged. The full answer (choice, probabilities, confidence) is in
``TargetResponse.decision``; the auditor stores it in the transcript.

Design notes:
    - **The answer key never travels.** The auditor hands this target the
      decision block without ``accepted`` (``TargetContext.extra``).
    - **State.** The scenario's ``decision.state`` plus the text of its
      ``documents`` (document marks are never sent). A scenario with neither
      sends the prompt text as the state.
    - **Limits are checked before sending.** System One endpoints accept 2–26
      options per question, and Ollama caps a request body at 64 KiB. Breaking
      a limit raises ``ValueError`` before any request, so the scenario is
      recorded as an error rather than silently shortened.
    - **One turn.** A decision model answers a question once; it cannot take
      part in a conversation. ``max_turns = 1`` tells the auditor to stop after
      the first turn.
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Mapping
from typing import Any

from ..context_marks import parse_documents
from .base import TargetContext, TargetResponse, history_documents, history_file_uri, token_count

#: Ollama's request-body cap for System One requests without images.
OLLAMA_MAX_BODY_BYTES = 64 * 1024

#: OpenRouter's System One endpoint (``POST /api/v1/systemone``).
OPENROUTER_SYSTEMONE_URL = "https://openrouter.ai/api/v1/systemone"


async def post_systemone(
    url: str,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    client: Any | None = None,
    timeout: float = 300.0,
    max_body_bytes: int | None = None,
    too_large_hint: str = "the request is not shortened",
) -> Any:
    """POST one System One request and return the decoded JSON body.

    Shared by :class:`DecisionTarget` and
    :class:`simpleaudit.decision_judge.DecisionJudgeClient`. A body over
    ``max_body_bytes`` raises ``ValueError`` before anything is sent; an HTTP
    error status raises ``RuntimeError`` with the start of the response text.
    ``client`` is an ``httpx.AsyncClient`` to reuse; without one, a client is
    created and closed for this request.
    """
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if max_body_bytes is not None and len(payload) > max_body_bytes:
        raise ValueError(
            f"request is {len(payload)} bytes, over this endpoint's {max_body_bytes}-byte "
            f"limit; {too_large_hint}"
        )
    send_headers = {**(headers or {}), "Content-Type": "application/json"}

    owns_client = client is None
    if owns_client:
        import httpx

        client = httpx.AsyncClient(timeout=timeout)
    try:
        resp = await client.post(url, content=payload, headers=send_headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"decision endpoint returned HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return resp.json()
    finally:
        if owns_client:
            await client.aclose()


class DecisionTarget:
    """Send a scenario's decision question to a System One endpoint."""

    #: A decision model answers once; the auditor caps scenarios at this many turns.
    max_turns = 1

    _warned_system = False  # warn once per target about the dropped system prompt

    def __init__(
        self,
        url: str,
        model: str,
        *,
        api_key: str | None = None,
        headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        timeout: float = 300.0,
        client: Any | None = None,
        min_options: int = 2,
        max_options: int | None = 26,
        max_body_bytes: int | None = None,
    ) -> None:
        self.url = url
        self.model = model
        self.headers = dict(headers or {})
        if api_key:
            self.headers.setdefault("Authorization", f"Bearer {api_key}")
        self.extra_body = dict(extra_body or {})
        self.timeout = timeout
        self._client = client
        self.min_options = min_options
        self.max_options = max_options
        self.max_body_bytes = max_body_bytes

    @classmethod
    def ollama(
        cls, model: str, base_url: str = "http://localhost:11434", **kwargs: Any
    ) -> DecisionTarget:
        """A decision model served by Ollama, e.g. ``DecisionTarget.ollama("clef")``."""
        kwargs.setdefault("max_body_bytes", OLLAMA_MAX_BODY_BYTES)
        return cls(f"{base_url.rstrip('/')}/v1/systemone", model, **kwargs)

    @classmethod
    def openrouter(cls, model: str, api_key: str | None = None, **kwargs: Any) -> DecisionTarget:
        """A decision model on OpenRouter's Decisions API, e.g. ``"typesafe/jev-1.13"``.

        The key defaults to ``OPENROUTER_API_KEY``. Provider routing (for example
        ``{"provider": {"zdr": True}}``) can be passed as ``extra_body``.
        """
        return cls(
            OPENROUTER_SYSTEMONE_URL,
            model,
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY"),
            **kwargs,
        )

    def build_request(
        self,
        decision: Mapping[str, Any],
        user: str,
        documents: list[str | dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """The request body for one decision question. Raises ``ValueError`` on a broken limit."""
        criteria = dict(decision["criteria"])
        if len(criteria) < self.min_options or (
            self.max_options is not None and len(criteria) > self.max_options
        ):
            limit = (
                f"{self.min_options}–{self.max_options}"
                if self.max_options
                else f"≥{self.min_options}"
            )
            raise ValueError(
                f"decision {decision['id']!r} has {len(criteria)} options; this endpoint accepts {limit}"
            )

        state: dict[str, Any] = dict(decision.get("state") or {})
        if documents:
            # Only the text: document marks are the judge's ground truth.
            state["documents"] = [mark.text for mark in parse_documents(documents)]
        return {
            **self.extra_body,
            "model": self.model,
            "state": state or user,
            "questions": {
                decision["id"]: {
                    "type": decision.get("type", "choice"),
                    "instructions": decision["instructions"],
                    "criteria": criteria,
                }
            },
        }

    async def send(
        self,
        *,
        system: str | None = None,
        user: str,
        history: list[dict[str, Any]] | None = None,
        file_uri: str | list[str] | None = None,
        documents: list[str | dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        context: TargetContext | None = None,
    ) -> TargetResponse:
        # Like the HTTP and OpenEnv targets: an attachment the endpoint never
        # receives must not be graded as if it had been sent.
        if file_uri or history_file_uri(history):
            raise ValueError("DecisionTarget cannot send attached files; this scenario attaches file_uri")
        if system and not self._warned_system:
            self._warned_system = True
            warnings.warn(
                "DecisionTarget does not send the system prompt to the decision endpoint.",
                stacklevel=2,
            )
        decision = (context.extra if context is not None else {}).get("decision")
        if decision is None:
            raise ValueError(
                "DecisionTarget needs a scenario with a 'decision' block "
                "(see the scenario guidelines, 'Decision Field')"
            )
        # The auditor attaches a scenario's documents to the first user turn.
        if documents is None:
            documents = history_documents(history)
        body = self.build_request(decision, user, documents)
        data = await post_systemone(
            self.url,
            body,
            headers={**self.headers, **(context.trace_headers if context is not None else {})},
            client=self._client,
            timeout=self.timeout,
            max_body_bytes=self.max_body_bytes,
            too_large_hint="the documents are not shortened",
        )

        answer = (data.get("answers") or {}).get(decision["id"]) if isinstance(data, dict) else None
        if not isinstance(answer, dict):
            raise RuntimeError(f"decision endpoint returned no answer for {decision['id']!r}")
        choice = answer.get("choice")
        if choice not in decision["criteria"]:
            raise RuntimeError(
                f"decision endpoint chose {choice!r}, which is not one of the options"
            )

        description = decision["criteria"][choice]
        usage = data.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        return TargetResponse(
            content=f"{choice}: {description}" if description else choice,
            raw=data,
            input_tokens=token_count(usage.get("input_tokens")),
            output_tokens=token_count(usage.get("output_tokens")),
            decision=answer,
        )
