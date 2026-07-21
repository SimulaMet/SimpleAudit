"""
Basic tests for SimpleAudit package.

Run with: pytest tests/test_basic.py
"""

import pytest
from simpleaudit import ModelAuditor, AuditResults, AuditResult, get_scenarios, list_scenario_packs


def test_list_scenario_packs():
    """Test that scenario packs are available."""
    packs = list_scenario_packs()
    
    assert "safety" in packs
    assert "rag" in packs
    assert "health" in packs
    assert "system_prompt" in packs
    assert "helpmed" in packs
    assert "ung" in packs
    assert "nav_aap" in packs
    assert "all" in packs
    assert "skatteetaten" in packs
    assert "helfo" in packs

    assert packs["safety"] > 0
    assert packs["rag"] > 0
    assert packs["health"] > 0
    assert packs["system_prompt"] > 0
    assert packs["helpmed"] > 0
    assert packs["ung"] > 0
    assert packs["nav_aap"] > 0
    assert packs["skatteetaten"] >= 0
    assert packs["helfo"] > 0
    assert packs["all"] == packs["safety"] + packs["rag"] + packs["health"] + packs["system_prompt"] + packs["helpmed"] + packs["ung"] + packs["bullshitbench"] + packs["health_bullshit"] + packs["hei_refusal"] + packs["nav_aap"] + packs["skatteetaten"] + packs["helfo"]


def test_get_scenarios():
    """Test getting scenarios from packs."""
    safety = get_scenarios("safety")
    
    assert isinstance(safety, list)
    assert len(safety) > 0
    
    for scenario in safety:
        assert "name" in scenario
        assert "description" in scenario


def test_get_scenarios_invalid():
    """Test that invalid pack name raises error."""
    with pytest.raises(ValueError):
        get_scenarios("nonexistent")


def test_audit_result_dataclass():
    """Test AuditResult dataclass."""
    result = AuditResult(
        scenario_name="Test",
        scenario_description="Test description",
        conversation=[{"role": "user", "content": "hello"}],
        severity="pass",
        issues_found=[],
        positive_behaviors=["good behavior"],
        summary="All good",
        recommendations=[],
    )
    
    assert result.scenario_name == "Test"
    assert result.severity == "pass"
    
    d = result.to_dict()
    assert d["scenario_name"] == "Test"


def test_audit_results_class():
    """Test AuditResults class."""
    results = AuditResults([
        AuditResult(
            scenario_name="Test 1",
            scenario_description="Desc 1",
            conversation=[],
            severity="pass",
            issues_found=[],
            positive_behaviors=[],
            summary="Good",
            recommendations=[],
        ),
        AuditResult(
            scenario_name="Test 2",
            scenario_description="Desc 2",
            conversation=[],
            severity="critical",
            issues_found=["Big issue"],
            positive_behaviors=[],
            summary="Bad",
            recommendations=["Fix it"],
        ),
    ])
    
    assert len(results) == 2
    assert results.passed == 1
    assert results.failed == 1
    assert results.critical_count == 1
    assert results.score == 50.0  # (4 + 0) / 8 * 100
    
    assert "pass" in results.severity_distribution
    assert "critical" in results.severity_distribution


def test_audit_results_severity_distribution_all_levels():
    """severity_distribution counts all five severity levels correctly."""
    from simpleaudit import AuditResult, AuditResults
    results = AuditResults([
        AuditResult(scenario_name=f"T{s}", scenario_description="d",
                    conversation=[], severity=s, issues_found=[],
                    positive_behaviors=[], summary="", recommendations=[])
        for s in ("pass", "low", "medium", "high", "critical")
    ])
    dist = results.severity_distribution
    for level in ("pass", "low", "medium", "high", "critical"):
        assert dist[level] == 1, f"Expected count 1 for '{level}', got {dist.get(level)}"


def test_model_auditor_requires_provider():
    """Test that ModelAuditor raises MissingApiKeyError when no API key is available."""
    import os

    try:
        from any_llm.exceptions import MissingApiKeyError
    except ImportError:
        pytest.skip("any_llm.exceptions not available")

    original_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
    original_openai = os.environ.pop("OPENAI_API_KEY", None)
    original_xai = os.environ.pop("XAI_API_KEY", None)

    try:
        with pytest.raises(MissingApiKeyError):
            ModelAuditor(
                model="claude-sonnet-4-20250514",
                provider="anthropic",
                judge_model="claude-sonnet-4-20250514",
                judge_provider="anthropic",
            )
    finally:
        if original_anthropic:
            os.environ["ANTHROPIC_API_KEY"] = original_anthropic
        if original_openai:
            os.environ["OPENAI_API_KEY"] = original_openai
        if original_xai:
            os.environ["XAI_API_KEY"] = original_xai
