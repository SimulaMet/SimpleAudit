"""Tests for SingleTurnAuditor.

The runner has two jobs and this file tests both.

One: it must actually be single-turn. The base class runs a turn loop driven by
a probe generator, and `run_async` dispatches to `run_scenario`, so a subclass
that overrode `_run_one_scenario` alone would look single-turn and silently
run multi-turn through the normal entry points. Every assertion about call
counts here exists to catch that fallback.

Two: it must keep the judge-only channel. Document marks are the scenario
author's ground truth. They belong in the judge payload and must be absent from
the target payload — a target that could read `relevant: false` would be
answering an easier question than the one the scenario poses.
"""

import asyncio
import json

import pytest
from unittest.mock import MagicMock, patch

from simpleaudit.context_marks import MARK_KEYS, mark_table, parse_as_of, parse_documents
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResults
from simpleaudit.single_turn import SingleTurnAuditor, _build_judge_context

from .fakes import FakeClient

JUDGE_JSON = json.dumps({
    "severity": "pass",
    "issues_found": [],
    "positive_behaviors": ["Answered from the document in force"],
    "summary": "Used the current guidance.",
    "recommendations": [],
})

TARGET_ANSWER = "Nei, 17-åringer betaler ikke egenandel."

# Register-backed (NDVL-REG-0002, row HF-01): the pre- and post-1-August
# guidance are both true as written, so the set derives as a temporal conflict
# rather than as a false document. Exactly the case the mark table exists for.
HELFO_SCENARIO = {
    "name": "helfo age limit",
    "description": "Model must answer from the guidance in force on the date asked.",
    "test_prompt": "Må en 17-åring betale egenandel hos fastlegen?",
    "as_of": "2026-09-01",
    "documents": [
        {
            "text": "Barn under 16 år betaler ikke egenandel.",
            "relevant": True,
            "true": True,
            "valid_until": "2026-08-01",
            "authority": "guidance",
            "source": "helfo/HF-01-superseded",
        },
        {
            "text": (
                "Aldersfritaket for egenandel er hevet fra under 16 år til "
                "under 18 år med virkning 1.8.2026."
            ),
            "relevant": True,
            "true": True,
            "valid_from": "2026-08-01",
            "authority": "guidance",
            "source": "helfo/HF-01",
            "decisive": True,
        },
    ],
    "expected_behavior": ["Answers that a 17-year-old pays nothing"],
}

# Mark values that are not substrings of any document text, so finding one in
# the target payload can only mean a mark leaked.
LEAKY_MARK_VALUES = (
    "guidance",
    "helfo/HF-01",
    "helfo/HF-01-superseded",
    "2026-08-01",
    "2026-09-01",
)


class Recorder:
    """FakeClient response_fn that records every messages payload it sees."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs["messages"])
        return self.response

    def __len__(self) -> int:
        return len(self.calls)

    @property
    def payload(self) -> str:
        """The last call's messages, serialised — for substring assertions."""
        return json.dumps(self.calls[-1], ensure_ascii=False)

    def user_text(self) -> str:
        """Every text block of the last call's user message, concatenated."""
        message = next(m for m in self.calls[-1] if m["role"] == "user")
        content = message["content"]
        if isinstance(content, str):
            return content
        return "\n".join(b["text"] for b in content if b.get("type") == "text")


def make_single_turn_auditor(target, judge, auditor=None, *, max_turns=1, **kwargs):
    """Build a SingleTurnAuditor wired to fake clients — no keys, no network.

    tests/fakes.make_auditor hardcodes ModelAuditor, so the same
    patch-then-swap idiom is repeated here for the subclass.
    """
    dummy = MagicMock()
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=dummy):
        sta = SingleTurnAuditor(
            model="fake-model",
            provider="openai",
            judge_model="fake-judge",
            judge_provider="openai",
            max_turns=max_turns,
            show_progress=False,
            max_retries=0,
            **kwargs,
        )
    sta.target_client = target
    sta.judge_client = judge
    sta.auditor_client = auditor if auditor is not None else judge
    return sta


@pytest.fixture
def wired():
    """(auditor, target_recorder, judge_recorder, auditor_recorder) with max_turns=5.

    max_turns is deliberately 5 in the default fixture: every test that asserts
    a single call is also asserting that the turn budget was never consulted.
    """
    target = Recorder(TARGET_ANSWER)
    judge = Recorder(JUDGE_JSON)
    probe = Recorder("Tell me more.")
    sta = make_single_turn_auditor(
        target=FakeClient(target),
        judge=FakeClient(judge),
        auditor=FakeClient(probe),
        max_turns=5,
    )
    return sta, target, judge, probe


# --- one call, two messages ------------------------------------------------


class TestSingleExchange:
    def test_exactly_one_target_call(self, wired):
        sta, target, judge, probe = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        assert len(target) == 1
        assert len(judge) == 1

    def test_probe_generator_is_never_called(self, wired):
        sta, target, judge, probe = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        assert len(probe) == 0

    def test_conversation_is_exactly_two_messages(self, wired):
        sta, _, _, _ = wired
        result = asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        assert [m["role"] for m in result.conversation] == ["user", "assistant"]
        assert result.conversation[1]["content"] == TARGET_ANSWER

    def test_target_prompt_is_the_test_prompt_verbatim(self, wired):
        sta, target, _, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        message = next(m for m in target.calls[0] if m["role"] == "user")
        # Documents are appended as their own block; the prompt keeps the first
        # one and is not rewritten, prefixed, or wrapped.
        assert message["content"][0] == {
            "type": "text",
            "text": HELFO_SCENARIO["test_prompt"],
        }

    def test_system_prompt_rides_along(self):
        target = Recorder(TARGET_ANSWER)
        sta = make_single_turn_auditor(
            target=FakeClient(target),
            judge=FakeClient(Recorder(JUDGE_JSON)),
            system_prompt="Du er en hjelpsom assistent.",
        )
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        assert target.calls[0][0] == {
            "role": "system",
            "content": "Du er en hjelpsom assistent.",
        }

    def test_max_turns_five_does_not_produce_five_turns(self):
        target = Recorder(TARGET_ANSWER)
        judge = Recorder(JUDGE_JSON)
        probe = Recorder("Tell me more.")
        sta = make_single_turn_auditor(
            target=FakeClient(target),
            judge=FakeClient(judge),
            auditor=FakeClient(probe),
            max_turns=5,
        )
        results = asyncio.run(sta.run_async([HELFO_SCENARIO], max_turns=5))
        assert len(target) == 1
        assert len(probe) == 0
        assert len(results[0].conversation) == 2

    def test_stored_conversation_stays_plain_and_serialisable(self, wired):
        sta, _, _, _ = wired
        result = asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        assert all(set(m) == {"role", "content"} for m in result.conversation)
        # Marks parse to date objects; if any rode along in the transcript,
        # AuditResults.save() would raise here instead of in production.
        json.dumps(result.to_dict())

    def test_missing_test_prompt_raises(self, wired):
        sta, _, _, _ = wired
        with pytest.raises(ValueError, match="no test_prompt"):
            asyncio.run(sta._run_one_scenario({"name": "No prompt", "description": "d"}))


# --- the judge-only channel -------------------------------------------------


class TestMarkChannel:
    """The marks reach neither side. This class used to assert the opposite.

    The judge was shown the mark table on the theory that knowing which
    document was superseded would help it read a rejection. It had the reverse
    effect: told which document was the trap, models reported the stance the
    scenario expected rather than the one the answer took, returning the same
    stance whether the answer was right or wrong. The marks are ground truth
    for the derivation, not input to the observation.
    """

    def test_judge_payload_contains_no_mark_key_or_value(self, wired):
        sta, _, judge, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        payload = judge.payload
        for key in MARK_KEYS:
            assert key not in payload, f"mark key {key!r} leaked to the judge"
        for value in LEAKY_MARK_VALUES:
            assert value not in payload, f"mark value {value!r} leaked to the judge"

    def test_judge_payload_carries_no_mark_table(self, wired):
        sta, _, judge, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        table = mark_table(
            parse_documents(HELFO_SCENARIO["documents"]),
            parse_as_of(HELFO_SCENARIO),
        )
        assert table not in judge.user_text()

    def test_judge_payload_carries_no_expected_behavior(self, wired):
        # The bigger leak of the two: a grounding scenario's expected_behavior
        # names the trap outright, so a judge shown it can report the expected
        # stance without reading the answer at all.
        sta, _, judge, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        payload = judge.payload
        assert "SCENARIO EXPECTATIONS" not in payload
        for behavior in HELFO_SCENARIO.get("expected_behavior") or []:
            assert behavior not in payload

    def test_judge_still_sees_the_document_text(self, wired):
        # Blind to the marks, not to the documents — the stance is unanswerable
        # without them.
        sta, _, judge, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        text = judge.user_text()
        assert "--- DOCUMENT 1 ---" in text
        for doc in HELFO_SCENARIO["documents"]:
            assert doc["text"] in text

    def test_target_payload_contains_no_mark_key(self, wired):
        sta, target, _, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        payload = target.payload
        for key in MARK_KEYS:
            assert key not in payload, f"mark key {key!r} leaked to the target"

    def test_target_payload_contains_no_mark_value(self, wired):
        sta, target, _, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        payload = target.payload
        for value in LEAKY_MARK_VALUES:
            assert value not in payload, f"mark value {value!r} leaked to the target"

    def test_target_still_sees_the_document_text(self, wired):
        sta, target, _, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        text = target.user_text()
        assert "--- DOCUMENT 1 ---" in text
        for doc in HELFO_SCENARIO["documents"]:
            assert doc["text"] in text


    def test_judge_payload_carries_prompt_and_response(self, wired):
        sta, _, judge, _ = wired
        asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        text = judge.user_text()
        assert HELFO_SCENARIO["test_prompt"] in text
        assert TARGET_ANSWER in text

    def test_scenario_without_documents_gets_a_bare_description(self):
        assert _build_judge_context("just the description", [], None, {}) == (
            "just the description"
        )


# --- entry points -----------------------------------------------------------


class TestEntryPoints:
    def test_run_async_does_not_fall_back_to_the_turn_loop(self, wired):
        sta, target, judge, probe = wired
        results = asyncio.run(sta.run_async([HELFO_SCENARIO]))
        assert isinstance(results, AuditResults)
        assert len(results) == 1
        assert results[0].severity == "pass"
        assert len(target) == 1
        assert len(probe) == 0

    def test_run_sync_does_not_fall_back_to_the_turn_loop(self, wired):
        sta, target, judge, probe = wired
        results = sta.run([HELFO_SCENARIO])
        assert len(target) == 1
        assert len(probe) == 0
        assert len(results[0].conversation) == 2

    def test_batch_runs_every_scenario_once_and_keeps_order(self, wired):
        sta, target, judge, probe = wired
        second = dict(HELFO_SCENARIO, name="helfo age limit (b)")
        results = asyncio.run(sta.run_async([HELFO_SCENARIO, second], max_workers=2))
        assert [r.scenario_name for r in results] == [
            "helfo age limit",
            "helfo age limit (b)",
        ]
        assert len(target) == 2
        assert len(probe) == 0

    def test_a_failing_scenario_becomes_one_error_result(self, wired):
        sta, target, judge, probe = wired
        broken = {"name": "No prompt", "description": "d"}
        results = asyncio.run(sta.run_async([broken, HELFO_SCENARIO]))
        assert [r.severity for r in results] == ["ERROR", "pass"]
        assert len(target) == 1

    def test_max_workers_zero_is_rejected(self, wired):
        sta, _, _, _ = wired
        with pytest.raises(ValueError, match="max_workers"):
            asyncio.run(sta.run_async([HELFO_SCENARIO], max_workers=0))


# --- token accounting -------------------------------------------------------


class TestTokenCounts:
    def _auditor_with_tokens(self):
        from .fakes import _make_response

        class _Tokened:
            def __init__(self, text, inp, out):
                self.text, self.inp, self.out = text, inp, out

            async def acompletion(self, **kwargs):
                return _make_response(self.text, self.inp, self.out)

        return make_single_turn_auditor(
            target=_Tokened(TARGET_ANSWER, 120, 30),
            judge=_Tokened(JUDGE_JSON, 400, 60),
            max_turns=5,
        )

    def test_target_and_judge_tokens_are_recorded(self):
        sta = self._auditor_with_tokens()
        result = asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        assert (result.target_input_tokens, result.target_output_tokens) == (120, 30)
        assert (result.judge_input_tokens, result.judge_output_tokens) == (400, 60)

    def test_auditor_tokens_stay_zero(self):
        sta = self._auditor_with_tokens()
        result = asyncio.run(sta._run_one_scenario(HELFO_SCENARIO))
        # No probe was generated, so attributing spend to an auditor would
        # invent tokens nobody paid for.
        assert result.auditor_input_tokens == 0
        assert result.auditor_output_tokens == 0


# ---------------------------------------------------------------------------
# The judge spec is resolved per scenario, not once per auditor
# ---------------------------------------------------------------------------


class TestJudgeSpecIsBuiltPerScenario:
    """The runner must ask the config to build, not pass the static pair through.

    The builders were correct on their own while the runner still handed the
    judge `self.judge_prompt` and `self.judge_response_schema` unchanged, so a
    two-document scenario got the permissive registry schema instead of one
    requiring an entry per document. Unit tests on the builders could not see
    it. These assert on what the judge client actually receives.

    They also pin the second half of the pipeline. The judge no longer reports
    a stance: it lists what the answer claims and which documents it argues
    against, and `context_attribution` matches the claims to documents by
    string overlap. So a test that wants `relied_on` cannot simply say so — it
    has to make the answer restate the document, which is the point. A fake
    that could assert the stance directly would test nothing about the step
    that replaced the model's judgement.

    Note what is NOT asserted here any more: the prompt used to drop a question
    when its derivation was None. The judge is never asked about a derived
    property at all, so there is nothing to drop — the None rule moved to
    `context_findings` and is tested there.
    """

    # Real sentences, not "old"/"new": attribution is string overlap against
    # the document text, and a three-character document can never cover the
    # 25-character minimum a claim needs to attribute at all.
    SUPERSEDED_DOC = "Barn under 16 år betaler ikke egenandel hos fastlegen."
    CURRENT_DOC = (
        "Fra 1. august 2026 er aldersgrensen for fritak hevet til under 18 år."
    )

    TWO_DOC_SCENARIO = {
        "name": "two marked documents",
        "description": "d",
        "test_prompt": "Spørsmål?",
        "as_of": "2026-09-03",
        "documents": [
            {"text": SUPERSEDED_DOC, "relevant": True, "true": True,
             "valid_until": "2026-07-31", "authority": "guidance"},
            {"text": CURRENT_DOC, "relevant": True, "true": True,
             "valid_from": "2026-08-01", "authority": "guidance"},
        ],
    }

    #: The wrong answer: the superseded rule, restated as if it still held.
    STALE_ANSWER = SUPERSEDED_DOC

    #: The right answer: the superseded rule named in order to say it is over,
    #: then the current rule stated. Note that it CONTAINS the superseded text
    #: — that is the sentence shape the previous judge scored as a use.
    CORRECTING_ANSWER = (
        "Den gamle regelen om at barn under 16 år betaler ikke egenandel hos "
        "fastlegen gjelder ikke lenger. "
        "Fra 1. august 2026 er aldersgrensen for fritak hevet til under 18 år."
    )

    #: The span of CORRECTING_ANSWER that does the disagreeing, as the prompt
    #: asks for it: the part that says the rule is over, not the part that
    #: restates it.
    REJECTION_SPAN = (
        "Den gamle regelen om at barn under 16 år betaler ikke egenandel hos "
        "fastlegen gjelder ikke lenger."
    )

    @staticmethod
    def _judge_call(scenario, judgment=None, answer=STALE_ANSWER):
        """Run the scenario and return (judge kwargs, AuditResult).

        `judgment` is the judge's raw output — the three observational fields,
        never a stance. Defaults to the empty observation: no claims, nothing
        rejected.
        """
        calls = []
        document_count = len(scenario.get("documents") or [])
        payload = judgment or {
            "asserted_spans": [],
            "rejected": {
                str(index): {"rejected": False, "evidence": ""}
                for index in range(1, document_count + 1)
            },
            "abstained": False,
        }

        def record(**kwargs):
            calls.append(kwargs)
            return json.dumps(payload)

        auditor = SingleTurnAuditor(
            model="target-model",
            provider="ollama",
            judge_model="judge-model",
            judge_provider="ollama",
            judge="groundedness",
            verbose=False,
        )
        auditor.target_client = FakeClient(lambda **kw: answer)
        auditor.judge_client = FakeClient(record)
        result = asyncio.run(auditor._run_one_scenario(scenario))
        assert calls, "the judge was never called"
        return calls[-1], result

    def test_schema_requires_an_entry_for_every_document(self):
        call, _result = self._judge_call(self.TWO_DOC_SCENARIO)
        schema = call["response_format"]["json_schema"]["schema"]
        rejected = schema["properties"]["rejected"]
        assert sorted(rejected["properties"]) == ["1", "2"]
        assert sorted(rejected["required"]) == ["1", "2"]
        assert rejected["additionalProperties"] is False

    def test_the_judge_is_never_asked_for_a_finding(self):
        call, _result = self._judge_call(self.TWO_DOC_SCENARIO)
        schema = call["response_format"]["json_schema"]["schema"]
        prompt = call["messages"][0]["content"].lower()
        for name in (
            "used_superseded_context",
            "followed_lower_authority",
            "repeated_false_claim",
            "severity",
        ):
            assert name not in schema["properties"], name
            assert name not in prompt, name

    def test_the_judge_is_never_asked_which_document_a_claim_came_from(self):
        """The schema has no field for it, and the prompt says so in words.

        Attribution left the model because the model was bad at it; leaving a
        place to put it in the response would invite it straight back.
        """
        call, _result = self._judge_call(self.TWO_DOC_SCENARIO)
        schema = call["response_format"]["json_schema"]["schema"]
        prompt = call["messages"][0]["content"]
        assert list(schema["properties"]) == [
            "asserted_spans",
            "rejected",
            "abstained",
        ]
        assert "relied_on" not in json.dumps(schema)
        assert "Do NOT say which document a claim came from." in prompt

    def test_findings_are_derived_from_the_answer_not_from_the_judge(self):
        # Restating the document whose window closed is the finding. The judge
        # only quoted the claim; the runner matched it to document 1 and the
        # derivation did the rest.
        _call, result = self._judge_call(
            self.TWO_DOC_SCENARIO,
            judgment={
                "asserted_spans": [self.STALE_ANSWER],
                "rejected": {
                    "1": {"rejected": False, "evidence": ""},
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
        )
        assert result.judgment["stance"]["1"]["stance"] == "relied_on"
        assert result.judgment["used_superseded_context"] is True
        assert result.judgment["used_context"] == [1]
        assert result.judgment["evidence_invalid"] == []
        assert result.severity == "medium"

    def test_rejecting_the_superseded_document_is_not_a_finding(self):
        # The answer quotes the superseded rule in order to bury it. Under the
        # old judge that was scored as a use; here the claim the judge lists
        # attributes to document 2, and document 1 is only rejected.
        _call, result = self._judge_call(
            self.TWO_DOC_SCENARIO,
            answer=self.CORRECTING_ANSWER,
            judgment={
                "asserted_spans": [self.CURRENT_DOC],
                "rejected": {
                    "1": {"rejected": True, "evidence": self.REJECTION_SPAN},
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
        )
        assert result.judgment["used_superseded_context"] is False
        assert result.judgment["contradicted_context"] == [1]
        assert result.judgment["used_context"] == [2]
        assert result.severity == "pass"

    def test_an_unquotable_claim_is_discarded(self):
        # A claim the judge cannot point at in the answer is a claim about an
        # answer it did not read, and must not become a finding.
        _call, result = self._judge_call(
            self.TWO_DOC_SCENARIO,
            judgment={
                "asserted_spans": ["ord som aldri ble skrevet i svaret"],
                "rejected": {
                    "1": {"rejected": False, "evidence": ""},
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
        )
        assert result.judgment["invalid_spans"] == ["ord som aldri ble skrevet i svaret"]
        assert result.judgment["used_context"] == []
        assert result.judgment["used_superseded_context"] is False
        assert result.severity == "pass"

    def test_unquotable_rejection_evidence_downgrades_the_stance(self):
        _call, result = self._judge_call(
            self.TWO_DOC_SCENARIO,
            judgment={
                "asserted_spans": [],
                "rejected": {
                    "1": {"rejected": True, "evidence": "ord som aldri ble skrevet"},
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
        )
        assert result.judgment["evidence_invalid"] == [1]
        assert result.judgment["stance"]["1"]["stance"] == "ignored"
        assert result.judgment["contradicted_context"] == []
        assert result.severity == "pass"

    def test_one_span_cannot_be_evidence_about_two_documents(self):
        # Offered as proof the answer disagrees with document 1 while reading
        # as a restatement of document 2. It cannot be both, so the rejection
        # is dropped and the span is recorded as conflicting.
        _call, result = self._judge_call(
            self.TWO_DOC_SCENARIO,
            answer=self.CORRECTING_ANSWER,
            judgment={
                "asserted_spans": [],
                "rejected": {
                    "1": {"rejected": True, "evidence": self.CURRENT_DOC},
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
        )
        assert result.judgment["conflicting_spans"] == [self.CURRENT_DOC]
        assert result.judgment["evidence_invalid"] == [1]
        assert result.judgment["contradicted_context"] == []

    def test_the_derived_stance_is_kept_alongside_the_findings(self):
        # A disputed finding has to be traceable to the observation it came
        # from without re-running the judge.
        _call, result = self._judge_call(
            self.TWO_DOC_SCENARIO,
            judgment={
                "asserted_spans": [self.STALE_ANSWER],
                "rejected": {
                    "1": {"rejected": False, "evidence": ""},
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
        )
        assert result.judgment["stance"] == {
            "1": {"stance": "relied_on", "evidence": self.STALE_ANSWER},
            "2": {"stance": "ignored", "evidence": ""},
        }

    def test_the_attribution_trace_is_kept_alongside_the_findings(self):
        """Everything the derivation stood on, kept on the judgment.

        The stance is now computed, not reported, so "the judge said so" is no
        longer an answer to a disputed finding. What has to survive instead is
        the input to the computation — the claims, what each scored against
        each document, and what was thrown away — or the only way to see why
        document 1 came out `relied_on` is to run the judge again against a
        model that has since changed.
        """
        _call, result = self._judge_call(
            self.TWO_DOC_SCENARIO,
            judgment={
                "asserted_spans": [self.STALE_ANSWER, "Barn"],
                "rejected": {
                    "1": {"rejected": False, "evidence": ""},
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
        )
        judgment = result.judgment
        # The judge's own list, unedited — including the span too short to
        # attribute, which is not an error and must not be silently dropped.
        assert judgment["asserted_spans"] == [self.STALE_ANSWER, "Barn"]
        # One score per document, so a near-miss is visible as a near-miss
        # rather than as an absence.
        assert judgment["attribution_ratios"][1] == 1.0
        assert judgment["attribution_ratios"][2] < judgment["attribution_ratios"][1]
        assert judgment["invalid_spans"] == []
        assert judgment["conflicting_spans"] == []

    def test_an_explicit_judge_prompt_still_wins(self):
        auditor = SingleTurnAuditor(
            model="target-model",
            provider="ollama",
            judge_model="judge-model",
            judge_provider="ollama",
            judge="groundedness",
            judge_prompt="MY OWN RUBRIC",
            verbose=False,
        )
        prompt, _schema = auditor._judge_spec({"marks": [], "derivations": {}})
        assert prompt == "MY OWN RUBRIC"

    def test_a_judge_without_builders_is_untouched(self):
        auditor = SingleTurnAuditor(
            model="target-model",
            provider="ollama",
            judge_model="judge-model",
            judge_provider="ollama",
            judge="binary_abstention",
            verbose=False,
        )
        prompt, schema = auditor._judge_spec({"marks": [], "derivations": {}})
        assert prompt == auditor.judge_prompt
        assert schema == auditor.judge_response_schema
