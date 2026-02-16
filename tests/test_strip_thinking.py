"""
Tests for ModelAuditor.strip_thinking and _call_async.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from simpleaudit.model_auditor import ModelAuditor


# --- strip_thinking tests ---


class TestStripThinking:
    """Test the strip_thinking static method."""

    def test_no_thinking_tags(self):
        """Text without thinking tags is returned as-is."""
        text = "Hello, how can I help you today?"
        assert ModelAuditor.strip_thinking(text) == text

    def test_basic_think_tags(self):
        """Content inside <think>...</think> is removed."""
        text = "<think>I need to consider this carefully.</think>Here is my answer."
        assert ModelAuditor.strip_thinking(text) == "Here is my answer."

    def test_basic_thinking_tags(self):
        """Content inside <thinking>...</thinking> is removed."""
        text = "<thinking>Let me reason about this.</thinking>The answer is 42."
        assert ModelAuditor.strip_thinking(text) == "The answer is 42."

    def test_case_insensitive(self):
        """Tags should be matched case-insensitively."""
        text = "<THINK>Some reasoning.</THINK>Result here."
        assert ModelAuditor.strip_thinking(text) == "Result here."

    def test_mixed_case(self):
        """Mixed case tags should work."""
        text = "<Think>Reasoning.</Think>Final answer."
        assert ModelAuditor.strip_thinking(text) == "Final answer."

    def test_unclosed_tag_returns_empty(self):
        """Unclosed thinking tag returns empty string (model only produced thinking)."""
        text = "<think>I'm still thinking about this and haven't finished..."
        assert ModelAuditor.strip_thinking(text) == ""

    def test_multiple_thinking_blocks(self):
        """Multiple thinking blocks should all be removed."""
        text = "<think>First thought.</think>Part 1. <think>Second thought.</think>Part 2."
        result = ModelAuditor.strip_thinking(text)
        assert "First thought" not in result
        assert "Second thought" not in result
        assert "Part 1." in result
        assert "Part 2." in result

    def test_multiline_thinking(self):
        """Thinking blocks spanning multiple lines should be removed."""
        text = "<think>\nLine 1\nLine 2\nLine 3\n</think>\nActual response."
        assert ModelAuditor.strip_thinking(text) == "Actual response."

    def test_empty_string(self):
        """Empty string should return empty string."""
        assert ModelAuditor.strip_thinking("") == ""

    def test_only_thinking_with_close(self):
        """If the model only produced thinking but closed the tag, return empty."""
        text = "<think>All I have is thoughts.</think>"
        assert ModelAuditor.strip_thinking(text) == ""

    def test_whitespace_in_tags(self):
        """Tags with extra whitespace should still be matched."""
        text = "< think >Spaced out thinking.</ think >Answer."
        result = ModelAuditor.strip_thinking(text)
        assert "Spaced out thinking" not in result
        assert "Answer." in result

    def test_thinking_tag_variant(self):
        """The <thinking> variant (vs <think>) should also be stripped."""
        text = "<thinking>Deep thoughts.</thinking>Final output."
        assert ModelAuditor.strip_thinking(text) == "Final output."

    def test_preserves_surrounding_whitespace_stripped(self):
        """Result should be stripped of leading/trailing whitespace."""
        text = "<think>Thoughts.</think>   Answer with spaces   "
        assert ModelAuditor.strip_thinking(text) == "Answer with spaces"


# --- _call_async tests ---


class TestCallAsync:
    """Test the _call_async static method."""

    def _make_mock_client(self, response_text: str) -> MagicMock:
        """Create a mock client that returns the given text."""
        mock_client = MagicMock()
        mock_message = MagicMock()
        mock_message.content = response_text
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_client.acompletion = AsyncMock(return_value=mock_response)
        return mock_client

    def test_basic_call(self):
        """Basic call with system and user messages."""
        client = self._make_mock_client("Hello!")
        result = asyncio.run(
            ModelAuditor._call_async(client, "gpt-4", "Be helpful", "Hi")
        )
        assert result == "Hello!"
        client.acompletion.assert_called_once()
        call_kwargs = client.acompletion.call_args[1]
        assert call_kwargs["model"] == "gpt-4"
        assert len(call_kwargs["messages"]) == 2
        assert call_kwargs["messages"][0]["role"] == "system"
        assert call_kwargs["messages"][1]["role"] == "user"
        assert call_kwargs["stream"] is False
        assert "response_format" not in call_kwargs

    def test_no_system_prompt(self):
        """When system is None, only user message should be sent."""
        client = self._make_mock_client("Response")
        result = asyncio.run(
            ModelAuditor._call_async(client, "gpt-4", None, "Hi")
        )
        assert result == "Response"
        call_kwargs = client.acompletion.call_args[1]
        assert len(call_kwargs["messages"]) == 1
        assert call_kwargs["messages"][0]["role"] == "user"

    def test_response_format_passed_when_provided(self):
        """response_format should only be included when explicitly passed."""
        client = self._make_mock_client("JSON response")
        asyncio.run(
            ModelAuditor._call_async(
                client, "gpt-4", "System", "User",
                response_format={"type": "json_object"},
            )
        )
        call_kwargs = client.acompletion.call_args[1]
        assert call_kwargs["response_format"] == {"type": "json_object"}

    def test_response_format_not_passed_when_none(self):
        """response_format should NOT be in kwargs when None (the bug fix)."""
        client = self._make_mock_client("Normal response")
        asyncio.run(
            ModelAuditor._call_async(client, "gpt-4", "System", "User")
        )
        call_kwargs = client.acompletion.call_args[1]
        assert "response_format" not in call_kwargs

    def test_empty_system_string_treated_as_falsy(self):
        """Empty string system prompt should not be sent."""
        client = self._make_mock_client("Response")
        asyncio.run(
            ModelAuditor._call_async(client, "gpt-4", "", "User message")
        )
        call_kwargs = client.acompletion.call_args[1]
        # Empty string is falsy, so no system message
        assert len(call_kwargs["messages"]) == 1
        assert call_kwargs["messages"][0]["role"] == "user"
