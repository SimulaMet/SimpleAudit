"""Endpoint provenance in target responses, the cleartext warning, neutral ASR filenames."""

import warnings

import httpx
import pytest

from simpleaudit import ASRTarget, DecisionTarget, HTTPAppTarget
from simpleaudit.targets._http import describe_endpoint

from .test_decision_target import _context, _endpoint, _send

URL = "https://user:secret@api.example.com:8443/v1/chat?key=abc123"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@pytest.fixture
def proxy_env(monkeypatch):
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    # An environment proxy for another scheme keeps the system settings out of it.
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.test:3128")
    return monkeypatch


@pytest.mark.parametrize(
    "https_proxy,no_proxy,expected",
    [(None, None, False), ("http://proxy.test:3128", None, True),
     ("http://proxy.test:3128", "api.example.com", False)],
)
def test_describe_endpoint_and_via_proxy(proxy_env, https_proxy, no_proxy, expected):
    if https_proxy:
        proxy_env.setenv("HTTPS_PROXY", https_proxy)
    if no_proxy:
        proxy_env.setenv("NO_PROXY", no_proxy)
    assert describe_endpoint(URL) == {
        "scheme": "https", "host": "api.example.com", "port": 8443, "via_proxy": expected
    }


def _http_target(payload):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, **payload)))
    return HTTPAppTarget(URL, response_path="answer", client=client)


@pytest.mark.asyncio
async def test_endpoint_block_is_merged_and_carries_no_path_query_or_key(proxy_env):
    raw = (await _http_target({"json": {"answer": "ok", "id": 7}}).send(user="q")).raw
    assert raw["answer"] == "ok" and raw["id"] == 7
    assert raw["endpoint"]["host"] == "api.example.com"
    assert not any(s in repr(raw["endpoint"]) for s in ("secret", "user", "/v1", "key", "abc123"))


@pytest.mark.asyncio
async def test_non_dict_or_conflicting_raw_is_wrapped_not_overwritten(proxy_env):
    raw = (await _http_target({"text": "plain"}).send(user="q")).raw
    assert raw["response"] == "plain" and raw["endpoint"]["scheme"] == "https"
    raw = (await _http_target({"json": {"answer": "ok", "endpoint": "provider"}}).send(user="q")).raw
    assert raw["response"]["endpoint"] == "provider" and raw["endpoint"]["port"] == 8443


def test_decision_response_carries_the_endpoint(proxy_env):
    client, _ = _endpoint()
    raw = _send(DecisionTarget(URL, "clef", client=client), context=_context()).raw
    assert raw["answers"]["verdict"]["choice"] == "yes" and raw["endpoint"]["port"] == 8443


def test_plain_http_to_a_remote_host_warns():
    with pytest.warns(UserWarning, match="http://example.com uses plain http"):
        HTTPAppTarget("http://example.com/chat")


@pytest.mark.parametrize(
    "url", ["http://localhost:11434", "http://127.0.0.5/v1", "http://[::1]:8000", "https://example.com"]
)
def test_loopback_and_https_do_not_warn(url):
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        DecisionTarget(url, "clef")
        ASRTarget("m", base_url=url)


@pytest.mark.asyncio
async def test_asr_upload_filename_is_neutral(tmp_path):
    audio = tmp_path / "Interview Kari Nordmann.WAV"
    audio.write_bytes(b"RIFF-audio")
    seen = []

    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json={"text": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await ASRTarget("m", client=client).send(user="", file_uri=str(audio))
    assert b'filename="audio.wav"' in seen[0]
    assert b"Kari" not in seen[0] and b"Interview" not in seen[0]
