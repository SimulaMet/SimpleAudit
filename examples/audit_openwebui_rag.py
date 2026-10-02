"""
Real black-box audit of an external Open WebUI RAG app over HTTP.

Target:  https://simulachat.sushant.pp.ua/api/v1/chat/completions  (OpenAI-compatible)
Model:   stm-radgiver (retrieval-grounded RAG)
Judge:   the same OpenAI-compatible endpoint (serves as the judge LLM)

Run:
    python examples/audit_openwebui_rag.py
"""

from __future__ import annotations

import asyncio
import json
import os

from simpleaudit import Auditor
from simpleaudit.targets.http import HTTPAppTarget

BASE = "https://simulachat.sushant.pp.ua"
API_KEY = os.environ.get("OWUI_API_KEY", "sk-3582392995f54374a6574414a37cd7c5")
MODEL = "stm-radgiver"


def main() -> None:
    target = HTTPAppTarget(
        url=f"{BASE}/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        request_template={"model": MODEL},
        message_field="messages",  # OpenAI-style messages list
        response_path="choices.0.message.content",
        token_paths=("usage.prompt_tokens", "usage.completion_tokens"),
        timeout=90.0,
    )

    auditor = Auditor(
        target=target,
        judge_model=MODEL,
        judge_provider="openai",
        judge_api_key=API_KEY,
        judge_base_url=f"{BASE}/api/v1",
    )

    print("=== Real black-box audit of Open WebUI RAG (stm-radgiver) ===\n")
    # max_workers runs scenarios in parallel (default 1 = sequential).
    result = asyncio.run(auditor.run_async("safety", max_turns=2, max_workers=4))

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(result.summary())
    print("\nSeverity distribution:", result.severity_distribution)
    print("Score:", result.score)
    print("Passed:", result.passed, " Failed:", result.failed)
    print("Target tokens (in/out):", result.total_target_input_tokens, "/", result.total_target_output_tokens)

    print("\n" + "=" * 70)
    print("PER-SCENARIO RESULTS")
    print("=" * 70)
    for r in result.results:
        d = r.to_dict()
        print(f"\n--- {d.get('scenario_name')} ---")
        print(f"  severity: {d.get('severity')}")
        print(f"  summary: {d.get('summary')}")
        issues = d.get("issues_found") or []
        for i in issues[:3]:
            print(f"  issue: {i}")


if __name__ == "__main__":
    main()
