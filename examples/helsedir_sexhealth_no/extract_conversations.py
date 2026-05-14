#!/usr/bin/env python3
"""
Extract the 47 qwen conversations from qwen_safety.json into a judge-agnostic
file. This decouples the (expensive) target-generation step from the (cheap)
judge-evaluation step — re-judging with a different config doesn't require
re-running the target.

Output: qwen_conversations.json
  {
    "target_model": "qwen3.5:9b",
    "target_provider": "ollama",
    "pack": "hei_refusal",
    "max_turns": 1,
    "language": "Norsk",
    "scenarios": [
      {"scenario_name": "...", "conversation": [...]},
      ...
    ]
  }
"""

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

with open(HERE / "qwen_safety.json", "r", encoding="utf-8") as f:
    safety = json.load(f)

out = {
    "target_model": "qwen3.5:9b",
    "target_provider": "ollama",
    "pack": "hei_refusal",
    "max_turns": 1,
    "language": "Norsk",
    "generated_at": safety["timestamp"],
    "scenarios": [
        {
            "scenario_name": r["scenario_name"],
            "conversation": r["conversation"],
        }
        for r in safety["results"]
    ],
}

out_path = HERE / "qwen_conversations.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)

print(f"✓ {len(out['scenarios'])} conversations written to {out_path}")
