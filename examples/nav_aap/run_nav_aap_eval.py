"""
Full eval run for nav_aap pack — credential-safe (see CLAUDE.md).

Runs two target models against all 15 scenarios with max_turns=3.
Judge: claude-opus-4-7 for both runs.
"""
import csv
import os
import sys
import time
from pathlib import Path

env_path = Path(__file__).parent.parent.parent / ".env"
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

key = os.environ.get("ANTHROPIC_API_KEY", "")
if not key or not key.startswith("sk-ant-"):
    print("ERROR: ANTHROPIC_API_KEY missing or malformed", file=sys.stderr)
    sys.exit(2)
print(f"Key: present, length={len(key)}, prefix_ok=True")

from simpleaudit import ModelAuditor, AuditResults

JUDGE_MODEL = "claude-opus-4-7"
RESULTS_DIR = Path(__file__).parent.parent.parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

RUNS = [
    {
        "target_model": "claude-sonnet-4-6",
        "label": "sonnet_4_6",
        "out": RESULTS_DIR / "nav_aap_sonnet_4_6.json",
    },
    {
        "target_model": "claude-haiku-4-5-20251001",
        "label": "haiku_4_5",
        "out": RESULTS_DIR / "nav_aap_haiku_4_5.json",
    },
]


def run_one(target_model: str, label: str, out: Path) -> AuditResults:
    print(f"\n{'='*60}")
    print(f"TARGET: {target_model}")
    print(f"JUDGE:  {JUDGE_MODEL}")
    print(f"OUT:    {out}")
    print("=" * 60)
    t0 = time.time()
    auditor = ModelAuditor(
        model=target_model,
        provider="anthropic",
        judge_model=JUDGE_MODEL,
        judge_provider="anthropic",
    )
    results = auditor.run("nav_aap", max_turns=3, language="Norwegian")
    elapsed = time.time() - t0
    print(f"\nElapsed: {elapsed:.0f}s ({elapsed/60:.1f}min)")
    results.summary()
    results.save(str(out))
    print(f"Saved: {out}")
    return results


all_results: dict[str, AuditResults] = {}
for run in RUNS:
    all_results[run["label"]] = run_one(run["target_model"], run["label"], run["out"])

labels = [r["label"] for r in RUNS]
scenario_names = [r.scenario_name for r in all_results[labels[0]].results]

csv_path = RESULTS_DIR / "summary.csv"
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    writer.writerow(["scenario_name"] + [f"{l}_severity" for l in labels])
    for i, name in enumerate(scenario_names):
        row = [name]
        for label in labels:
            row.append(all_results[label].results[i].severity)
        writer.writerow(row)
print(f"\nSaved: {csv_path}")

print("\n\n" + "=" * 60)
print("CROSS-MODEL COMPARISON")
print("=" * 60)
header = f"{'Scenario':<52} " + " ".join(f"{l:<10}" for l in labels)
print(header)
print("-" * len(header))
for i, name in enumerate(scenario_names):
    short = name[:50]
    severities = [all_results[l].results[i].severity for l in labels]
    print(f"{short:<52} " + " ".join(f"{s:<10}" for s in severities))

print("\nSeverity distribution per model:")
for label in labels:
    dist = all_results[label].severity_distribution()
    print(f"  {label}: {dist}")
