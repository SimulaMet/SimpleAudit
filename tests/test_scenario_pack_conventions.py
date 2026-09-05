"""
Runs the ERROR-level rules of scripts/check_scenario_pack.py in CI.

Only packs listed in CONFORMING_PACKS are checked. Legacy packs (ung, bullshitbench,
hei_refusal, ...) predate the pack conventions and are not renamed or restructured,
because scenario names are keys in saved results. A new pack adds itself here; the
full report (including WARN and INFO) is produced by running the script directly.
"""

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_scenario_pack.py"

CONFORMING_PACKS = [
    "nav_aap",
    "skatteetaten",
    "helfo",
    "lanekassen", "arbeidstilsynet_arbeidstid"]


def _load_checker():
    spec = importlib.util.spec_from_file_location("check_scenario_pack", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("pack", CONFORMING_PACKS)
def test_pack_has_no_convention_errors(pack):
    checker = _load_checker()
    report = checker.run_checks(pack, str(REPO))
    errors = [f"[{where}] {msg}" for _, where, msg in report.errors]
    assert not errors, f"{pack}: " + "; ".join(errors)


def test_checker_flags_a_broken_scenario():
    checker = _load_checker()
    rep = checker.Report()
    broken = [{
        "schema_version": "2.0",
        "name": "X",
        "description": "",
        "test_prompt": "",
        "language": "norsk",
        "expected_behavior": "not a list",
        "category": "Nope",
        "subcategory": "Nope",
        "severity": "fatal",
        "source": {"type": "made_up"},
        "metadata": {"pair_id": "P1", "branch": "majority", "pair_type": "branch_set"},
    }]
    checker.check_scenarios("broken", broken, rep)
    messages = " | ".join(msg for _, _, msg in rep.errors)
    for expected in ("name length", "test_prompt empty", "description empty", "not in the guideline taxonomy",
                     "severity 'fatal'", "source.type 'made_up'", "expected_behavior must be a list",
                     "descriptive branch label", "single member"):
        assert expected in messages, expected
