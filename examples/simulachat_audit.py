#!/usr/bin/env python3
"""
Example: audit a SimulaChat gateway model with simpleaudit.

The SimulaChat gateway (OpenAI-compatible) rejects any request whose
User-Agent identifies the OpenAI SDK, replying 403 "Your request was
blocked". simpleaudit talks to every provider through any-llm, which
constructs an AsyncOpenAI client internally, so the override has to be
passed down via ModelAuditor's `client_kwargs`.

Usage:
    export SIMULACHAT_API_KEY=sk-...
    python simulachat_audit.py                          # safety pack, default model
    python simulachat_audit.py --pack health --turns 3
    python simulachat_audit.py --judge-model gpt-4o --judge-provider openai
    python simulachat_audit.py --target-model X --judge-model Y   # different models

Both target and judge default to the same endpoint/model, which is the
cheapest way to get a run. Note that self-judging is not a valid
comparative instrument -- pass an independent --judge-model when the
score needs to mean something.
"""

import argparse
import os

from simpleaudit import ModelAuditor, list_scenario_packs

DEFAULT_BASE_URL = "https://simulachat.sushant.info.np/api"
DEFAULT_MODEL = "Inferact/Qwen3.8-Flash-Next-NVFP4"

# The gateway's filter matches the OpenAI SDK User-Agent pattern. Any of
# these pass; an empty User-Agent is blocked too.
BROWSER_UA = "curl/8.0"


def list_models(base_url: str, api_key: str) -> None:
    """Print the model ids the gateway offers, for --target-model discovery."""
    import httpx

    r = httpx.get(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": BROWSER_UA},
        timeout=30,
    )
    r.raise_for_status()
    for m in r.json().get("data", []):
        ctx = m.get("max_model_len") or "?"
        print(f"  {m['id']}  (ctx {ctx})")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("--base-url", default=os.getenv("SIMULACHAT_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--api-key", default=os.getenv("SIMULACHAT_API_KEY"))
    p.add_argument("--target-model", default=os.getenv("SIMULACHAT_MODEL", DEFAULT_MODEL))
    p.add_argument("--judge-model", default=None, help="defaults to --target-model")
    p.add_argument("--judge-provider", default="openai", help="provider for --judge-model")
    p.add_argument("--judge-base-url", default=None, help="defaults to --base-url")
    p.add_argument("--judge-api-key", default=None, help="defaults to --api-key")
    p.add_argument("--pack", default="safety", help="scenario pack, see --list-packs")
    p.add_argument("--turns", type=int, default=5)
    p.add_argument("--workers", type=int, default=5)
    p.add_argument("--out", default=None, help="write results JSON here")
    p.add_argument("--list-packs", action="store_true")
    p.add_argument("--list-models", action="store_true")
    args = p.parse_args()

    if args.list_packs:
        for pack, count in list_scenario_packs().items():
            print(f"  {pack}: {count}")
        return

    if not args.api_key:
        raise SystemExit("set SIMULACHAT_API_KEY or pass --api-key")

    if args.list_models:
        list_models(args.base_url, args.api_key)
        return

    judge_model = args.judge_model or args.target_model
    client_kwargs = {"default_headers": {"User-Agent": BROWSER_UA}}

    print(f"target : {args.target_model} @ {args.base_url}")
    print(f"judge  : {judge_model} @ {args.judge_base_url or args.base_url}")
    print(f"pack   : {args.pack}  turns={args.turns}  workers={args.workers}")

    auditor = ModelAuditor(
        model=args.target_model,
        provider="openai",
        base_url=args.base_url,
        api_key=args.api_key,
        judge_model=judge_model,
        judge_provider=args.judge_provider,
        judge_base_url=args.judge_base_url or args.base_url,
        judge_api_key=args.judge_api_key or args.api_key,
        # Without this every call comes back 403 "Your request was blocked".
        client_kwargs=client_kwargs,
        show_progress=True,
    )

    results = auditor.run(args.pack, max_turns=args.turns, max_workers=args.workers)
    results.summary()

    if args.out:
        results.save(args.out)
        print(f"saved -> {args.out}")


if __name__ == "__main__":
    main()
