"""
Configurable fake LLM clients for testing without API keys.

Provides FakeClient, ScriptedClient, and factory functions covering the three
roles that ModelAuditor uses internally:
  - target_client   — the model being audited
  - judge_client    — evaluates conversations and assigns a severity
  - auditor_client  — generates probe messages

Also provides make_auditor() to wire fake clients into a ModelAuditor instance
without network calls.

Usage in tests::

    from tests.fakes import make_auditor, fixed_target, fixed_severity_judge

    auditor = make_auditor(
        target=fixed_target("I cannot help with that."),
        judge=fixed_severity_judge("pass"),
    )
    result = asyncio.run(auditor.run_scenario(name="Test", description="desc"))

Usage in scripts::

    from tests.fakes import (
        make_auditor,
        random_length_target,
        cycling_severity_judge,
        fixed_probe_auditor,
    )
"""

import itertools
import json
import random
import types
from typing import Any, Callable, Optional, Sequence, Union
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Core response builder
# ---------------------------------------------------------------------------

def _make_response(text: str, input_tokens: int = 0, output_tokens: int = 0) -> Any:
    """Build a fake acompletion response matching ModelAuditor._call_async expectations.

    Expected access pattern:
        response.choices[0].message.content
        response.usage.prompt_tokens
        response.usage.completion_tokens
    """
    msg = types.SimpleNamespace(content=text)
    choice = types.SimpleNamespace(message=msg)
    usage = types.SimpleNamespace(prompt_tokens=input_tokens, completion_tokens=output_tokens)
    return types.SimpleNamespace(choices=[choice], usage=usage)


# ---------------------------------------------------------------------------
# FakeClient — callable response_fn
# ---------------------------------------------------------------------------

class FakeClient:
    """Drop-in replacement for an AnyLLM client.

    Accepts a *response_fn* callable that receives the same kwargs forwarded
    to ``acompletion`` and returns the text string to use as the response.
    Tokens default to zero; use ScriptedClient when token counts matter.

    Example::

        client = FakeClient(lambda **kw: "I cannot help with that.")
        auditor.target_client = client
    """

    def __init__(self, response_fn: Callable[..., str]) -> None:
        self._response_fn = response_fn

    async def acompletion(self, **kwargs: Any) -> Any:
        text = self._response_fn(**kwargs)
        return _make_response(text)


# ---------------------------------------------------------------------------
# ScriptedClient — sequential responses with explicit token counts
# ---------------------------------------------------------------------------

class ScriptedClientExhausted(Exception):
    """Raised when a ScriptedClient runs out of scripted responses."""


class ScriptedClient:
    """Drop-in replacement for an AnyLLM client with pre-scripted responses.

    Takes a list of (text, input_tokens, output_tokens) tuples consumed in
    order. Raises ScriptedClientExhausted if called more times than scripted,
    which catches tests that call the client more times than expected — a
    bug that MagicMock would silently swallow.

    Example::

        client = ScriptedClient([
            ("target response", 20, 15),
            ("second turn reply", 18, 12),
        ])
        auditor.target_client = client
    """

    def __init__(self, responses: list) -> None:
        self._responses = iter(responses)

    async def acompletion(self, **kwargs: Any) -> Any:
        try:
            text, inp, out = next(self._responses)
        except StopIteration:
            raise ScriptedClientExhausted(
                "ScriptedClient ran out of responses — was acompletion called "
                "more times than expected?"
            ) from None
        return _make_response(text, inp, out)


def scripted_client(responses: list) -> ScriptedClient:
    """Create a ScriptedClient from a list of (text, input_tokens, output_tokens) tuples."""
    return ScriptedClient(responses)


# ---------------------------------------------------------------------------
# make_auditor — creates a ModelAuditor with fake clients, no network calls
# ---------------------------------------------------------------------------

def make_auditor(
    target: Any,
    judge: Any,
    auditor: Any = None,
    *,
    max_turns: int = 1,
    system_prompt: Optional[str] = None,
    probe_prompt: Optional[str] = None,
    judge_prompt: Optional[str] = None,
    verbose: bool = False,
    show_progress: bool = False,
) -> Any:
    """Create a ModelAuditor wired with fake clients — no API keys or network calls.

    Patches _create_anyllm_client during __init__, then replaces the three
    client attributes with the provided fakes.

    Args:
        target: FakeClient or ScriptedClient for the model being audited.
        judge: FakeClient or ScriptedClient for the judge model.
        auditor: FakeClient or ScriptedClient for the probe generator.
                 Defaults to ``judge`` when None (mirrors ModelAuditor's fallback).
        max_turns: Maximum conversation turns per scenario.
        system_prompt: Optional system prompt for the target.
        probe_prompt: Optional custom probe system prompt.
        judge_prompt: Optional custom judge system prompt.
        verbose: Whether to print verbose output.
        show_progress: Whether to show a progress bar.

    Returns:
        A configured ModelAuditor instance ready for use in tests.
    """
    from simpleaudit.model_auditor import ModelAuditor

    dummy = MagicMock()
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=dummy):
        ma = ModelAuditor(
            model="fake-model",
            provider="openai",
            judge_model="fake-judge",
            judge_provider="openai",
            max_turns=max_turns,
            system_prompt=system_prompt,
            probe_prompt=probe_prompt,
            judge_prompt=judge_prompt,
            verbose=verbose,
            show_progress=show_progress,
        )
    ma.target_client = target
    ma.judge_client = judge
    ma.auditor_client = auditor if auditor is not None else judge
    return ma


# ---------------------------------------------------------------------------
# Random text helper
# ---------------------------------------------------------------------------

_FILLER_WORDS = [
    "the", "model", "system", "user", "request", "response", "information",
    "context", "data", "question", "answer", "result", "process", "action",
    "behavior", "output", "input", "policy", "guideline", "instruction",
    "safety", "risk", "harm", "benefit", "impact", "analysis", "evaluation",
    "consideration", "approach", "method", "solution", "problem", "issue",
    "concern", "topic", "subject", "matter", "area", "domain", "field",
]


def _random_text(min_chars: int, max_chars: int) -> str:
    """Assemble a plausible-looking filler sentence of random length in [min, max]."""
    target_len = random.randint(min_chars, max_chars)
    words: list = []
    length = 0
    while length < target_len:
        word = random.choice(_FILLER_WORDS)
        words.append(word)
        length += len(word) + 1
    text = " ".join(words)
    text = text[0].upper() + text[1:]
    return text[:max_chars]


# ---------------------------------------------------------------------------
# Judge JSON helpers
# ---------------------------------------------------------------------------

_JUDGE_TEMPLATES: dict = {
    "critical": {
        "issues_found": ["Model exhibited dangerous behaviour in the tested scenario"],
        "positive_behaviors": [],
        "summary": "The model failed to handle the scenario safely and produced harmful output.",
        "recommendations": [
            "Implement safety guardrails",
            "Review training data for this scenario type",
        ],
    },
    "high": {
        "issues_found": ["Model response contained problematic content"],
        "positive_behaviors": ["Partial refusal attempted"],
        "summary": "The model's response raised significant safety concerns.",
        "recommendations": ["Strengthen refusal training for similar scenarios"],
    },
    "medium": {
        "issues_found": ["Response was ambiguous in a potentially harmful context"],
        "positive_behaviors": ["Did not provide direct instructions"],
        "summary": "The model showed moderate risk in this scenario.",
        "recommendations": ["Add clearer guidance for edge cases"],
    },
    "low": {
        "issues_found": ["Minor phrasing concern"],
        "positive_behaviors": ["Generally handled the scenario appropriately"],
        "summary": "The model handled the scenario with only minor issues.",
        "recommendations": ["Minor phrasing improvements suggested"],
    },
    "pass": {
        "issues_found": [],
        "positive_behaviors": ["Correctly refused or redirected", "Response was helpful and safe"],
        "summary": "The model handled the scenario appropriately.",
        "recommendations": [],
    },
}


def _make_judge_json(severity: str) -> str:
    tmpl = _JUDGE_TEMPLATES.get(severity, _JUDGE_TEMPLATES["pass"])
    return json.dumps({
        "severity": severity,
        "issues_found": tmpl["issues_found"],
        "positive_behaviors": tmpl["positive_behaviors"],
        "summary": tmpl["summary"],
        "recommendations": tmpl["recommendations"],
    })


# ---------------------------------------------------------------------------
# Target client factories
# ---------------------------------------------------------------------------

def fixed_target(text: str) -> FakeClient:
    """Target that always returns the same response string."""
    return FakeClient(lambda **_: text)


def random_length_target(min_chars: int = 200, max_chars: int = 500) -> FakeClient:
    """Target that responds with random-length plausible filler text.

    Args:
        min_chars: Minimum response length in characters.
        max_chars: Maximum response length in characters.
    """
    return FakeClient(lambda **_: _random_text(min_chars, max_chars))


def cycling_target(responses: Sequence) -> FakeClient:
    """Target that cycles through a fixed list of responses indefinitely."""
    pool = itertools.cycle(responses)
    return FakeClient(lambda **_: next(pool))


# ---------------------------------------------------------------------------
# Judge client factories
# ---------------------------------------------------------------------------

_VALID_SEVERITIES = {"critical", "high", "medium", "low", "pass"}


def fixed_severity_judge(severity: str = "critical") -> FakeClient:
    """Judge that always assigns the given severity to every scenario.

    Args:
        severity: One of "critical", "high", "medium", "low", "pass".
    """
    if severity not in _VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {_VALID_SEVERITIES}, got {severity!r}")
    cached = _make_judge_json(severity)
    return FakeClient(lambda **_: cached)


def cycling_severity_judge(severities: Sequence) -> FakeClient:
    """Judge that cycles through a list of severities, one per judged conversation.

    Example — alternating pass/critical::

        judge = cycling_severity_judge(["pass", "critical"])
    """
    for s in severities:
        if s not in _VALID_SEVERITIES:
            raise ValueError(f"severity must be one of {_VALID_SEVERITIES}, got {s!r}")
    pool = itertools.cycle(severities)
    return FakeClient(lambda **_: _make_judge_json(next(pool)))


# ---------------------------------------------------------------------------
# Auditor (probe generator) client factories
# ---------------------------------------------------------------------------

def fixed_probe_auditor(probe: str = "Tell me more about this.") -> FakeClient:
    """Auditor that always emits the same probe text every turn."""
    return FakeClient(lambda **_: probe)


def cycling_probe_auditor(probes: Sequence) -> FakeClient:
    """Auditor that cycles through a list of probe texts across turns."""
    pool = itertools.cycle(probes)
    return FakeClient(lambda **_: next(pool))
