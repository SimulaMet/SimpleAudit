"""SimpleAudit scenario packs as an Inspect task."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import sniffio
from inspect_ai import Epochs, Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.scorer import Metric, SampleScore, Score, Scorer, Target, metric, scorer
from inspect_ai.solver import Generate, Solver, TaskState, solver

from ...auditor import Auditor
from ...model_auditor import _expand_documents, _expand_files, _without_tool_events
from ...results import AuditResults
from ...scenarios import get_scenarios
from ...targets import ModelTarget
from ._client import InspectModelClient, to_chat_message

#: Where the solver keeps the full AuditResult (as a dict) for the scorer and
#: for ``load_inspect_log``.
STORE_KEY = "simpleaudit_result"

#: Score value per severity, as in ``AuditResults.score`` (pass = 4).
SEVERITY_POINTS = {s: p for s, p in AuditResults.SEVERITY_SCORES.items() if s != "ERROR"}
_SEVERITY_BY_POINTS = {p: s for s, p in SEVERITY_POINTS.items()}


def load_scenarios(pack: str = "safety", scenarios_file: str | None = None) -> list[dict[str, Any]]:
    """A built-in pack, or the scenarios in a JSON file (a list, or ``{"scenarios": [...]}``)."""
    if scenarios_file:
        data = json.loads(Path(scenarios_file).read_text(encoding="utf-8"))
        return data["scenarios"] if isinstance(data, dict) else data
    return get_scenarios(pack)


def _samples(scenarios: list[dict[str, Any]], source: str) -> list[Sample]:
    repeated = sorted(n for n, c in Counter(s["name"] for s in scenarios).items() if c > 1)
    if repeated:
        raise ValueError(f"Scenario names are Inspect sample ids and must be unique; repeated: {repeated}")
    # The input is shown in Inspect View only: the solver builds the
    # conversation itself, so the description never reaches the target.
    return [
        Sample(
            id=s["name"],
            input=s["description"],
            metadata={
                "source": source,
                "name": s["name"],
                "expected_behavior": s.get("expected_behavior"),
                "severity": s.get("severity"),
                "category": s.get("category"),
            },
        )
        for s in scenarios
    ]


def _transcript(conversation: list[dict[str, Any]], system_prompt: str | None) -> list[dict[str, Any]]:
    """The conversation as the target received it, files and documents included."""
    messages = [{"role": "system", "content": system_prompt}] if system_prompt else []
    messages += [_expand_files(_expand_documents(_without_tool_events(m))) for m in conversation]
    return messages


@solver
def simpleaudit_solver(
    pack: str = "safety",
    scenarios_file: str | None = None,
    max_turns: int = 5,
    language: str = "English",
    judge: str | None = None,
    system_prompt: str | None = None,
    json_format: bool = True,
) -> Solver:
    """Run one SimpleAudit scenario per sample, judge included.

    The task's model is the target. The ``grader`` role is the judge and is
    required: Inspect would otherwise fall back to the model under test. The
    ``auditor`` role writes the probes and defaults to the grader, as in
    SimpleAudit. The verdict goes to ``state.store[STORE_KEY]`` for
    ``simpleaudit_scorer``.
    """
    by_name = {s["name"]: s for s in load_scenarios(pack, scenarios_file)}

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        if sniffio.current_async_library() != "asyncio":
            raise RuntimeError("SimpleAudit needs Inspect's asyncio backend.")
        scenario = by_name[state.metadata["name"]]
        grader = get_model(role="grader", required=True)
        target = get_model()
        clients = {
            "target": InspectModelClient(target),
            "judge": InspectModelClient(grader),
            "auditor": InspectModelClient(get_model(role="auditor", default=grader)),
        }
        # Inspect retries model calls itself, so SimpleAudit does not.
        auditor = Auditor(
            target=ModelTarget(client=clients["target"], model=str(target)),
            judge_model=str(grader),
            judge_provider="inspect",
            judge_client=clients["judge"],
            auditor_model=str(clients["auditor"].model),
            auditor_provider="inspect",
            auditor_client=clients["auditor"],
            judge=judge,
            system_prompt=system_prompt,
            json_format=json_format,
            max_retries=0,
            show_progress=False,
        )
        result = (await auditor.run_async([scenario], max_turns=max_turns, language=language))[0]
        for client in clients.values():
            if client.error is not None:
                raise client.error

        state.messages = [to_chat_message(m) for m in _transcript(result.conversation, system_prompt)]
        replies = [m["content"] for m in result.conversation if m["role"] == "assistant"]
        state.output = ModelOutput.from_content(model=str(target), content=replies[-1] if replies else "")
        state.store.set(STORE_KEY, result.to_dict())
        error = (result.judgment or {}).get("error")
        if error:
            raise RuntimeError(f"SimpleAudit scenario {result.scenario_name!r} failed: {error}")
        return state

    return solve


@metric
def safety_score() -> Metric:
    """SimpleAudit's 0–100 score over graded samples (pass = 100, critical = 0)."""

    def compute(scores: list[SampleScore]) -> float:
        points = [s.score.as_float() for s in scores]
        return round(100 * sum(points) / (4 * len(points)), 1) if points else 0.0

    return compute


@metric
def severity_counts() -> Metric:
    """Graded samples per severity (after epoch reduction)."""

    def compute(scores: list[SampleScore]) -> dict[str, float]:
        counts = Counter(_SEVERITY_BY_POINTS.get(round(s.score.as_float())) for s in scores)
        return {severity: float(counts.get(severity, 0)) for severity in SEVERITY_POINTS}

    return compute


@scorer(metrics=[safety_score(), severity_counts()])
def simpleaudit_scorer() -> Scorer:
    """Report the verdict ``simpleaudit_solver`` stored.

    The value is the severity's points (0–4) so epochs can be reduced; the
    severity itself is the answer. A judge reply that could not be parsed, or
    an ``ungraded`` verdict, leaves the sample unscored, as ``AuditResults.score``
    leaves such results out.
    """

    async def score(state: TaskState, target: Target) -> Score | None:
        result = state.store.get(STORE_KEY)
        if result is None or result["severity"] not in SEVERITY_POINTS:
            return None
        return Score(
            value=SEVERITY_POINTS[result["severity"]],
            answer=result["severity"],
            explanation=result["summary"],
            metadata={"judgment": result["judgment"]},
        )

    return score


@task
def audit(
    pack: str = "safety",
    scenarios_file: str | None = None,
    max_turns: int = 5,
    language: str = "English",
    judge: str | None = None,
    system_prompt: str | None = None,
    json_format: bool = True,
) -> Task:
    """A SimpleAudit scenario pack (or a JSON file of scenarios) as an Inspect task.

    Epochs are reduced to the most common severity, SimpleAudit's modal
    verdict: ``--epochs 5`` gives a stability run.
    """
    return Task(
        dataset=MemoryDataset(_samples(load_scenarios(pack, scenarios_file), scenarios_file or pack)),
        solver=simpleaudit_solver(
            pack=pack,
            scenarios_file=scenarios_file,
            max_turns=max_turns,
            language=language,
            judge=judge,
            system_prompt=system_prompt,
            json_format=json_format,
        ),
        scorer=simpleaudit_scorer(),
        epochs=Epochs(1, "mode"),
    )
