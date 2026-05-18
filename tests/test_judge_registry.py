"""
Tests for the built-in judge configuration registry.

Covers:
- list_judge_configs() returns exactly the five expected judges
- get_judge(name) returns a dict with required keys
- get_judge("unknown") raises ValueError
- Each config has non-empty probe_prompt and judge_prompt strings
- ModelAuditor resolves named judges and stores prompts correctly
"""

import pytest
from unittest.mock import MagicMock, patch

from simpleaudit.judges import get_judge, list_judge_configs, JUDGE_CONFIGS
from simpleaudit.model_auditor import ModelAuditor


EXPECTED_JUDGES = {"safety", "abstention", "helpfulness", "factuality", "harm"}
REQUIRED_CONFIG_KEYS = {"probe_prompt", "judge_prompt", "description"}


# ---------------------------------------------------------------------------
# list_judge_configs
# ---------------------------------------------------------------------------

class TestListJudgeConfigs:
    def test_returns_all_five_judges(self):
        configs = list_judge_configs()
        assert set(configs.keys()) == EXPECTED_JUDGES

    def test_values_are_non_empty_strings(self):
        for name, description in list_judge_configs().items():
            assert isinstance(description, str), f"{name} description is not a str"
            assert description.strip(), f"{name} description is empty"


# ---------------------------------------------------------------------------
# get_judge
# ---------------------------------------------------------------------------

class TestGetJudge:
    @pytest.mark.parametrize("name", sorted(EXPECTED_JUDGES))
    def test_returns_dict_with_required_keys(self, name):
        config = get_judge(name)
        for key in REQUIRED_CONFIG_KEYS:
            assert key in config, f"'{key}' missing from '{name}' config"

    @pytest.mark.parametrize("name", sorted(EXPECTED_JUDGES))
    def test_probe_prompt_is_non_empty_string(self, name):
        config = get_judge(name)
        assert isinstance(config["probe_prompt"], str)
        assert config["probe_prompt"].strip()

    @pytest.mark.parametrize("name", sorted(EXPECTED_JUDGES))
    def test_judge_prompt_is_non_empty_string(self, name):
        config = get_judge(name)
        assert isinstance(config["judge_prompt"], str)
        assert config["judge_prompt"].strip()

    def test_unknown_name_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown judge config"):
            get_judge("nonexistent_judge")

    def test_error_message_lists_available_judges(self):
        with pytest.raises(ValueError) as exc_info:
            get_judge("nonexistent")
        msg = str(exc_info.value)
        for name in EXPECTED_JUDGES:
            assert name in msg

    def test_returns_same_object_as_judge_configs_dict(self):
        for name in EXPECTED_JUDGES:
            assert get_judge(name) is JUDGE_CONFIGS[name]


# ---------------------------------------------------------------------------
# ModelAuditor — named judge resolution
# ---------------------------------------------------------------------------

def _make_auditor(**kwargs) -> ModelAuditor:
    with patch.object(ModelAuditor, "_create_anyllm_client", return_value=MagicMock()):
        return ModelAuditor(
            model="target",
            provider="openai",
            judge_model="judge",
            judge_provider="openai",
            show_progress=False,
            **kwargs,
        )


class TestModelAuditorJudgeResolution:
    @pytest.mark.parametrize("name", sorted(EXPECTED_JUDGES))
    def test_named_judge_sets_probe_and_judge_prompt(self, name):
        auditor = _make_auditor(judge=name)
        config = get_judge(name)
        assert auditor.probe_prompt == config["probe_prompt"]
        assert auditor.judge_prompt == config["judge_prompt"]

    def test_no_judge_leaves_prompts_none(self):
        auditor = _make_auditor()
        assert auditor.probe_prompt is None
        assert auditor.judge_prompt is None

    def test_explicit_probe_prompt_overrides_config(self):
        auditor = _make_auditor(judge="safety", probe_prompt="custom probe")
        assert auditor.probe_prompt == "custom probe"
        assert auditor.judge_prompt == get_judge("safety")["judge_prompt"]

    def test_explicit_judge_prompt_overrides_config(self):
        auditor = _make_auditor(judge="helpfulness", judge_prompt="custom judge")
        assert auditor.judge_prompt == "custom judge"

    def test_unknown_judge_raises(self):
        with pytest.raises(ValueError, match="Unknown judge config"):
            _make_auditor(judge="nonexistent")
