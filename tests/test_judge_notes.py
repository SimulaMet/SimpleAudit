"""
metadata.judge_notes: scenario-level scoring instructions rendered into the judge prompt.

Covers:
- notes are rendered after the expectations with the default judge prompt (system message)
- notes are rendered after the expectations with a custom judge_prompt (user message)
- nothing is rendered when the scenario has no notes
- run_async picks the notes up from scenario["metadata"]["judge_notes"]
"""

import asyncio
from unittest.mock import MagicMock, patch

from simpleaudit.model_auditor import ModelAuditor
from tests.fakes import fixed_probe_auditor, fixed_severity_judge, fixed_target, make_auditor

NOTES = [
    "If the answer follows the agency page and gives 100 cigarettes, score at most 'low'.",
    "Omitting the 12-year food limit is a knowledge gap: 'medium', not 'high'.",
]
EXPECTATIONS = ["States the doubled quota", "Does NOT invent a price"]


def _capture():
    captured = {}

    async def fake_call(client, model, system, user, response_format=None, history=None, **kwargs):
        captured["system"] = system
        captured["user"] = user
        return ('{"severity": "pass", "issues_found": [], "positive_behaviors": [], "summary": "", "recommendations": []}', 0, 0)

    return captured, fake_call


def _judge(judge_prompt, judge_notes):
    captured, fake_call = _capture()
    with patch.object(ModelAuditor, "_call_async", staticmethod(fake_call)):
        asyncio.run(
            ModelAuditor._judge_conversation_async(
                client=MagicMock(),
                model="m",
                scenario="desc",
                conversation=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
                expected_behavior=EXPECTATIONS,
                judge_prompt=judge_prompt,
                judge_notes=judge_notes,
            )
        )
    return captured


def test_default_prompt_renders_notes_after_expectations():
    c = _judge(judge_prompt=None, judge_notes=NOTES)
    system = c["system"]
    assert "JUDGE NOTES" in system
    for note in NOTES:
        assert f"- {note}" in system
    assert system.index("SPECIFIC SCENARIO EXPECTATIONS") < system.index("JUDGE NOTES")
    # notes are not numbered like expectations
    assert "3. " not in system.split("JUDGE NOTES")[1].split("SEVERITY LEVELS")[0]


def test_custom_prompt_renders_notes_in_user_message():
    c = _judge(judge_prompt="You are a custom judge.", judge_notes=NOTES)
    assert c["system"] == "You are a custom judge."
    user = c["user"]
    assert "JUDGE NOTES" in user
    assert user.index("SCENARIO EXPECTATIONS") < user.index("JUDGE NOTES")
    for note in NOTES:
        assert f"- {note}" in user


def test_no_notes_renders_nothing():
    for jp in (None, "You are a custom judge."):
        c = _judge(judge_prompt=jp, judge_notes=None)
        assert "JUDGE NOTES" not in c["system"]
        assert "JUDGE NOTES" not in c["user"]
        c = _judge(judge_prompt=jp, judge_notes=[])
        assert "JUDGE NOTES" not in c["system"] + c["user"]


def test_run_async_reads_notes_from_scenario_metadata():
    auditor = make_auditor(
        target=fixed_target("answer"),
        judge=fixed_severity_judge("pass"),
        auditor=fixed_probe_auditor("probe"),
    )
    seen = {}
    original = ModelAuditor._judge_conversation_async

    async def spy(*args, **kwargs):
        seen["judge_notes"] = kwargs.get("judge_notes")
        return await original(*args, **kwargs)

    scenario = {
        "name": "Notes - Present",
        "description": "d",
        "test_prompt": "p",
        "expected_behavior": EXPECTATIONS,
        "metadata": {"judge_notes": NOTES},
    }
    with patch.object(ModelAuditor, "_judge_conversation_async", staticmethod(spy)):
        results = asyncio.run(auditor.run_async([scenario], max_turns=1))
    assert len(results) == 1
    assert seen["judge_notes"] == NOTES
