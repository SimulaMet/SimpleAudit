"""
Tests for OTLP receiver authentication (simpleaudit.tracing.auth).

Covers the three modes (none / basic / bearer), credential management
(add / revoke / rotate), header parsing edge cases, and receiver integration
(401 on bad auth, target_id tagging on success).
"""

import base64

import pytest

from simpleaudit.tracing import (
    TARGET_ID_ATTR,
    EphemeralOTLPReceiver,
    OTLPAuth,
    OTLPTraceReceiver,
    tag_spans,
)


def _basic_header(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_default_mode_is_none():
    assert OTLPAuth().mode == "none"


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        OTLPAuth(mode="mtls")


def test_add_basic_requires_username():
    auth = OTLPAuth(mode="basic")
    with pytest.raises(ValueError):
        auth.add_basic("", "pw", target_id="t")


def test_add_bearer_requires_token():
    auth = OTLPAuth(mode="bearer")
    with pytest.raises(ValueError):
        auth.add_bearer("", target_id="t")


# ---------------------------------------------------------------------------
# none mode
# ---------------------------------------------------------------------------

def test_none_mode_verify_ok_without_header():
    auth = OTLPAuth(mode="none")
    result = auth.verify(None)
    assert result.ok is True
    assert result.target_id is None


def test_none_mode_verify_ok_ignores_header():
    auth = OTLPAuth(mode="none")
    result = auth.verify("Basic whatever")
    assert result.ok is True
    assert result.target_id is None


# ---------------------------------------------------------------------------
# basic mode — verify
# ---------------------------------------------------------------------------

def test_basic_verify_success_returns_target_id():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_owui_17", "p4ss", target_id="owui_17")
    result = auth.verify(_basic_header("sa_owui_17", "p4ss"))
    assert result.ok is True
    assert result.target_id == "owui_17"


def test_basic_verify_wrong_password():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_owui_17", "p4ss", target_id="owui_17")
    result = auth.verify(_basic_header("sa_owui_17", "wrong"))
    assert result.ok is False
    assert result.reason == "invalid_credentials"


def test_basic_verify_unknown_username():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_owui_17", "p4ss", target_id="owui_17")
    result = auth.verify(_basic_header("sa_other", "p4ss"))
    assert result.ok is False
    assert result.reason == "unknown_username"


def test_basic_verify_missing_header():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_owui_17", "p4ss", target_id="owui_17")
    result = auth.verify(None)
    assert result.ok is False
    assert result.reason == "missing_authorization_header"


def test_basic_verify_wrong_scheme():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_owui_17", "p4ss", target_id="owui_17")
    result = auth.verify("Bearer " + base64.b64encode(b"sa_owui_17:p4ss").decode())
    assert result.ok is False
    assert result.reason == "expected_basic_scheme"


def test_basic_verify_malformed_base64():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_owui_17", "p4ss", target_id="owui_17")
    result = auth.verify("Basic !!!not-base64!!!")
    assert result.ok is False
    assert result.reason == "invalid_basic_encoding"


def test_basic_verify_no_colon():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_owui_17", "p4ss", target_id="owui_17")
    result = auth.verify("Basic " + base64.b64encode(b"justusername").decode())
    assert result.ok is False
    assert result.reason == "invalid_basic_credentials"


def test_basic_verify_password_with_colon():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("u", "pa:ss:word", target_id="t")
    result = auth.verify(_basic_header("u", "pa:ss:word"))
    assert result.ok is True
    assert result.target_id == "t"


# ---------------------------------------------------------------------------
# basic mode — revoke / rotate
# ---------------------------------------------------------------------------

def test_basic_revoke_blocks_access():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("u", "pw", target_id="t")
    assert auth.verify(_basic_header("u", "pw")).ok is True
    assert auth.revoke("u") is True
    result = auth.verify(_basic_header("u", "pw"))
    assert result.ok is False
    assert result.reason == "invalid_credentials"


def test_basic_revoke_unknown_returns_false():
    auth = OTLPAuth(mode="basic")
    assert auth.revoke("nobody") is False


def test_basic_rotate_allows_new_password_only():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("u", "old", target_id="t")
    auth.rotate_basic("u", "new")
    assert auth.verify(_basic_header("u", "old")).ok is False
    assert auth.verify(_basic_header("u", "new")).ok is True


def test_basic_rotate_unknown_raises():
    auth = OTLPAuth(mode="basic")
    with pytest.raises(KeyError):
        auth.rotate_basic("ghost", "x")


# ---------------------------------------------------------------------------
# bearer mode — verify
# ---------------------------------------------------------------------------

def test_bearer_verify_success_returns_target_id():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("tok_abc123", target_id="agent_9")
    result = auth.verify("Bearer tok_abc123")
    assert result.ok is True
    assert result.target_id == "agent_9"


def test_bearer_verify_wrong_token():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("tok_abc123", target_id="agent_9")
    result = auth.verify("Bearer tok_wrong")
    assert result.ok is False
    assert result.reason == "unknown_token"


def test_bearer_verify_missing_header():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("tok_abc123", target_id="agent_9")
    result = auth.verify(None)
    assert result.ok is False
    assert result.reason == "missing_authorization_header"


def test_bearer_verify_wrong_scheme():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("tok_abc123", target_id="agent_9")
    result = auth.verify("Basic tok_abc123")
    assert result.ok is False
    assert result.reason == "expected_bearer_scheme"


def test_bearer_verify_empty_token():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("tok_abc123", target_id="agent_9")
    result = auth.verify("Bearer ")
    assert result.ok is False
    assert result.reason == "malformed_authorization_header"


# ---------------------------------------------------------------------------
# bearer mode — revoke / rotate
# ---------------------------------------------------------------------------

def test_bearer_revoke_blocks_access():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("tok_abc", target_id="t")
    assert auth.verify("Bearer tok_abc").ok is True
    assert auth.revoke("tok_abc") is True
    result = auth.verify("Bearer tok_abc")
    assert result.ok is False
    assert result.reason == "unknown_token"


def test_bearer_rotate_replaces_token():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("old_tok", target_id="t")
    auth.rotate_bearer("old_tok", "new_tok")
    assert auth.verify("Bearer old_tok").ok is False
    assert auth.verify("Bearer new_tok").ok is True
    assert auth.verify("Bearer new_tok").target_id == "t"


def test_bearer_rotate_unknown_raises():
    auth = OTLPAuth(mode="bearer")
    with pytest.raises(KeyError):
        auth.rotate_bearer("ghost", "new")


# ---------------------------------------------------------------------------
# tag_spans
# ---------------------------------------------------------------------------

def test_tag_spans_stamps_target_id():
    spans = [{"attributes": {"a": 1}}, {"attributes": None}, {}]
    tag_spans(spans, "owui_17")
    assert spans[0]["attributes"][TARGET_ID_ATTR] == "owui_17"
    assert spans[1]["attributes"][TARGET_ID_ATTR] == "owui_17"
    assert spans[2]["attributes"][TARGET_ID_ATTR] == "owui_17"


def test_tag_spans_noop_when_no_target():
    spans = [{"attributes": {"a": 1}}]
    tag_spans(spans, None)
    assert TARGET_ID_ATTR not in spans[0]["attributes"]


# ---------------------------------------------------------------------------
# OTLPTraceReceiver integration
# ---------------------------------------------------------------------------

def _otlp_json(trace_id: str = "a" * 32) -> str:
    import json

    return json.dumps(
        {
            "resourceSpans": [
                {
                    "resource": {"attributes": []},
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": "b" * 16,
                                    "name": "chat",
                                    "kind": 1,
                                    "startTimeUnixNano": 0,
                                    "endTimeUnixNano": 1,
                                    "attributes": [],
                                    "status": {"code": 0},
                                }
                            ]
                        }
                    ],
                }
            ]
        }
    )


@pytest.mark.asyncio
async def test_receiver_none_mode_stores_without_header():
    rx = OTLPTraceReceiver()
    ack = await rx.handle(_otlp_json())
    assert ack["authenticated"] is True
    assert len(rx.store) == 1


@pytest.mark.asyncio
async def test_receiver_basic_mode_success_tags_target():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_17", "pw", target_id="owui_17")
    rx = OTLPTraceReceiver(auth=auth)
    ack = await rx.handle(_otlp_json(), authorization=_basic_header("sa_17", "pw"))
    assert ack["authenticated"] is True
    assert len(rx.store) == 1
    span = rx.store.all()[0]
    assert span["attributes"][TARGET_ID_ATTR] == "owui_17"


@pytest.mark.asyncio
async def test_receiver_basic_mode_rejects_bad_auth():
    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_17", "pw", target_id="owui_17")
    rx = OTLPTraceReceiver(auth=auth)
    ack = await rx.handle(_otlp_json(), authorization=_basic_header("sa_17", "wrong"))
    assert ack["authenticated"] is False
    assert ack["reason"] == "invalid_credentials"
    assert len(rx.store) == 0


@pytest.mark.asyncio
async def test_receiver_bearer_mode_success():
    auth = OTLPAuth(mode="bearer")
    auth.add_bearer("tok", target_id="agent_1")
    rx = OTLPTraceReceiver(auth=auth)
    ack = await rx.handle(_otlp_json(), authorization="Bearer tok")
    assert ack["authenticated"] is True
    assert rx.store.all()[0]["attributes"][TARGET_ID_ATTR] == "agent_1"


# ---------------------------------------------------------------------------
# EphemeralOTLPReceiver integration (real HTTP)
# ---------------------------------------------------------------------------

def test_ephemeral_receiver_basic_auth_401_and_200():
    import asyncio

    import httpx

    auth = OTLPAuth(mode="basic")
    auth.add_basic("sa_17", "pw", target_id="owui_17")

    async def _run():
        rx = EphemeralOTLPReceiver(auth=auth).start()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # Bad password -> 401, nothing stored.
                r = await client.post(
                    rx.endpoint,
                    json={"resourceSpans": []},
                    headers={"Authorization": _basic_header("sa_17", "wrong")},
                )
                assert r.status_code == 401
                assert r.json()["authenticated"] is False
                assert len(rx.store) == 0

                # Good password -> 200, span stored + tagged.
                r = await client.post(
                    rx.endpoint,
                    json=_otlp_http_payload(),
                    headers={"Authorization": _basic_header("sa_17", "pw")},
                )
                assert r.status_code == 200
                assert r.json()["authenticated"] is True
                assert len(rx.store) == 1
                assert rx.store.all()[0]["attributes"][TARGET_ID_ATTR] == "owui_17"
        finally:
            rx.stop()

    asyncio.run(_run())


def _otlp_http_payload(trace_id: str = "a" * 32, span_id: str = "b" * 16) -> dict:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": trace_id,
                                "spanId": span_id,
                                "name": "POST /chat",
                                "kind": 2,
                                "startTimeUnixNano": 1_000_000_000,
                                "endTimeUnixNano": 2_000_000_000,
                                "attributes": [],
                                "status": {"code": 1},
                            }
                        ]
                    }
                ],
            }
        ]
    }
