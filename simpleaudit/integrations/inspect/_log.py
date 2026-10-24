"""Read Inspect eval logs as SimpleAudit results."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from inspect_ai.log import EvalLog, EvalSample, read_eval_log

from ...repeated_results import RepeatedExperimentResults
from ...results import AuditResult, AuditResults
from ...utils import UNGRADED, VALID_SEVERITIES
from ._task import STORE_KEY


def load_inspect_log(
    log: str | Path | EvalLog,
    *,
    scenario: Callable[[EvalSample], dict[str, Any]] | None = None,
    severity: Callable[[EvalSample], str] | None = None,
) -> RepeatedExperimentResults:
    """Inspect log -> SimpleAudit results, one ``AuditResults`` per epoch.

    Samples from the ``simpleaudit/audit`` task come back exactly as the
    solver stored them. Samples from any other task are rebuilt from their
    messages: tool calls and tool results become the reply's ``tool_events``,
    and system messages and images are dropped.

    For those samples ``scenario(sample)`` may return ``name``,
    ``description`` and ``expected_behavior``; by default they come from the
    sample's metadata and input. ``severity(sample)`` maps the sample's Inspect
    scores to a SimpleAudit severity; by default a score whose value or
    answer is a severity is used, and anything else is ``ungraded``. The
    original scores stay in ``judgment["inspect_scores"]``.

    The result is keyed by the log's model, so ``rejudge(runs.runs(model)[0],
    ...)``, ``load_stored_records(run.to_dict())`` and ``.stability(model)``
    work as on a SimpleAudit run.
    """
    if not isinstance(log, EvalLog):
        log = read_eval_log(str(log))
    if not log.samples:
        raise ValueError("The log has no samples (was it read with header_only=True?).")
    by_epoch: dict[int, list[AuditResult]] = {}
    for sample in log.samples:
        by_epoch.setdefault(sample.epoch, []).append(
            sample_to_result(sample, scenario=scenario, severity=severity)
        )
    return RepeatedExperimentResults({log.eval.model: [AuditResults(by_epoch[e]) for e in sorted(by_epoch)]})


def sample_to_result(
    sample: EvalSample,
    *,
    scenario: Callable[[EvalSample], dict[str, Any]] | None = None,
    severity: Callable[[EvalSample], str] | None = None,
) -> AuditResult:
    """One Inspect sample as an ``AuditResult`` (see ``load_inspect_log``)."""
    stored = (sample.store or {}).get(STORE_KEY)
    if stored is not None:
        return AuditResult(**stored)

    info = scenario(sample) if scenario else {}
    metadata = sample.metadata or {}
    scores = sample.scores or {}
    judgment: dict[str, Any] = {"inspect_scores": {n: s.model_dump(mode="json") for n, s in scores.items()}}
    if sample.error is not None:
        verdict = "ERROR"
        judgment["error"] = sample.error.message
    else:
        verdict = severity(sample) if severity else _scored_severity(scores)
    judgment["severity"] = verdict
    return AuditResult(
        scenario_name=str(info.get("name") or sample.id),
        scenario_description=info.get("description") or metadata.get("description") or _input_text(sample),
        conversation=conversation_from_messages(sample.messages),
        severity=verdict,
        issues_found=[],
        positive_behaviors=[],
        summary=next((s.explanation for s in scores.values() if s.explanation), ""),
        recommendations=[],
        expected_behavior=info.get("expected_behavior", metadata.get("expected_behavior")),
        judgment=judgment,
    )


def conversation_from_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """Inspect chat messages -> SimpleAudit conversation.

    An agent's assistant messages and tool messages up to the next user
    message form one assistant turn: their text joined, their tool calls and
    results in ``tool_events``, which the judge sees as tool activity.
    """
    conversation: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "user":
            conversation.append({"role": "user", "content": message.text})
            continue
        if message.role not in ("assistant", "tool"):
            continue
        if not conversation or conversation[-1]["role"] != "assistant":
            conversation.append({"role": "assistant", "content": ""})
        turn = conversation[-1]
        if message.role == "assistant":
            if message.text:
                turn["content"] = f"{turn['content']}\n\n{message.text}" if turn["content"] else message.text
            events = [
                {"type": "tool_call", "name": call.function, "arguments": call.arguments, "id": call.id}
                for call in message.tool_calls or []
            ]
        else:
            events = [{
                "type": "tool_result",
                "name": message.function or "?",
                "result": message.text,
                "error": message.error is not None,
                "id": message.tool_call_id,
            }]
        if events:
            turn.setdefault("tool_events", []).extend(events)
    return conversation


def _scored_severity(scores: dict[str, Any]) -> str:
    for score in scores.values():
        for candidate in (score.value, score.answer):
            if isinstance(candidate, str) and candidate in VALID_SEVERITIES:
                return candidate
    return UNGRADED


def _input_text(sample: EvalSample) -> str:
    if isinstance(sample.input, str):
        return sample.input
    return "\n".join(m.text for m in sample.input)
