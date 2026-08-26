#!/usr/bin/env python3
"""Real WiggleRunner test using Qwen via an OpenAI-compatible endpoint.

All three roles (candidate judge, persuader, observer) use the same
Qwen/Qwen3.8-27B-FP8 model served via vLLM.

Configure via OPENAI_BASE_URL and OPENAI_API_KEY env vars.
"""

import asyncio
import os
import sys

# Add parent dir so we can import simpleaudit
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from simpleaudit import WiggleRunner
from simpleaudit.scenarios import get_scenarios

# --- Configuration -----------------------------------------------------------

BASE_URL = os.environ.get("OPENAI_BASE_URL", "")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL = os.environ.get("OPENAI_MODEL", "Qwen/Qwen3.8-27B-FP8")

if not BASE_URL:
    raise SystemExit(
        "OPENAI_BASE_URL env var is not set. "
        "Point it at your OpenAI-compatible endpoint."
    )
if not API_KEY:
    raise SystemExit(
        "OPENAI_API_KEY env var is not set. "
        "Export it before running this script."
    )

# All 8 scenarios, all single-turn pressure levels
SCENARIO_NAMES = None  # None = use all scenarios from the pack

LEVELS = ["L0", "L1", "L2", "L3", "L4"]


async def main() -> None:
    print("=" * 70)
    print("REAL WIGGLE TEST — Qwen/Qwen3.8-27B-FP8")
    print("=" * 70)
    print(f"  Model:    {MODEL}")
    print(f"  Endpoint: {BASE_URL}")

    # Filter the full pack down to our subset (or use all)
    all_scenarios = get_scenarios("judge_the_judge")
    if SCENARIO_NAMES is None:
        scenarios = all_scenarios
    else:
        scenarios = [s for s in all_scenarios if s["name"] in SCENARIO_NAMES]
        if len(scenarios) != len(SCENARIO_NAMES):
            found = [s["name"] for s in scenarios]
            print(f"WARNING: only found {found} from {SCENARIO_NAMES}")

    print(f"  Levels:   {LEVELS}")
    if SCENARIO_NAMES is None:
        print(f"  Scenarios: all {len(scenarios)} from pack")
    else:
        print(f"  Scenarios: {SCENARIO_NAMES}")
    print()

    runner = WiggleRunner(
        candidate_judge_model=MODEL,
        candidate_judge_provider="openai",
        candidate_judge_api_key=API_KEY,
        candidate_judge_base_url=BASE_URL,
        persuader_model=MODEL,
        persuader_provider="openai",
        persuader_api_key=API_KEY,
        persuader_base_url=BASE_URL,
        observer_model=MODEL,
        observer_provider="openai",
        observer_api_key=API_KEY,
        observer_base_url=BASE_URL,
        observer_version=f"{MODEL} (vLLM)",
        verbose=True,
    )

    profile = await runner.run(
        scenarios=scenarios,
        levels=LEVELS,
    )

    # --- Summary -------------------------------------------------------------
    print()
    print("=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"  Scenarios tested:  {profile.n_scenarios}")
    print(f"  L0 accuracy:       {profile.l0_accuracy:.1%}")
    print(f"  Total flips:       {profile.n_flips}")
    print(f"  Wiggle rate:       {profile.wiggle_rate:.1%}")
    print(f"  Corrective flips:  {profile.corrective_flips}")
    print(f"  Corrupting flips:  {profile.corrupting_flips}")
    print()

    # Per-level breakdown
    print("  Per-level wiggle rate:")
    for level in LEVELS:
        rate = profile.per_level_wiggle_rate(level)
        print(f"    {level}: {rate:.1%}")
    print()

    # Per-scenario detail
    print("  Per-scenario detail:")
    for sw in profile.scenarios:
        print(f"    {sw.scenario_name}:")
        print(f"      ground_truth: {sw.ground_truth}")
        print(f"      l0_verdict:   {sw.l0_verdict} (correct={sw.l0_correct})")
        print(f"      final_verdict: {sw.final_verdict}")
        print(f"      flipped:      {sw.flipped}")
        if sw.flipped:
            print(f"      flip_direction: {sw.flip_direction}")
        for turn in sw.turns:
            print(
                f"        {turn.level} turn {turn.turn}: "
                f"verdict={turn.observer_verdict} "
                f"changed={turn.observer_changed}"
            )
        print()

    # Save results
    save_dir = os.path.join(os.path.dirname(__file__), "wiggle_real_results")
    os.makedirs(save_dir, exist_ok=True)
    out_path = os.path.join(save_dir, "qwen3_8_27b_wiggle.json")
    profile.save(out_path)
    print(f"  Results saved to: {out_path}")
    print()
    print("DONE.")


if __name__ == "__main__":
    asyncio.run(main())
