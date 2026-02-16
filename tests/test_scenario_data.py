"""
Tests validating the integrity of all scenario pack data.

Ensures every scenario in every pack has the required fields and valid structure.
"""

import pytest

from simpleaudit.scenarios import SCENARIO_PACKS, get_scenarios, list_scenario_packs


class TestScenarioDataIntegrity:
    """Validate that all scenario packs have correct data structure."""

    REQUIRED_FIELDS = {"name", "description"}

    @pytest.fixture
    def all_pack_names(self):
        """Return all pack names except 'all' (which is a composite)."""
        return [name for name in SCENARIO_PACKS if name != "all"]

    def test_all_packs_non_empty(self, all_pack_names):
        """Every scenario pack should contain at least one scenario."""
        for pack_name in all_pack_names:
            scenarios = get_scenarios(pack_name)
            assert len(scenarios) > 0, f"Pack '{pack_name}' is empty"

    def test_all_scenarios_have_required_fields(self, all_pack_names):
        """Every scenario must have 'name' and 'description' fields."""
        for pack_name in all_pack_names:
            for i, scenario in enumerate(get_scenarios(pack_name)):
                for field in self.REQUIRED_FIELDS:
                    assert field in scenario, (
                        f"Pack '{pack_name}', scenario {i}: missing '{field}'"
                    )

    def test_all_names_are_non_empty_strings(self, all_pack_names):
        """Scenario names must be non-empty strings."""
        for pack_name in all_pack_names:
            for i, scenario in enumerate(get_scenarios(pack_name)):
                name = scenario["name"]
                assert isinstance(name, str), (
                    f"Pack '{pack_name}', scenario {i}: name is {type(name)}, expected str"
                )
                assert len(name.strip()) > 0, (
                    f"Pack '{pack_name}', scenario {i}: name is empty/whitespace"
                )

    def test_all_descriptions_are_non_empty_strings(self, all_pack_names):
        """Scenario descriptions must be non-empty strings."""
        for pack_name in all_pack_names:
            for i, scenario in enumerate(get_scenarios(pack_name)):
                desc = scenario["description"]
                assert isinstance(desc, str), (
                    f"Pack '{pack_name}', scenario {i} ({scenario.get('name', '?')}): "
                    f"description is {type(desc)}, expected str"
                )
                assert len(desc.strip()) > 0, (
                    f"Pack '{pack_name}', scenario {i} ({scenario.get('name', '?')}): "
                    f"description is empty/whitespace"
                )

    def test_expected_behavior_is_list_when_present(self, all_pack_names):
        """If expected_behavior is present, it must be a list of strings."""
        for pack_name in all_pack_names:
            for i, scenario in enumerate(get_scenarios(pack_name)):
                eb = scenario.get("expected_behavior")
                if eb is not None:
                    assert isinstance(eb, list), (
                        f"Pack '{pack_name}', scenario {i} ({scenario['name']}): "
                        f"expected_behavior is {type(eb)}, expected list"
                    )
                    for j, item in enumerate(eb):
                        assert isinstance(item, str), (
                            f"Pack '{pack_name}', scenario {i} ({scenario['name']}): "
                            f"expected_behavior[{j}] is {type(item)}, expected str"
                        )

    def test_same_name_scenarios_have_different_descriptions(self, all_pack_names):
        """If two scenarios share a name, they must have different descriptions."""
        for pack_name in all_pack_names:
            scenarios = get_scenarios(pack_name)
            by_name: dict[str, list[str]] = {}
            for s in scenarios:
                by_name.setdefault(s["name"], []).append(s["description"])
            for name, descs in by_name.items():
                if len(descs) > 1:
                    assert len(set(descs)) == len(descs), (
                        f"Pack '{pack_name}': scenarios named '{name}' share "
                        f"identical descriptions — these are true duplicates"
                    )

    def test_all_pack_is_union_of_individual_packs(self, all_pack_names):
        """The 'all' pack should be the combination of all individual packs."""
        all_scenarios = get_scenarios("all")
        combined = []
        for pack_name in all_pack_names:
            combined.extend(get_scenarios(pack_name))
        assert len(all_scenarios) == len(combined), (
            f"'all' pack has {len(all_scenarios)} scenarios, "
            f"but individual packs sum to {len(combined)}"
        )

    def test_list_scenario_packs_counts(self, all_pack_names):
        """list_scenario_packs should return correct counts for each pack."""
        packs = list_scenario_packs()
        for pack_name in all_pack_names:
            expected = len(get_scenarios(pack_name))
            assert packs[pack_name] == expected, (
                f"list_scenario_packs reports {packs[pack_name]} for '{pack_name}', "
                f"expected {expected}"
            )

    def test_get_scenarios_unknown_pack_raises(self):
        """Requesting a non-existent pack should raise ValueError."""
        with pytest.raises(ValueError, match="Unknown scenario pack"):
            get_scenarios("nonexistent_pack_xyz")

    def test_scenario_names_reasonable_length(self, all_pack_names):
        """Scenario names should be between 3 and 200 characters."""
        for pack_name in all_pack_names:
            for scenario in get_scenarios(pack_name):
                name = scenario["name"]
                assert 3 <= len(name) <= 200, (
                    f"Pack '{pack_name}': name '{name}' length {len(name)} "
                    f"outside expected range [3, 200]"
                )
