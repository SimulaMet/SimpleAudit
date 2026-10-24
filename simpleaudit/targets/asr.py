"""Single-turn speech transcription over the OpenAI-compatible audio API."""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ._http import make_client, warn_if_cleartext, with_endpoint
from .base import TargetContext, TargetResponse


class ASRTarget:
    """Upload one local audio file to ``POST /audio/transcriptions``.

    ``base_url`` includes the API version (e.g. /v1 or /api/v1). Only explicit
    transcription options are sent; prompts, documents and references are not.
    An injected async httpx client remains owned by its caller.
    """

    max_turns = 1
    preserve_content = True
    default_prompt = "Transcribe the recording."

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout: float = 300,
        options: dict[str, Any] | None = None,
        client: Any = None,
    ) -> None:
        self.model = model
        self.url = base_url.rstrip("/") + "/audio/transcriptions"
        warn_if_cleartext(self.url)
        self.api_key = api_key
        self.timeout = timeout
        self.options = dict(options or {})
        self._client = client

    @classmethod
    def openai(cls, model: str = "whisper-1", **kwargs: Any) -> ASRTarget:
        kwargs.setdefault("api_key", os.environ.get("OPENAI_API_KEY"))
        return cls(model, **kwargs)

    @classmethod
    def openrouter(cls, model: str, **kwargs: Any) -> ASRTarget:
        kwargs.setdefault("api_key", os.environ.get("OPENROUTER_API_KEY"))
        kwargs.setdefault("base_url", "https://openrouter.ai/api/v1")
        return cls(model, **kwargs)

    async def send(
        self,
        *,
        user: str,
        file_uri: str | Path | list[str] | None = None,
        params: dict[str, Any] | None = None,
        context: TargetContext | None = None,
        system: str | None = None,
        history: list[dict[str, Any]] | None = None,
        documents: list[str | dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> TargetResponse:
        if file_uri is None:
            file_uri = next(
                (
                    m.get("file_uri")
                    for m in reversed(history or [])
                    if m.get("role") == "user" and m.get("file_uri")
                ),
                None,
            )
        if isinstance(file_uri, (str, Path)):
            uris = [str(file_uri)]
        elif isinstance(file_uri, (list, tuple)):
            uris = file_uri
        else:
            uris = None
        if not uris or len(uris) != 1:
            raise ValueError("ASRTarget requires exactly one local audio file in file_uri")
        uri = urlparse(str(uris[0]))
        if uri.scheme not in ("", "file") or uri.netloc not in ("", "localhost"):
            raise ValueError("ASRTarget accepts local paths or file:// URIs only")
        path = Path(unquote(uri.path) if uri.scheme else str(uris[0]))
        options = {**self.options, **(params or {})}
        allowed = {
            "language",
            "prompt",
            "temperature",
            "response_format",
            "timestamp_granularities[]",
        }
        if set(options) - allowed:
            raise ValueError(f"Unsupported transcription options: {sorted(set(options) - allowed)}")
        if options.get("response_format", "json") not in ("json", "verbose_json", "diarized_json"):
            raise ValueError("ASRTarget requires a JSON response format")
        data = {"model": self.model, "response_format": "json", **options}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if context:
            headers.update(context.trace_headers)
        client = self._client if self._client is not None else make_client(self.timeout)
        try:
            with path.open("rb") as audio:
                response = await client.post(
                    self.url,
                    data=data,
                    headers=headers,
                    files={
                        "file": (
                            # Neutral name: the local filename stays local.
                            "audio" + path.suffix.lower(),
                            audio,
                            mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                        )
                    },
                    timeout=self.timeout,
                )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
                raise ValueError("Transcription response must contain a string 'text' field")
            return TargetResponse(content=payload["text"], raw=with_endpoint(payload, self.url))
        finally:
            if self._client is None:
                await client.aclose()
