"""
Basic tests for SimpleAudit package.

Run with: pytest tests/test_basic.py
"""

import pytest
from simpleaudit import Auditor, AuditResults, AuditResult, get_scenarios, list_scenario_packs


def test_list_scenario_packs():
    """Test that scenario packs are available."""
    packs = list_scenario_packs()
    
    assert "safety" in packs
    assert "rag" in packs
    assert "health" in packs
    assert "all" in packs
    
    assert packs["safety"] > 0
    assert packs["rag"] > 0
    assert packs["health"] > 0
    assert packs["all"] == packs["safety"] + packs["rag"] + packs["health"]


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


def test_auditor_requires_api_key():
    """Test that Auditor requires API key or package."""
    import os
    
    # Temporarily remove API key
    original = os.environ.pop("ANTHROPIC_API_KEY", None)
    
    try:
        # Should raise either:
        # - ValueError if anthropic package installed but no API key
        # - ImportError if anthropic package not installed
        with pytest.raises((ValueError, ImportError)):
            Auditor(target="http://localhost:8000", prompt_for_key=False)
    finally:
        if original:
            os.environ["ANTHROPIC_API_KEY"] = original
