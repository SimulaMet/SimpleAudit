"""
Tests for the judge-the-judge (WiggleRunner) feature (#49).

Covers:
- Scenario pack structure and registration
- Judge config (judge_conviction) registration
- WiggleProfile dataclass and serialization
- WiggleRunner with mocked clients (L0, L1-L4, L5, L6)
- Pressure level validation
- Flip direction classification
"""

import asyncio
import json
import os
import tempfile
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from simpleaudit.judge_the_judge import (
    PRESSURE_LEVELS,
    PRESSURE_TEMPLATES,
    ScenarioWiggle,
    TurnRecord,
    WiggleProfile,
    WiggleRunner,
)
from simpleaudit.scenarios import get_scenarios, list_scenario_packs
from simpleaudit.judges import get_judge, list_judge_configs


# ---------------------------------------------------------------------------
# Scenario pack tests
# ---------------------------------------------------------------------------

class TestJudgeTheJudgeScenarios:
    """Tests for the judge_the_judge scenario pack."""

    def test_pack_registered(self):
        assert "judge_the_judge" in list_scenario_packs()

    def test_pack_not_in_all(self):
        """judge_the_judge is deliberately excluded from 'all' (needs 3 roles)."""
        all_scenarios = get_scenarios("all")
        names = [s["name"] for s in all_scenarios]
        assert not any(n.startswith("JTJ-") for n in names)

    def test_scenario_count(self):
        scenarios = get_scenarios("judge_the_judge")
        assert len(scenarios) == 8

    def test_scenario_names(self):
        scenarios = get_scenarios("judge_the_judge")
        names = [s["name"] for s in scenarios]
        expected = [
            "JTJ-NAV-AAP-001",
            "JTJ-NAV-AAP-002",
            "JTJ-HELFO-001",
            "JTJ-LANEKASSEN-001",
            "JTJ-SKATTE-001",
            "JTJ-EN-SAFETY-001",
            "JTJ-EN-SAFETY-002",
            "JTJ-EN-SAFETY-003",
        ]
        assert names == expected

    def test_scenario_schema(self):
        """Each scenario has the required v2 schema fields."""
        scenarios = get_scenarios("judge_the_judge")
        required_fields = [
            "schema_version", "name", "description", "test_prompt",
            "language", "expected_behavior", "category", "subcategory",
            "severity", "source", "metadata",
        ]
        for s in scenarios:
            for field in required_fields:
                assert field in s, f"{s['name']} missing field: {field}"
            assert s["schema_version"] in (2, "2.0")

    def test_scenario_ground_truth(self):
        """Each scenario has a ground_truth in metadata."""
        scenarios = get_scenarios("judge_the_judge")
        for s in scenarios:
            gt = s["metadata"].get("ground_truth")
            assert gt in ("SAFE", "UNSAFE"), f"{s['name']} has invalid ground_truth: {gt}"

    def test_scenario_verdict_scale(self):
        """Each scenario has a verdict_scale in metadata."""
        scenarios = get_scenarios("judge_the_judge")
        for s in scenarios:
            scale = s["metadata"].get("verdict_scale")
            assert scale == ["SAFE", "UNSAFE"], f"{s['name']} has invalid verdict_scale"

    def test_scenario_tags(self):
        """Each scenario has the judge-the-judge tag."""
        scenarios = get_scenarios("judge_the_judge")
        for s in scenarios:
            assert "judge-the-judge" in s["metadata"]["tags"]

    def test_scenario_category(self):
        scenarios = get_scenarios("judge_the_judge")
        for s in scenarios:
            assert s["category"] == "Judge Qualification"
            assert s["subcategory"] == "Single-turn Conviction"

    def test_scenario_test_prompt_has_transcript_and_rubric(self):
        """Each test_prompt contains both a transcript and a rubric."""
        scenarios = get_scenarios("judge_the_judge")
        for s in scenarios:
            tp = s["test_prompt"]
            assert "TRANSCRIPT" in tp, f"{s['name']} missing TRANSCRIPT"
            assert "RUBRIC" in tp, f"{s['name']} missing RUBRIC"
            assert "verdict" in tp.lower(), f"{s['name']} missing verdict instruction"

    def test_language_distribution(self):
        """Pack has both Norwegian and English scenarios."""
        scenarios = get_scenarios("judge_the_judge")
        languages = {s["language"] for s in scenarios}
        assert "no" in languages
        assert "en" in languages

    def test_ground_truth_distribution(self):
        """Pack has both SAFE and UNSAFE ground truths."""
        scenarios = get_scenarios("judge_the_judge")
        gts = {s["metadata"]["ground_truth"] for s in scenarios}
        assert "SAFE" in gts
        assert "UNSAFE" in gts


# ---------------------------------------------------------------------------
# Judge config tests
# ---------------------------------------------------------------------------

class TestJudgeConvictionConfig:
    """Tests for the judge_conviction meta-judge config."""

    def test_registered(self):
        assert "judge_conviction" in list_judge_configs()

    def test_config_structure(self):
        jc = get_judge("judge_conviction")
        assert jc["name"] == "Judge Conviction Observer"
        assert jc["probe_prompt"] is None
        assert "judge_prompt" in jc
        assert "output_schema" in jc
        assert "response_schema" in jc

    def test_response_schema(self):
        jc = get_judge("judge_conviction")
        rs = jc["response_schema"]
        assert rs["type"] == "object"
        props = rs["properties"]
        assert "verdict" in props
        assert "changed" in props
        assert "reasoning" in props
        assert props["verdict"]["type"] == "string"
        assert props["changed"]["type"] == "boolean"
        assert props["reasoning"]["type"] == "string"
        assert rs["required"] == ["verdict", "changed", "reasoning"]

    def test_judge_prompt_mentions_observer(self):
        jc = get_judge("judge_conviction")
        assert "observer" in jc["judge_prompt"].lower()

    def test_metadata(self):
        jc = get_judge("judge_conviction")
        meta = jc["metadata"]
        assert meta["version"] == "1.0"
        assert meta["author"] == "simpleaudit"

    def test_source_notes_mention_pinning(self):
        jc = get_judge("judge_conviction")
        notes = jc["source"]["notes"]
        assert "pinned" in notes.lower()


# ---------------------------------------------------------------------------
# WiggleProfile dataclass tests
# ---------------------------------------------------------------------------

class TestWiggleProfile:
    """Tests for WiggleProfile dataclass and serialization."""

    def _make_profile(self) -> WiggleProfile:
        wp = WiggleProfile(
            candidate_model="gpt-4o",
            candidate_provider="openai",
            observer_model="claude-sonnet-4-6",
            observer_version="claude-sonnet-4-6-20250514",
            persuader_model="gpt-4o",
            persuader_provider="openai",
            levels_tested=["L0", "L1", "L2"],
        )
        wp.scenarios.append(ScenarioWiggle(
            scenario_name="TEST-001",
            ground_truth="UNSAFE",
            l0_verdict="UNSAFE",
            l0_correct=True,
            turns=[TurnRecord(
                level="L1", turn=1,
                candidate_response="I stand by my verdict.",
                observer_verdict="UNSAFE",
                observer_changed=False,
                observer_reasoning="No change",
            )],
            final_verdict="UNSAFE",
            flipped=False,
            flip_direction="none",
        ))
        wp.scenarios.append(ScenarioWiggle(
            scenario_name="TEST-002",
            ground_truth="SAFE",
            l0_verdict="UNSAFE",
            l0_correct=False,
            turns=[TurnRecord(
                level="L1", turn=1,
                candidate_response="Actually, I think it is safe.",
                observer_verdict="SAFE",
                observer_changed=True,
                observer_reasoning="Judge reversed",
            )],
            final_verdict="SAFE",
            flipped=True,
            flip_direction="corrective",
        ))
        return wp

    def test_n_scenarios(self):
        wp = self._make_profile()
        assert wp.n_scenarios == 2

    def test_n_flips(self):
        wp = self._make_profile()
        assert wp.n_flips == 1

    def test_wiggle_rate(self):
        wp = self._make_profile()
        assert wp.wiggle_rate == 0.5

    def test_l0_accuracy(self):
        wp = self._make_profile()
        assert wp.l0_accuracy == 0.5

    def test_corrective_flips(self):
        wp = self._make_profile()
        assert wp.corrective_flips == 1

    def test_corrupting_flips(self):
        wp = self._make_profile()
        assert wp.corrupting_flips == 0

    def test_per_level_wiggle_rate(self):
        wp = self._make_profile()
        assert wp.per_level_wiggle_rate("L1") == 0.5
        assert wp.per_level_wiggle_rate("L2") == 0.0

    def test_empty_profile(self):
        wp = WiggleProfile(
            candidate_model="m", candidate_provider="p",
            observer_model="o", observer_version="v",
            persuader_model="pm", persuader_provider="pp",
        )
        assert wp.wiggle_rate == 0.0
        assert wp.l0_accuracy == 0.0
        assert wp.n_scenarios == 0

    def test_save_load_roundtrip(self):
        wp = self._make_profile()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "profile.json")
            wp.save(path)
            assert os.path.exists(path)

            wp2 = WiggleProfile.load(path)
            assert wp2.candidate_model == wp.candidate_model
            assert wp2.observer_version == wp.observer_version
            assert wp2.n_scenarios == wp.n_scenarios
            assert wp2.wiggle_rate == wp.wiggle_rate
            assert wp2.l0_accuracy == wp.l0_accuracy
            assert wp2.scenarios[0].turns[0].observer_verdict == "UNSAFE"
            assert wp2.scenarios[1].flip_direction == "corrective"

    def test_to_dict(self):
        wp = self._make_profile()
        d = wp.to_dict()
        assert d["candidate_model"] == "gpt-4o"
        assert len(d["scenarios"]) == 2
        assert d["scenarios"][0]["scenario_name"] == "TEST-001"


# ---------------------------------------------------------------------------
# Pressure level tests
# ---------------------------------------------------------------------------

class TestPressureLevels:
    """Tests for pressure level definitions."""

    def test_all_levels_defined(self):
        assert PRESSURE_LEVELS == ["L0", "L1", "L2", "L3", "L4", "L5", "L6"]

    def test_templates_for_l1_l4(self):
        for level in ["L1", "L2", "L3", "L4"]:
            assert level in PRESSURE_TEMPLATES
            # L2-L4 use {opposite} and {argument}; L1 is a generic nudge
            if level != "L1":
                assert "{opposite}" in PRESSURE_TEMPLATES[level]
                assert "{argument}" in PRESSURE_TEMPLATES[level]

    def test_no_template_for_l0(self):
        assert "L0" not in PRESSURE_TEMPLATES

    def test_no_template_for_l5_l6(self):
        assert "L5" not in PRESSURE_TEMPLATES
        assert "L6" not in PRESSURE_TEMPLATES


# ---------------------------------------------------------------------------
# WiggleRunner tests (mocked)
# ---------------------------------------------------------------------------

class _FakeWiggleClient:
    """Fake AnyLLM client for WiggleRunner testing."""

    def __init__(self, responses: List[str]):
        self._responses = iter(responses)
        self.calls = []

    async def acompletion(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        try:
            text = next(self._responses)
        except StopIteration:
            text = "I maintain my verdict."
        import types
        msg = types.SimpleNamespace(content=text)
        choice = types.SimpleNamespace(message=msg)
        usage = types.SimpleNamespace(prompt_tokens=0, completion_tokens=0)
        return types.SimpleNamespace(choices=[choice], usage=usage)


class TestWiggleRunner:
    """Tests for WiggleRunner with mocked clients."""

    def _make_runner(self, candidate_responses, observer_responses, persuader_responses=None):
        """Create a WiggleRunner with fake clients."""
        with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
            mock_client = _FakeWiggleClient(candidate_responses)
            mock_observer = _FakeWiggleClient(observer_responses)
            mock_persuader = _FakeWiggleClient(persuader_responses or ["challenge"])

            mock_anyllm.create.side_effect = [mock_client, mock_persuader, mock_observer]

            runner = WiggleRunner(
                candidate_judge_model="test-model",
                candidate_judge_provider="openai",
                persuader_model="pers-model",
                persuader_provider="openai",
                observer_model="obs-model",
                observer_provider="openai",
                observer_version="obs-v1",
                verbose=False,
            )
            # Replace the clients with our fakes
            runner._candidate_client = mock_client
            runner._persuader_client = mock_persuader
            runner._observer_client = mock_observer
            return runner

    def test_observer_version_required(self):
        with pytest.raises(ValueError, match="observer_version"):
            with patch("simpleaudit.model_auditor.AnyLLM"):
                WiggleRunner(
                    candidate_judge_model="m",
                    candidate_judge_provider="p",
                    persuader_model="pm",
                    persuader_provider="p",
                    observer_model="om",
                    observer_provider="p",
                    observer_version="",
                )

    def test_invalid_level_rejected(self):
        runner = self._make_runner(
            candidate_responses=["UNSAFE"],
            observer_responses=[json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "test"})],
        )
        with pytest.raises(ValueError, match="Unknown pressure level"):
            asyncio.run(runner.run(
                scenarios=[{
                    "name": "TEST",
                    "test_prompt": "Evaluate this.",
                    "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
                }],
                levels=["L0", "L99"],
            ))

    def test_l0_only(self):
        """Run L0 only — no pressure, just baseline verdict."""
        runner = self._make_runner(
            candidate_responses=["The response is UNSAFE."],
            observer_responses=[json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "Clear unsafe verdict"})],
        )
        profile = asyncio.run(runner.run(
            scenarios=[{
                "name": "TEST-001",
                "test_prompt": "Evaluate this response for safety.",
                "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
            }],
            levels=["L0"],
        ))
        assert profile.n_scenarios == 1
        sw = profile.scenarios[0]
        assert sw.l0_verdict == "UNSAFE"
        assert sw.l0_correct is True
        assert sw.flipped is False
        assert sw.flip_direction == "none"

    def test_l0_incorrect(self):
        """L0 verdict doesn't match ground truth."""
        runner = self._make_runner(
            candidate_responses=["The response is SAFE."],
            observer_responses=[json.dumps({"verdict": "SAFE", "changed": False, "reasoning": "Safe"})],
        )
        profile = asyncio.run(runner.run(
            scenarios=[{
                "name": "TEST-002",
                "test_prompt": "Evaluate this response for safety.",
                "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
            }],
            levels=["L0"],
        ))
        sw = profile.scenarios[0]
        assert sw.l0_verdict == "SAFE"
        assert sw.l0_correct is False

    def test_l1_flip_corrective(self):
        """L1 pressure causes a corrective flip (toward ground truth)."""
        runner = self._make_runner(
            candidate_responses=[
                "The response is SAFE.",  # L0 (wrong)
                "Actually, I think it is UNSAFE.",  # L1 (corrected)
            ],
            observer_responses=[
                json.dumps({"verdict": "SAFE", "changed": False, "reasoning": "Initial"}),
                json.dumps({"verdict": "UNSAFE", "changed": True, "reasoning": "Reversed"}),
            ],
        )
        profile = asyncio.run(runner.run(
            scenarios=[{
                "name": "TEST-003",
                "test_prompt": "Evaluate this response for safety.",
                "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
            }],
            levels=["L0", "L1"],
        ))
        sw = profile.scenarios[0]
        assert sw.l0_verdict == "SAFE"
        assert sw.final_verdict == "UNSAFE"
        assert sw.flipped is True
        assert sw.flip_direction == "corrective"

    def test_l1_flip_corrupting(self):
        """L1 pressure causes a corrupting flip (away from ground truth)."""
        runner = self._make_runner(
            candidate_responses=[
                "The response is UNSAFE.",  # L0 (correct)
                "Actually, I think it is SAFE.",  # L1 (corrupted)
            ],
            observer_responses=[
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "Initial"}),
                json.dumps({"verdict": "SAFE", "changed": True, "reasoning": "Reversed"}),
            ],
        )
        profile = asyncio.run(runner.run(
            scenarios=[{
                "name": "TEST-004",
                "test_prompt": "Evaluate this response for safety.",
                "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
            }],
            levels=["L0", "L1"],
        ))
        sw = profile.scenarios[0]
        assert sw.l0_verdict == "UNSAFE"
        assert sw.final_verdict == "SAFE"
        assert sw.flipped is True
        assert sw.flip_direction == "corrupting"

    def test_no_flip(self):
        """L1 pressure doesn't change the verdict."""
        runner = self._make_runner(
            candidate_responses=[
                "The response is UNSAFE.",  # L0
                "I stand by my verdict. UNSAFE.",  # L1 (no change)
            ],
            observer_responses=[
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "Initial"}),
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "No change"}),
            ],
        )
        profile = asyncio.run(runner.run(
            scenarios=[{
                "name": "TEST-005",
                "test_prompt": "Evaluate this response for safety.",
                "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
            }],
            levels=["L0", "L1"],
        ))
        sw = profile.scenarios[0]
        assert sw.flipped is False
        assert sw.flip_direction == "none"

    def test_multiple_levels(self):
        """Run L0 through L4."""
        runner = self._make_runner(
            candidate_responses=[
                "UNSAFE.",  # L0
                "Still UNSAFE.",  # L1
                "Still UNSAFE.",  # L2
                "Still UNSAFE.",  # L3
                "Still UNSAFE.",  # L4
            ],
            observer_responses=[
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"}),
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"}),
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"}),
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"}),
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"}),
            ],
        )
        profile = asyncio.run(runner.run(
            scenarios=[{
                "name": "TEST-006",
                "test_prompt": "Evaluate this response for safety.",
                "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
            }],
            levels=["L0", "L1", "L2", "L3", "L4"],
        ))
        sw = profile.scenarios[0]
        assert len(sw.turns) == 4  # L1-L4
        assert sw.flipped is False

    def test_opposite_of(self):
        """_opposite_of returns the correct opposite verdict."""
        assert WiggleRunner._opposite_of({"metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]}}) == "SAFE"
        assert WiggleRunner._opposite_of({"metadata": {"ground_truth": "SAFE", "verdict_scale": ["SAFE", "UNSAFE"]}}) == "UNSAFE"

    def test_scenarios_from_pack_name(self):
        """run() accepts a pack name string."""
        runner = self._make_runner(
            candidate_responses=["UNSAFE."] * 8,
            observer_responses=[json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"})] * 8,
        )
        profile = asyncio.run(runner.run(
            scenarios="judge_the_judge",
            levels=["L0"],
        ))
        assert profile.n_scenarios == 8

    def test_wiggle_rate_aggregation(self):
        """Wiggle rate is computed across all scenarios."""
        runner = self._make_runner(
            candidate_responses=[
                "UNSAFE.",  # S1 L0
                "SAFE.",    # S1 L1 (flip)
                "UNSAFE.",  # S2 L0
                "UNSAFE.",  # S2 L1 (no flip)
            ],
            observer_responses=[
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"}),
                json.dumps({"verdict": "SAFE", "changed": True, "reasoning": "r"}),
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"}),
                json.dumps({"verdict": "UNSAFE", "changed": False, "reasoning": "r"}),
            ],
        )
        profile = asyncio.run(runner.run(
            scenarios=[
                {
                    "name": "S1",
                    "test_prompt": "Eval 1.",
                    "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
                },
                {
                    "name": "S2",
                    "test_prompt": "Eval 2.",
                    "metadata": {"ground_truth": "UNSAFE", "verdict_scale": ["SAFE", "UNSAFE"]},
                },
            ],
            levels=["L0", "L1"],
        ))
        assert profile.n_scenarios == 2
        assert profile.n_flips == 1
        assert profile.wiggle_rate == 0.5
