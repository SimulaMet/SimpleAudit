"""Defensive handling of provider responses.

OpenAI-compatible gateways do not always fail with an HTTP error. OpenRouter,
for example, documents that a non-streaming request that fails after the
headers were sent comes back as ``200 OK`` with a body holding only an
``error`` object (plus the generation ``id``) and no ``choices``. Client
libraries that assume a well-formed completion then crash while converting it
-- any-llm's OpenAI-compatible path calls ``int(response.created)`` and raises
``TypeError: int() argument must be ... not 'NoneType'`` -- and the real cause
(an upstream provider failure) is lost.

The helpers here turn those situations into explicit, retryable errors with a
readable message, so an audit retries them and, if they persist, records an
ERROR result that says what actually happened instead of a bare ``TypeError``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple


class ProviderResponseError(RuntimeError):
    """The provider answered, but not with a usable completion.

    Raised for error bodies delivered with HTTP 200, responses without
    ``choices``, and crashes inside the client library while converting a
    malformed response. Always worth retrying.
    """

    retryable = True


class EmptyResponseError(ProviderResponseError):
    """The provider returned a completion with no text content.

    ``finish_reason == "length"`` means the token budget was exhausted
    (typically by hidden reasoning) and ``"content_filter"`` that a provider
    safety system stopped the reply; retrying with the same settings rarely
    changes either, so those are not retried.
    """

    def __init__(self, message: str, finish_reason: Optional[str] = None):
        super().__init__(message)
        self.finish_reason = finish_reason
        self.retryable = finish_reason not in ("length", "content_filter")


#: Exceptions that, when raised from *inside* the client library call, almost
#: always mean "the response did not have the expected shape" rather than a
#: bug in simpleaudit. They are wrapped in ProviderResponseError so the stored
#: error names the likely cause.
_CONVERSION_ERRORS = (TypeError, KeyError, AttributeError, IndexError)


def wrap_client_exception(exc: BaseException) -> BaseException:
    """Give conversion crashes from the client library a readable cause."""
    if isinstance(exc, ProviderResponseError):
        return exc
    text = str(exc)
    if isinstance(exc, TypeError) and ("keyword argument" in text or "positional argument" in text):
        return exc  # a bad call signature (e.g. an unsupported param), not a bad response
    if isinstance(exc, _CONVERSION_ERRORS) or type(exc).__name__ == "ValidationError":
        wrapped = ProviderResponseError(
            "Provider returned a response the client library could not parse "
            f"({type(exc).__name__}: {exc}). With OpenAI-compatible gateways such as "
            "OpenRouter this is usually an upstream error delivered with HTTP 200 "
            "(an 'error' object and no 'choices'). Treat as a transient infrastructure "
            "failure, not model behaviour."
        )
        wrapped.__cause__ = exc
        return wrapped
    return exc


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    value = getattr(obj, key, None)
    if value is None:
        # pydantic models keep unknown fields (e.g. OpenRouter's "error") here
        extra = getattr(obj, "model_extra", None) or {}
        value = extra.get(key) if isinstance(extra, dict) else None
    return value


def _content_to_text(content: Any) -> str:
    """Normalise message content to plain text.

    Some OpenAI-compatible servers return ``content`` as a list of parts
    (``[{"type": "thinking", ...}, {"type": "text", "text": "..."}]``) instead
    of a string; only the text parts are the answer.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            ptype = _get(part, "type")
            text = _get(part, "text")
            if ptype in (None, "text", "output_text") and isinstance(text, str):
                parts.append(text)
        return "".join(parts)
    return str(content)


def extract_completion(response: Any) -> Tuple[str, Optional[str]]:
    """Return ``(text, finish_reason)`` from an OpenAI-style completion.

    Raises ProviderResponseError when the response carries an error object or
    has no usable choice, so the caller's retry loop handles it like any
    other transient failure.
    """
    error = _get(response, "error")
    choices = _get(response, "choices")
    if error:
        message = _get(error, "message") or str(error)
        code = _get(error, "code")
        raise ProviderResponseError(
            f"Provider returned an error in a successful (HTTP 200) response"
            f"{f' [code {code}]' if code is not None else ''}: {message}"
        )
    if not choices:
        raise ProviderResponseError("Provider response contained no choices")
    choice = choices[0]
    finish_reason = _get(choice, "finish_reason")
    choice_error = _get(choice, "error")
    message = _get(choice, "message")
    if finish_reason == "error" or choice_error:
        detail = _get(choice_error, "message") if choice_error else None
        raise ProviderResponseError(
            f"Provider reported an error during generation: {detail or 'finish_reason=error'}"
        )
    if message is None:
        raise ProviderResponseError("Provider response choice has no message")
    return _content_to_text(_get(message, "content")), finish_reason


_LIST_FIELDS = ("issues_found", "positive_behaviors", "recommendations")


def coerce_judgment_fields(judgment: Any) -> Dict[str, Any]:
    """Make judge output safe for the result/report code.

    A judge may emit ``"summary": null`` or ``"issues_found": "one issue"``.
    Left alone, the first crashes the result logging with a TypeError (and
    turns a correctly judged scenario into ERROR), and the second is later
    ``extend``-ed character by character into the issue list.
    """
    if not isinstance(judgment, dict):
        return {"severity": "ERROR", "summary": f"Judge output was not an object: {judgment!r}"[:500]}
    summary = judgment.get("summary")
    if summary is None:
        judgment["summary"] = ""
    elif not isinstance(summary, str):
        judgment["summary"] = str(summary)
    for key in _LIST_FIELDS:
        value = judgment.get(key)
        if value is None:
            if key in judgment:
                judgment[key] = []
        elif isinstance(value, str):
            judgment[key] = [value] if value.strip() else []
        elif not isinstance(value, list):
            judgment[key] = [str(value)]
    return judgment
