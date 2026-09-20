"""
Single-turn auditing.

:class:`~simpleaudit.model_auditor.ModelAuditor` runs a scenario as a
conversation: an auditor model writes a probe, the target answers, and the loop
repeats up to ``max_turns``. Some evaluations must not work that way. A
context-grounding scenario hands the target one fixed question over one fixed
document set and asks exactly once what it does with them. A probe generator
rewriting that question would be measuring its own paraphrase, and a second
turn would let the target walk back the answer the scenario already captured.

:class:`SingleTurnAuditor` is that runner: one target call with the scenario's
``test_prompt`` verbatim, no probe generation, no turn loop, ``max_turns``
never consulted. The mechanism is lifted from ``BrokenPremiseAuditor`` in
``examples/bullshit_bench/run_bullshitbench.py``, which proved it against
BullshitBench first; the example is left alone and can migrate later.

It also carries the judge-only channel the context-grounding design needs.
Document marks are the scenario author's ground truth: they must reach the
judge, which grades an answer it did not write, and must never reach the
target, which would otherwise be told which document to trust instead of
having to work it out. With no auditor model in the loop there is exactly one
model that could leak them, and this runner does not hand them to it — the
target gets ``render_documents`` (text only), the judge gets the same text plus
the mark table and the derived set properties.

Usage::

    from simpleaudit.single_turn import SingleTurnAuditor

    auditor = SingleTurnAuditor(
        model="gpt-4o-mini", provider="openai",
        judge_model="gpt-4o", judge_provider="openai",
        judge="groundedness",
    )
    results = auditor.run("context_grounding")
"""

import asyncio
from datetime import date
from typing import Any, Dict, List, Optional, Union

from tqdm.auto import tqdm

from .context_derivations import derive_all
from .context_attribution import derive_stance
from .context_findings import derive_findings
from .context_marks import DocumentMark, parse_as_of, parse_documents, render_documents
from .model_auditor import ModelAuditor
from .results import AuditResult, AuditResults
from .utils import image_data_uri, normalize_severity, severity_from_score


def _build_judge_context(
    description: str,
    marks: List[DocumentMark],
    as_of: Optional[date] = None,
    derivations: Optional[Dict[str, Any]] = None,
) -> str:
    """Compose what the judge sees beside the conversation: the documents, blind.

    The judge gets the document text and nothing else. No mark, no derived
    property, and not the scenario description either — the description names
    which document is superseded or which source governs, which is the same
    leak by another route.

    An earlier version showed the judge a mark table and the derived set
    properties, on the theory that knowing which document was superseded would
    help it read an answer that rejects one. It did the opposite: told which
    document was the trap, models reported the stance the scenario EXPECTED
    rather than the one the answer took, and the same stance came back whether
    the answer was right or wrong. Blind, the only thing the judge can report
    is what it actually reads.

    `as_of` and `derivations` are accepted and ignored. They are what the
    caller has in hand, and taking them keeps the call site honest about the
    fact that this function is the boundary the marks do not cross.

    A scenario with no documents gets its description back untouched: there is
    nothing to be blind about, and every non-grounding pack reusing this runner
    still needs its description.
    """
    if not marks:
        return description

    return "\n".join([
        "RETRIEVED CONTEXT — the model saw the text below and nothing else:",
        render_documents(marks).lstrip("\n"),
    ])


class SingleTurnAuditor(ModelAuditor):
    """ModelAuditor that runs each scenario as one exchange, not a conversation.

    Differences from the base class:

    - the scenario's ``test_prompt`` is sent verbatim; nothing is generated
    - exactly one target call, so ``max_turns`` is never read
    - the auditor model is never called, so ``auditor_*`` token counts stay 0
    - the scenario's ``documents`` ride to the target as text-only blocks while
      their marks and the derived set properties go to the judge alone

    Everything else — client construction, judge config resolution, retries,
    JSON schema handling, result shape — is inherited unchanged.
    """

    def _judge_spec(self, context: Dict[str, Any]) -> tuple:
        """Resolve the judge prompt and response schema for this document set.

        A judge whose questions depend on what the author marked cannot be a
        static prompt: asking about a property nobody marked invites the judge
        to invent an answer, and the derivation would have said `None`. Such a
        judge declares `build_judge_prompt` / `build_response_schema` in its
        config and is handed the derived properties here, so the question and
        the schema field appear together or not at all.

        A prompt or schema the caller passed explicitly wins, exactly as it does
        for a static judge: the attribute then differs from the config's own
        value and the builders are skipped. Judges without the builders — every
        existing one — fall through to the flattened attributes unchanged.

        Args:
            context: What the builders may need — ``marks`` (parsed document
                marks), ``as_of`` and ``derivations``. Passed as a dict rather
                than as positional arguments so a judge can start reading
                something new without every caller changing shape.

        Returns:
            ``(judge_prompt, response_schema)`` to pass to the judging call.
        """
        config = getattr(self, "judge_config", None) or {}
        judge_prompt = self.judge_prompt
        response_schema = self.judge_response_schema

        build_prompt = config.get("build_judge_prompt")
        if build_prompt is not None and judge_prompt == config.get("judge_prompt"):
            judge_prompt, _active = build_prompt(context)

        build_schema = config.get("build_response_schema")
        # judge_fields is a deliberate caller-side restriction of the output and
        # must not be widened back out by a builder.
        if (
            build_schema is not None
            and self.judge_fields is None
            and response_schema == config.get("response_schema")
        ):
            response_schema = build_schema(context)

        return judge_prompt, response_schema

    async def _run_one_scenario(self, scenario: Dict[str, Any]) -> AuditResult:
        """Run one scenario as a single exchange and judge the result.

        Args:
            scenario: A scenario dict. ``test_prompt`` is required — it is the
                probe, and there is no auditor model to write one instead.
                ``documents``, ``as_of`` and ``file_uri`` are optional.

        Returns:
            An AuditResult whose conversation is the two messages that were
            actually exchanged.

        Raises:
            ValueError: If the scenario has no ``test_prompt``.
        """
        name = scenario["name"]
        description = scenario.get("description", "")
        test_prompt = scenario.get("test_prompt")
        expected_behavior = scenario.get("expected_behavior")
        file_uri = scenario.get("file_uri")
        documents = scenario.get("documents")

        if not test_prompt:
            raise ValueError(
                f"Scenario {name!r} has no test_prompt. A single-turn run has no "
                "auditor model to generate a probe, so the prompt must be written "
                "into the scenario."
            )

        # Parsed before the target call, not after: a mis-spelled mark key is an
        # authoring bug, and failing on it here costs nothing, whereas failing
        # after the call has already spent target tokens on a scenario that
        # cannot be judged.
        marks = parse_documents(documents)
        as_of = parse_as_of(scenario)
        derivations = derive_all(marks, as_of)

        self._log(f"--- Started Scenario (single-turn): {name} ---")
        prompt_preview = test_prompt[:80] + "..." if len(test_prompt) > 80 else test_prompt
        self._log(f"PROMPT: {prompt_preview}", name=name)

        # The stored turn carries no `documents` key: the marks contain parsed
        # dates that would not survive AuditResults.save(), and the design keeps
        # transcripts plain text. The judge reads the documents from the
        # briefing below instead.
        entry: Dict[str, Any] = {"role": "user", "content": test_prompt}
        if file_uri:
            entry["file_uri"] = file_uri
        conversation: List[Dict[str, Any]] = [entry]

        judge_input_tokens = 0
        judge_output_tokens = 0
        target_input_tokens = 0
        target_output_tokens = 0

        # Same containment as run_scenario: a provider blip becomes one ERROR
        # result rather than an exception that discards the whole batch.
        error: Optional[str] = None
        try:
            response, target_input_tokens, target_output_tokens = await self._call_async(
                self.target_client,
                self.target_model,
                self.system_prompt,
                test_prompt,
                file_uri=file_uri,
                documents=documents,
                max_retries=self.max_retries,
                retry_backoff=self.retry_backoff,
            )
            response = ModelAuditor.strip_thinking(response)
            response_preview = response[:80] + "..." if len(response) > 80 else response
            self._log(f"TARGET: {response_preview}", name=name)
            conversation.append({"role": "assistant", "content": response})
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self._log(f"--- Scenario FAILED on the target call: {name} [{error}] ---")

        if error is None:
            self._log("Judging response...", name=name)
            judge_spec_context = {
                "marks": marks,
                "as_of": as_of,
                "derivations": derivations,
            }
            judge_prompt, response_schema = self._judge_spec(judge_spec_context)
            try:
                judgment, j_in, j_out = await self._judge_conversation_async(
                    self.judge_client,
                    self.judge_model,
                    _build_judge_context(description, marks, as_of, derivations),
                    conversation,
                    # Withheld from a blind judge, and this is the larger of the
                    # two leaks: a grounding scenario's expected_behavior names
                    # the trap outright ("the finding is followed_lower_authority",
                    # "do not be distracted by the planted chunk at index 1"), so
                    # a judge shown it can report the expected stance without
                    # reading the answer. It still travels on the AuditResult,
                    # where it belongs.
                    None if marks else expected_behavior,
                    judge_prompt=judge_prompt,
                    json_format=self.json_format,
                    response_schema=response_schema,
                    judge_fields=self.judge_fields,
                    max_retries=self.max_retries,
                    retry_backoff=self.retry_backoff,
                )
                judge_input_tokens += j_in
                judge_output_tokens += j_out
                # The judge reports what the response did with each document;
                # the findings and the severity are worked out here. Keeping
                # the raw stance alongside them means a disputed finding can
                # be traced back to the observation it came from without
                # re-running the judge.
                if marks and ("asserted_spans" in judgment or "stance" in judgment):
                    observation = dict(judgment)
                    # Attribution first: the judge said what the answer claims
                    # and what it argues against, and the claims are matched to
                    # documents by string comparison here, not by the model.
                    attribution = derive_stance(judgment, marks, response)
                    judgment = derive_findings(
                        {"stance": attribution["stance"],
                         "abstained": judgment.get("abstained")},
                        marks, as_of, derivations, response,
                    )
                    # Everything the derivation stood on, kept so a disputed
                    # finding can be traced without re-running the judge.
                    judgment["stance"] = attribution["stance"]
                    judgment["asserted_spans"] = observation.get("asserted_spans")
                    judgment["attribution_ratios"] = attribution["ratios"]
                    judgment["invalid_spans"] = attribution["invalid_spans"]
                    judgment["conflicting_spans"] = attribution["conflicting_spans"]
                    judgment["evidence_invalid"] = sorted(
                        set(judgment.get("evidence_invalid") or [])
                        | set(attribution["evidence_invalid"])
                    )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                self._log(f"--- Judging FAILED: {name} [{error}] ---")

        if error is not None:
            judgment = {
                "severity": "ERROR",
                "issues_found": [f"Scenario execution failed: {error}"],
                "summary": f"Scenario did not complete due to an error: {error}"[:500],
                "recommendations": [
                    "Re-run this scenario; check API credentials, rate limits, and connectivity."
                ],
                "error": error,
            }

        severity = judgment.get("severity")
        if severity is None and "score" in judgment:
            # Score-based judges emit a 1-10 score and no severity; derive one
            # so their results don't all collapse to the "medium" default.
            severity = severity_from_score(judgment.get("score"))
        severity = normalize_severity(severity or "medium")
        self._log(f"--- Finished Scenario: {name} [Result: {severity.upper()}] ---")

        return AuditResult(
            scenario_name=name,
            scenario_description=description,
            conversation=conversation,
            severity=severity,
            issues_found=judgment.get("issues_found", []),
            positive_behaviors=judgment.get("positive_behaviors", []),
            summary=judgment.get("summary", ""),
            recommendations=judgment.get("recommendations", []),
            expected_behavior=expected_behavior,
            judgment=judgment,
            # auditor_* keep their zero defaults — no probe was generated, so
            # attributing tokens to an auditor here would invent spend.
            judge_input_tokens=judge_input_tokens,
            judge_output_tokens=judge_output_tokens,
            target_input_tokens=target_input_tokens,
            target_output_tokens=target_output_tokens,
        )

    async def run_async(
        self,
        scenarios: Union[str, List[Dict]],
        max_turns: Optional[int] = None,
        language: str = "English",
        max_workers: int = 1,
    ) -> AuditResults:
        """Run every scenario single-turn and collect the results.

        Overridden rather than inherited because ModelAuditor.run_async
        dispatches to ``run_scenario``, which owns the turn loop and the probe
        generator. A subclass that only replaced ``_run_one_scenario`` would
        look single-turn and still run multi-turn through the normal entry
        points, which is the failure mode this override exists to prevent.

        Args:
            scenarios: A pack name or a list of scenario dicts.
            max_turns: Accepted so this stays a drop-in for callers such as
                AuditExperiment, and ignored — there is exactly one turn.
            language: Likewise ignored; there is no probe to write.
            max_workers: Scenarios run concurrently up to this many at a time.

        Returns:
            AuditResults over every scenario, in the order they were given.
        """
        if max_workers < 1:
            raise ValueError(
                f"max_workers must be >= 1, got {max_workers} "
                "(a semaphore of 0 permits would deadlock the run)"
            )
        # Cached on URI alone, so a file regenerated between two audits in one
        # process would otherwise be replayed from its old bytes.
        image_data_uri.cache_clear()
        scenario_list = (
            self.get_scenarios(scenarios) if isinstance(scenarios, str) else scenarios
        )

        target_info = f"{self._target_client_config['provider']} ({self.target_model})"
        judge_info = f"{self._judge_client_config['provider']} ({self.judge_model})"
        mode_desc = f"Parallel ({max_workers} workers)" if max_workers > 1 else "Sequential"

        self._log(f"\n🔍 SingleTurnAuditor - Running {len(scenario_list)} scenarios")
        self._log(f"   Target: {target_info}")
        self._log(f"   Judge: {judge_info}")
        self._log(f"   System Prompt: {'Yes' if self.system_prompt else 'No'}")
        self._log(f"   Mode: {mode_desc} | single-turn, no probe generation\n")

        # One target call per scenario — the audit bar counts scenarios, not
        # turns, because there is no turn count to multiply by.
        audit_desc = f"Single Turn & {len(scenario_list)} Scenarios | Audit Progress"

        results: List[AuditResult] = []
        semaphore = asyncio.Semaphore(max_workers)

        async def _run_one(scenario: Dict) -> AuditResult:
            async with semaphore:
                try:
                    result = await self._run_one_scenario(scenario)
                except Exception as exc:
                    # Don't let one failing scenario abort the whole batch and
                    # discard every other (possibly expensive) result. Record an
                    # ERROR result instead. CancelledError/KeyboardInterrupt are
                    # BaseException subclasses and still propagate.
                    name = scenario.get("name", "<unknown>")
                    self._log(f"--- Scenario FAILED: {name} [{type(exc).__name__}: {exc}] ---")
                    result = AuditResult(
                        scenario_name=name,
                        scenario_description=scenario.get("description", ""),
                        conversation=[],
                        severity="ERROR",
                        issues_found=[f"Scenario execution failed: {type(exc).__name__}: {exc}"],
                        positive_behaviors=[],
                        summary=f"Scenario did not complete due to an error: {exc}"[:500],
                        recommendations=["Re-run this scenario; check API credentials, rate limits, and connectivity."],
                        expected_behavior=scenario.get("expected_behavior"),
                        judgment={"severity": "ERROR", "error": f"{type(exc).__name__}: {exc}"},
                    )
                # Ticked here rather than at the two call sites so the ERROR
                # path advances the bars too and they never finish short.
                pbar_audit.update(1)
                pbar_judge.update(1)
                return result

        with tqdm(total=len(scenario_list), desc=audit_desc, disable=not self.show_progress, position=0) as pbar_audit:
            with tqdm(total=len(scenario_list), desc="Judge Progress", disable=not self.show_progress, position=1) as pbar_judge:
                tasks = [asyncio.create_task(_run_one(scenario)) for scenario in scenario_list]
                try:
                    for task in tasks:
                        results.append(await task)
                except BaseException:
                    # Cancellation (Ctrl-C, asyncio timeout) or a scenario
                    # raising something fatal: don't orphan the in-flight
                    # tasks — cancel them and wait for them to unwind before
                    # propagating.
                    for t in tasks:
                        t.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise

        return AuditResults(results)
