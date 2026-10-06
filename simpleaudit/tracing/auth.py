"""
OTLP receiver authentication — the "WHO is allowed to submit" layer.

This module is deliberately **framework-agnostic**: it has no Django (or any
web-framework) dependency so both the standalone ``simpleaudit`` library and
the SimpleAuditStudio app can use the same auth. It answers one question —
which target is allowed to push spans — and maps the credential to a stable
``target_id`` that the receiver tags onto every ingested span.

Two auth modes (plus ``none``):

    - ``none``   — no check. Local / ephemeral receiver on a trusted network.
    - ``basic``  — ``Authorization: Basic base64(username:password)``.
                   Best for OpenWebUI, which natively supports
                   ``OTEL_BASIC_AUTH_USERNAME`` / ``OTEL_BASIC_AUTH_PASSWORD``.
    - ``bearer`` — ``Authorization: Bearer <token>``. Best for generic OTel
                   exporters that can set arbitrary headers
                   (``OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <t>``).

Credentials are revocable and rotatable. Secrets are stored as salted
SHA-256 hashes and compared in constant time (``hmac.compare_digest``) to
avoid timing side-channels.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol, Tuple

# Salt length (bytes) prepended to each secret before hashing.
_SALT_LEN = 16
# Attribute key the receiver stamps onto every authenticated span.
TARGET_ID_ATTR = "simpleaudit.target_id"

SALT_LEN = _SALT_LEN
BEARER_TOKEN_PREFIX = "sa_otlp_"
TOKEN_LOOKUP_PREFIX_LEN = 16


def new_salt() -> bytes:
    return _new_salt()


def hash_secret(secret: str, salt: bytes) -> bytes:
    return _hash_secret(secret, salt)


def verify_secret(secret: str, salt: bytes, expected_hash: bytes) -> bool:
    if not salt or not expected_hash:
        return False
    return hmac.compare_digest(expected_hash, _hash_secret(secret, salt))


def generate_password() -> str:
    return secrets.token_urlsafe(32)


def generate_token() -> str:
    return BEARER_TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_lookup_prefix(token: str) -> str:
    return (token or "")[:TOKEN_LOOKUP_PREFIX_LEN]


def parse_basic_header(authorization: Optional[str]) -> Optional[Tuple[str, str]]:
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in decoded:
        return None
    return tuple(decoded.split(":", 1))


def parse_bearer_header(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


class Authenticator(Protocol):
    def __call__(self, authorization: Optional[str]) -> "AuthResult": ...


def make_basic_bearer_authenticator(
    verify: Callable[[Optional[str]], Optional[str]],
) -> Authenticator:
    def _authenticate(authorization: Optional[str]) -> AuthResult:
        identity = verify(authorization)
        return AuthResult(ok=identity is not None, target_id=identity)

    return _authenticate


def new_salt() -> bytes:
    """Return a fresh salt for a persisted credential."""
    return _new_salt()


def hash_secret(secret: str, salt: bytes) -> bytes:
    """Hash a credential secret using the shared salted primitive."""
    return _hash_secret(secret, salt)


def verify_secret(secret: str, salt: bytes, secret_hash: bytes) -> bool:
    """Verify a credential secret in constant time."""
    return hmac.compare_digest(secret_hash, _hash_secret(secret, salt))


def generate_password() -> str:
    """Generate a URL-safe password suitable for Basic authentication."""
    return secrets.token_urlsafe(24)


def generate_token() -> str:
    """Generate a URL-safe bearer token."""
    return "sa_otlp_" + secrets.token_urlsafe(32)


def token_lookup_prefix(token: str) -> str:
    """Return a non-secret stable prefix for indexed bearer lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def parse_basic_header(header: str | None) -> tuple[str, str] | None:
    """Decode an HTTP Basic authorization header, preserving colons in secrets."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def parse_bearer_header(header: str | None) -> str | None:
    """Extract a non-empty bearer token from an HTTP authorization header."""
    if not header or not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    return token or None


def _hash_secret(secret: str, salt: bytes) -> bytes:
    """Salted SHA-256 of a secret. Deterministic for a given salt."""
    return hashlib.sha256(salt + secret.encode("utf-8")).digest()


def _new_salt() -> bytes:
    return os.urandom(_SALT_LEN)


@dataclass
class _Credential:
    """A stored credential: salt + hash of the secret, bound to a target_id."""

    target_id: str
    salt: bytes
    secret_hash: bytes
    revoked: bool = False

    def matches(self, secret: str) -> bool:
        """Constant-time check of a presented secret against the stored hash."""
        if self.revoked:
            return False
        return hmac.compare_digest(self.secret_hash, _hash_secret(secret, self.salt))


@dataclass
class AuthResult:
    """Outcome of an :meth:`OTLPAuth.verify` call.

    ``ok`` is True when the request may proceed. ``target_id`` is set (and
    non-empty) when a credential was matched; it is ``None`` in ``none`` mode
    and on failure. ``reason`` is a short, log-safe explanation on failure.
    """

    ok: bool
    target_id: Optional[str] = None
    reason: Optional[str] = None

    @property
    def authenticated(self) -> bool:
        return self.ok

    @property
    def identity(self) -> Optional[str]:
        return self.target_id


class OTLPAuth:
    """Credential store + verifier for an OTLP receiver.

    Construct with a mode, add credentials, then hand the instance to an
    :class:`~simpleaudit.tracing.otlp.OTLPTraceReceiver` or
    :class:`~simpleaudit.tracing.otlp.EphemeralOTLPReceiver`. The receiver
    calls :meth:`verify` on each request's ``Authorization`` header.

    Example::

        auth = OTLPAuth(mode="basic")
        auth.add_basic("sa_owui_17", "p4ss", target_id="owui_17")
        result = auth.verify("Basic " + b64("sa_owui_17:p4ss"))
        assert result.ok and result.target_id == "owui_17"
    """

    def __init__(self, mode: str = "none") -> None:
        mode = (mode or "none").strip().lower()
        if mode not in ("none", "basic", "bearer"):
            raise ValueError(f"Unknown OTLPAuth mode: {mode!r} (expected 'none', 'basic', or 'bearer')")
        self.mode = mode
        # username -> credential (basic)
        self._basic: Dict[str, _Credential] = {}
        # token-hash-keyed credential (bearer). We index by a *lookup* hash of
        # the token (salted per-credential) so we can find the record to
        # compare against without storing the raw token.
        self._bearer: Dict[str, _Credential] = {}

    # ── Credential management ────────────────────────────────────────────

    def add_basic(self, username: str, password: str, target_id: str) -> None:
        """Register (or replace) a Basic-Auth credential for ``target_id``."""
        if not username:
            raise ValueError("username is required")
        salt = _new_salt()
        self._basic[username] = _Credential(
            target_id=target_id, salt=salt, secret_hash=_hash_secret(password, salt)
        )

    def add_bearer(self, token: str, target_id: str) -> None:
        """Register (or replace) a Bearer credential for ``target_id``."""
        if not token:
            raise ValueError("token is required")
        salt = _new_salt()
        cred = _Credential(target_id=target_id, salt=salt, secret_hash=_hash_secret(token, salt))
        # Index by the salted hash so lookup needs only the presented token.
        self._bearer[_hash_secret(token, salt).hex()] = cred

    def revoke(self, username_or_token: str) -> bool:
        """Revoke a credential by username (basic) or token (bearer).

        Returns True if a matching credential was found and revoked.
        """
        if username_or_token in self._basic:
            self._basic[username_or_token].revoked = True
            return True
        # For bearer we must find the record by hashing the token with each
        # stored salt; the index key is that hash, so we can't reverse it.
        # Instead we scan (credential sets are small — one per target).
        for cred in self._bearer.values():
            if cred.matches(username_or_token):
                cred.revoked = True
                return True
        return False

    def rotate_basic(self, username: str, new_password: str) -> None:
        """Re-issue the password for an existing Basic credential (same target)."""
        cred = self._basic.get(username)
        if cred is None:
            raise KeyError(f"No Basic credential for username {username!r}")
        salt = _new_salt()
        cred.salt = salt
        cred.secret_hash = _hash_secret(new_password, salt)
        cred.revoked = False

    def rotate_bearer(self, old_token: str, new_token: str) -> None:
        """Replace a Bearer token, keeping the same target_id."""
        old_cred = self._find_bearer(old_token)
        if old_cred is None:
            raise KeyError("No Bearer credential for the given token")
        # Remove the old index entry and register the new token under the
        # same target_id.
        for key, cred in list(self._bearer.items()):
            if cred is old_cred:
                del self._bearer[key]
                break
        self.add_bearer(new_token, old_cred.target_id)

    def _find_bearer(self, token: str) -> Optional[_Credential]:
        for cred in self._bearer.values():
            if cred.matches(token):
                return cred
        return None

    # ── Verification ─────────────────────────────────────────────────────

    def verify(self, authorization_header: Optional[str]) -> AuthResult:
        """Verify an ``Authorization`` header and return the target id.

        In ``none`` mode this always succeeds (``target_id`` is ``None``).
        """
        if self.mode == "none":
            return AuthResult(ok=True)

        header = (authorization_header or "").strip()
        if not header:
            return AuthResult(ok=False, reason="missing_authorization_header")

        parts = header.split(" ", 1)
        if len(parts) != 2 or not parts[1].strip():
            return AuthResult(ok=False, reason="malformed_authorization_header")
        scheme, value = parts[0].strip().lower(), parts[1].strip()

        # The scheme must match the configured mode.
        if self.mode == "basic":
            if scheme != "basic":
                return AuthResult(ok=False, reason="expected_basic_scheme")
            return self._verify_basic(value)
        # bearer
        if scheme != "bearer":
            return AuthResult(ok=False, reason="expected_bearer_scheme")
        return self._verify_bearer(value)

    def _verify_basic(self, value: str) -> AuthResult:
        # value should be base64("username:password")
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return AuthResult(ok=False, reason="invalid_basic_encoding")
        if ":" not in decoded:
            return AuthResult(ok=False, reason="invalid_basic_credentials")
        username, password = decoded.split(":", 1)
        cred = self._basic.get(username)
        if cred is None:
            return AuthResult(ok=False, reason="unknown_username")
        if not cred.matches(password):
            return AuthResult(ok=False, reason="invalid_credentials")
        return AuthResult(ok=True, target_id=cred.target_id)

    def _verify_bearer(self, token: str) -> AuthResult:
        if not token:
            return AuthResult(ok=False, reason="empty_bearer_token")
        cred = self._find_bearer(token)
        if cred is None:
            return AuthResult(ok=False, reason="unknown_token")
        return AuthResult(ok=True, target_id=cred.target_id)


def tag_spans(spans, target_id: Optional[str]) -> None:
    """Stamp ``simpleaudit.target_id`` onto each span's attributes in place.

    No-op when ``target_id`` is falsy (e.g. ``none`` mode). Mutates the raw
    span dicts before they are normalized/stored, so the tag flows through
    :func:`simpleaudit.tracing.otlp.parse_otlp_json` and is queryable via
    ``SpanStore.by_attribute``.
    """
    if not target_id:
        return
    for span in spans:
        attrs = span.get("attributes")
        if attrs is None:
            span["attributes"] = attrs = {}
        attrs.setdefault(TARGET_ID_ATTR, target_id)
