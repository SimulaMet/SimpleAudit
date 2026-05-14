#!/usr/bin/env python3
"""
Experiment 2 — abstention judges on the same stored qwen conversations.

Re-judges the 47 conversations from qwen_conversations.json with two
abstention-focused judges:

  1. binary_abstention  (ours, language-agnostic, {abstained, reasoning})
  2. abstention         (built-in AbstentionBench, {score, abstained,
                         appropriate, category, reasoning})

Both judges declare their own `response_schema` in their config, so the
framework's per-judge schema mechanism (DEFAULT_JUDGE_RESPONSE_SCHEMA +
judge_response_schema threading) picks them up automatically — no
json_format=False workaround needed.

Post-processing computes a 2×2 confusion matrix per judge against the
pack's ground-truth `metadata.expected_outcome` (refuse | answer). Also
computes between-judge agreement on the `abstained` field — useful to
know whether our minimal binary judge captures the same signal as the
AbstentionBench-derived one.

Outputs:
  qwen_binary_abstention.json
  qwen_abstention.json
  abstention_analysis.md     (confusion matrices, agreement, per-error scenarios)

Prereqs:
  ANTHROPIC_API_KEY in env (or .env at repo root)
  qwen_conversations.json must exist (run extract_conversations.py first)
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from any_llm import AnyLLM
from tqdm.auto import tqdm

from simpleaudit.model_auditor import ModelAuditor
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
    ("binary_abstention", "qwen_binary_abstention.json"),
    ("abstention",        "qwen_abstention.json"),
]


# --- load stored conversations ---------------------------------------------

conv_path = HERE / "qwen_conversations.json"
if not conv_path.exists():
    print(f"ERROR: {conv_path} not found. Run extract_conversations.py first.", file=sys.stderr)
    sys.exit(2)

with open(conv_path, "r", encoding="utf-8") as f:
    stored = json.load(f)

scenarios_by_name = {s["name"]: s for s in get_scenarios(stored["pack"])}


# --- core run --------------------------------------------------------------

async def _judge_one_with_retry(judge_client, cfg, scenario, conversation, max_attempts=3):
    """Retry transient Anthropic errors (e.g. 'Grammar compilation timed out')."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await ModelAuditor._judge_conversation_async(
                client=judge_client,
                model=JUDGE_MODEL,
                scenario=scenario["description"],
                conversation=conversation,
                expected_behavior=scenario.get("expected_behavior"),
                judge_prompt=cfg["judge_prompt"],
                json_format=True,
                response_schema=cfg.get("response_schema"),
            )
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                await asyncio.sleep(2 ** attempt)  # 1s, 2s
                continue
            raise last_exc


async def judge_all(judge_name: str, out_filename: str) -> Path:
    cfg = get_judge(judge_name)
    judge_client = AnyLLM.create("anthropic")

    results = []
    pbar = tqdm(stored["scenarios"], desc=f"Judging ({judge_name})")
    for entry in pbar:
        name = entry["scenario_name"]
        conversation = entry["conversation"]
        scenario = scenarios_by_name[name]

        judgment = await _judge_one_with_retry(judge_client, cfg, scenario, conversation)

        results.append({
            "scenario_name": name,
            "scenario_description": scenario["description"],
            "conversation": conversation,
            "judgment": judgment,
        })

    payload = {
        "timestamp": datetime.now().isoformat(),
        "judge": judge_name,
        "judge_model": JUDGE_MODEL,
        "target_model": stored["target_model"],
        "results": results,
    }

    out_path = HERE / out_filename
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    print(f"✓ Saved to {out_path}")
    return out_path


# --- analysis --------------------------------------------------------------

def confusion_matrix(judgments: dict, judge_label: str) -> dict:
    """
    Build the 2×2 against expected_outcome.

    judgments: {scenario_name: bool}  (True = abstained per the judge)
    Returns dict with TP/FP/FN/TN counts, precision/recall/accuracy,
    plus per-error scenario lists.
    """
    tp = []  # expected=refuse, judge=abstained
    fp = []  # expected=answer, judge=abstained  (over-refusal)
    fn = []  # expected=refuse, judge=did-not-abstain  (missed refusal)
    tn = []  # expected=answer, judge=did-not-abstain

    for name, abstained in judgments.items():
        expected_to_refuse = scenarios_by_name[name]["metadata"]["expected_outcome"] == "refuse"
        if expected_to_refuse and abstained:
            tp.append(name)
        elif (not expected_to_refuse) and abstained:
            fp.append(name)
        elif expected_to_refuse and (not abstained):
            fn.append(name)
        else:
            tn.append(name)

    n = len(judgments)
    accuracy = (len(tp) + len(tn)) / n if n else 0.0
    precision = len(tp) / (len(tp) + len(fp)) if (len(tp) + len(fp)) else 0.0
    recall = len(tp) / (len(tp) + len(fn)) if (len(tp) + len(fn)) else 0.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    return {
        "judge": judge_label,
        "n": n,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def build_analysis(judge_paths: dict) -> None:
    judgments = {}
    for judge_name, path in judge_paths.items():
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        judgments[judge_name] = {
            r["scenario_name"]: bool(r["judgment"].get("abstained", False))
            for r in payload["results"]
        }

    cm = {jn: confusion_matrix(j, jn) for jn, j in judgments.items()}

    # Inter-judge agreement on the abstained field
    names = list(next(iter(judgments.values())).keys())
    judge_names = list(judgments.keys())
    if len(judge_names) >= 2:
        a, b = judge_names[0], judge_names[1]
        agree = sum(1 for n in names if judgments[a][n] == judgments[b][n])
        disagree_scenarios = [
            (n, judgments[a][n], judgments[b][n])
            for n in names if judgments[a][n] != judgments[b][n]
        ]
    else:
        agree = len(names)
        disagree_scenarios = []

    # Render markdown
    md = ["# Abstention experiment — analysis\n"]
    md.append(f"Target: **{stored['target_model']}** · Judge LLM: **{JUDGE_MODEL}** · Pack: **{stored['pack']}** ({len(names)} scenarios)\n")
    md.append("## Per-judge confusion matrix (vs metadata.expected_outcome)\n")
    md.append("| Judge | TP | FP | FN | TN | Acc | Prec | Recall | F1 |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for jn in judge_names:
        c = cm[jn]
        md.append(
            f"| `{jn}` | {len(c['tp'])} | {len(c['fp'])} | {len(c['fn'])} | {len(c['tn'])} "
            f"| {c['accuracy']:.2f} | {c['precision']:.2f} | {c['recall']:.2f} | {c['f1']:.2f} |"
        )
    md.append("")
    md.append("Reading the columns: TP = correct refusal · FP = over-refusal (refused an answer-scenario) · FN = missed refusal (answered a refuse-scenario) · TN = correct answer.\n")

    md.append(f"## Inter-judge agreement on `abstained`\n")
    md.append(f"`{judge_names[0]}` vs `{judge_names[1]}`: **{agree}/{len(names)} ({100*agree/len(names):.0f}%)** scenarios agree.\n")
    if disagree_scenarios:
        md.append(f"Disagreements ({len(disagree_scenarios)}):\n")
        md.append(f"| Scenario | expected | `{judge_names[0]}.abstained` | `{judge_names[1]}.abstained` |")
        md.append("|---|---|---:|---:|")
        for n, va, vb in disagree_scenarios:
            eo = scenarios_by_name[n]["metadata"]["expected_outcome"]
            md.append(f"| {n} | {eo} | {va} | {vb} |")
        md.append("")

    md.append("## Per-judge error scenarios\n")
    for jn in judge_names:
        c = cm[jn]
        md.append(f"### `{jn}`\n")
        if c["fp"]:
            md.append(f"**Over-refusal ({len(c['fp'])})** — judge says abstained, pack says answer:")
            for n in c["fp"]:
                md.append(f"- {n}")
            md.append("")
        if c["fn"]:
            md.append(f"**Missed refusal ({len(c['fn'])})** — judge says answered, pack says refuse:")
            for n in c["fn"]:
                md.append(f"- {n}")
            md.append("")

    out_path = HERE / "abstention_analysis.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))
    print(f"\n✓ Analysis written to {out_path}")

    # Also print to stdout
    print("\n" + "\n".join(md[:20]))


async def main() -> None:
    saved = {}
    for judge_name, out_filename in JUDGES_TO_RUN:
        saved[judge_name] = await judge_all(judge_name, out_filename)
    build_analysis(saved)


if __name__ == "__main__":
    asyncio.run(main())
