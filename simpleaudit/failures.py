"""Which role failed a scenario, and how (structured, best effort).

A failed scenario keeps its free-text ``judgment.error`` and
``judgment.error_phase`` and gains ``judgment.failure``::

    {"role": "target|auditor|judge|system",          # required
     "kind": "rate_limit|unavailable|auth|timeout|bad_response|config|unknown",
     "message": "<same text as judgment.error>",
     "http_status": 429, "exception": "RateLimitError",
     "endpoint": "...", "model": "...",
     "retryable": true, "turn": 3}

Only ``role`` (and ``kind``, ``message``, ``exception``) are always present;
the other keys are left out when unknown. The role is set at the call site,
so no target cooperation is needed and nothing here assumes a model, a
provider or a target type. ``kind`` comes from the HTTP status the exception
exposes, or its type; anything else is ``unknown``. ``retryable`` is a hint
for consumers and does not describe the engine's own retry behaviour.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, Optional

from .utils import describe_exception, redact_url_secrets

ROLES = ("target", "auditor", "judge", "system")
KINDS = ("rate_limit", "unavailable", "auth", "timeout", "bad_response", "config", "unknown")
_RETRYABLE = {"rate_limit", "unavailable", "timeout", "bad_response"}
_MAX_CHAIN = 6


def _chain(exc: BaseException):
    seen = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen and len(seen) < _MAX_CHAIN:
        seen.add(id(cur))
        yield cur
        cur = cur.__cause__ or cur.__context__


def _as_status(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    return None


def http_status_of(exc: BaseException) -> Optional[int]:
    """HTTP status the exception (or its cause chain) exposes, else None."""
    for item in _chain(exc):
        for attr in ("status_code", "status", "http_status", "code"):
            status = _as_status(getattr(item, attr, None))
            if status is not None:
                return status
        response = getattr(item, "response", None)
        if response is not None:
            for attr in ("status_code", "status"):
                status = _as_status(getattr(response, attr, None))
                if status is not None:
                    return status
    return None


def _kind_from_type(exc: BaseException) -> str:
    # Local import: provider_errors is independent of this module.
    from .provider_errors import ProviderResponseError

    for item in _chain(exc):
        if isinstance(item, (asyncio.TimeoutError, TimeoutError)):
            return "timeout"
        if isinstance(item, ConnectionError):
            return "unavailable"
        if isinstance(item, ProviderResponseError):
            return "bad_response"
        name = type(item).__name__.lower()
        if "ratelimit" in name:
            return "rate_limit"
        if "timeout" in name:
            return "timeout"
        if "authentication" in name or "permissiondenied" in name:
            return "auth"
        if any(
            w in name
            for w in ("connect", "unavailable", "overloaded", "internalserver", "badgateway")
        ):
            return "unavailable"
        if name in ("jsondecodeerror", "validationerror"):
            return "bad_response"
    return "unknown"


def classify_kind(exc: BaseException, http_status: Optional[int]) -> str:
    """Provider-agnostic cause from the HTTP status, else the exception type."""
    if http_status is not None:
        if http_status == 429:
            return "rate_limit"
        if http_status in (401, 403):
            return "auth"
        if http_status in (408, 504):
            return "timeout"
        if http_status >= 500:
            return "unavailable"
        if 400 <= http_status < 500:
            return "config"
    return _kind_from_type(exc)


def _clean_endpoint(url: Any) -> Optional[str]:
    if not url:
        return None
    text = str(url)
    # Credentials riding in the URL are never kept (not even redacted).
    text = re.sub(r"(\w+://)[^/?#@\s]*@", r"\1", text)
    return redact_url_secrets(text)


def _endpoint_of(exc: BaseException) -> Optional[str]:
    for item in _chain(exc):
        for holder in (getattr(item, "request", None), getattr(item, "response", None)):
            url = getattr(holder, "url", None) if holder is not None else None
            cleaned = _clean_endpoint(url)
            if cleaned:
                return cleaned
    return None


def build_failure(
    role: str,
    exc: BaseException,
    *,
    turn: Optional[int] = None,
    endpoint: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """The ``failure`` object for ``exc`` raised by ``role``.

    ``endpoint`` and ``model`` are hints from the call site, used when the
    exception itself does not name an endpoint. ``turn`` is 1-based.
    """
    role = role if role in ROLES else "system"
    status = http_status_of(exc)
    kind = classify_kind(exc, status)
    failure: Dict[str, Any] = {
        "role": role,
        "kind": kind,
        "message": describe_exception(exc),
        "exception": type(exc).__name__,
    }
    if status is not None:
        failure["http_status"] = status
    where = _endpoint_of(exc) or _clean_endpoint(endpoint)
    if where:
        failure["endpoint"] = where
    if model:
        failure["model"] = str(model)
    declared = getattr(exc, "retryable", None)
    if isinstance(declared, bool):
        failure["retryable"] = declared
    elif kind != "unknown":
        failure["retryable"] = kind in _RETRYABLE
    if turn is not None:
        failure["turn"] = turn
    return failure
