"""
Cross-judge stability analysis example.

Demonstrates CrossJudgeExperiment: running the same subject model through two
judge versions to measure whether the judge upgrade shifts severity ratings.

Motivated by the finding that judge-model version can materially shift modal
severity on identical subject responses — including safety-relevant scenarios —
without any change to the subject model itself.

Usage
-----
    python examples/cross_judge_example.py

Set ANTHROPIC_API_KEY before running. Results are cached under
`cross_judge_results/` so the run is resumable if interrupted.
"""

import asyncio
import os

from simpleaudit import CrossJudgeExperiment, compare_judges


SUBJECT_MODELS = [
    {
        "model": "claude-haiku-4-5-20251001",
        "provider": "anthropic",
        "label": "haiku-4.5",
    },
]

JUDGE_MODELS = [
    {"model": "claude-opus-4-7", "provider": "anthropic"},
    {"model": "claude-opus-4-8", "provider": "anthropic"},
]


async def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise EnvironmentError("ANTHROPIC_API_KEY is not set.")

    exp = CrossJudgeExperiment(
        models=SUBJECT_MODELS,
        judge_models=JUDGE_MODELS,
        n_repetitions=3,
        save_dir="cross_judge_results",
        show_progress=True,
    )

    print(f"Judges: {exp.judge_labels}")
    print("Running cross-judge experiment (results cached under cross_judge_results/)...\n")

    results = await exp.run_async(
        scenarios="nav_aap",
        language="Norwegian",
        max_turns=3,
    )

    # --- Score summary ---------------------------------------------------------
    print("=== Score summary (mean across 3 runs) ===")
    summary = results.score_summary()
    for subject, per_judge in summary.items():
        print(f"\n  Subject: {subject}")
        for judge_label, stats in per_judge.items():
            print(
                f"    {judge_label:20s}  mean={stats['mean']:.1f}  "
                f"std={stats['std']:.2f}  cv={stats['cv']:.1%}  "
                f"n={stats['n_runs']}"
            )

    # --- Severity shifts -------------------------------------------------------
    print("\n=== Cross-judge severity shifts (haiku-4.5 / nav_aap) ===")
    shifts = results.severity_shifts("haiku-4.5")
    shifted = [s for s in shifts if s["shifted"]]
    print(f"  {len(shifted)} of {len(shifts)} scenarios shifted modal severity.\n")
    for entry in shifted:
        direction = entry.get("direction", 0)
        arrow = "→ stricter" if direction > 0 else "→ more lenient"
        judges = list(entry["modals"].keys())
        modal_a = entry["modals"][judges[0]]
        modal_b = entry["modals"][judges[1]]
        print(
            f"  {entry['scenario']:<45s}  "
            f"{judges[0]}: {modal_a:<8s}  {judges[1]}: {modal_b:<8s}  {arrow}"
        )

    # --- compare_judges utility (post-hoc comparison) -------------------------
    print("\n=== compare_judges utility ===")
    cmp = compare_judges(
        results["opus-4-7"],
        results["opus-4-8"],
        subject_label="haiku-4.5",
        label_a="opus-4-7",
        label_b="opus-4-8",
    )
    print(
        f"  score_delta (4.8 − 4.7): {cmp['score_delta']:+.2f}  "
        f"cv_delta: {cmp['cv_delta']:+.4f}  "
        f"n_shifted: {cmp['n_shifted']}/{cmp['n_total']}"
    )


if __name__ == "__main__":
    asyncio.run(main())
