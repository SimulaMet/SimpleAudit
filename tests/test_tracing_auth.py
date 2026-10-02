"""
Tests for the OTLP credential primitive (:mod:`simpleaudit.tracing.auth`) and
the optional auth gate on the OTLP receivers.

Nothing here needs a model or a network server: the primitive is pure, and the
receiver gate is exercised through ``OTLPTraceReceiver.handle`` (the async
method) rather than the threaded ``EphemeralOTLPReceiver``.
"""

import base64

import pytest

from simpleaudit.tracing.auth import (
    AuthResult,
    hash_secret,
    make_basic_bearer_authenticator,
    new_salt,
    parse_basic_header,
    parse_bearer_header,
    token_lookup_prefix,
    verify_secret,
)
from simpleaudit.tracing.otlp import OTLPTraceReceiver
from simpleaudit.tracing.store import SpanStore


# ---------------------------------------------------------------------------
# hash_secret / verify_secret
# ---------------------------------------------------------------------------

def test_hash_is_deterministic_for_same_salt():
    salt = new_salt()
    assert hash_secret("hunter2", salt) == hash_secret("hunter2", salt)


def test_hash_differs_across_salts():
    assert hash_secret("hunter2", new_salt()) != hash_secret("hunter2", new_salt())


def test_verify_secret_accepts_correct_secret():
    salt = new_salt()
    digest = hash_secret("hunter2", salt)
    assert verify_secret("hunter2", salt, digest) is True


def test_verify_secret_rejects_wrong_secret():
    salt = new_salt()
    digest = hash_secret("hunter2", salt)
    assert verify_secret("hunter3", salt, digest) is False


def test_verify_secret_rejects_missing_salt_or_hash():
    salt = new_salt()
    digest = hash_secret("hunter2", salt)
    assert verify_secret("hunter2", b"", digest) is False
    assert verify_secret("hunter2", salt, b"") is False


def test_salt_is_random_and_16_bytes():
    assert len(new_salt()) == 16
    assert new_salt() != new_salt()


# ---------------------------------------------------------------------------
# Secret generation
# ---------------------------------------------------------------------------

def test_generate_password_is_url_safe_and_unique():
    from simpleaudit.tracing.auth import generate_password

    p1, p2 = generate_password(), generate_password()
    assert p1 != p2
    assert all(c.isalnum() or c in "-_" for c in p1)


def test_generate_token_is_prefixed_and_unique():
    from simpleaudit.tracing.auth import BEARER_TOKEN_PREFIX, generate_token

    t1, t2 = generate_token(), generate_token()
    assert t1.startswith(BEARER_TOKEN_PREFIX)
    assert t2.startswith(BEARER_TOKEN_PREFIX)
    assert t1 != t2


def test_token_lookup_prefix_is_short_and_stable():
    from simpleaudit.tracing.auth import generate_token

    token = generate_token()
    prefix = token_lookup_prefix(token)
    assert prefix == token[:16]
    assert token_lookup_prefix("") == ""
    assert token_lookup_prefix(None) == ""


# ---------------------------------------------------------------------------
# Authorization-header parsing
# ---------------------------------------------------------------------------

def _basic_header(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def test_parse_basic_header_roundtrip():
    assert parse_basic_header(_basic_header("sa_t1", "p@ss/word")) == ("sa_t1", "p@ss/word")


def test_parse_basic_header_rejects_missing_and_malformed():
    assert parse_basic_header(None) is None
    assert parse_basic_header("") is None
    assert parse_basic_header("Bearer abc") is None
    assert parse_basic_header("Basic not-base64!!!") is None
    # Valid base64 but no colon separator.
    assert parse_basic_header("Basic " + base64.b64encode(b"no-colon").decode()) is None


def test_parse_basic_header_password_may_contain_colon():
    assert parse_basic_header(_basic_header("u", "a:b:c")) == ("u", "a:b:c")


def test_parse_bearer_header_roundtrip():
    assert parse_bearer_header("Bearer sa_otlp_abc123") == "sa_otlp_abc123"


def test_parse_bearer_header_rejects_missing_and_malformed():
    assert parse_bearer_header(None) is None
    assert parse_bearer_header("Basic abc") is None
    assert parse_bearer_header("Bearer") is None
    assert parse_bearer_header("Bearer   ") is None


# ---------------------------------------------------------------------------
# make_basic_bearer_authenticator
# ---------------------------------------------------------------------------

def test_authenticator_allows_when_lookup_succeeds():
    auth = make_basic_bearer_authenticator(lambda header: "target_1" if header == "Bearer tok" else None)
    assert auth("Bearer tok") == AuthResult(authenticated=True, identity="target_1")


def test_authenticator_denies_when_lookup_fails():
    auth = make_basic_bearer_authenticator(lambda header: None)
    result = auth("Bearer tok")
    assert result.authenticated is False
    assert result.identity is None


# ---------------------------------------------------------------------------
# Receiver auth gate (OTLPTraceReceiver.handle)
# ---------------------------------------------------------------------------

def _body() -> bytes:
    # Minimal OTLP/HTTP JSON export with one span.
    import json

    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [
                        {
                            "scope": {},
                            "spans": [
                                {
                                    "traceId": "aa" * 16,
                                    "spanId": "bb" * 8,
                                    "name": "op",
                                    "kind": 2,
                                    "startTimeUnixNano": "1",
                                    "endTimeUnixNano": "2",
                                    "attributes": [],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    ).encode()


def test_receiver_open_by_default_stores_spans():
    rx = OTLPTraceReceiver(store=SpanStore())
    ack = _run(rx.handle(_body()))
    assert ack == {"partialSuccess": {"rejectedSpans": 0}}
    assert len(rx.store) == 1


def test_receiver_with_authenticator_rejects_bad_credentials():
    auth = make_basic_bearer_authenticator(lambda header: None)
    rx = OTLPTraceReceiver(store=SpanStore(), authenticator=auth)
    ack = _run(rx.handle(_body(), authorization="Bearer wrong"))
    assert ack.get("status") == 401
    assert len(rx.store) == 0  # nothing stored on rejection


def test_receiver_with_authenticator_allows_good_credentials():
    auth = make_basic_bearer_authenticator(lambda header: "target_1" if header == "Bearer ok" else None)
    rx = OTLPTraceReceiver(store=SpanStore(), authenticator=auth)
    ack = _run(rx.handle(_body(), authorization="Bearer ok"))
    assert ack == {"partialSuccess": {"rejectedSpans": 0}}
    assert len(rx.store) == 1


def _run(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
