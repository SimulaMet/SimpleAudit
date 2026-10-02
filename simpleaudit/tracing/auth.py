"""
Credential primitive for authenticating OTLP trace pushers.

The OTLP receivers in :mod:`simpleaudit.tracing.otlp` accept spans over
``POST /v1/traces``. When a target is external (a separate process or service
pushing its own traces), you usually want to gate that endpoint with a secret
so only the target you issued a credential to can write. This module is that
secret-handling primitive: it generates secrets, stores only a salted hash of
them, and verifies a presented secret in constant time.

It is deliberately **pure** — no database, no framework. A credential here is
just a ``(salt, secret_hash)`` pair plus an optional lookup prefix. Persisting
those pairs, looking them up by username/target, and deciding which credential
applies to a request are the *caller's* concerns (a web app stores them in a
table; a script can hold them in a dict). The :class:`Authenticator` protocol
at the bottom is the seam the OTLP receivers accept, so any such lookup can be
plugged in.

Security notes:
    - Only the salted SHA-256 hash is ever stored; the plaintext secret is
      returned once at creation and never recoverable.
    - Verification uses :func:`hmac.compare_digest`, so timing does not leak
      how much of the secret matched.
    - The salt is per-credential, so two credentials with the same secret hash
      to different values and a leaked hash of one is not reusable for the
      other.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Tuple

#: Bytes of random salt per credential.
SALT_LEN = 16

#: Prefix on bearer tokens so they are recognisable in logs/env files.
BEARER_TOKEN_PREFIX = "sa_otlp_"

#: Length of the non-secret lookup prefix stored on a bearer credential.
TOKEN_LOOKUP_PREFIX_LEN = 16


# ---------------------------------------------------------------------------
# Secret hashing / verification (the core of the primitive)
# ---------------------------------------------------------------------------

def new_salt() -> bytes:
    """A fresh per-credential random salt."""
    return os.urandom(SALT_LEN)


def hash_secret(secret: str, salt: bytes) -> bytes:
    """Salted SHA-256 of *secret*. This is the only form that gets stored."""
    return hashlib.sha256(salt + secret.encode("utf-8")).digest()


def verify_secret(secret: str, salt: bytes, expected_hash: bytes) -> bool:
    """Constant-time check that *secret* hashes to *expected_hash* under *salt*."""
    if not salt or not expected_hash:
        return False
    return hmac.compare_digest(expected_hash, hash_secret(secret, salt))


# ---------------------------------------------------------------------------
# Secret generation
# ---------------------------------------------------------------------------

def generate_password() -> str:
    """A strong, URL-safe password for a Basic-Auth credential."""
    return secrets.token_urlsafe(32)


def generate_token() -> str:
    """A strong bearer token, prefixed for easy recognition in logs."""
    return BEARER_TOKEN_PREFIX + secrets.token_urlsafe(32)


def token_lookup_prefix(token: str) -> str:
    """A short, non-secret prefix of a bearer token for indexed lookup.

    Stored alongside the credential so a verifier can narrow the candidate set
    before hashing. Deliberately short and never used for authentication —
    only the salted hash is.
    """
    return (token or "")[:TOKEN_LOOKUP_PREFIX_LEN]


# ---------------------------------------------------------------------------
# Authorization-header parsing
# ---------------------------------------------------------------------------

def parse_basic_header(authorization: Optional[str]) -> Optional[Tuple[str, str]]:
    """Parse an ``Authorization: Basic ...`` header into ``(username, password)``.

    Returns ``None`` when the header is missing, not Basic, or not valid base64.
    """
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].strip().lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(parts[1].strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None
    if ":" not in decoded:
        return None
    username, password = decoded.split(":", 1)
    return username, password


def parse_bearer_header(authorization: Optional[str]) -> Optional[str]:
    """Parse an ``Authorization: ****** header into the token, else ``None``."""
    if not authorization:
        return None
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].strip().lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


# ---------------------------------------------------------------------------
# The seam the OTLP receivers accept
# ---------------------------------------------------------------------------

@dataclass
class AuthResult:
    """Outcome of authenticating one request.

    ``authenticated`` is whether the request may proceed. ``identity`` is an
    opaque value the caller associates with the credential (e.g. a target id)
    so it can tag the ingested spans; ``None`` when unauthenticated.
    """

    authenticated: bool
    identity: Optional[str] = None


class Authenticator(Protocol):
    """Anything that can decide whether an OTLP request is allowed in.

    The OTLP receivers call this with the raw ``Authorization`` header (which
    may be ``None``). Return an :class:`AuthResult`; when ``authenticated`` is
    false the receiver rejects the export with a 401.
    """

    def __call__(self, authorization: Optional[str]) -> AuthResult: ...


def make_basic_bearer_authenticator(
    verify: Callable[[Optional[str]], Optional[str]]
) -> Authenticator:
    """Build an :class:`Authenticator` from a header->identity lookup.

    *verify* receives the raw ``Authorization`` header and returns the
    credential's identity (e.g. target id) when it is valid and enabled, else
    ``None``. This keeps the DB/framework-specific lookup in the caller while
    the header parsing and the allow/deny decision live here.
    """

    def _authenticate(authorization: Optional[str]) -> AuthResult:
        identity = verify(authorization)
        if identity is None:
            return AuthResult(authenticated=False)
        return AuthResult(authenticated=True, identity=identity)

    return _authenticate
