#!/usr/bin/env python3
"""
Example: Named Judge Configs with Ollama

Demonstrates the built-in judge configurations introduced alongside the
custom prompt feature. Instead of writing judge prompts from scratch, you
can pick a named config grounded in published evaluation research.

Three modes are shown back to back, each paired with a scenario pack that
makes the evaluation meaningful:

  1. safety      — safety scenarios   — Constitutional AI safety criteria
  2. helpfulness — rag scenarios      — MT-Bench response quality (4 sub-dimensions)
  3. factuality  — bullshitbench      — G-Eval hallucination detection

All use local Ollama models — no API keys required.

Prerequisites:
    ollama serve
    ollama pull llama3.2:3b     # target (small/fast)
    ollama pull gemma3:latest   # judge

Usage:
    python judge_configs_ollama.py
"""

from simpleaudit import ModelAuditor, get_scenarios, list_judge_configs

TARGET_MODEL = "llama3.2:3b"
JUDGE_MODEL  = "gemma3:latest"

COMMON = dict(
    model=TARGET_MODEL,
    provider="ollama",
    judge_model=JUDGE_MODEL,
    judge_provider="ollama",
    json_format=False,   # Ollama doesn't support OpenAI-style json_object format
    max_turns=2,
    verbose=False,
    show_progress=True,
)


def _helpfulness_score(j: dict) -> float | None:
    """Compute average from sub-scores if the judge omitted the overall score."""
    if "score" in j:
        return float(j["score"])
    dims = [j.get(k) for k in ("relevance", "accuracy", "clarity", "completeness")]
    nums = [v for v in dims if isinstance(v, (int, float))]
    return round(sum(nums) / len(nums), 1) if nums else None


# ---------------------------------------------------------------------------
# Mode 1: safety judge (Constitutional AI — Bai et al., 2022)
#         Paired with: safety scenarios — designed to probe harm, manipulation,
#         hallucination and boundary violations.
# ---------------------------------------------------------------------------

def run_safety():
    print("\n" + "=" * 60)
    print("MODE 1 — safety judge  ×  safety scenarios")
    print("         (Constitutional AI, Bai et al. 2022)")
    print("=" * 60)

    scenarios = get_scenarios("safety")[:3]
    auditor = ModelAuditor(**COMMON, judge="safety")
    results = auditor.run(scenarios)
    results.summary()

    print("\nPer-scenario severities:")
    for r in results:
        print(f"  {r.scenario_name}: {r.severity.upper()}")
        if r.summary:
            print(f"    → {r.summary[:120]}")

    results.save("examples/results_judge_safety.json")
    print("\nSaved → examples/results_judge_safety.json")


# ---------------------------------------------------------------------------
# Mode 2: helpfulness judge (MT-Bench — Zheng et al., 2023)
#         Paired with: rag scenarios — test whether responses are relevant,
#         accurate, clear and complete when answering information requests.
# ---------------------------------------------------------------------------

def run_helpfulness():
    print("\n" + "=" * 60)
    print("MODE 2 — helpfulness judge  ×  rag scenarios")
    print("         (MT-Bench, Zheng et al. 2023)")
    print("=" * 60)

    scenarios = get_scenarios("rag")[:3]
    auditor = ModelAuditor(**COMMON, judge="helpfulness")
    results = auditor.run(scenarios)

    print("\nPer-scenario scores:")
    all_scores = []
    for r in results:
        j = r.judgment or {}
        score        = _helpfulness_score(j)
        relevance    = j.get("relevance", "?")
        accuracy     = j.get("accuracy", "?")
        clarity      = j.get("clarity", "?")
        completeness = j.get("completeness", "?")
        feedback     = j.get("feedback", "")
        score_str    = f"{score}/10" if score is not None else "?/10"
        print(f"  {r.scenario_name}")
        print(f"    overall={score_str}  "
              f"relevance={relevance}  accuracy={accuracy}  "
              f"clarity={clarity}  completeness={completeness}")
        if feedback:
            print(f"    → {feedback[:120]}")
        if score is not None:
            all_scores.append(score)

    if all_scores:
        avg = sum(all_scores) / len(all_scores)
        print(f"\nAverage helpfulness score: {avg:.1f}/10")

    results.save("examples/results_judge_helpfulness.json")
    print("\nSaved → examples/results_judge_helpfulness.json")
    print("(Open in SimpleAudit visualizer to see the custom schema rendered)")


# ---------------------------------------------------------------------------
# Mode 3: factuality judge (G-Eval — Liu et al., 2023)
#         Paired with: bullshitbench — scenarios built around broken premises
#         that tempt models into confident but inaccurate responses.
# ---------------------------------------------------------------------------

def run_factuality():
    print("\n" + "=" * 60)
    print("MODE 3 — factuality judge  ×  bullshitbench scenarios")
    print("         (G-Eval, Liu et al. 2023)")
    print("=" * 60)

    scenarios = get_scenarios("bullshitbench")[:3]
    auditor = ModelAuditor(**COMMON, judge="factuality")
    results = auditor.run(scenarios)

    print("\nPer-scenario factuality:")
    all_scores = []
    for r in results:
        j       = r.judgment or {}
        score   = j.get("score")
        verdict = j.get("verdict", "?")
        errors  = j.get("factual_errors", [])
        reason  = j.get("reasoning", "")
        # reasoning may be a string or a list of claim objects from the judge
        reason_str = reason if isinstance(reason, str) else f"[{len(reason)} claims analysed]"
        score_str = f"{score}/10" if score is not None else "?/10"
        print(f"  {r.scenario_name}")
        print(f"    score={score_str}  verdict={verdict}")
        if errors:
            print(f"    errors: {errors[:2]}")
        if reason_str:
            print(f"    → {reason_str[:120]}")
        if score is not None:
            all_scores.append(score)

    if all_scores:
        avg = sum(all_scores) / len(all_scores)
        print(f"\nAverage factuality score: {avg:.1f}/10")
        print("  (lower = more hallucination; higher = more accurate)")

    results.save("examples/results_judge_factuality.json")
    print("\nSaved → examples/results_judge_factuality.json")
    print("(Open in SimpleAudit visualizer to see the custom schema rendered)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("SimpleAudit — Named Judge Configs Demo")
    print(f"Target : {TARGET_MODEL} (ollama)")
    print(f"Judge  : {JUDGE_MODEL} (ollama)")
    print(f"\nAvailable judge configs:")
    for name, desc in list_judge_configs().items():
        print(f"  {name:12s} — {desc[:70]}...")

    run_safety()
    run_helpfulness()
    run_factuality()

    print("\nDone.")
