"""
OpenEnvTarget — audit an agent hosted in an OpenEnv environment.

OpenEnv (https://github.com/meta-pytorch/OpenEnv) serves agents and their tools
as environments with a ``reset`` / ``step`` API. ``HarnessEnvironment`` (OpenEnv
RFC 005) wraps a real agent harness such as Claude Code: each ``step`` sends one
user message, the agent runs its own tool loop against the environment's tools,
and the observation carries the agent's reply and the tool calls it made.

``OpenEnvTarget`` gives every scenario a fresh episode and plays each auditor
message as one step. The agent's tool calls and results come back as
``TargetResponse.tool_events``, so the judge grades what the agent did as well
as what it said. ``openenv`` is imported only when a scenario starts, so this
module loads without the ``openenv`` extra.
"""

from __future__ import annotations

import asyncio
import importlib.util
import warnings
from collections.abc import Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

from .base import TargetContext, TargetResponse

ResetKwargs = Mapping[str, Any] | Callable[[str | None], Mapping[str, Any]]


def _require_openenv() -> None:
    if importlib.util.find_spec("openenv") is None:  # pragma: no cover - depends on the environment
        raise ImportError("OpenEnvTarget needs OpenEnv: pip install 'simpleaudit[openenv]'")


@dataclass
class _Session:
    env: Any
    remote: bool
    exit_stack: AsyncExitStack | None = None
    done: bool = False
    slot: asyncio.Semaphore | None = field(default=None, repr=False)


class OpenEnvTarget:
    """Send audit probes to an agent running in an OpenEnv environment.

    Parameters
    ----------
    env_factory:
        Callable returning a new ``Environment`` (e.g. a ``HarnessEnvironment``)
        to run in this process. Each scenario gets its own instance, so
        scenarios can run in parallel.
    base_url:
        URL of a running OpenEnv server, used through ``GenericEnvClient``.
        Give exactly one of ``env_factory`` and ``base_url``.
    reset_kwargs:
        Keyword arguments for ``reset``, or a callable that takes the scenario
        name and returns them, so each scenario can start in its own world.
    max_sessions:
        Episodes open at once. Defaults to 1 for a server, because a harness
        server holds one session, and to no limit for a factory.
    message_timeout_s:
        Client timeout for one agent turn. Keep it above the server's
        ``session_timeout_s`` (600 s by default).

    The agent keeps its own context, so the history SimpleAudit passes is not
    resent, and the audit's ``system_prompt`` is ignored (configure the agent
    instead). Scenarios with ``file_uri`` or ``documents`` are not supported.
    Subclass and override ``_action`` / ``_read`` for an environment whose
    actions or observations differ from ``HarnessEnvironment``'s.

    Episodes open in ``start_scenario`` and close in ``end_scenario``, which
    ``Auditor`` / ``ModelAuditor`` call around each scenario. ``SingleTurnAuditor``
    does not, so it cannot drive this target.
    """

    def __init__(
        self,
        env_factory: Callable[[], Any] | None = None,
        *,
        base_url: str | None = None,
        reset_kwargs: ResetKwargs | None = None,
        max_sessions: int | None = None,
        message_timeout_s: float = 900.0,
    ) -> None:
        if (env_factory is None) == (base_url is None):
            raise ValueError("OpenEnvTarget needs exactly one of env_factory and base_url")
        self._factory = env_factory
        self._base_url = base_url
        self._reset_kwargs = reset_kwargs
        self._limit = max_sessions if max_sessions is not None else (1 if base_url else None)
        self._message_timeout_s = message_timeout_s
        self._sessions: dict[str, _Session] = {}
        self._slots: asyncio.Semaphore | None = None
        self._slots_loop: asyncio.AbstractEventLoop | None = None
        self._warned_system = False

    # -- lifecycle ---------------------------------------------------------

    async def start_scenario(self, context: TargetContext) -> None:
        slot = self._slot()
        if slot is not None:
            await slot.acquire()
        try:
            session = await self._open(context)
        except BaseException:
            if slot is not None:
                slot.release()
            raise
        session.slot = slot
        self._sessions[context.scenario_run_id] = session

    async def end_scenario(self, context: TargetContext) -> None:
        session = self._sessions.pop(context.scenario_run_id, None)
        if session is not None:
            await self._close(session)

    async def aclose(self) -> None:
        """Close any episode still open, e.g. after an interrupted run."""
        while self._sessions:
            _, session = self._sessions.popitem()
            await self._close(session)

    async def __aenter__(self) -> OpenEnvTarget:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    # -- Target protocol ---------------------------------------------------

    async def send(
        self,
        *,
        system: str | None = None,
        user: str,
        history: list[dict[str, Any]] | None = None,
        file_uri: str | list[str] | None = None,
        documents: list[str | dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        context: TargetContext | None = None,
    ) -> TargetResponse:
        session = self._sessions.get(context.scenario_run_id) if context is not None else None
        if session is None:
            raise RuntimeError(
                "OpenEnvTarget has no open episode for this scenario. Run it with Auditor or "
                "ModelAuditor, which call start_scenario; SingleTurnAuditor does not."
            )
        # The engine carries a scenario's attachments as markers on the
        # history's first user turn, not as send() arguments.
        if file_uri or documents or any(m.get("file_uri") or m.get("documents") for m in history or []):
            raise ValueError("OpenEnvTarget sends text only; this scenario attaches files or documents")
        if session.done:
            raise RuntimeError("The agent already ended this episode")
        if system and not self._warned_system:
            self._warned_system = True
            warnings.warn(
                "OpenEnvTarget ignores the audit's system_prompt; configure the agent in the "
                "environment instead.",
                stacklevel=2,
            )

        if session.remote:
            result = await session.env.step(self._action(user, remote=True))
        else:
            result = await session.env.step_async(self._action(user, remote=False))
        metadata, done = self._read(result, remote=session.remote)
        if metadata.get("error"):
            raise RuntimeError(
                f"OpenEnv agent turn failed ({metadata.get('error_type', 'error')}): {metadata['error']}"
            )
        session.done = done
        events = metadata.get("turn_events") or []
        input_tokens, output_tokens = _usage(events)
        return TargetResponse(
            content=metadata.get("response") or "",
            raw=metadata,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            tool_events=_tool_events(events),
            end_conversation=done,
        )

    # -- environment specifics (override for other environments) ----------

    def _action(self, message: str, *, remote: bool) -> Any:
        """The step action carrying one user message (a ``HarnessAction``)."""
        if remote:
            return {"message": message}
        from openenv.core.harness import HarnessAction

        return HarnessAction(message=message)

    def _read(self, result: Any, *, remote: bool) -> tuple[dict[str, Any], bool]:
        """``(metadata, done)`` from a step: a ``StepResult`` remotely, an ``Observation`` in process."""
        if remote:
            metadata = (result.observation or {}).get("metadata") or result.metadata or {}
            return metadata, bool(result.done)
        return result.metadata or {}, bool(result.done)

    # -- internals ---------------------------------------------------------

    def _slot(self) -> asyncio.Semaphore | None:
        # One semaphore per event loop: a sync auditor.run() starts a new loop
        # each call, and a semaphore must not be shared across loops.
        if self._limit is None:
            return None
        loop = asyncio.get_running_loop()
        if self._slots_loop is not loop:
            self._slots, self._slots_loop = asyncio.Semaphore(self._limit), loop
        return self._slots

    def _kwargs(self, context: TargetContext) -> dict[str, Any]:
        if callable(self._reset_kwargs):
            return dict(self._reset_kwargs(context.extra.get("scenario_name")))
        return dict(self._reset_kwargs or {})

    async def _open(self, context: TargetContext) -> _Session:
        _require_openenv()
        kwargs = self._kwargs(context)
        if self._factory is not None:
            env = self._factory()
            session = _Session(env=env, remote=False)
            try:
                await env.reset_async(**kwargs)
            except BaseException:
                await self._close(session)
                raise
            return session
        from openenv.core.generic_client import GenericEnvClient

        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                GenericEnvClient(base_url=self._base_url, message_timeout_s=self._message_timeout_s)
            )
            await client.reset(**kwargs)
        except BaseException:
            await stack.aclose()
            raise
        return _Session(env=client, remote=True, exit_stack=stack)

    async def _close(self, session: _Session) -> None:
        try:
            if session.remote:
                await session.exit_stack.aclose()
            else:
                # Environment.close is synchronous; HarnessEnvironment stops
                # the agent process on a loop of its own.
                await asyncio.to_thread(session.env.close)
        finally:
            if session.slot is not None:
                session.slot.release()


def _tool_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenEnv harness events -> SimpleAudit ``tool_events``."""
    converted = []
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "tool_call":
            converted.append({"type": "tool_call", "name": data.get("tool_name"), "arguments": data.get("arguments")})
        elif event.get("type") == "tool_result":
            converted.append({
                "type": "tool_result",
                "name": data.get("tool_name"),
                "result": data.get("result"),
                "error": bool(data.get("error")),
            })
    return converted


def _usage(events: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """Token usage from the turn_complete event, when the adapter reports it."""
    for event in reversed(events):
        if event.get("type") == "turn_complete":
            usage = (event.get("data") or {}).get("usage") or {}
            return usage.get("input_tokens"), usage.get("output_tokens")
    return None, None
