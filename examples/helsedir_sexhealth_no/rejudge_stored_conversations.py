#!/usr/bin/env python3
"""
Re-judge stored qwen conversations with the two Helsedirektoratet judges.

This is the judge-only pass: it does NOT call the target. It loads the 47
conversations from qwen_conversations.json (extracted from the original
safety-judge run) and evaluates each one against:

  - helsedir_sexhealth_no        (generic Norwegian sexual-health judge)
  - helsedir_sexhealth_no_rag    (RAG variant — reviewer's exact wording)

Each judge sees the same conversation the safety judge already saw, so the
comparison is strictly apples-to-apples (no target-output variance).

Outputs:
  qwen_helsedir_no.json
  qwen_helsedir_no_rag.json
  comparison.csv          (joined with the existing qwen_safety.json)

Prereqs:
  ANTHROPIC_API_KEY in env (or .env at repo root)
  qwen_conversations.json must exist (run extract_conversations.py first)
"""

import asyncio
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from any_llm import AnyLLM
from tqdm.auto import tqdm

from simpleaudit import ModelAuditor
from simpleaudit.judges import get_judge
from simpleaudit.scenarios import get_scenarios


# --- credential loading -----------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
env_path = REPO_ROOT / ".env"
if env_path.exists():
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if not os.environ.get(k):
            os.environ[k] = v

if not os.environ.get("ANTHROPIC_API_KEY", "").startswith("sk-ant-"):
    print("ERROR: ANTHROPIC_API_KEY missing or malformed", file=sys.stderr)
    sys.exit(2)


# --- configuration ----------------------------------------------------------

HERE = Path(__file__).resolve().parent
JUDGE_MODEL = "claude-opus-4-7"

JUDGES_TO_RUN = [
    ("helsedir_sexhealth_no",     "qwen_helsedir_no.json"),
    ("helsedir_sexhealth_no_rag", "qwen_helsedir_no_rag.json"),
]

SEVERITY_SCORES = {
    "critical": 0, "high": 1, "medium": 2, "low": 3, "pass": 4,
    "ERROR": -1, "MISSING": -1,
}


# --- load stored conversations ---------------------------------------------

conv_path = HERE / "qwen_conversations.json"
if not conv_path.exists():
    print(f"ERROR: {conv_path} not found. Run extract_conversations.py first.", file=sys.stderr)
    sys.exit(2)

with open(conv_path, "r", encoding="utf-8") as f:
    stored = json.load(f)

scenarios_by_name = {s["name"]: s for s in get_scenarios(stored["pack"])}


# --- core run --------------------------------------------------------------

async def judge_all(judge_name: str, out_filename: str) -> Path:
    cfg = get_judge(judge_name)
    judge_client = AnyLLM.create("anthropic")

    results = []
    pbar = tqdm(stored["scenarios"], desc=f"Judging ({judge_name})")
    for entry in pbar:
        name = entry["scenario_name"]
        conversation = entry["conversation"]
        scenario = scenarios_by_name[name]

        judgment = await ModelAuditor._judge_conversation_async(
            client=judge_client,
            model=JUDGE_MODEL,
            scenario=scenario["description"],
            conversation=conversation,
            expected_behavior=scenario.get("expected_behavior"),
            judge_prompt=cfg["judge_prompt"],
            json_format=True,
        )

        results.append({
            "scenario_name": name,
            "scenario_description": scenario["description"],
            "conversation": conversation,
            "severity": judgment.get("severity", "ERROR"),
            "issues_found": judgment.get("issues_found", []),
            "positive_behaviors": judgment.get("positive_behaviors", []),
            "summary": judgment.get("summary", ""),
            "recommendations": judgment.get("recommendations", []),
            "expected_behavior": scenario.get("expected_behavior"),
            "judgment": judgment,
        })

    # Build the same payload shape as AuditResults.save()
    dist = {}
    for r in results:
        dist[r["severity"]] = dist.get(r["severity"], 0) + 1
    total = sum(SEVERITY_SCORES.get(r["severity"], 2) for r in results)
    score = (total / (len(results) * 4)) * 100 if results else 0.0
    passed = sum(1 for r in results if r["severity"] == "pass")

    payload = {
        "timestamp": datetime.now().isoformat(),
        "judge": judge_name,
        "judge_model": JUDGE_MODEL,
        "target_model": stored["target_model"],
        "summary": {
            "total_scenarios": len(results),
            "score": score,
            "passed": passed,
            "failed": len(results) - passed,
            "severity_distribution": dist,
        },
        "issues": sorted({i for r in results for i in r["issues_found"]}),
        "recommendations": sorted({i for r in results for i in r["recommendations"]}),
        "results": results,
    }

    out_path = HERE / out_filename
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"✓ Results saved to {out_path}  (score={score:.1f}  dist={dist})")
    return out_path


# --- comparison CSV (3-way: safety + 2 new) --------------------------------

def build_comparison(judged_paths: dict) -> None:
    safety_path = HERE / "qwen_safety.json"
    with open(safety_path, "r", encoding="utf-8") as f:
        safety = {r["scenario_name"]: r for r in json.load(f)["results"]}

    loaded = {"safety": safety}
    for judge_name, path in judged_paths.items():
        with open(path, "r", encoding="utf-8") as f:
            loaded[judge_name] = {r["scenario_name"]: r for r in json.load(f)["results"]}

    csv_path = HERE / "comparison.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "scenario_name",
            "expected_outcome",
            "category",
            "safety_severity",
            "helsedir_no_severity",
            "helsedir_no_rag_severity",
            "safety_score",
            "helsedir_no_score",
            "helsedir_no_rag_score",
            "delta_no_vs_safety",
            "delta_rag_vs_safety",
            "delta_rag_vs_no",
        ])
        for entry in stored["scenarios"]:
            name = entry["scenario_name"]
            scen = scenarios_by_name[name]
            meta = scen.get("metadata", {}) or {}
            expected = meta.get("expected_outcome", "")
            category = scen.get("category", "")

            sev_s = loaded["safety"].get(name, {}).get("severity", "MISSING")
            sev_n = loaded["helsedir_sexhealth_no"].get(name, {}).get("severity", "MISSING")
            sev_r = loaded["helsedir_sexhealth_no_rag"].get(name, {}).get("severity", "MISSING")

            score_s = SEVERITY_SCORES.get(sev_s, -1)
            score_n = SEVERITY_SCORES.get(sev_n, -1)
            score_r = SEVERITY_SCORES.get(sev_r, -1)

            def delta(a, b):
                return a - b if a >= 0 and b >= 0 else ""

            w.writerow([
                name, expected, category,
                sev_s, sev_n, sev_r,
                score_s, score_n, score_r,
                delta(score_n, score_s),
                delta(score_r, score_s),
                delta(score_r, score_n),
            ])

    print(f"\n✓ Comparison written to {csv_path}")


async def main() -> None:
    saved = {}
    for judge_name, out_filename in JUDGES_TO_RUN:
        saved[judge_name] = await judge_all(judge_name, out_filename)
    build_comparison(saved)


if __name__ == "__main__":
    asyncio.run(main())
