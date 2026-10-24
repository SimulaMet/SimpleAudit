"""HTTP plumbing shared by the targets: client creation and endpoint provenance.

``describe_endpoint`` records where a request went (scheme, host, port, and
whether the standard proxy settings route it through a proxy) so an audit
record can say which endpoint answered. It never includes the path, query,
userinfo, headers or keys.
"""

from __future__ import annotations

import ipaddress
import urllib.request
import warnings
from typing import Any
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


def make_client(timeout: float) -> Any:
    """A fresh ``httpx.AsyncClient(timeout=timeout)``, as the targets always created."""
    import httpx

    return httpx.AsyncClient(timeout=timeout)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def describe_endpoint(url: str) -> dict[str, Any]:
    """``{"scheme", "host", "port", "via_proxy"}`` for ``url``.

    ``via_proxy`` follows ``urllib.request.getproxies()`` / ``proxy_bypass()``
    (the environment's proxy variables); an injected client's own proxy
    configuration is not visible here.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    proxies = urllib.request.getproxies()
    proxied = bool(proxies.get(scheme) or proxies.get("all"))
    return {
        "scheme": scheme,
        "host": host,
        "port": parts.port or _DEFAULT_PORTS.get(scheme),
        "via_proxy": proxied and not urllib.request.proxy_bypass(host),
    }


def warn_if_cleartext(url: str) -> None:
    """Warn when ``url`` is plain http to a host other than loopback."""
    parts = urlsplit(url)
    if parts.scheme.lower() == "http" and not _is_loopback(parts.hostname or ""):
        warnings.warn(
            f"target endpoint http://{parts.hostname} uses plain http: requests "
            "and responses to it are not encrypted",
            UserWarning,
            stacklevel=3,
        )


def with_endpoint(raw: Any, url: str) -> dict[str, Any]:
    """``raw`` with an ``endpoint`` block added; provider fields are never overwritten.

    A dict without an ``endpoint`` key gets one; anything else is wrapped as
    ``{"response": raw, "endpoint": ...}``.
    """
    endpoint = describe_endpoint(url)
    if isinstance(raw, dict) and "endpoint" not in raw:
        return {**raw, "endpoint": endpoint}
    return {"response": raw, "endpoint": endpoint}
