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

import asyncio
import json
import os
from collections.abc import Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from ..context_marks import parse_documents
from ._http import make_client, warn_if_cleartext, with_endpoint
from .base import TargetContext, TargetResponse, history_documents

#: Ollama's request-body cap for System One requests without images.
OLLAMA_MAX_BODY_BYTES = 64 * 1024

#: Statuses worth another attempt: rate limiting and server-side failures.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Statuses whose ``Retry-After`` header (RFC 9110 section 10.2.3) is honoured.
RETRY_AFTER_STATUSES = frozenset({429, 503})

#: Longest wait, in seconds, that a ``Retry-After`` header can ask for.
_RETRY_AFTER_MAX = 60.0

#: OpenRouter's System One endpoint (``POST /api/v1/systemone``).
OPENROUTER_SYSTEMONE_URL = "https://openrouter.ai/api/v1/systemone"


def _retry_after_seconds(resp: Any) -> float | None:
    """Seconds a ``Retry-After`` header asks for (delay-seconds or HTTP-date), else None."""
    value = (resp.headers.get("Retry-After") or "").strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class DecisionTarget:
    """Send a scenario's decision question to a System One endpoint."""

    #: A decision model answers once; the auditor caps scenarios at this many turns.
    max_turns = 1

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
        max_retries: int = 0,
        retry_backoff: float = 0.5,
        reuse_client: bool = False,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be 0 or more")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be 0 or more")
        self.url = url
        warn_if_cleartext(url)
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
        #: Extra attempts after a transport error or a status in ``RETRY_STATUSES``;
        #: 0 (the default) sends each request once. Attempt ``n`` waits
        #: ``retry_backoff * 2**n`` seconds before the next one, as in ``ModelTarget``.
        #: A ``Retry-After`` header on a 429 or 503 replaces that wait, capped at
        #: ``_RETRY_AFTER_MAX`` (60 s).
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self._reuse_client = reuse_client
        self._shared_client: Any | None = None
        self._shared_loop: asyncio.AbstractEventLoop | None = None

    def _acquire_client(self) -> tuple[Any, bool]:
        """The client for one request and whether the request must close it.

        An injected client is never closed here. With ``reuse_client`` one
        client serves every request on the running event loop. Each sync
        ``run()`` has its own loop, and a client cannot be used or closed on
        another loop, so a client from an earlier loop is dropped and replaced.
        """
        if self._client is not None:
            return self._client, False
        if not self._reuse_client:
            return make_client(self.timeout), True
        loop = asyncio.get_running_loop()
        if self._shared_client is None or self._shared_loop is not loop:
            self._shared_client = make_client(self.timeout)
            self._shared_loop = loop
        return self._shared_client, False

    async def aclose(self) -> None:
        """Close the client kept by ``reuse_client``; an injected client is the caller's."""
        client, loop = self._shared_client, self._shared_loop
        self._shared_client = self._shared_loop = None
        if client is not None and loop is asyncio.get_running_loop():
            await client.aclose()

    async def __aenter__(self) -> DecisionTarget:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _post(self, payload: bytes, headers: dict[str, str]) -> Any:
        """POST ``payload``, retrying transient failures up to ``max_retries`` times."""
        import httpx

        client, owns_client = self._acquire_client()
        try:
            for attempt in range(self.max_retries + 1):
                last = attempt == self.max_retries
                delay = self.retry_backoff * 2**attempt
                try:
                    resp = await client.post(self.url, content=payload, headers=headers)
                except httpx.TransportError:
                    if last:
                        raise
                else:
                    if resp.status_code not in RETRY_STATUSES or last:
                        return resp
                    if resp.status_code in RETRY_AFTER_STATUSES:
                        asked = _retry_after_seconds(resp)
                        if asked is not None:
                            delay = min(asked, _RETRY_AFTER_MAX)
                await asyncio.sleep(delay)
        finally:
            if owns_client:
                await client.aclose()

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
        payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if self.max_body_bytes is not None and len(payload) > self.max_body_bytes:
            raise ValueError(
                f"request is {len(payload)} bytes, over this endpoint's {self.max_body_bytes}-byte "
                "limit; the documents are not shortened"
            )

        headers = {**self.headers, "Content-Type": "application/json"}
        if context is not None:
            headers.update(context.trace_headers)

        resp = await self._post(payload, headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"decision endpoint returned HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()

        answer = (data.get("answers") or {}).get(decision["id"]) if isinstance(data, dict) else None
        if not isinstance(answer, dict):
            raise RuntimeError(f"decision endpoint returned no answer for {decision['id']!r}")
        choice = answer.get("choice")
        if choice not in decision["criteria"]:
            raise RuntimeError(
                f"decision endpoint chose {choice!r}, which is not one of the options"
            )

        description = decision["criteria"][choice]
        usage = data.get("usage") or {}
        return TargetResponse(
            content=f"{choice}: {description}" if description else choice,
            raw=with_endpoint(data, self.url),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            decision=answer,
        )
