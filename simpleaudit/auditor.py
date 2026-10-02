"""
Auditor — the primary, target-agnostic entry point.

``Auditor`` is the long-term primary class. It accepts any :class:`Target`
(model, HTTP app, callable) and any judge configuration, and delegates to the
full :class:`ModelAuditor` engine (scenarios, judges, findings, reports).

    auditor = Auditor(
        target=HTTPAppTarget(url="https://agent.example.com/chat",
                             response_path="answer"),
        judge_model="gpt-4o",
        judge_provider="openai",
    )
    results = await auditor.run_async("safety")

``ModelAuditor`` remains available as a backwards-compatible convenience
wrapper for model-only audits.
"""

from __future__ import annotations

from typing import Any, Optional

from .model_auditor import ModelAuditor
from .targets import Target


class Auditor:
    """Target-agnostic auditor.

    Parameters
    ----------
    target:
        Any :class:`~simpleaudit.targets.Target`. Required.
    judge_model / judge_provider / judge_api_key / judge_base_url:
        Judge LLM configuration (defaults to OpenAI).
    **kwargs:
        Forwarded to :class:`ModelAuditor` for advanced options (scenarios,
        system_prompt, max_retries, on_turn, etc.).

    Notes
    -----
    Because the engine's scenario/judge/report machinery lives in
    :class:`ModelAuditor`, ``Auditor`` constructs one and overrides its target.
    The ``model`` / ``provider`` / ``api_key`` / ``base_url`` arguments are
    accepted for compatibility but are ignored when an explicit ``target`` is
    provided (the target already knows how to reach the system under test).
    """

    def __init__(
        self,
        target: Target,
        *,
        judge_model: str = "gpt-4o",
        judge_provider: str = "openai",
        judge_api_key: Optional[str] = None,
        judge_base_url: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        if target is None:
            raise ValueError("Auditor requires a target")

        # Build the underlying engine. Since an explicit target is provided,
        # skip creating the (unused) AnyLLM target client so we don't require
        # an API key / network for a target we will never call.
        ModelAuditor._skip_target_client = True
        try:
            self._engine = ModelAuditor(
                model=kwargs.pop("model", "unused"),
                provider=kwargs.pop("provider", "openai"),
                judge_model=judge_model,
                judge_provider=judge_provider,
                judge_api_key=judge_api_key,
                judge_base_url=judge_base_url,
                **kwargs,
            )
        finally:
            ModelAuditor._skip_target_client = False
        self._engine.set_target(target)
        self._target = target

    @property
    def target(self) -> Target:
        return self._target

    @property
    def engine(self) -> ModelAuditor:
        """The underlying :class:`ModelAuditor` engine (advanced access)."""
        return self._engine

    def __getattr__(self, name: str) -> Any:
        # Delegate everything else (run_async, run, results, etc.) to the engine.
        return getattr(self._engine, name)

    async def run_async(self, *args: Any, **kwargs: Any) -> Any:
        return await self._engine.run_async(*args, **kwargs)

    def run(self, *args: Any, **kwargs: Any) -> Any:
        return self._engine.run(*args, **kwargs)
