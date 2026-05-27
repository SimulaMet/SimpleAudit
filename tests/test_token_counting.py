"""
Tests for token counting in AuditResult / AuditResults.

Covers:
- AuditResult stores per-component token counts
- AuditResults.token_usage aggregates all six components correctly
- token_usage["total"] is the sum of all components
- save/load round-trip preserves token counts
- End-to-end: fake client returning real usage values produces non-zero token counts
"""

import asyncio
import json

import pytest

from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult, AuditResults
from tests.fakes import make_auditor, scripted_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(**token_kwargs) -> AuditResult:
    defaults = dict(
        scenario_name="s",
        scenario_description="d",
        conversation=[],
        severity="pass",
        issues_found=[],
        positive_behaviors=[],
        summary="",
        recommendations=[],
        auditor_input_tokens=0,
        auditor_output_tokens=0,
        judge_input_tokens=0,
        judge_output_tokens=0,
        target_input_tokens=0,
        target_output_tokens=0,
    )
    defaults.update(token_kwargs)
    return AuditResult(**defaults)



# ---------------------------------------------------------------------------
# AuditResult — field presence and defaults
# ---------------------------------------------------------------------------

class TestAuditResultTokenFields:
    def test_default_token_counts_are_zero(self):
        result = _make_result()
        assert result.auditor_input_tokens == 0
        assert result.auditor_output_tokens == 0
        assert result.judge_input_tokens == 0
        assert result.judge_output_tokens == 0
        assert result.target_input_tokens == 0
        assert result.target_output_tokens == 0

    def test_explicit_token_counts_stored(self):
        result = _make_result(
            auditor_input_tokens=10,
            auditor_output_tokens=20,
            judge_input_tokens=30,
            judge_output_tokens=40,
            target_input_tokens=50,
            target_output_tokens=60,
        )
        assert result.auditor_input_tokens == 10
        assert result.auditor_output_tokens == 20
        assert result.judge_input_tokens == 30
        assert result.judge_output_tokens == 40
        assert result.target_input_tokens == 50
        assert result.target_output_tokens == 60

    def test_to_dict_includes_token_fields(self):
        result = _make_result(target_input_tokens=7, judge_output_tokens=3)
        d = result.to_dict()
        assert d["target_input_tokens"] == 7
        assert d["judge_output_tokens"] == 3


# ---------------------------------------------------------------------------
# AuditResults — token_usage aggregation
# ---------------------------------------------------------------------------

class TestAuditResultsTokenUsage:
    def test_token_usage_sums_across_results(self):
        results = AuditResults([
            _make_result(auditor_input_tokens=10, target_output_tokens=5),
            _make_result(auditor_input_tokens=20, target_output_tokens=15),
        ])
        tu = results.token_usage
        assert tu["auditor_input"] == 30
        assert tu["target_output"] == 20

    def test_token_usage_total_is_sum_of_all_components(self):
        results = AuditResults([
            _make_result(
                auditor_input_tokens=1,
                auditor_output_tokens=2,
                judge_input_tokens=3,
                judge_output_tokens=4,
                target_input_tokens=5,
                target_output_tokens=6,
            )
        ])
        tu = results.token_usage
        assert tu["total"] == 1 + 2 + 3 + 4 + 5 + 6

    def test_all_six_components_present_in_token_usage(self):
        results = AuditResults([_make_result()])
        tu = results.token_usage
        for key in ("auditor_input", "auditor_output", "judge_input",
                    "judge_output", "target_input", "target_output", "total"):
            assert key in tu, f"'{key}' missing from token_usage"

    def test_zero_tokens_when_all_zero(self):
        results = AuditResults([_make_result(), _make_result()])
        assert results.token_usage["total"] == 0

    def test_individual_aggregation_properties(self):
        results = AuditResults([
            _make_result(auditor_input_tokens=5),
            _make_result(auditor_input_tokens=7),
        ])
        assert results.total_auditor_input_tokens == 12

    def test_to_dict_includes_token_usage(self):
        results = AuditResults([_make_result(target_input_tokens=42)])
        d = results.to_dict()
        assert d["summary"]["token_usage"]["target_input"] == 42


# ---------------------------------------------------------------------------
# save/load round-trip preserves token counts
# ---------------------------------------------------------------------------

class TestTokenRoundTrip:
    def test_save_load_preserves_per_result_token_counts(self, tmp_path):
        results = AuditResults([
            _make_result(
                auditor_input_tokens=11,
                auditor_output_tokens=22,
                judge_input_tokens=33,
                judge_output_tokens=44,
                target_input_tokens=55,
                target_output_tokens=66,
            )
        ])
        path = str(tmp_path / "results.json")
        results.save(path)
        loaded = AuditResults.load(path)

        r = loaded[0]
        assert r.auditor_input_tokens == 11
        assert r.auditor_output_tokens == 22
        assert r.judge_input_tokens == 33
        assert r.judge_output_tokens == 44
        assert r.target_input_tokens == 55
        assert r.target_output_tokens == 66

    def test_save_load_preserves_aggregated_total(self, tmp_path):
        results = AuditResults([_make_result(auditor_input_tokens=100, judge_output_tokens=50)])
        path = str(tmp_path / "results.json")
        results.save(path)
        loaded = AuditResults.load(path)

        assert loaded.token_usage["total"] == 150


# ---------------------------------------------------------------------------
# End-to-end: fake client produces non-zero token counts
# ---------------------------------------------------------------------------

_JUDGE_JSON = '{"severity":"pass","issues_found":[],"positive_behaviors":[],"summary":"ok","recommendations":[]}'


class TestTokenCountingEndToEnd:
    def test_run_scenario_records_token_counts(self):
        """Separate scripted clients per role → token fields in AuditResult are non-zero."""
        auditor = make_auditor(
            target=scripted_client([("target response", 20, 15)]),
            judge=scripted_client([(_JUDGE_JSON, 10, 8)]),
            auditor=scripted_client([("probe text", 5, 3)]),
            max_turns=1, show_progress=False, verbose=False,
        )

        result = asyncio.run(
            auditor.run_scenario(name="test", description="desc", max_turns=1)
        )

        assert result.auditor_input_tokens == 5
        assert result.auditor_output_tokens == 3
        assert result.target_input_tokens == 20
        assert result.target_output_tokens == 15
        assert result.judge_input_tokens == 10
        assert result.judge_output_tokens == 8

    def test_run_async_token_usage_aggregated_across_scenarios(self):
        """run_async over two scenarios → both contribute to token totals."""
        scenarios = [
            {"name": "s1", "description": "d1"},
            {"name": "s2", "description": "d2"},
        ]

        # Each client gets 2 responses (one per scenario).
        auditor = make_auditor(
            target=scripted_client([("target", 20, 15), ("target", 20, 15)]),
            judge=scripted_client([(_JUDGE_JSON, 10, 8), (_JUDGE_JSON, 10, 8)]),
            auditor=scripted_client([("probe", 5, 3), ("probe", 5, 3)]),
            max_turns=1, show_progress=False, verbose=False,
        )

        results = asyncio.run(auditor.run_async(scenarios=scenarios, max_turns=1))

        assert results.total_auditor_input_tokens == 10  # 5 + 5
        assert results.total_target_input_tokens == 40   # 20 + 20
        assert results.total_judge_output_tokens == 16   # 8 + 8
        assert results.token_usage["total"] > 0
