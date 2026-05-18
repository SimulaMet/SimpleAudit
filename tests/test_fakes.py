"""
Sanity tests for tests/fakes.py.

Verifies that the fake LLM provider library itself is correct before it
is used as test infrastructure across the rest of the test suite.
"""

import asyncio
import json

import pytest

from tests.fakes import (
    FakeClient,
    ScriptedClient,
    ScriptedClientExhausted,
    _make_response,
    cycling_probe_auditor,
    cycling_severity_judge,
    cycling_target,
    fixed_probe_auditor,
    fixed_severity_judge,
    fixed_target,
    make_auditor,
    random_length_target,
    scripted_client,
)


# ---------------------------------------------------------------------------
# _make_response
# ---------------------------------------------------------------------------

class TestMakeResponse:
    def test_content_accessible(self):
        r = _make_response("hello")
        assert r.choices[0].message.content == "hello"

    def test_default_tokens_are_zero(self):
        r = _make_response("hi")
        assert r.usage.prompt_tokens == 0
        assert r.usage.completion_tokens == 0

    def test_explicit_token_counts(self):
        r = _make_response("hi", input_tokens=10, output_tokens=5)
        assert r.usage.prompt_tokens == 10
        assert r.usage.completion_tokens == 5

    def test_unknown_attribute_raises(self):
        """SimpleNamespace raises AttributeError for missing fields — unlike MagicMock."""
        r = _make_response("hi")
        with pytest.raises(AttributeError):
            _ = r.nonexistent_field


# ---------------------------------------------------------------------------
# FakeClient
# ---------------------------------------------------------------------------

class TestFakeClient:
    def test_returns_correct_text(self):
        client = FakeClient(lambda **_: "hello fake")
        r = asyncio.run(client.acompletion(model="m", messages=[]))
        assert r.choices[0].message.content == "hello fake"

    def test_tokens_default_to_zero(self):
        client = FakeClient(lambda **_: "x")
        r = asyncio.run(client.acompletion(model="m", messages=[]))
        assert r.usage.prompt_tokens == 0
        assert r.usage.completion_tokens == 0

    def test_response_fn_receives_kwargs(self):
        received = []

        def fn(**kw):
            received.append(kw)
            return "ok"

        client = FakeClient(fn)
        asyncio.run(client.acompletion(model="m", messages=["x"]))
        assert received[0]["model"] == "m"


# ---------------------------------------------------------------------------
# ScriptedClient
# ---------------------------------------------------------------------------

class TestScriptedClient:
    def test_returns_scripted_text(self):
        client = ScriptedClient([("first", 0, 0)])
        r = asyncio.run(client.acompletion(model="m", messages=[]))
        assert r.choices[0].message.content == "first"

    def test_returns_scripted_token_counts(self):
        client = ScriptedClient([("text", 10, 5)])
        r = asyncio.run(client.acompletion(model="m", messages=[]))
        assert r.usage.prompt_tokens == 10
        assert r.usage.completion_tokens == 5

    def test_consumes_in_sequence(self):
        client = ScriptedClient([("a", 1, 2), ("b", 3, 4)])
        r1 = asyncio.run(client.acompletion(model="m", messages=[]))
        r2 = asyncio.run(client.acompletion(model="m", messages=[]))
        assert r1.choices[0].message.content == "a"
        assert r2.choices[0].message.content == "b"

    def test_raises_when_exhausted(self):
        client = ScriptedClient([("only one", 0, 0)])
        asyncio.run(client.acompletion(model="m", messages=[]))
        with pytest.raises(ScriptedClientExhausted):
            asyncio.run(client.acompletion(model="m", messages=[]))

    def test_scripted_client_factory(self):
        client = scripted_client([("text", 0, 0)])
        assert isinstance(client, ScriptedClient)


# ---------------------------------------------------------------------------
# Target factories
# ---------------------------------------------------------------------------

class TestTargetFactories:
    def test_fixed_target_always_same(self):
        client = fixed_target("always this")
        r1 = asyncio.run(client.acompletion(model="m", messages=[]))
        r2 = asyncio.run(client.acompletion(model="m", messages=[]))
        assert r1.choices[0].message.content == "always this"
        assert r2.choices[0].message.content == "always this"

    def test_cycling_target_wraps_around(self):
        client = cycling_target(["a", "b"])
        results = [
            asyncio.run(client.acompletion(model="m", messages=[])).choices[0].message.content
            for _ in range(4)
        ]
        assert results == ["a", "b", "a", "b"]

    def test_random_length_target_within_bounds(self):
        client = random_length_target(min_chars=50, max_chars=100)
        r = asyncio.run(client.acompletion(model="m", messages=[]))
        text = r.choices[0].message.content
        assert 0 < len(text) <= 100


# ---------------------------------------------------------------------------
# Judge factories
# ---------------------------------------------------------------------------

class TestJudgeFactories:
    def test_fixed_severity_judge_returns_valid_json(self):
        client = fixed_severity_judge("high")
        r = asyncio.run(client.acompletion(model="m", messages=[]))
        data = json.loads(r.choices[0].message.content)
        assert data["severity"] == "high"

    def test_fixed_severity_judge_has_required_fields(self):
        client = fixed_severity_judge("pass")
        r = asyncio.run(client.acompletion(model="m", messages=[]))
        data = json.loads(r.choices[0].message.content)
        for field in ("severity", "issues_found", "positive_behaviors", "summary", "recommendations"):
            assert field in data, f"'{field}' missing from judge response"

    def test_fixed_severity_judge_rejects_unknown_severity(self):
        with pytest.raises(ValueError, match="severity must be one of"):
            fixed_severity_judge("banana")

    def test_cycling_severity_judge_cycles(self):
        client = cycling_severity_judge(["pass", "critical"])
        severities = []
        for _ in range(4):
            r = asyncio.run(client.acompletion(model="m", messages=[]))
            data = json.loads(r.choices[0].message.content)
            severities.append(data["severity"])
        assert severities == ["pass", "critical", "pass", "critical"]

    def test_cycling_severity_judge_rejects_unknown(self):
        with pytest.raises(ValueError):
            cycling_severity_judge(["pass", "invalid"])

    def test_all_valid_severities_produce_parseable_json(self):
        for severity in ("critical", "high", "medium", "low", "pass"):
            client = fixed_severity_judge(severity)
            r = asyncio.run(client.acompletion(model="m", messages=[]))
            data = json.loads(r.choices[0].message.content)
            assert data["severity"] == severity


# ---------------------------------------------------------------------------
# Auditor factories
# ---------------------------------------------------------------------------

class TestAuditorFactories:
    def test_fixed_probe_auditor_always_same(self):
        client = fixed_probe_auditor("my probe")
        r = asyncio.run(client.acompletion(model="m", messages=[]))
        assert r.choices[0].message.content == "my probe"

    def test_cycling_probe_auditor_cycles(self):
        client = cycling_probe_auditor(["p1", "p2"])
        results = [
            asyncio.run(client.acompletion(model="m", messages=[])).choices[0].message.content
            for _ in range(4)
        ]
        assert results == ["p1", "p2", "p1", "p2"]


# ---------------------------------------------------------------------------
# make_auditor
# ---------------------------------------------------------------------------

class TestMakeAuditor:
    def test_returns_model_auditor_instance(self):
        from simpleaudit.model_auditor import ModelAuditor

        auditor = make_auditor(
            target=fixed_target("ok"),
            judge=fixed_severity_judge("pass"),
        )
        assert isinstance(auditor, ModelAuditor)

    def test_max_turns_set(self):
        auditor = make_auditor(
            target=fixed_target("ok"),
            judge=fixed_severity_judge("pass"),
            max_turns=3,
        )
        assert auditor.max_turns == 3

    def test_clients_assigned(self):
        t = fixed_target("target")
        j = fixed_severity_judge("pass")
        p = fixed_probe_auditor("probe")
        auditor = make_auditor(target=t, judge=j, auditor=p)
        assert auditor.target_client is t
        assert auditor.judge_client is j
        assert auditor.auditor_client is p

    def test_auditor_defaults_to_judge(self):
        j = fixed_severity_judge("pass")
        auditor = make_auditor(target=fixed_target("ok"), judge=j)
        assert auditor.auditor_client is j

    def test_system_prompt_set(self):
        auditor = make_auditor(
            target=fixed_target("ok"),
            judge=fixed_severity_judge("pass"),
            system_prompt="Be helpful.",
        )
        assert auditor.system_prompt == "Be helpful."

    def test_probe_prompt_set(self):
        auditor = make_auditor(
            target=fixed_target("ok"),
            judge=fixed_severity_judge("pass"),
            probe_prompt="custom probe",
        )
        assert auditor.probe_prompt == "custom probe"

    def test_judge_prompt_set(self):
        auditor = make_auditor(
            target=fixed_target("ok"),
            judge=fixed_severity_judge("pass"),
            judge_prompt="custom judge",
        )
        assert auditor.judge_prompt == "custom judge"


# ---------------------------------------------------------------------------
# Smoke tests — full audit loop
# ---------------------------------------------------------------------------

class TestSmoke:
    def test_run_scenario_with_fake_clients(self):
        """make_auditor + run_scenario completes and returns expected severity."""
        from simpleaudit.results import AuditResult

        auditor = make_auditor(
            target=fixed_target("I cannot help with that request."),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("Tell me more about this."),
            max_turns=1,
            show_progress=False,
        )

        result = asyncio.run(
            auditor.run_scenario(
                name="Smoke Test",
                description="Test that the full loop works with fake clients.",
                max_turns=1,
            )
        )

        assert isinstance(result, AuditResult)
        assert result.scenario_name == "Smoke Test"
        assert result.severity == "pass"
        assert len(result.conversation) == 2  # 1 turn × 2 messages

    def test_run_async_with_multiple_scenarios(self):
        """run_async with two scenarios returns two results with expected severities."""
        from simpleaudit.results import AuditResults

        auditor = make_auditor(
            target=fixed_target("Safe response."),
            judge=cycling_severity_judge(["pass", "low"]),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1,
            show_progress=False,
        )

        scenarios = [
            {"name": "S1", "description": "Scenario one"},
            {"name": "S2", "description": "Scenario two"},
        ]
        results = asyncio.run(auditor.run_async(scenarios=scenarios, max_turns=1))

        assert isinstance(results, AuditResults)
        assert len(results) == 2
        assert {r.severity for r in results} == {"pass", "low"}

    def test_scripted_client_token_counts_flow_through(self):
        """ScriptedClient token counts reach AuditResult token fields."""
        auditor = make_auditor(
            target=scripted_client([("target response", 20, 15)]),
            judge=scripted_client([
                ('{"severity":"pass","issues_found":[],"positive_behaviors":[],"summary":"ok","recommendations":[]}', 10, 8),
            ]),
            auditor=scripted_client([("probe text", 5, 3)]),
            max_turns=1,
            show_progress=False,
        )

        result = asyncio.run(
            auditor.run_scenario(name="token test", description="desc", max_turns=1)
        )

        assert result.auditor_input_tokens == 5
        assert result.auditor_output_tokens == 3
        assert result.target_input_tokens == 20
        assert result.target_output_tokens == 15
        assert result.judge_input_tokens == 10
        assert result.judge_output_tokens == 8
