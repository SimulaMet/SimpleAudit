"""
Smoke test for nav_aap pack — credential-safe.

Loads .env silently into os.environ. Prints ONLY length/structure of the
key, never the value. See CLAUDE.md.
"""
import json
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
if not key:
    print("ERROR: ANTHROPIC_API_KEY not set after .env load", file=sys.stderr)
    sys.exit(2)
print(f"Key check: present, length={len(key)}, prefix_ok={key.startswith('sk-ant-')}")

from simpleaudit import ModelAuditor
from simpleaudit.scenarios.nav_aap import NAV_AAP_SCENARIOS

picked = next(s for s in NAV_AAP_SCENARIOS if s["name"] == "AAP Hallusinasjon - Oppfunnet Ordning")
print(f"Smoke scenario: {picked['name']}")
print(f"Severity: {picked['severity']}, Category: {picked['category']}")
print(f"Prompt: {picked['test_prompt']}")
print("-" * 60)

TARGET_MODEL = "claude-sonnet-4-6"
JUDGE_MODEL = "claude-opus-4-7"

print(f"Target: {TARGET_MODEL} (anthropic)")
print(f"Judge:  {JUDGE_MODEL} (anthropic)")
print("-" * 60)

start = time.time()
try:
    auditor = ModelAuditor(
        model=TARGET_MODEL,
        provider="anthropic",
        judge_model=JUDGE_MODEL,
        judge_provider="anthropic",
    )
    results = auditor.run([picked], max_turns=2, language="Norwegian")
except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")
    raise

elapsed = time.time() - start
print(f"\nElapsed: {elapsed:.1f}s")
print("-" * 60)
print("RESULTS SUMMARY:")
results.summary()
print("-" * 60)

for r in results.results:
    print(f"\nScenario: {r.scenario_name}")
    print(f"Severity: {r.severity}")
    print(f"\nJudge reasoning (full):")
    reasoning = getattr(r, "reasoning", None) or getattr(r, "judge_reasoning", None) or "<no reasoning attr>"
    print(reasoning)
    print(f"\nConversation ({len(r.conversation)} turns):")
    for i, turn in enumerate(r.conversation):
        role = turn.get("role", "?")
        content = turn.get("content", "")
        print(f"  [{i}] {role}: {content[:400]}{'...' if len(content) > 400 else ''}")
    for attr in ("usage", "tokens", "cost", "input_tokens", "output_tokens"):
        if hasattr(r, attr):
            print(f"  {attr}: {getattr(r, attr)}")

out = Path(__file__).parent.parent.parent / "results" / "smoke_nav_aap.json"
out.parent.mkdir(exist_ok=True)
try:
    text = results.to_json() if hasattr(results, "to_json") else json.dumps(
        [{k: getattr(r, k) for k in dir(r) if not k.startswith("_") and not callable(getattr(r, k))} for r in results.results],
        indent=2, default=str,
    )
except Exception as e:
    text = json.dumps({"error_serializing": str(e), "n_results": len(results.results)}, indent=2)
out.write_text(text)
print(f"\nSaved smoke result: {out}")
