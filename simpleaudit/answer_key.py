"""
Calibrate a judge against an answer key.

A scenario with a ``decision`` block and an accepted answer says which reply is right,
and with a ``metadata.triage`` block it says how wrong each other reply is. That is a
calibration set with known labels and no labelling work: let a stand-in target answer
every question with every option in turn, have the judge grade those replies the way it
grades a model's, and compare its verdicts with the ones the key gives.

    from simpleaudit import Auditor, calibrate, get_scenarios, planted_answers

    planted = planted_answers(
        get_scenarios("normedqa_triage"),
        reason="Dette er etter min vurdering det riktige tiltaket for denne pasienten.",
    )
    auditor = Auditor(planted.target, judge="checklist", judge_model="...", judge_provider="...")
    results = auditor.run(planted.scenarios, language="Norwegian")

    calibrate(results, planted.reference).summary()
    planted.verdicts_by_outcome(results)      # what the judge said about each kind of answer

The run goes through the ordinary audit path, so the judge sees what it sees in a real
audit: the scenario's description, its expectations and its judge notes. Only the
target is replaced, by one that returns the planted reply. That target answers one turn
per scenario, so the run is single-turn whatever ``max_turns`` says.

Two ways to build a set:

- :func:`planted_answers` writes the replies itself, one per option. Every question
  then contributes its right answer (two, when the key accepts two options) and all
  its wrong ones, so the set has many more failures than a model's own answers would.
  A planted reply has no reasoning of its own. A judge that weighs the justification
  can mark a right planted reply down for that, so read how many right answers a
  judge passes from replayed answers.
- :func:`replayed_answers` takes the replies a target gave in a stored single-turn
  run. They carry the target's own reasoning, but most of them are right when the
  target is good.

The labels are the verdicts of the ``triage_match`` judge, which grades in code: the
accepted option passes, and a wrong one gets the severity its direction calls for, or
the scenario's designed severity when the scenario records no directions.

``reveal`` controls how much of the key the judge is shown:

- ``"full"`` (default): the scenario as it is. This measures whether the judge applies
  the expectations and notes it was given.
- ``"key"``: the accepted option, and nothing about the wrong ones. The description is
  replaced and the judge notes are removed, so the judge knows what is right and has
  to rate a wrong answer on its own.
- ``"none"``: no key at all. The judge has to know the answer. This measures the judge
  model's own grasp of the subject, which is what an audit leans on wherever a
  scenario's expectations are incomplete.

The labels are the same in all three. Under ``"key"`` and ``"none"`` read the pass/fail
numbers: the judge was not told which severity a wrong answer should get, and a judge
whose code caps the verdict at the scenario's designed severity (``checklist``) still
takes that ceiling from the scenario.
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .calibration import Rater
from .decision import validate_decision
from .judges.triage_match import grade_triage_match
from .results import AuditResults
from .targets import CallableTarget

#: How a planted reply states its option.
STYLES = ("letter", "text")
#: How much of the answer key the judge is shown.
REVEAL = ("full", "key", "none")
#: Which options get a planted reply.
OPTIONS = ("all", "wrong", "accepted")

_NEUTRAL_DESCRIPTION = (
    "A closed question with a fixed set of options. The reply should choose the correct option."
)


def _grade(scenario: Mapping[str, Any], conversation: Sequence[Mapping[str, Any]]) -> tuple[str, str]:
    """The severity and outcome the answer key gives a conversation's last reply."""
    meta = {
        "severity": scenario.get("severity"),
        "decision": scenario["decision"],
        "metadata": scenario.get("metadata") or {},
    }
    judgment = grade_triage_match(conversation=list(conversation), scenario_meta=meta)
    return judgment["severity"], judgment["triage"]["outcome"]


def _has_key(scenario: Mapping[str, Any]) -> bool:
    decision = scenario.get("decision")
    return isinstance(decision, Mapping) and bool(decision.get("accepted"))


def _checked(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """The scenario's decision block, validated; an error names the scenario."""
    try:
        decision = validate_decision(scenario["decision"])
        # the triage block is validated here too, so a bad one is reported before any run
        _grade({**scenario, "decision": decision}, [{"role": "assistant", "content": "", "decision": {"choice": None}}])
    except ValueError as exc:
        raise ValueError(f"scenario {scenario.get('name')!r}: {exc}") from exc
    return decision


def _shown(scenario: Mapping[str, Any], decision: Mapping[str, Any], reveal: str) -> dict[str, Any]:
    """The scenario as the judge is to see it under *reveal*."""
    metadata = dict(scenario.get("metadata") or {})
    if reveal == "full":
        return {**scenario, "metadata": metadata}
    if reveal == "key":
        options = [
            f"{key}: {decision['criteria'][key]}" if decision["criteria"][key] else key
            for key in decision["accepted"]
        ]
        expected = ["Chooses option " + " or ".join(options)]
    else:
        expected = ["Chooses the correct option"]
    metadata.pop("judge_notes", None)
    return {**scenario, "description": _NEUTRAL_DESCRIPTION, "expected_behavior": expected, "metadata": metadata}


@dataclass
class AnswerKeySet:
    """Replies with a verdict from the answer key, ready to put in front of a judge."""

    #: The scenarios to run, one per reply. Their decision ids name the reply to give.
    scenarios: list[dict[str, Any]]
    #: ``{decision id: reply text}``, what :attr:`target` answers with.
    replies: dict[str, str]
    #: ``{scenario name: severity}`` according to the answer key.
    labels: dict[str, str]
    #: ``{scenario name: outcome}``: correct, under_triage, over_triage or other_wrong.
    outcomes: dict[str, str]
    #: ``{reason: count}`` for scenarios or stored replies that were left out.
    left_out: dict[str, int] = field(default_factory=dict)

    @property
    def target(self) -> CallableTarget:
        """A target that gives each scenario its reply, once, and answers nothing else."""
        replies = self.replies

        def answer(*, context: Any = None, **_: Any) -> str:
            decision = (getattr(context, "extra", None) or {}).get("decision") or {}
            try:
                return replies[decision["id"]]
            except KeyError:
                raise ValueError(
                    "this target only answers the scenarios of its own AnswerKeySet; "
                    f"it was asked about decision id {decision.get('id')!r}"
                ) from None

        target = CallableTarget(answer)
        # One reply per scenario: the engine caps the run at this (ModelAuditor._turns_for_target).
        target.max_turns = 1
        return target

    @property
    def reference(self) -> Rater:
        """The labels as a reference for :func:`simpleaudit.calibrate`."""
        return Rater(name="answer key", kind="answer_key", ratings=dict(self.labels))

    def verdicts_by_outcome(self, results: AuditResults | str | Path | Iterable[Any]) -> dict[str, dict[str, int]]:
        """``{outcome: {judge severity: count}}`` for a judged run of :attr:`scenarios`.

        Shows where a judge's errors sit: which severities it gave the under-triage
        replies, the over-triage replies, and so on. A judge result that is an
        ``ERROR`` is counted under that name. Results for scenarios that are not in
        this set are ignored.
        """
        if isinstance(results, (str, Path)):
            results = AuditResults.load(str(results))
        table: dict[str, Counter] = {}
        for result in results:
            outcome = self.outcomes.get(result.scenario_name)
            if outcome is not None:
                table.setdefault(outcome, Counter())[result.severity] += 1
        return {outcome: dict(counts) for outcome, counts in table.items()}


def planted_answers(
    scenarios: Iterable[Mapping[str, Any]],
    *,
    style: str = "letter",
    reveal: str = "full",
    options: str = "all",
    reason: str | None = None,
    reply: Callable[[Mapping[str, Any], str], str] | None = None,
    transform: Callable[[Sequence[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> AnswerKeySet:
    """One reply per option of every scenario that has an answer key.

    Args:
        scenarios: scenarios with a ``decision`` block, each with its own name. Those
            without ``accepted`` are left out; at least one must have it.
        style: ``"letter"`` writes the option key on the first line and the option's
            text on the second, the way a model answers a choice question. ``"text"``
            writes the text alone, so the judge has to match the reply to an option
            by content.
        reveal: how much of the key the judge sees: ``"full"``, ``"key"`` or
            ``"none"`` (see the module docstring).
        options: ``"all"`` options, only the ``"wrong"`` ones, or only the
            ``"accepted"`` ones.
        reason: a sentence added as the reply's last line, the same for every reply.
            Give one, in the prompt's language, when the scenarios ask the target to
            justify its answer: a judge can mark a bare letter down for the missing
            justification, and then fails correct replies for a reason that has
            nothing to do with the key. Not used when ``reply`` is given.
        reply: a function ``(scenario, option key) -> reply text`` to use in place
            of ``style`` and ``reason``.
        transform: applied to the reply as a one-message conversation, for the
            style perturbations in :mod:`simpleaudit.perturbations`
            (``transform=authority_claim("Norwegian")``). The label does not change:
            it follows the option, not the wording.

    Returns:
        An :class:`AnswerKeySet`. Each scenario is named after its source with
        " [answer <key>]" appended.
    """
    if style not in STYLES:
        raise ValueError(f"style {style!r} not in {list(STYLES)}")
    if reveal not in REVEAL:
        raise ValueError(f"reveal {reveal!r} not in {list(REVEAL)}")
    if options not in OPTIONS:
        raise ValueError(f"options {options!r} not in {list(OPTIONS)}")

    planted: list[dict[str, Any]] = []
    replies: dict[str, str] = {}
    labels: dict[str, str] = {}
    outcomes: dict[str, str] = {}
    left_out: Counter = Counter()
    seen: set[str] = set()
    for index, scenario in enumerate(scenarios):
        if not _has_key(scenario):
            left_out["no answer key"] += 1
            continue
        if scenario["name"] in seen:
            raise ValueError(f"two scenarios are named {scenario['name']!r}; labels are kept by scenario name")
        seen.add(scenario["name"])
        decision = _checked(scenario)
        shown = _shown(scenario, decision, reveal)
        for key, text in decision["criteria"].items():
            accepted = key in decision["accepted"]
            if (options == "wrong" and accepted) or (options == "accepted" and not accepted):
                continue
            if reply is not None:
                content = reply(scenario, key)
            else:
                content = (f"{key}\n{text}" if text else key) if style == "letter" else (text or key)
                if reason:
                    content = f"{content}\n{reason}"
            if transform is not None:
                turns = transform([{"role": "assistant", "content": content}])
                content = next(m["content"] for m in reversed(turns) if m.get("role") == "assistant")
            name = f"{scenario['name']} [answer {key}]"
            # The scenario's position is part of the id, so scenarios without an id of
            # their own (they all default to "answer") still get their own replies.
            planted_id = f"{index}:{decision['id']}:{key}"
            # Graded on the option itself, so a reply in "text" style gets the same label.
            chosen = [{"role": "assistant", "content": "", "decision": {"choice": key}}]
            labels[name], outcomes[name] = _grade({**scenario, "decision": decision}, chosen)
            replies[planted_id] = content
            planted.append({
                **shown,
                "name": name,
                "decision": {**scenario["decision"], "id": planted_id},
                "metadata": {**shown["metadata"], "planted": {"scenario": scenario["name"], "option": key}},
            })
    if not planted:
        raise ValueError("no scenario has a decision block with accepted options, so there is no answer key to plant")
    return AnswerKeySet(scenarios=planted, replies=replies, labels=labels, outcomes=outcomes, left_out=dict(left_out))


def replayed_answers(
    results: AuditResults | str | Path,
    scenarios: Iterable[Mapping[str, Any]],
    *,
    reveal: str = "full",
) -> AnswerKeySet:
    """The replies of a stored single-turn run, labelled by the answer key, to judge again.

    Args:
        results: a run of *scenarios* (an :class:`AuditResults` or a saved file), by
            any judge. Only its transcripts are used. The run must be single-turn:
            the judge is shown the scenario's prompt and one reply.
        scenarios: the scenarios that run was made on.
        reveal: how much of the key the judge sees, as in :func:`planted_answers`.

    Returns:
        An :class:`AnswerKeySet` whose scenarios keep their names, so the judged run
        lines up with the stored one. A result is left out, and counted in
        ``left_out``, when its scenario is not among *scenarios* or has no answer
        key, when it has no reply text, and when the reply names no option the key
        can read: such a reply has no label to score a judge against.
    """
    if reveal not in REVEAL:
        raise ValueError(f"reveal {reveal!r} not in {list(REVEAL)}")
    if isinstance(results, (str, Path)):
        results = AuditResults.load(str(results))
    by_name = {scenario["name"]: scenario for scenario in scenarios if _has_key(scenario)}

    replayed: list[dict[str, Any]] = []
    replies: dict[str, str] = {}
    labels: dict[str, str] = {}
    outcomes: dict[str, str] = {}
    left_out: Counter = Counter()
    for index, result in enumerate(results):
        scenario = by_name.get(result.scenario_name)
        if scenario is None:
            left_out["no scenario with an answer key"] += 1
            continue
        turns = [m for m in (result.conversation or []) if m.get("role") == "assistant"]
        if len(turns) > 1:
            raise ValueError(
                f"{result.scenario_name!r} has {len(turns)} assistant turns; replayed_answers needs a single-turn run"
            )
        if not turns or not (turns[0].get("content") or "").strip():
            left_out["no reply text"] += 1
            continue
        if result.scenario_name in labels:
            raise ValueError(f"the run holds {result.scenario_name!r} twice; replay one run per call")
        decision = _checked(scenario)
        label, outcome = _grade({**scenario, "decision": decision}, result.conversation)
        if outcome == "no_answer":
            left_out["no option recognised in the reply"] += 1
            continue
        replay_id = f"{index}:{decision['id']}:replay"
        labels[result.scenario_name], outcomes[result.scenario_name] = label, outcome
        replies[replay_id] = turns[0]["content"]
        shown = _shown(scenario, decision, reveal)
        replayed.append({**shown, "decision": {**scenario["decision"], "id": replay_id}})
    if not replayed:
        raise ValueError("no result matches a scenario with an answer key and a reply the key can read")
    return AnswerKeySet(scenarios=replayed, replies=replies, labels=labels, outcomes=outcomes, left_out=dict(left_out))
