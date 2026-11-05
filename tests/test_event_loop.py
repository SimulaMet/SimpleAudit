"""run_sync: one event loop per sync run, without the stale-client close message."""

import asyncio
import gc
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from simpleaudit._event_loop import _exception_handler, _is_stale_client_close, run_sync

NEVER_RETRIEVED = "Task exception was never retrieved"


class AsyncClient:
    """Stands in for httpx's client: only the coroutine's qualified name is checked."""

    async def aclose(self):
        raise RuntimeError("Event loop is closed")


class Other:
    async def aclose(self):
        raise RuntimeError("Event loop is closed")


def _context(coro, exception):
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(coro)
        loop.run_until_complete(asyncio.wait([task]))
        return {"message": NEVER_RETRIEVED, "exception": exception, "future": task}
    finally:
        loop.close()


class TestFilter:
    def test_a_client_close_on_a_closed_loop_is_dropped(self):
        ctx = _context(AsyncClient().aclose(), RuntimeError("Event loop is closed"))
        assert _is_stale_client_close(ctx)

    def test_another_coroutine_failing_the_same_way_is_kept(self):
        ctx = _context(Other().aclose(), RuntimeError("Event loop is closed"))
        assert not _is_stale_client_close(ctx)

    def test_a_client_close_failing_differently_is_kept(self):
        ctx = _context(AsyncClient().aclose(), RuntimeError("connection reset"))
        assert not _is_stale_client_close(ctx)

    def test_kept_contexts_reach_the_default_handler(self):
        seen = []

        class Loop:
            def default_exception_handler(self, context):
                seen.append(context)

        kept = {"message": "something else", "exception": ValueError("x")}
        _exception_handler(Loop(), kept)
        _exception_handler(Loop(), _context(AsyncClient().aclose(), RuntimeError("Event loop is closed")))
        assert seen == [kept]


def test_run_sync_returns_the_result_and_raises_the_error():
    async def ok():
        return 42

    async def bad():
        raise ValueError("boom")

    assert run_sync(ok()) == 42
    with pytest.raises(ValueError, match="boom"):
        run_sync(bad())


class _Completions(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive, so the connection stays pooled

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps({
            "id": "x", "object": "chat.completion", "created": 0, "model": "m",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def base_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Completions)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    server.shutdown()
    server.server_close()


def _two_runs(runner, base_url):
    """A client used in one run and garbage-collected during the next."""
    openai = pytest.importorskip("openai")
    holder = {}

    async def first():
        client = openai.AsyncOpenAI(base_url=base_url, api_key="x", max_retries=0)
        await client.chat.completions.create(model="m", messages=[{"role": "user", "content": "hi"}])
        holder["client"] = client

    async def second():
        holder.clear()
        gc.collect()  # the client sits in a reference cycle; its __del__ runs here
        await asyncio.sleep(0.05)  # the aclose() task it scheduled runs and fails
        gc.collect()

    runner(first())
    runner(second())


def test_a_client_from_an_earlier_run_closes_quietly(base_url, caplog):
    caplog.set_level(logging.ERROR, logger="asyncio")
    _two_runs(asyncio.run, base_url)
    if NEVER_RETRIEVED not in caplog.text:
        pytest.skip("this openai version no longer schedules aclose() from __del__")
    caplog.clear()

    _two_runs(run_sync, base_url)
    assert NEVER_RETRIEVED not in caplog.text
