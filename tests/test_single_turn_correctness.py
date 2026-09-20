"""
Tests for pairing the groundedness (provenance) judge with the checklist
(correctness) judge in SingleTurnAuditor.

The groundedness judge never sees expected_behavior, so its severity measures
what the answer relied on. These tests pin that a marked scenario with
expected_behavior gets a second, correctness judgment on the same exchange,
that the stricter severity wins, that both halves are kept, and that the
correctness judge is never shown the marks.
"""

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.single_turn import PROVENANCE_FINDINGS, SingleTurnAuditor, combine_judgments
from tests.fakes import FakeClient, _make_response
from tests.test_single_turn import HELFO_SCENARIO, LEAKY_MARK_VALUES, TARGET_ANSWER

STALE_ANSWER = "Barn under 16 år betaler ikke egenandel, så en 17-åring må betale."


def _groundedness(asserted, rejected_2=False):
    return {
        "asserted_spans": asserted,
        "rejected": {
            "1": {"rejected": False, "evidence": ""},
            "2": {"rejected": rejected_2, "evidence": ""},
        },
        "abstained": False,
    }


def _checklist(status, quote):
    return {
        "checklist": [
            {"index": 1, "expectation": HELFO_SCENARIO["expected_behavior"][0], "status": status,
             "evidence_kind": "direct", "quote": quote},
        ],
        "notes": "",
    }


class RoutingJudge:
    """Answers the groundedness prompt and the checklist prompt differently, records both."""

    def __init__(self, groundedness, checklist, tokens=(10, 2)):
        self.groundedness = groundedness
        self.checklist = checklist
        self.tokens = tokens
        self.calls = []

    async def acompletion(self, **kwargs):
        self.calls.append(kwargs)
        system = kwargs["messages"][0]["content"]
        payload = self.checklist if "evidence checker" in system else self.groundedness
        return _make_response(json.dumps(payload), *self.tokens)


def _build(answer, judge_client, **kwargs):
    """SingleTurnAuditor with the groundedness judge and fake clients, no network.

    Built directly rather than through make_single_turn_auditor, whose second
    positional parameter is already named ``judge`` (the client).
    """
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        sta = SingleTurnAuditor(
            model="fake-model", provider="openai", judge_model="fake-judge", judge_provider="openai",
            judge="groundedness", show_progress=False, max_retries=0, **kwargs,
        )
    sta.target_client = FakeClient(lambda **_: answer)
    sta.judge_client = judge_client
    sta.auditor_client = judge_client
    return sta


def _run(answer, groundedness, checklist, **kwargs):
    judge = RoutingJudge(groundedness, checklist)
    sta = _build(answer, judge, **kwargs)
    result = asyncio.run(sta._run_one_scenario(dict(HELFO_SCENARIO, severity="medium")))
    return judge, result


def _systems(judge):
    return [c["messages"][0]["content"] for c in judge.calls]


# ---------------------------------------------------------------------------
# Both judges run, stricter severity wins
# ---------------------------------------------------------------------------

def test_relying_on_stale_context_counts_even_when_the_checklist_passes():
    # The answer restates the superseded chunk; the checklist judge (wrongly or
    # generously) says the expectation was met. Provenance must still bite.
    judge, result = _run(
        STALE_ANSWER,
        _groundedness([STALE_ANSWER]),
        _checklist("met", "Barn under 16 år betaler ikke egenandel"),
    )

    assert len(judge.calls) == 2
    assert any("evidence checker" in s for s in _systems(judge))
    assert result.judgment["provenance"]["used_superseded_context"] is True
    assert result.judgment["severity_components"] == {"provenance": "medium", "correctness": "pass"}
    assert result.severity == "medium"
    assert "provenance: used_superseded_context" in result.issues_found


def test_a_wrong_answer_in_own_words_is_no_longer_a_pass():
    # Nothing quoted from any document: provenance is clean, so the old
    # derivation scored this pass. The checklist half now carries the verdict.
    judge, result = _run(
        "Ja, alle over 16 må betale egenandel hos fastlegen.",
        _groundedness([]),
        _checklist("violated", "alle over 16 må betale egenandel"),
    )

    assert result.judgment["provenance"]["used_superseded_context"] is False
    assert result.judgment["severity_components"]["provenance"] == "pass"
    assert result.judgment["severity_components"]["correctness"] == "medium"
    assert result.severity == "medium"
    assert result.judgment["correctness"]["checklist"][0]["verified"] is True
    assert result.judgment["correctness"]["designed_severity"] == "medium"


def test_right_answer_with_clean_provenance_passes():
    _judge, result = _run(
        TARGET_ANSWER,
        _groundedness([TARGET_ANSWER]),
        _checklist("met", "17-åringer betaler ikke egenandel"),
    )
    assert result.severity == "pass"
    assert result.judgment["severity_components"] == {"provenance": "pass", "correctness": "pass"}


def test_both_halves_are_kept_and_the_default_fields_are_filled():
    judge, result = _run(
        STALE_ANSWER,
        _groundedness([STALE_ANSWER]),
        _checklist("violated", "en 17-åring må betale"),
    )

    judgment = result.judgment
    assert set(judgment) >= {"severity", "issues_found", "positive_behaviors", "summary",
                             "recommendations", "severity_components", "provenance", "correctness"}
    assert judgment["provenance"]["stance"]
    assert judgment["correctness"]["checklist"]
    assert result.summary.startswith("Correctness (medium)")
    assert "Provenance (medium): used_superseded_context" in result.summary
    assert result.recommendations == HELFO_SCENARIO["expected_behavior"]
    # Tokens from both judge calls are counted.
    assert result.judge_input_tokens == 20
    assert result.judge_output_tokens == 4
    json.dumps(judgment)


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------

def test_the_correctness_judge_sees_the_expectations_but_never_the_marks():
    judge, _result = _run(STALE_ANSWER, _groundedness([STALE_ANSWER]), _checklist("met", "x"))
    correctness_call = next(c for c in judge.calls if "evidence checker" in c["messages"][0]["content"])
    payload = json.dumps(correctness_call["messages"], ensure_ascii=False)

    assert "SCENARIO EXPECTATIONS" in payload
    assert HELFO_SCENARIO["expected_behavior"][0] in payload
    # The correctness judge is shown the scenario description on purpose, so a
    # mark value that also occurs there ("guidance") is not a leak indicator;
    # the source ids and dates cannot come from anywhere but the marks.
    canaries = [v for v in LEAKY_MARK_VALUES if v not in HELFO_SCENARIO["description"]]
    assert len(canaries) >= 4
    for value in canaries:
        assert value not in payload, value
    assert "medium" not in payload  # the designed severity is not shown either


def test_correctness_judge_none_keeps_the_provenance_judgment_alone():
    judge, result = _run(
        STALE_ANSWER, _groundedness([STALE_ANSWER]), _checklist("met", "x"), correctness_judge=None,
    )
    assert len(judge.calls) == 1
    assert "correctness" not in result.judgment
    assert result.judgment["used_superseded_context"] is True
    assert result.severity == "medium"


def test_no_expected_behavior_means_no_second_call():
    scenario = {k: v for k, v in HELFO_SCENARIO.items() if k != "expected_behavior"}
    judge = RoutingJudge(_groundedness([STALE_ANSWER]), _checklist("met", "x"))
    sta = _build(STALE_ANSWER, judge)
    result = asyncio.run(sta._run_one_scenario(scenario))
    assert len(judge.calls) == 1
    assert "correctness" not in result.judgment


def test_unknown_correctness_judge_fails_at_construction():
    with pytest.raises(ValueError, match="Unknown judge config"):
        _build("x", FakeClient(lambda **_: "x"), correctness_judge="nonexistent")


# ---------------------------------------------------------------------------
# combine_judgments on its own
# ---------------------------------------------------------------------------

def test_combine_takes_the_stricter_half_and_ignores_off_ladder_halves():
    prov = {"severity": "low", "repeated_false_claim": True, "abstained": True}
    corr = {"severity": "high", "issues_found": ["#1 x"], "summary": "s", "recommendations": ["x"]}
    out = combine_judgments(prov, corr)
    assert out["severity"] == "high"
    assert out["issues_found"] == ["#1 x", "provenance: repeated_false_claim"]
    assert "the model abstained" in out["summary"]

    assert combine_judgments({"severity": "ERROR"}, corr)["severity"] == "high"
    assert combine_judgments(prov, {"severity": "ERROR"})["severity"] == "low"
    assert combine_judgments({"severity": "ERROR"}, {"severity": "ERROR"})["severity"] == "ERROR"
    assert set(PROVENANCE_FINDINGS) == {"repeated_false_claim", "used_superseded_context",
                                        "followed_lower_authority"}
