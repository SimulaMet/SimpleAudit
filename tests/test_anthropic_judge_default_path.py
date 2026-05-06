"""
Smoke test for the Anthropic judge default path (json_format=True).

Verifies that _judge_conversation_async completes without raising
UnsupportedParameterError or any json_format-related any-llm error when
using an Anthropic judge with json_format=True (the default).

Run with: pytest tests/test_anthropic_judge_default_path.py -v
Requires: ANTHROPIC_API_KEY in environment
"""

import os

import pytest
from any_llm import AnyLLM

from simpleaudit.model_auditor import ModelAuditor

JUDGE_MODEL = "claude-haiku-4-5-20251001"


@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
async def test_anthropic_judge_default_path_completes():
    """Default-path Anthropic judge (json_format=True) returns valid judgment dict."""
    client = AnyLLM.create("anthropic")
    conversation = [
        {"role": "user", "content": "What is the capital of France?"},
        {"role": "assistant", "content": "The capital of France is Paris."},
    ]

    judgment = await ModelAuditor._judge_conversation_async(
        client=client,
        model=JUDGE_MODEL,
        scenario="Factual question about European capitals",
        conversation=conversation,
        json_format=True,
    )

    assert "severity" in judgment, f"Missing 'severity' in judgment: {judgment}"
    assert judgment["severity"] in {"critical", "high", "medium", "low", "pass"}, (
        f"Unexpected severity value: {judgment['severity']}"
    )
    assert "issues_found" in judgment
    assert "positive_behaviors" in judgment
    assert "summary" in judgment
    assert "recommendations" in judgment
    assert isinstance(judgment["issues_found"], list)
    assert isinstance(judgment["positive_behaviors"], list)
    assert isinstance(judgment["recommendations"], list)
    assert isinstance(judgment["summary"], str)
