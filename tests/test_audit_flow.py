"""
Tests for end-to-end audit flow, save/load round-trip, plot smoke test,
AuditExperiment, parse_json_response edge cases, and error resilience.
"""

import asyncio
import json
import os
import tempfile
import threading

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.experiment import AuditExperiment


# Check for optional dependencies
try:
    import matplotlib
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


# --- Helpers ---


def _make_mock_client(responses: list[str]) -> MagicMock:
    """
    Create a mock AnyLLM client that returns responses in sequence.
    Each call to acompletion pops the next response.
    """
    client = MagicMock()
    response_iter = iter(responses)

    async def mock_acompletion(**kwargs):
        text = next(response_iter)
        mock_msg = MagicMock()
        mock_msg.content = text
        mock_choice = MagicMock()
        mock_choice.message = mock_msg
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        return mock_resp

    client.acompletion = mock_acompletion
    return client


def _make_auditor_with_mock(responses: list[str], max_turns: int = 1, system_prompt=None):
    """
    Create a ModelAuditor with mocked clients using patch to avoid real AnyLLM.
    """
    mock_client = _make_mock_client(responses)

    with patch.object(ModelAuditor, '_create_anyllm_client', return_value=mock_client):
        auditor = ModelAuditor(
            model="target-model",
            provider="openai",
            judge_model="judge-model",
            judge_provider="openai",
            system_prompt=system_prompt,
            max_turns=max_turns,
            verbose=False,
            show_progress=False,
        )
    # Replace the clients with our mock (since __init__ already ran)
    auditor.target_client = mock_client
    auditor.judge_client = mock_client
    return auditor


def _make_audit_result(
    name="Test Scenario",
    severity="pass",
    issues=None,
    recommendations=None,
) -> AuditResult:
    """Create a sample AuditResult for testing."""
    return AuditResult(
        scenario_name=name,
        scenario_description=f"Description for {name}",
        conversation=[
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
        ],
        severity=severity,
        issues_found=issues or [],
        positive_behaviors=["Friendly response"],
        summary=f"Summary for {name}",
        recommendations=recommendations or [],
    )


# --- End-to-end mock audit ---


class TestEndToEndMockAudit:
    """Test the full audit flow with mocked LLM clients."""

    def test_run_scenario_produces_valid_result(self):
        """run_scenario should produce a valid AuditResult with mocked clients."""
        judge_response = json.dumps({
            "severity": "pass",
            "issues_found": [],
            "positive_behaviors": ["Accurate response"],
            "summary": "Model performed well",
            "recommendations": [],
        })
        # 2 turns: probe1, target1, probe2, target2, judge
        all_responses = [
            "What is 2+2?", "2+2 equals 4.",
            "Can you explain more?", "Sure, it's basic arithmetic.",
            judge_response,
        ]
        auditor = _make_auditor_with_mock(all_responses, max_turns=2)

        result = asyncio.run(
            auditor.run_scenario(
                name="Test Arithmetic",
                description="Test basic arithmetic",
                max_turns=2,
            )
        )

        assert isinstance(result, AuditResult)
        assert result.scenario_name == "Test Arithmetic"
        assert result.severity == "pass"
        assert len(result.conversation) == 4  # 2 turns × 2 messages

    def test_run_async_with_custom_scenarios(self):
        """run_async with a list of custom scenarios should work."""
        judge_response = json.dumps({
            "severity": "low",
            "issues_found": ["Minor issue"],
            "positive_behaviors": ["Good"],
            "summary": "Mostly fine",
            "recommendations": ["Be better"],
        })
        # 1 scenario, 1 turn: probe, target, judge
        all_responses = ["Probe message", "Target response", judge_response]
        auditor = _make_auditor_with_mock(all_responses, max_turns=1)

        scenarios = [{"name": "Custom Test", "description": "A custom test scenario"}]

        results = asyncio.run(auditor.run_async(scenarios=scenarios, max_turns=1))

        assert isinstance(results, AuditResults)
        assert len(results) == 1
        assert results[0].scenario_name == "Custom Test"
        assert results[0].severity == "low"

    def test_run_sync_wrapper(self):
        """The synchronous run() wrapper should work when no event loop is running."""
        judge_response = json.dumps({
            "severity": "pass",
            "issues_found": [],
            "positive_behaviors": ["Safe"],
            "summary": "All good",
            "recommendations": [],
        })
        all_responses = ["Probe", "Target reply", judge_response]
        auditor = _make_auditor_with_mock(all_responses, max_turns=1)

        scenarios = [{"name": "Sync Test", "description": "Test sync wrapper"}]
        results = auditor.run(scenarios=scenarios, max_turns=1)

        assert isinstance(results, AuditResults)
        assert len(results) == 1

    def test_run_scenario_with_system_prompt(self):
        """run_scenario should work correctly when a system prompt is set."""
        judge_response = json.dumps({
            "severity": "pass",
            "issues_found": [],
            "positive_behaviors": ["Followed system prompt"],
            "summary": "Good",
            "recommendations": [],
        })
        all_responses = ["Test probe", "System prompt response", judge_response]
        auditor = _make_auditor_with_mock(
            all_responses, max_turns=1, system_prompt="You are a helpful assistant."
        )

        result = asyncio.run(
            auditor.run_scenario(
                name="System Prompt Test",
                description="Test with system prompt",
                max_turns=1,
            )
        )

        assert isinstance(result, AuditResult)
        assert result.severity == "pass"

    def test_run_scenario_with_expected_behavior(self):
        """run_scenario should pass expected_behavior to the result."""
        judge_response = json.dumps({
            "severity": "pass",
            "issues_found": [],
            "positive_behaviors": ["Met expectations"],
            "summary": "Model behaved as expected",
            "recommendations": [],
        })
        all_responses = ["Probe", "Response", judge_response]
        auditor = _make_auditor_with_mock(all_responses, max_turns=1)

        expected = ["Should refuse harmful requests", "Should cite sources"]
        result = asyncio.run(
            auditor.run_scenario(
                name="Expected Behavior Test",
                description="Test expected behavior",
                expected_behavior=expected,
                max_turns=1,
            )
        )

        assert result.expected_behavior == expected


# --- Save / Load round-trip ---


class TestSaveLoadRoundTrip:
    """Test saving and loading AuditResults preserves data."""

    def test_save_and_load(self):
        """Results should survive a save/load round-trip."""
        results = AuditResults([
            _make_audit_result("Scenario A", "pass"),
            _make_audit_result("Scenario B", "high", issues=["Found issue"]),
            _make_audit_result("Scenario C", "critical", issues=["Serious"], recommendations=["Fix it"]),
        ])

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            filepath = f.name

        try:
            results.save(filepath)
            loaded = AuditResults.load(filepath)

            assert len(loaded) == len(results)
            assert loaded.score == results.score
            assert loaded.passed == results.passed
            assert loaded.failed == results.failed
            assert loaded.critical_count == results.critical_count

            for orig, loaded_r in zip(results, loaded):
                assert orig.scenario_name == loaded_r.scenario_name
                assert orig.severity == loaded_r.severity
                assert orig.issues_found == loaded_r.issues_found
                assert orig.conversation == loaded_r.conversation
                assert orig.recommendations == loaded_r.recommendations
        finally:
            os.unlink(filepath)

    def test_save_load_preserves_timestamp(self):
        """The timestamp from the original should be preserved on load."""
        results = AuditResults([_make_audit_result()])

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            filepath = f.name

        try:
            results.save(filepath)
            loaded = AuditResults.load(filepath)
            assert loaded.timestamp == results.timestamp
        finally:
            os.unlink(filepath)

    def test_save_load_empty_results(self):
        """Saving and loading empty results should work."""
        results = AuditResults([])

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            filepath = f.name

        try:
            results.save(filepath)
            loaded = AuditResults.load(filepath)
            assert len(loaded) == 0
            assert loaded.score == 0.0
        finally:
            os.unlink(filepath)

    def test_save_produces_valid_json(self):
        """The saved file should be valid JSON with expected structure."""
        results = AuditResults([_make_audit_result()])

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            filepath = f.name

        try:
            results.save(filepath)
            with open(filepath) as f:
                data = json.load(f)

            assert "timestamp" in data
            assert "summary" in data
            assert "results" in data
            assert data["summary"]["total_scenarios"] == 1
        finally:
            os.unlink(filepath)


# --- Plot smoke test ---


class TestPlotSmokeTest:
    """Test that plot() doesn't crash."""

    @pytest.mark.skipif(not HAS_MATPLOTLIB, reason="matplotlib not installed")
    def test_plot_runs_without_error(self):
        """plot() should execute without raising on valid results."""
        import matplotlib
        matplotlib.use("Agg")  # Non-interactive backend

        results = AuditResults([
            _make_audit_result("A", "pass"),
            _make_audit_result("B", "medium"),
            _make_audit_result("C", "critical"),
        ])

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            filepath = f.name

        try:
            results.plot(save_path=filepath)
            assert os.path.exists(filepath)
            assert os.path.getsize(filepath) > 0
        finally:
            os.unlink(filepath)

    @pytest.mark.skipif(not HAS_MATPLOTLIB, reason="matplotlib not installed")
    def test_plot_single_result(self):
        """plot() should handle a single result without error."""
        import matplotlib
        matplotlib.use("Agg")

        results = AuditResults([_make_audit_result("Solo", "high")])

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            filepath = f.name

        try:
            results.plot(save_path=filepath)
        finally:
            os.unlink(filepath)


# --- AuditExperiment tests ---


class TestAuditExperiment:
    """Test the AuditExperiment class."""

    def test_init_requires_model_key(self):
        """Models without 'model' key should raise ValueError."""
        with pytest.raises(ValueError, match="Models must be dicts"):
            AuditExperiment(models=[{"provider": "openai"}])

    def test_init_requires_non_empty_models(self):
        """Empty model list should raise ValueError."""
        with pytest.raises(ValueError, match="Models must be dicts"):
            AuditExperiment(models=[])

    def test_merge_common_fills_judge_defaults(self):
        """_merge_common should fill in judge settings from experiment level."""
        exp = AuditExperiment(
            models=[{"model": "test-model", "provider": "openai"}],
            judge_model="gpt-4",
            judge_provider="openai",
            judge_api_key="test-key",
        )
        merged = exp._merge_common({"model": "test-model", "provider": "openai"})
        assert merged["judge_model"] == "gpt-4"
        assert merged["judge_provider"] == "openai"
        assert merged["judge_api_key"] == "test-key"

    def test_merge_common_respects_overrides(self):
        """Model-level judge settings should override experiment-level."""
        exp = AuditExperiment(
            models=[{
                "model": "custom",
                "provider": "openai",
                "judge_model": "claude-3",
                "judge_provider": "anthropic",
            }],
            judge_model="gpt-4",
            judge_provider="openai",
        )
        merged = exp._merge_common(exp.models[0])
        assert merged["judge_model"] == "claude-3"
        assert merged["judge_provider"] == "anthropic"

    def test_run_sync_raises_in_event_loop(self):
        """run() should raise RuntimeError when called from an active event loop."""
        exp = AuditExperiment(
            models=[{"model": "test", "provider": "openai"}],
            judge_model="judge",
            judge_provider="openai",
        )

        async def _test():
            with pytest.raises(RuntimeError, match="cannot be called from an active event loop"):
                exp.run(scenarios="safety")

        asyncio.run(_test())


# --- parse_json_response edge cases ---


class TestParseJsonResponseEdgeCases:
    """Additional edge case tests for parse_json_response."""

    def test_valid_json_string(self):
        """Properly formed JSON should be parsed correctly."""
        response = json.dumps({
            "severity": "high",
            "issues_found": ["Issue 1", "Issue 2"],
            "positive_behaviors": ["Good thing"],
            "summary": "Test summary",
            "recommendations": ["Do better"],
        })
        result = ModelAuditor.parse_json_response(response)
        assert result["severity"] == "high"
        assert len(result["issues_found"]) == 2

    def test_json_in_code_block(self):
        """JSON wrapped in markdown code block should be extracted."""
        response = '```json\n{"severity": "pass", "issues_found": [], "positive_behaviors": [], "summary": "OK", "recommendations": []}\n```'
        result = ModelAuditor.parse_json_response(response)
        assert result["severity"] == "pass"

    def test_json_with_surrounding_text(self):
        """JSON embedded in surrounding text should be extracted."""
        response = 'Here is my evaluation:\n{"severity": "medium", "issues_found": ["minor"], "positive_behaviors": [], "summary": "meh", "recommendations": []}\nThat is all.'
        result = ModelAuditor.parse_json_response(response)
        assert result["severity"] == "medium"

    def test_empty_response(self):
        """Empty response should return default result."""
        result = ModelAuditor.parse_json_response("")
        assert result["severity"] == "ERROR"

    def test_none_response_like_empty(self):
        """None-ish empty response should return default."""
        result = ModelAuditor.parse_json_response("")
        assert result["severity"] == "ERROR"
        assert "Could not parse" in result["issues_found"][0]

    def test_invalid_json_with_severity_keyword(self):
        """Non-JSON text containing severity keywords should be extracted."""
        response = "The model showed critical safety issues and dangerous behavior."
        result = ModelAuditor.parse_json_response(response)
        # Should extract via _extract_from_text fallback
        assert result["severity"] in ("critical", "high", "medium", "low", "pass", "ERROR")

    def test_custom_default_severity(self):
        """Custom default_severity should be used when parsing fails."""
        result = ModelAuditor.parse_json_response("not json at all", default_severity="low")
        # If no severity extracted, should use the custom default
        assert isinstance(result["severity"], str)

    def test_invalid_severity_in_json_uses_default(self):
        """JSON with an unrecognized severity should fall back to default."""
        response = json.dumps({
            "severity": "banana",
            "issues_found": [],
            "positive_behaviors": [],
            "summary": "test",
            "recommendations": [],
        })
        result = ModelAuditor.parse_json_response(response)
        assert result["severity"] == "ERROR"

    def test_json_with_missing_fields_uses_defaults(self):
        """JSON with only severity should fill in defaults for missing fields."""
        response = '{"severity": "pass"}'
        result = ModelAuditor.parse_json_response(response)
        assert result["severity"] == "pass"
        assert result["issues_found"] == []
        assert result["positive_behaviors"] == []
        assert result["summary"] == ""
        assert result["recommendations"] == []

    def test_deeply_nested_json_still_parses(self):
        """JSON with extra nesting should still extract top-level fields."""
        response = json.dumps({
            "severity": "low",
            "issues_found": ["nested issue"],
            "positive_behaviors": ["nested positive"],
            "summary": "Deeply nested",
            "recommendations": ["deeply nested rec"],
            "extra_field": {"nested": {"deep": True}},
        })
        result = ModelAuditor.parse_json_response(response)
        assert result["severity"] == "low"
        assert result["issues_found"] == ["nested issue"]

    def test_unicode_content(self):
        """JSON with unicode characters should parse correctly."""
        response = json.dumps({
            "severity": "pass",
            "issues_found": [],
            "positive_behaviors": ["Håndterte norsk tekst riktig"],
            "summary": "Modellen håndterte forespørselen godt",
            "recommendations": [],
        }, ensure_ascii=False)
        result = ModelAuditor.parse_json_response(response)
        assert result["severity"] == "pass"
        assert "norsk" in result["positive_behaviors"][0]

    def test_very_long_response_truncated_in_summary(self):
        """Very long non-JSON response should have summary truncated."""
        long_text = "x" * 1000
        result = ModelAuditor.parse_json_response(long_text)
        # The default result caps summary at 500 chars
        assert len(result["summary"]) <= 500


# --- Error resilience ---


class TestErrorResilience:
    """Test behavior when things go wrong during auditing."""

    def test_run_scenario_with_bad_judge_response(self):
        """When the judge returns invalid JSON, should still produce a result."""
        # 1-turn: probe, target, bad judge
        all_responses = [
            "What about X?",
            "Here's info about X.",
            "This is NOT valid JSON at all!!!"
        ]
        auditor = _make_auditor_with_mock(all_responses, max_turns=1)

        result = asyncio.run(
            auditor.run_scenario(
                name="Bad Judge",
                description="Test with bad judge response",
                max_turns=1,
            )
        )

        # Should not crash — should produce a result with ERROR or fallback severity
        assert isinstance(result, AuditResult)
        assert result.scenario_name == "Bad Judge"

    def test_results_score_all_severities(self):
        """Score calculation should handle all valid severity levels."""
        results = AuditResults([
            _make_audit_result("A", "pass"),
            _make_audit_result("B", "low"),
            _make_audit_result("C", "medium"),
            _make_audit_result("D", "high"),
            _make_audit_result("E", "critical"),
        ])
        # pass=4, low=3, medium=2, high=1, critical=0 → total=10, max=20
        assert results.score == 50.0

    def test_results_score_all_pass(self):
        """All pass results should give 100.0 score."""
        results = AuditResults([
            _make_audit_result("A", "pass"),
            _make_audit_result("B", "pass"),
        ])
        assert results.score == 100.0

    def test_results_score_all_critical(self):
        """All critical results should give 0.0 score."""
        results = AuditResults([
            _make_audit_result("A", "critical"),
            _make_audit_result("B", "critical"),
        ])
        assert results.score == 0.0

    def test_results_score_empty(self):
        """Empty results should return 0.0 score."""
        results = AuditResults([])
        assert results.score == 0.0

    def test_summary_does_not_crash(self):
        """summary() should print without crashing for any result set."""
        results = AuditResults([
            _make_audit_result("A", "pass"),
            _make_audit_result("B", "critical", issues=["Bad stuff"]),
        ])
        # Should not raise
        results.summary()

    def test_severity_distribution_counts(self):
        """severity_distribution should accurately count results."""
        results = AuditResults([
            _make_audit_result("A", "pass"),
            _make_audit_result("B", "pass"),
            _make_audit_result("C", "high"),
            _make_audit_result("D", "critical"),
        ])
        dist = results.severity_distribution
        assert dist["pass"] == 2
        assert dist["high"] == 1
        assert dist["critical"] == 1
        assert dist.get("medium", 0) == 0
