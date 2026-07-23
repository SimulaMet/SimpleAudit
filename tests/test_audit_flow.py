"""
Tests for end-to-end audit flow, save/load round-trip, plot smoke test,
AuditExperiment, parse_json_response edge cases, and error resilience.
"""

import asyncio
import json
import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest

from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult, AuditResults
from simpleaudit.experiment import AuditExperiment
from simpleaudit.utils import normalize_severity, severity_from_score
from tests.fakes import (
    FakeClient,
    _make_response,
    cycling_probe_auditor,
    cycling_target,
    fixed_probe_auditor,
    fixed_severity_judge,
    fixed_target,
    make_auditor,
    scripted_client,
)


# Check for optional dependencies
try:
    import matplotlib
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


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
        # 2 turns: separate clients per role make the sequence explicit
        auditor = make_auditor(
            target=cycling_target(["2+2 equals 4.", "Sure, it's basic arithmetic."]),
            judge=fixed_severity_judge("pass"),
            auditor=cycling_probe_auditor(["What is 2+2?", "Can you explain more?"]),
            max_turns=2,
            show_progress=False,
        )

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
        auditor = make_auditor(
            target=fixed_target("Target response"),
            judge=fixed_severity_judge("low"),
            auditor=fixed_probe_auditor("Probe message"),
            max_turns=1,
            show_progress=False,
        )

        scenarios = [{"name": "Custom Test", "description": "A custom test scenario"}]

        results = asyncio.run(auditor.run_async(scenarios=scenarios, max_turns=1))

        assert isinstance(results, AuditResults)
        assert len(results) == 1
        assert results[0].scenario_name == "Custom Test"
        assert results[0].severity == "low"

    def test_run_sync_wrapper(self):
        """The synchronous run() wrapper should work when no event loop is running."""
        auditor = make_auditor(
            target=fixed_target("Target reply"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("Probe"),
            max_turns=1,
            show_progress=False,
        )

        scenarios = [{"name": "Sync Test", "description": "Test sync wrapper"}]
        results = auditor.run(scenarios=scenarios, max_turns=1)

        assert isinstance(results, AuditResults)
        assert len(results) == 1

    def test_run_sync_raises_in_event_loop(self):
        """run() should raise RuntimeError when called from an active event loop."""
        auditor = make_auditor(
            target=fixed_target("Target reply"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("Probe"),
            max_turns=1,
            show_progress=False,
        )

        async def _test():
            with pytest.raises(RuntimeError, match="cannot be called from an active event loop"):
                auditor.run(scenarios=[{"name": "Loop Test", "description": "d"}])

        asyncio.run(_test())

    def test_run_scenario_with_system_prompt(self):
        """run_scenario should work correctly when a system prompt is set."""
        auditor = make_auditor(
            target=fixed_target("System prompt response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("Test probe"),
            max_turns=1,
            system_prompt="You are a helpful assistant.",
            show_progress=False,
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

    def test_run_scenario_system_prompt_forwarded_to_target(self):
        """system_prompt is forwarded as the system arg to target _call_async calls."""
        captured = []

        async def spy_call(client, model, system, user, response_format=None, history=None, **kwargs):
            captured.append({"model": model, "system": system})
            return ("response", 0, 0)

        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1,
            system_prompt="Be helpful and safe.",
            show_progress=False,
        )

        with patch.object(ModelAuditor, "_call_async", side_effect=spy_call):
            asyncio.run(auditor.run_scenario(
                name="System Prompt Test",
                description="desc",
                max_turns=1,
            ))

        target_calls = [c for c in captured if c["model"] == "fake-model"]
        assert len(target_calls) >= 1
        assert target_calls[0]["system"] == "Be helpful and safe."

    def test_run_scenario_with_expected_behavior(self):
        """run_scenario should pass expected_behavior to the result."""
        auditor = make_auditor(
            target=fixed_target("Response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("Probe"),
            max_turns=1,
            show_progress=False,
        )

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
        # _extract_from_text fallback must pick up the "critical" keyword
        assert result["severity"] == "critical"

    def test_custom_default_severity(self):
        """Custom default_severity should be used when parsing fails."""
        result = ModelAuditor.parse_json_response("not json at all", default_severity="low")
        # Nothing extractable from the text, so the custom default applies
        assert result["severity"] == "low"

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

    @pytest.mark.parametrize("payload", ["null", '"pass"', "42", '["a"]'])
    def test_valid_non_dict_json_falls_back_to_dict(self, payload):
        """Valid JSON that is not an object must still yield the dict contract."""
        result = ModelAuditor.parse_json_response(payload)
        assert isinstance(result, dict)
        assert result["severity"] == "ERROR"
        assert isinstance(result["issues_found"], list)

    def test_pass_indicators_are_word_bounded(self):
        """'inappropriate' must not be scored as the pass indicator 'appropriate'."""
        result = ModelAuditor.parse_json_response("The response was inappropriate.")
        assert result["severity"] != "pass"

    def test_pass_indicator_exact_word_still_matches(self):
        result = ModelAuditor.parse_json_response("The response was appropriate.")
        assert result["severity"] == "pass"

    def test_non_json_code_block_does_not_shadow_json_block(self):
        """A stray non-JSON code block before the JSON block must not win."""
        response = (
            "Notes:\n```\nnot json here\n```\nVerdict:\n"
            '```json\n{"severity": "low", "issues_found": [], '
            '"positive_behaviors": [], "summary": "ok", "recommendations": []}\n```'
        )
        result = ModelAuditor.parse_json_response(response)
        assert result["severity"] == "low"
        assert result["summary"] == "ok"


# --- normalize_severity ---


class TestNormalizeSeverity:
    """Severity vocabulary normalization onto the canonical ladder."""

    def test_none_string_is_pass_alias(self):
        assert normalize_severity("none") == "pass"

    def test_case_and_whitespace_insensitive(self):
        assert normalize_severity("PASS ") == "pass"
        assert normalize_severity("  Critical") == "critical"
        assert normalize_severity("NONE") == "pass"

    def test_none_value_means_missing_and_maps_to_medium(self):
        assert normalize_severity(None) == "medium"

    def test_error_maps_to_upper_error(self):
        assert normalize_severity("Error") == "ERROR"
        assert normalize_severity("error") == "ERROR"

    def test_canonical_values_pass_through(self):
        for sev in ("critical", "high", "medium", "low", "pass"):
            assert normalize_severity(sev) == sev

    def test_unknown_vocab_passes_through_stripped(self):
        assert normalize_severity("banana") == "banana"
        assert normalize_severity(" score:7 ") == "score:7"


# --- severity_from_score ---


class TestSeverityFromScore:
    """Score-based judges get a severity derived from their 1-10 score."""

    def test_score_bands(self):
        assert severity_from_score(10) == "pass"
        assert severity_from_score(9) == "pass"
        assert severity_from_score(8) == "low"
        assert severity_from_score(7) == "low"
        assert severity_from_score(6) == "medium"
        assert severity_from_score(5) == "medium"
        assert severity_from_score(4) == "high"
        assert severity_from_score(3) == "high"
        assert severity_from_score(2) == "critical"
        assert severity_from_score(1) == "critical"

    def test_non_numeric_returns_none(self):
        assert severity_from_score("great") is None
        assert severity_from_score(None) is None

    def test_run_scenario_derives_severity_from_score_only_judgment(self):
        judge = FakeClient(lambda **_: json.dumps({"score": 9, "reasoning": "solid"}))
        auditor = make_auditor(
            target=fixed_target("An answer."),
            judge=judge,
            judge_prompt="You are a score-based judge.",
            max_turns=1,
        )
        result = asyncio.run(auditor.run_scenario(
            name="scored", description="d", test_prompt="q",
        ))
        assert result.severity == "pass"
        assert result.judgment == {"score": 9, "reasoning": "solid"}

    def test_run_scenario_explicit_severity_wins_over_score(self):
        judge = FakeClient(
            lambda **_: json.dumps({"score": 9, "severity": "high", "reasoning": "r"})
        )
        auditor = make_auditor(
            target=fixed_target("An answer."),
            judge=judge,
            judge_prompt="You are a judge.",
            max_turns=1,
        )
        result = asyncio.run(auditor.run_scenario(
            name="scored", description="d", test_prompt="q",
        ))
        assert result.severity == "high"


# --- Error resilience ---


class TestErrorResilience:
    """Test behavior when things go wrong during auditing."""

    def test_run_scenario_with_bad_judge_response(self):
        """When the judge returns invalid JSON, should still produce a result."""
        auditor = make_auditor(
            target=fixed_target("Here's info about X."),
            judge=FakeClient(lambda **_: "This is NOT valid JSON at all!!!"),
            auditor=fixed_probe_auditor("What about X?"),
            max_turns=1,
            show_progress=False,
        )

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

    def test_partial_failure_preserves_conversation_and_tokens(self):
        """A target failure on turn 2 yields an ERROR result that keeps the
        completed turn's transcript and its accumulated token counts."""
        auditor = make_auditor(
            target=scripted_client([("First turn reply.", 20, 15)]),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("follow-up probe"),
            max_turns=2,
            show_progress=False,
        )

        result = asyncio.run(
            auditor.run_scenario(
                name="Partial",
                description="desc",
                test_prompt="first probe",
                max_turns=2,
            )
        )

        assert result.severity == "ERROR"
        # Turn 1 completed (user + assistant) and turn 2's probe was sent
        # before the target raised — none of it may be discarded.
        assert len(result.conversation) >= 2
        assert result.conversation[0] == {"role": "user", "content": "first probe"}
        assert result.conversation[1] == {"role": "assistant", "content": "First turn reply."}
        assert result.target_input_tokens == 20
        assert result.target_output_tokens == 15
        assert "ScriptedClientExhausted" in result.judgment["error"]


class TestCallRetries:
    """Transient API failures are retried up to max_retries before erroring."""

    class _FlakyClient:
        """Fails the first `fail_times` calls, then answers normally."""

        def __init__(self, fail_times: int, text: str = "A safe reply."):
            self.fail_times = fail_times
            self.calls = 0
            self.text = text

        async def acompletion(self, **kwargs):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise RuntimeError("transient provider blip")
            return _make_response(self.text)

    def _make_retry_auditor(self, target, max_retries):
        with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
            auditor = ModelAuditor(
                model="fake-model",
                provider="openai",
                judge_model="fake-judge",
                judge_provider="openai",
                max_turns=1,
                show_progress=False,
                max_retries=max_retries,
                retry_backoff=0,
            )
        auditor.target_client = target
        auditor.judge_client = fixed_severity_judge("pass")
        auditor.auditor_client = fixed_probe_auditor("probe")
        return auditor

    def test_single_failure_retried_and_scenario_succeeds(self):
        flaky = self._FlakyClient(fail_times=1)
        auditor = self._make_retry_auditor(flaky, max_retries=1)

        result = asyncio.run(
            auditor.run_scenario(
                name="Retry", description="d", test_prompt="hello", max_turns=1
            )
        )

        assert flaky.calls == 2
        assert result.severity == "pass"
        assert result.conversation[1]["content"] == "A safe reply."

    def test_no_retries_turns_failure_into_error_result(self):
        flaky = self._FlakyClient(fail_times=1)
        auditor = self._make_retry_auditor(flaky, max_retries=0)

        result = asyncio.run(
            auditor.run_scenario(
                name="NoRetry", description="d", test_prompt="hello", max_turns=1
            )
        )

        assert flaky.calls == 1
        assert result.severity == "ERROR"
        assert "transient provider blip" in result.judgment["error"]


# --- Language parameter ---


class TestLanguageParameter:
    """run_scenario forwards the language argument to _generate_probe_async."""

    def test_language_forwarded_from_run_scenario(self):
        captured_lang = []

        async def spy_probe(client, model, scenario, conversation,
                            language="English", probe_prompt=None, **kwargs):
            captured_lang.append(language)
            return ("probe", 0, 0)

        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )

        with patch.object(ModelAuditor, "_generate_probe_async", side_effect=spy_probe):
            asyncio.run(auditor.run_scenario(
                name="test", description="desc",
                max_turns=1, language="Norwegian",
            ))

        assert captured_lang[0] == "Norwegian"

    def test_language_defaults_to_english(self):
        captured_lang = []

        async def spy_probe(client, model, scenario, conversation,
                            language="English", probe_prompt=None, **kwargs):
            captured_lang.append(language)
            return ("probe", 0, 0)

        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )

        with patch.object(ModelAuditor, "_generate_probe_async", side_effect=spy_probe):
            asyncio.run(auditor.run_scenario(name="test", description="desc", max_turns=1))

        assert captured_lang[0] == "English"


# --- max_workers parameter ---


class TestMaxWorkers:
    """run() / run_async() respect the max_workers concurrency cap."""

    def test_max_workers_all_scenarios_complete(self):
        """All scenarios complete and none are dropped."""
        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )
        scenarios = [{"name": f"s{i}", "description": f"d{i}"} for i in range(6)]
        results = auditor.run(scenarios=scenarios, max_workers=2)
        assert len(results) == 6

    def test_max_workers_1_all_scenarios_complete(self):
        """max_workers=1 serializes execution; all scenarios still complete."""
        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )
        scenarios = [{"name": f"s{i}", "description": f"d{i}"} for i in range(4)]
        results = auditor.run(scenarios=scenarios, max_workers=1)
        assert len(results) == 4

    def test_max_workers_concurrency_cap(self):
        """In-flight target calls never exceed max_workers simultaneously."""
        active = [0]
        peak = [0]

        class ConcurrencyTrackingTarget:
            async def acompletion(self, **kwargs):
                active[0] += 1
                if active[0] > peak[0]:
                    peak[0] = active[0]
                await asyncio.sleep(0)
                active[0] -= 1
                return _make_response("response")

        max_workers = 2
        auditor = make_auditor(
            target=ConcurrencyTrackingTarget(),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )
        scenarios = [{"name": f"s{i}", "description": f"d{i}"} for i in range(6)]
        asyncio.run(auditor.run_async(scenarios=scenarios, max_workers=max_workers))
        assert peak[0] <= max_workers

    def test_max_workers_zero_raises(self):
        """max_workers=0 would deadlock the semaphore — it must raise upfront."""
        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )
        scenarios = [{"name": "s0", "description": "d0"}]
        with pytest.raises(ValueError, match="max_workers"):
            asyncio.run(auditor.run_async(scenarios=scenarios, max_workers=0))


# --- String pack name in run() ---


class TestRunWithPackName:
    """run('pack_name') fetches the pack and runs all scenarios."""

    def test_run_safety_string_resolves_and_runs(self):
        from simpleaudit import get_scenarios
        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )
        results = auditor.run("safety", max_turns=1)
        assert len(results) == len(get_scenarios("safety"))

    def test_run_invalid_pack_name_raises(self):
        auditor = make_auditor(
            target=fixed_target("response"),
            judge=fixed_severity_judge("pass"),
            auditor=fixed_probe_auditor("probe"),
            max_turns=1, show_progress=False,
        )
        with pytest.raises(ValueError):
            auditor.run("nonexistent_pack_xyz", max_turns=1)


# --- Save/load: judgment and expected_behavior fields ---


class TestSaveLoadExtendedFields:
    """save/load round-trip preserves judgment and expected_behavior on AuditResult."""

    def test_save_load_preserves_judgment(self):
        results = AuditResults([AuditResult(
            scenario_name="T", scenario_description="D", conversation=[],
            severity="low", issues_found=[], positive_behaviors=[], summary="",
            recommendations=[], judgment={"score": 7, "reasoning": "test reasoning"},
        )])
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            filepath = f.name
        try:
            results.save(filepath)
            loaded = AuditResults.load(filepath)
            assert loaded[0].judgment == {"score": 7, "reasoning": "test reasoning"}
        finally:
            os.unlink(filepath)

    def test_save_load_preserves_expected_behavior(self):
        results = AuditResults([AuditResult(
            scenario_name="T", scenario_description="D", conversation=[],
            severity="pass", issues_found=[], positive_behaviors=[], summary="",
            recommendations=[], expected_behavior=["Should refuse", "Should cite sources"],
        )])
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            filepath = f.name
        try:
            results.save(filepath)
            loaded = AuditResults.load(filepath)
            assert loaded[0].expected_behavior == ["Should refuse", "Should cite sources"]
        finally:
            os.unlink(filepath)

    def test_save_load_none_judgment_stays_none(self):
        results = AuditResults([_make_audit_result()])
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            filepath = f.name
        try:
            results.save(filepath)
            loaded = AuditResults.load(filepath)
            assert loaded[0].judgment is None
        finally:
            os.unlink(filepath)
