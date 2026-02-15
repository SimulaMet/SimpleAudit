import unittest
from unittest.mock import MagicMock, patch
from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.results import AuditResult

class TestExpectedBehavior(unittest.TestCase):
    """Test expected behavior functionality with ModelAuditor."""
    
    def test_model_auditor_instantiation(self):
        """Test that ModelAuditor can be instantiated with basic config."""
        with patch("simpleaudit.model_auditor.AnyLLM") as mock_anyllm:
            mock_provider = MagicMock()
            mock_provider.model = "test-model"
            mock_anyllm.create.return_value = mock_provider
            
            # Create auditor instance
            auditor = ModelAuditor(
                model="claude-sonnet-4-20250514",
                provider="anthropic",
                judge_model="claude-sonnet-4-20250514",
                judge_provider="anthropic",
            )
            
            # Verify basic configuration
            assert auditor.target_model is not None
            assert auditor.system_prompt is None

if __name__ == '__main__':
    unittest.main()
