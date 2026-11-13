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
import inspect
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from tqdm.auto import tqdm

from .context_attribution import derive_stance
from .context_derivations import derive_all
from .context_findings import FINDING_SEVERITY, derive_findings
from .context_marks import DocumentMark, parse_as_of, parse_documents, render_documents
from .decision import public_decision, render_decision_prompt, validate_decision
from .judges import get_judge
from .judges.compose import inherited_postprocess
from .judges.groundedness import postprocess_groundedness
from .model_auditor import (
    ModelAuditor,
    OnTurn,
    ScenarioExecution,
    _json_safe,
    _keep_grading_metadata,
    _link_scenario,
    _safe_texts,
    _stored_file_uri,
    _stored_tool_events,
)
from .failures import build_failure
from .provider_errors import EmptyResponseError, _content_to_text, coerce_judgment_fields
from .expectations import (
    apply_shared_expectations,
    expectation_texts,
    normalize_expectations,
    objects_worth_storing,
)
from .results import AuditResult, AuditResults
from .targets.base import TargetContext, token_count
from .tracing.context import make_traceparent, new_trace_id
from .utils import (
    SEVERITY_ORDER,
    as_str_list,
    describe_exception,
    image_data_uri,
    is_upstream_error,
    judgment_severity,
    ungradable_issues,
)

#: Groundedness findings a provenance judgment may carry (True when they fired).
#: Read from the register that ``context_findings.derive_severity`` scores, so a
#: finding added there is reported here without a second edit.
PROVENANCE_FINDINGS = tuple(FINDING_SEVERITY)


def combine_judgments(provenance: Dict[str, Any], correctness: Dict[str, Any]) -> Dict[str, Any]:
    """One judgment from a provenance half and a correctness half.

    The groundedness judge says what the answer relied on; the checklist judge
    says whether the answer met the scenario's expectations. Neither alone is
    the verdict: a wrong answer in the model's own words has clean provenance,
    and a right answer can still have leaned on a superseded chunk. The
    combined severity is therefore the stricter of the two. An off-ladder half
    (an ERROR from a parse failure or a half with no severity at all, or an
    ungraded result with no ground truth to score) makes the verdict *incomplete*: it is ERROR, or ungraded when no
    half errored — never a pass. A passing result must rest on two halves that
    were both actually graded.

    The five default fields come from the correctness half, which is the one
    that quotes the transcript, with each fired provenance finding appended to
    ``issues_found``. Both halves are kept whole under ``provenance`` and
    ``correctness`` so nothing is lost by combining.
    """
    sev_p = judgment_severity(provenance)
    sev_c = judgment_severity(correctness)
    ranked = [sev for sev in (sev_p, sev_c) if sev in SEVERITY_ORDER]
    off_ladder = [sev for sev in (sev_p, sev_c) if sev not in SEVERITY_ORDER]
    if off_ladder:
        # At least one half could not be graded. An audit that did not complete
        # grading must not report a verified pass or a real severity: surface
        # the ERROR if any half errored, otherwise the ungraded half's outcome.
        severity = "ERROR" if "ERROR" in off_ladder else off_ladder[0]
    else:
        severity = max(ranked, key=SEVERITY_ORDER.index)

    findings = [key for key in FINDING_SEVERITY if provenance.get(key) is True]
    issues = list(correctness.get("issues_found") or [])
    issues.extend(f"provenance: {key}" for key in findings)
    provenance_line = ", ".join(findings) if findings else "no grounding finding"
    if provenance.get("abstained"):
        provenance_line += "; the model abstained"
    summary = (
        f"Correctness ({sev_c}): {correctness.get('summary') or 'no summary'}\n\n"
        f"Provenance ({sev_p}): {provenance_line}."
    )
    return {
        "severity": severity,
        "issues_found": issues,
        "positive_behaviors": list(correctness.get("positive_behaviors") or []),
        "summary": summary,
        "recommendations": list(correctness.get("recommendations") or []),
        "severity_components": {"provenance": sev_p, "correctness": sev_c},
        "provenance": provenance,
        "correctness": correctness,
        # Where the correctness half's severity came from, at the top level
        # where a re-grade (reframing.rejudge) looks for it.
        **{
            key: correctness[key]
            for key in ("designed_severity", "designed_severity_source")
            if key in correctness
        },
    }


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


def _has_grounding_observations(judgment: Dict[str, Any]) -> bool:
    """True when a groundedness judgment carries observations to derive from.

    ``asserted_spans`` must be a list of strings (one bare string counts as
    one span). An empty span list is a valid observation ("the answer
    asserts nothing from the documents"); a stance map alone is not.
    """
    # The per-document map, when given, must be a mapping whose entries are
    # flags or {rejected, evidence} with text evidence: "rejected": [1] or
    # "evidence": [...] is a malformed reply, not an execution failure.
    for key in ("rejected", "stance"):
        mapping = judgment.get(key)
        if mapping in (None, {}):
            continue
        if not isinstance(mapping, dict):
            return False
        for entry in mapping.values():
            if isinstance(entry, dict):
                if not isinstance(entry.get("evidence"), (str, type(None))):
                    return False
            elif not isinstance(entry, (bool, str, type(None))):
                return False
    spans = judgment.get("asserted_spans")
    if isinstance(spans, str):
        return True
    # A stance map alone is not enough: derive_stance attributes claims only
    # from asserted_spans and reads `stance` merely as the rejected map, so a
    # stance-only reply ("relied_on" on the superseded document) would derive
    # every document as ignored — a pass.
    return isinstance(spans, list) and all(isinstance(x, str) for x in spans)


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

    Correctness next to provenance. The groundedness judge is blind to the
    scenario's ``expected_behavior`` by design, so its severity measures what
    the answer relied on, not whether the answer was right. When a marked
    scenario also carries ``expected_behavior``, the runner makes a second
    judge call with the ``correctness_judge`` config (``"checklist"`` by
    default) on the same single exchange and combines the two with
    :func:`combine_judgments`: the stricter severity wins, both halves are kept
    under ``provenance`` and ``correctness``. Pass ``correctness_judge=None``
    to get the provenance judgment alone.
    """

    def __init__(self, *args: Any, correctness_judge: Optional[str] = "checklist", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if correctness_judge is not None:
            config = get_judge(correctness_judge)  # fail at construction on an unknown name
            # The correctness half is a model judgment on the expectations: a
            # code-graded judge (choice_match) would send its placeholder
            # prompt to the model, and a grounding judge needs marks it is
            # never given here.
            if config.get("grade") is not None or config.get("output") in ("grounding", "decision"):
                raise ValueError(
                    f"correctness_judge={correctness_judge!r} cannot grade correctness: "
                    "use a model judge such as 'checklist'"
                )
        self.correctness_judge = correctness_judge

    async def _judge_correctness(
        self,
        description: str,
        conversation: List[Dict[str, Any]],
        expected_behavior: List[str],
        scenario: Dict[str, Any],
        *,
        params: Optional[Dict[str, Any]] = None,
        evidence_spans: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple:
        """Grade the same exchange against the scenario's expectations.

        The correctness judge is shown the expectations on purpose: they are
        its rubric. It is never shown the marks: the conversation entry carries
        the document text only, and the description is the scenario's own.
        ``params`` and ``evidence_spans`` are the ones the groundedness call
        gets, so judge params apply to both halves.
        """
        config = get_judge(self.correctness_judge)
        return await self._judge_conversation_async(
            self.judge_client,
            self.judge_model,
            description,
            conversation,
            expected_behavior,
            judge_prompt=config["judge_prompt"],
            json_format=self.json_format,
            # Scoring notes qualify the expectations, which this judge sees.
            judge_notes=as_str_list((scenario.get("metadata") or {}).get("judge_notes")),
            response_schema=config.get("response_schema"),
            max_retries=self.max_retries,
            retry_backoff=self.retry_backoff,
            postprocess=inherited_postprocess(config, False),
            scenario_meta={
                "severity": scenario.get("severity"),
                "category": scenario.get("category"),
                "metadata": scenario.get("metadata") or {},
            },
            params=params,
            evidence_spans=evidence_spans,
        )

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
            # The config's criteria ride along, so a judge made with
            # customize_judge() keeps them and only the format is rebuilt.
            judge_prompt, _active = build_prompt({**context, "criteria": config.get("criteria")})

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

    async def _run_one_scenario(
        self,
        scenario: Dict[str, Any],
        *,
        params: Optional[Dict[str, Any]] = None,
        target_params: Optional[Dict[str, Any]] = None,
        judge_params: Optional[Dict[str, Any]] = None,
        on_turn: Optional[OnTurn] = None,
        evidence_spans: Optional[List[Dict[str, Any]]] = None,
        audit_run_id: Optional[str] = None,
        trace_correlation: Optional[Any] = None,
        evidence_resolver: Optional[Callable[[ScenarioExecution], Union[List[Dict[str, Any]], None]]] = None,
    ) -> AuditResult:
        """Run one scenario as a single exchange and judge the result.

        Args:
            scenario: A scenario dict. ``test_prompt`` is required — it is the
                probe, and there is no auditor model to write one instead.
                ``documents``, ``as_of`` and ``file_uri`` are optional.
            params, target_params, judge_params, on_turn, evidence_spans,
            audit_run_id, trace_correlation, evidence_resolver: As for
                ``ModelAuditor.run_scenario``. The exchange is reported to
                ``on_turn`` as turn 0 of 1.

        Returns:
            An AuditResult whose conversation is the two messages that were
            actually exchanged.

        Raises:
            ValueError: If the scenario has no ``test_prompt``.
        """
        name = scenario["name"]
        description = scenario.get("description", "")
        test_prompt = scenario.get("test_prompt")
        # Same decision handling as ModelAuditor.run_scenario: validated, asked
        # as text when there is no test_prompt, sent to the target without its
        # answer key, and handed to the judge's code through scenario_meta.
        decision = scenario.get("decision")
        if decision is not None:
            decision = validate_decision(decision)
            if not test_prompt:
                test_prompt = render_decision_prompt(decision)
        if not test_prompt:
            # As ModelAuditor: a target that needs no question (ASRTarget's
            # "Transcribe the recording.") supplies its own.
            test_prompt = getattr(self.target, "default_prompt", None)
        # As ModelAuditor.run_scenario: judges see the texts, the checklist
        # post-processor gets the flags through scenario_meta.
        expectations = normalize_expectations(scenario.get("expected_behavior"))
        expected_behavior = (
            expectation_texts(expectations) if scenario.get("expected_behavior") is not None else None
        )
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
        # Targets must see document TEXT only. The marks (relevant / true /
        # decisive / valid_until / authority) are evaluation-only ground truth;
        # strip them here, so no target can read them. The built-in targets
        # also reduce documents to text themselves, but a custom target may
        # forward `documents` as it gets them.
        target_documents = [mark.text for mark in marks] if marks else None
        as_of = parse_as_of(scenario)
        derivations = derive_all(marks, as_of)

        # Same layering as ModelAuditor.run_scenario: construction-time params,
        # then per-call ones, with the role-specific dict over the shared one.
        base = {**(self.params or {}), **(params or {})}
        effective_target = {**base, **(self.target_params or {}), **(target_params or {})}
        effective_judge = {**base, **(self.judge_params or {}), **(judge_params or {})}
        effective_on_turn = on_turn if on_turn is not None else self.on_turn
        scenario_run_id = f"scen_{new_trace_id()[:12]}"
        scenario_trace_id = new_trace_id()
        turn_id = f"{scenario_run_id}_t1"

        self._log(f"--- Started Scenario (single-turn): {name} ---")
        prompt_preview = test_prompt[:80] + "..." if len(test_prompt) > 80 else test_prompt
        self._log(f"PROMPT: {prompt_preview}", name=name)

        # The stored turn carries the document TEXT the target was sent (never
        # the marks, whose parsed dates would not survive AuditResults.save()
        # and which are the judge's hidden ground truth). The correctness judge,
        # rejudge and reframing_check read the documents from here, as they do
        # on ModelAuditor; the blind groundedness call reads them from its
        # briefing instead and gets the turn without them (see below).
        entry: Dict[str, Any] = {"role": "user", "content": test_prompt}
        if target_documents:
            entry["documents"] = list(target_documents)
        if file_uri:
            entry["file_uri"] = _stored_file_uri(file_uri)
        conversation: List[Dict[str, Any]] = [entry]

        judge_input_tokens = 0
        judge_output_tokens = 0
        target_input_tokens = 0
        target_output_tokens = 0
        # No auditor runs in a single-turn audit, so its token counters stay
        # zero. They exist only to complete the ScenarioExecution handed to an
        # evidence_resolver.
        auditor_input_tokens = 0
        auditor_output_tokens = 0

        # Same containment as run_scenario: a provider blip becomes one ERROR
        # result rather than an exception that discards the whole batch.
        error: Optional[str] = None
        error_phase: Optional[str] = None
        failure: Optional[Dict[str, Any]] = None
        current_role = "system"
        # What every judge hook gets, as on ModelAuditor (not the raw scenario).
        scenario_meta: Dict[str, Any] = {
            "severity": scenario.get("severity"),
            "category": scenario.get("category"),
            "metadata": scenario.get("metadata") or {},
        }
        if decision is not None:
            scenario_meta["decision"] = decision
        if expectations:
            scenario_meta["expectations"] = expectations
        try:
            # Same order as ModelAuditor.run_scenario: a scenario the judge
            # cannot grade fails before any target tokens are spent.
            validate = getattr(self.judge_postprocess, "validate_scenario", None)
            if validate is not None:
                validate(expected_behavior, scenario_meta)
            # Recorded before the call, as in ModelAuditor.run_scenario.
            if trace_correlation is not None:
                _link_scenario(trace_correlation, scenario_run_id, name)
                trace_correlation.record(turn_id, scenario_trace_id)
            # Through self.target, not target_client: an explicit Target (an
            # HTTP app, a callable) replaces the model client, and the context
            # carries the traceparent an instrumented target propagates.
            current_role = "target"
            target_resp = await self.target.send(
                system=self.system_prompt,
                user=test_prompt,
                file_uri=file_uri,
                documents=target_documents,
                params=effective_target or None,
                context=TargetContext(
                    audit_run_id=audit_run_id,
                    scenario_run_id=scenario_run_id,
                    turn_id=turn_id,
                    trace_headers={"traceparent": make_traceparent(scenario_trace_id)},
                    extra={"decision": public_decision(decision)} if decision is not None else {},
                ),
            )
            target_input_tokens = token_count(target_resp.input_tokens) or 0
            target_output_tokens = token_count(target_resp.output_tokens) or 0
            response = (
                _content_to_text(target_resp.content)
                if getattr(self.target, "preserve_content", False)
                else ModelAuditor.strip_thinking(target_resp.content)
            )
            self._fire_on_turn(0, 1, "target", effective_on_turn)
            # Same empty-reply rule as ModelAuditor.run_scenario: custom
            # targets bypass _call_async, and an empty reply is a failed call,
            # not an answer to grade.
            if (
                self.empty_response == "error"
                # A target whose empty reply is an answer (ASR silence) is graded.
                and not getattr(self.target, "empty_is_answer", False)
                and (response is None or (isinstance(response, str) and not response.strip()))
                and getattr(target_resp, "decision", None) is None
                and not getattr(target_resp, "tool_events", None)
            ):
                raise EmptyResponseError("Target returned an empty response")
            response_preview = response[:80] + "..." if len(response) > 80 else response
            self._log(f"TARGET: {response_preview}", name=name)
            reply: Dict[str, Any] = {"role": "assistant", "content": response}
            if getattr(target_resp, "decision", None) is not None:
                reply["decision"] = _json_safe(target_resp.decision)
            tool_events = getattr(target_resp, "tool_events", None)
            if tool_events:
                # Kept so the judge sees TOOL ACTIVITY and the saved result
                # records what the agent did, as in ModelAuditor.
                reply["tool_events"] = _stored_tool_events(tool_events)
            conversation.append(reply)
        except Exception as exc:
            error = describe_exception(exc)
            error_phase = "conversation"
            failure = self._failure_for(current_role, exc, turn=1)
            self._log(f"--- Scenario FAILED on the target call: {name} [{error}] ---")

        if error is None:
            # Same evidence-resolution contract as ModelAuditor.run_async: an
            # optional resolver maps the (single-turn) execution to a list of
            # spans that replace the static evidence_spans for judging. Without
            # one, the static evidence_spans are used verbatim.
            resolved_evidence = evidence_spans
            resolved_agentic: Optional[Dict[str, Any]] = None
            resolved_guidance: Optional[str] = None
            if evidence_resolver is not None:
                execution = ScenarioExecution(
                    scenario_name=name,
                    scenario_description=description,
                    conversation=conversation,
                    # The same scenario_meta ModelAuditor hands a resolver.
                    scenario_meta=scenario_meta,
                    expected_behavior=expected_behavior,
                    trace_correlation=trace_correlation,
                    audit_run_id=audit_run_id,
                    error=None,
                    target_input_tokens=target_input_tokens,
                    target_output_tokens=target_output_tokens,
                    auditor_input_tokens=auditor_input_tokens,
                    auditor_output_tokens=auditor_output_tokens,
                )
                try:
                    resolver_result = evidence_resolver(execution)
                    if inspect.isawaitable(resolver_result):
                        resolver_result = await resolver_result
                    # Same contract as ModelAuditor: a plain span list, or a
                    # dict with selected_spans / agentic_evaluation /
                    # agentic_guidance.
                    if isinstance(resolver_result, dict):
                        resolved_evidence = resolver_result.get("selected_spans", evidence_spans)
                        resolved_agentic = resolver_result.get("agentic_evaluation")
                        resolved_guidance = resolver_result.get("agentic_guidance")
                    else:
                        resolved_evidence = resolver_result
                except Exception as exc:
                    self._log(f"--- Evidence resolution failed: {describe_exception(exc)} ---")
                    resolved_evidence = evidence_spans

            self._log("Judging response...", name=name)
            judge_spec_context = {
                "marks": marks,
                "as_of": as_of,
                "derivations": derivations,
            }
            judge_prompt, response_schema = self._judge_spec(judge_spec_context)
            # Same judge contract as ModelAuditor.run_scenario: the judge's
            # post-processor, the default-judge fallback for scenarios without
            # expectations, judge notes, and code-graded judges. The one
            # exception is groundedness, whose findings are derived below from
            # the marks; its postprocess exists only to flag paths that cannot
            # do that, so it is not applied here.
            judge_prompt, response_schema, postprocess, fell_back = self._resolve_judge_spec(
                judge_prompt,
                response_schema,
                self.judge_postprocess,
                self.judge_requires_expected_behavior,
                expected_behavior,
            )
            # Only with marks can the findings be derived here; without them the
            # groundedness hook stays on and reports the scenario as ERROR,
            # exactly as on ModelAuditor.
            grounded = postprocess is postprocess_groundedness and bool(marks)
            if grounded:
                postprocess = None
            if fell_back:
                self._warn_no_expectations(name)
            try:
                if self.judge_grade is not None and not fell_back:
                    # Graded in code (judges/choice_match.py): no judge call, no tokens.
                    judgment = self.judge_grade(
                        conversation=conversation,
                        expected_behavior=expected_behavior,
                        scenario_meta=scenario_meta,
                    )
                    j_in = j_out = 0
                else:
                    judgment, j_in, j_out = await self._judge_conversation_async(
                        self.judge_client,
                        self.judge_model,
                        _build_judge_context(description, marks, as_of, derivations),
                        # The briefing already lists the documents; strip
                        # them from the turn so the judge does not read them
                        # twice.
                        [
                            {k: v for k, v in m.items() if k != "documents"}
                            for m in conversation
                        ] if marks else conversation,
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
                        # Withheld from the blind groundedness judge like the
                        # expectations: notes can name the trap too. The
                        # correctness judge gets them (see _judge_correctness).
                        judge_notes=None if marks else as_str_list((scenario.get("metadata") or {}).get("judge_notes")),
                        response_schema=response_schema,
                        judge_fields=self.judge_fields,
                        max_retries=self.max_retries,
                        retry_backoff=self.retry_backoff,
                        postprocess=postprocess,
                        scenario_meta=scenario_meta,
                        params=effective_judge or None,
                        evidence_spans=resolved_evidence,
                        agentic_evaluation=resolved_agentic,
                        agentic_guidance=resolved_guidance,
                        require_content=self.empty_response == "error",
                    )
                judge_input_tokens += j_in
                judge_output_tokens += j_out
                if fell_back and isinstance(judgment, dict):
                    judgment["judge_fallback"] = "default"
                if (
                    marks and grounded and isinstance(judgment, dict)
                    and not is_upstream_error(judgment)
                    and not _has_grounding_observations(judgment)
                ):
                    # The grade is derived from the observations alone; a reply
                    # without usable ones (missing, null, wrong type) must not
                    # be graded from a stray severity/score, nor read as
                    # "every document ignored", which derives pass.
                    judgment = {
                        **{k: v for k, v in judgment.items() if k not in ("severity", "score")},
                        "severity": "ERROR",
                        "parse_error": True,  # the framework's verdict, not the judge's
                        "issues_found": [
                            "Judge returned no usable grounding observations "
                            "(asserted_spans must be a list of quoted strings)"
                        ],
                    }
                # The judge reports what the response did with each document;
                # the findings and the severity are worked out here. Keeping
                # the raw stance alongside them means a disputed finding can
                # be traced back to the observation it came from without
                # re-running the judge.
                # Derived only for the groundedness judge — decided by the
                # configured judge, never by the reply's keys: a stray
                # "asserted_spans" in a safety or score judge's reply would
                # otherwise throw away its grade.
                if grounded and not is_upstream_error(judgment):
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
                    # Provenance says what the answer leaned on; it cannot say
                    # whether the answer was right. Ask that of the checklist
                    # judge on the same exchange and keep both.
                    if expected_behavior and self.correctness_judge:
                        correctness, c_in, c_out = await self._judge_correctness(
                            description, conversation, expected_behavior, scenario,
                            params=effective_judge or None,
                            evidence_spans=resolved_evidence,
                        )
                        judge_input_tokens += c_in
                        judge_output_tokens += c_out
                        judgment = combine_judgments(judgment, correctness)
                self._fire_on_turn(0, 1, "judge", effective_on_turn)
            except Exception as exc:
                error = describe_exception(exc)
                error_phase = "judge"
                failure = self._failure_for("judge", exc)
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
                "error_phase": error_phase,
                "failure": failure,
            }
        else:
            judgment = coerce_judgment_fields(judgment)
        _keep_grading_metadata(judgment, {"metadata": scenario.get("metadata") or {}})

        # Same reading as ModelAuditor: an explicit severity, else one derived
        # from a 1-10 score, else ERROR (the judge did not grade).
        severity = judgment_severity(judgment)
        issues_found = ungradable_issues(judgment, severity)
        self._log(f"--- Finished Scenario: {name} [Result: {severity.upper()}] ---")

        return AuditResult(
            scenario_name=name,
            scenario_description=description,
            conversation=conversation,
            severity=severity,
            issues_found=issues_found,
            positive_behaviors=judgment.get("positive_behaviors", []),
            summary=judgment.get("summary", ""),
            recommendations=judgment.get("recommendations", []),
            expected_behavior=expected_behavior,
            judgment=judgment,
            expectations=objects_worth_storing(scenario.get("expected_behavior"), expectations),
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
        params: Optional[Dict[str, Any]] = None,
        target_params: Optional[Dict[str, Any]] = None,
        judge_params: Optional[Dict[str, Any]] = None,
        auditor_params: Optional[Dict[str, Any]] = None,
        on_turn: Optional[OnTurn] = None,
        evidence_spans: Optional[List[Dict[str, Any]]] = None,
        audit_run_id: Optional[str] = None,
        trace_correlation: Optional[Any] = None,
        evidence_resolver: Optional[Callable[[ScenarioExecution], Union[List[Dict[str, Any]], None]]] = None,
        shared_expectations: Optional[Sequence[Any]] = None,
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
            params, target_params, judge_params, on_turn, evidence_spans,
            audit_run_id, trace_correlation, shared_expectations: As for
                ``ModelAuditor.run_async``.
            auditor_params: Accepted for the same drop-in reason and ignored —
                there is no auditor call.

        Returns:
            AuditResults over every scenario, in the order they were given.
        """
        if max_workers < 1:
            raise ValueError(
                f"max_workers must be >= 1, got {max_workers} "
                "(a semaphore of 0 permits would deadlock the run)"
            )
        audit_run_id = audit_run_id or f"audit_{new_trace_id()[:12]}"
        # Cached on URI alone, so a file regenerated between two audits in one
        # process would otherwise be replayed from its old bytes.
        image_data_uri.cache_clear()
        scenario_list = (
            self.get_scenarios(scenarios) if isinstance(scenarios, str) else scenarios
        )
        if shared_expectations:
            scenario_list = apply_shared_expectations(scenario_list, shared_expectations)
        for scenario in scenario_list:
            try:
                normalize_expectations(scenario.get("expected_behavior"))
            except ValueError as exc:
                raise ValueError(f"Scenario {scenario.get('name')!r}: {exc}") from None

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
                    result = await self._run_one_scenario(
                        scenario,
                        params=params,
                        target_params=target_params,
                        judge_params=judge_params,
                        on_turn=on_turn,
                        evidence_spans=evidence_spans,
                        audit_run_id=audit_run_id,
                        trace_correlation=trace_correlation,
                        evidence_resolver=evidence_resolver,
                    )
                except Exception as exc:
                    # Don't let one failing scenario abort the whole batch and
                    # discard every other (possibly expensive) result. Record an
                    # ERROR result instead. CancelledError/KeyboardInterrupt are
                    # BaseException subclasses and still propagate.
                    name = scenario.get("name", "<unknown>")
                    self._log(f"--- Scenario FAILED: {name} [{describe_exception(exc)}] ---")
                    result = AuditResult(
                        scenario_name=name,
                        scenario_description=scenario.get("description", ""),
                        conversation=[],
                        severity="ERROR",
                        issues_found=[f"Scenario execution failed: {describe_exception(exc)}"],
                        positive_behaviors=[],
                        summary=f"Scenario did not complete due to an error: {describe_exception(exc)}"[:500],
                        recommendations=["Re-run this scenario; check API credentials, rate limits, and connectivity."],
                        expected_behavior=_safe_texts(scenario.get("expected_behavior")),
                        judgment={
                            "severity": "ERROR",
                            "error": describe_exception(exc),
                            "failure": build_failure("system", exc),
                        },
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

        # max_turns is accepted and ignored here, so it is not validated either.
        meta = self._run_meta(scenarios, None, language)
        meta["max_turns"] = 1  # one exchange per scenario, whatever was requested
        return AuditResults(results, meta=meta)
