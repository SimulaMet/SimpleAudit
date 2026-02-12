import unittest
from unittest.mock import MagicMock, patch
from simpleaudit.auditor import Auditor
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult

class TestExpectedBehavior(unittest.TestCase):
    def setUp(self):
        self.mock_provider = MagicMock()
        self.mock_provider.call.return_value = '{"severity": "pass", "summary": "Test summary"}'
        self.mock_provider.name = "mock_provider"
        self.mock_provider.model = "mock_model"

    @patch('simpleaudit.auditor.get_provider')
    @patch('simpleaudit.auditor.TargetClient')
    def test_auditor_passes_expected_behavior(self, mock_client, mock_get_provider):
        mock_get_provider.return_value = self.mock_provider
        
        auditor = Auditor("http://mock-target")
        # Mock _call_llm and client chat to avoid network calls
        auditor._call_llm = MagicMock(return_value="Mock probe")
        auditor.target.chat = MagicMock(return_value="Mock response")

        expected_behavior = ["Do not mention pink elephants", "Be polite"]
        
        # We want to intercept the call to judge_provider.call to check the prompt
        auditor.judge_provider.call = MagicMock(return_value='{"severity": "pass"}')

        result = auditor.run_scenario(
            name="Test Scenario",
            description="Test description",
            expected_behavior=expected_behavior,
            max_turns=1
        )

        # 1. Verify AuditResult stores exepcted_behavior
        self.assertEqual(result.expected_behavior, expected_behavior)

        # 2. Verify judge prompt contained the expected behavior
        call_args = auditor.judge_provider.call.call_args
        system_prompt = call_args[0][0]
        
        self.assertIn("SPECIFIC SCENARIO EXPECTATIONS:", system_prompt)
        self.assertIn("1. Do not mention pink elephants", system_prompt)
        self.assertIn("2. Be polite", system_prompt)

    @patch('simpleaudit.model_auditor.get_provider')
    def test_model_auditor_passes_expected_behavior(self, mock_get_provider):
        mock_get_provider.return_value = self.mock_provider
        
        auditor = ModelAuditor(provider="mock")
        # Mock internal calls
        auditor._call_target = MagicMock(return_value="Mock response")
        auditor._generate_probe = MagicMock(return_value="Mock probe")
        
        # Intercept judge call
        auditor.judge_provider.call = MagicMock(return_value='{"severity": "pass"}')

        expected_behavior = ["Must rhyme", "No emojis"]

        result = auditor.run_scenario(
            name="Test Model Scenario", 
            description="Desc",
            expected_behavior=expected_behavior,
            max_turns=1
        )

        # 1. Verify AuditResult
        self.assertEqual(result.expected_behavior, expected_behavior)

        # 2. Verify Prompt
        call_args = auditor.judge_provider.call.call_args
        system_prompt = call_args[0][0]
        
        self.assertIn("SPECIFIC SCENARIO EXPECTATIONS:", system_prompt)
        self.assertIn("1. Must rhyme", system_prompt)
        self.assertIn("2. No emojis", system_prompt)

if __name__ == '__main__':
    unittest.main()
