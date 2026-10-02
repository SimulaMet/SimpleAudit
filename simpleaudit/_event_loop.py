"""Running SimpleAudit's coroutines from synchronous code."""

import asyncio
from typing import Any, Coroutine, Dict


def _is_stale_client_close(context: Dict[str, Any]) -> bool:
    """Is this an HTTP client from an earlier run, closed on a loop that is gone?

    Each sync ``run()`` gets its own event loop. The OpenAI SDK's async client sits
    in a reference cycle, so one from an earlier run can be garbage-collected while
    a later run's loop is running. Its ``__del__`` then schedules ``aclose()`` on
    that loop, for connections that were opened on the earlier, closed one, and the
    task fails with "Event loop is closed". Nothing is lost: those connections ended
    with their loop. But asyncio prints the traceback as "Task exception was never
    retrieved", which reads like a failed audit.

    any-llm has no public way to close a provider's client, so the client cannot be
    closed before its loop ends; until it does, the message is dropped here.
    """
    exception = context.get("exception")
    if not isinstance(exception, RuntimeError) or str(exception) != "Event loop is closed":
        return False
    task = context.get("future") or context.get("task")
    get_coro = getattr(task, "get_coro", None)
    coro = get_coro() if get_coro is not None else None
    return getattr(coro, "__qualname__", "").endswith("AsyncClient.aclose")


def _exception_handler(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
    if _is_stale_client_close(context):
        return
    loop.default_exception_handler(context)


def run_sync(coro: Coroutine[Any, Any, Any]) -> Any:
    """``asyncio.run(coro)``, minus the stale-client message described above."""
    with asyncio.Runner() as runner:
        runner.get_loop().set_exception_handler(_exception_handler)
        return runner.run(coro)
