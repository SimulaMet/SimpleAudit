#!/usr/bin/env python3
"""Audit a real recording against a UTF-8 reference transcript.

Run ``python examples/audit_asr.py --help`` for arguments. Requires an installed
SimpleAudit package and credentials in environment variables. Hosted ASR and
judge calls incur provider charges. Audio/reference data is never bundled here.

Inputs: an audio file, a UTF-8 reference (empty means silence), and a JSON array
of expectations, e.g. ["Preserve the amount.", "Preserve whether approval was given."].
With OPENROUTER_API_KEY set, run from the repository root:

    python examples/audit_asr.py --audio /path/clip.mp3 --reference /path/reference.txt \
        --expectations /path/expectations.json --asr-provider openrouter \
        --asr-model openai/whisper-1 --judge-provider openrouter \
        --judge-model openai/gpt-4o --normalization normalized \
        --output /path/local-results/asr.json

For direct OpenAI, select provider openai, models whisper-1 and gpt-4o, and set
OPENAI_API_KEY. Local ASR requires --asr-provider local and --asr-base-url;
ASR_API_KEY is optional. Results contain private text: keep them outside the repo.
Exit codes: 0 = graded audit (including model failures), 1 = execution error,
2 = invalid input or ungraded result. A zero exit does not imply accurate ASR.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from simpleaudit import ASRTarget, Auditor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path)
    parser.add_argument(
        "--reference", required=True, type=Path, help="UTF-8 text; empty means silence"
    )
    parser.add_argument(
        "--expectations", required=True, type=Path, help="JSON array of expectations"
    )
    parser.add_argument(
        "--asr-provider", choices=("openai", "openrouter", "local"), default="openai"
    )
    parser.add_argument("--asr-model", required=True)
    parser.add_argument(
        "--asr-base-url", help="Required for a compatible local transcription server"
    )
    parser.add_argument("--language", help="Optional transcription language hint, e.g. no")
    parser.add_argument(
        "--judge-provider", choices=("openai", "openrouter", "ollama"), default="openai"
    )
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--judge-base-url", help="Optional OpenAI-compatible judge API base URL")
    parser.add_argument("--normalization", choices=("none", "normalized"), default="none")
    parser.add_argument(
        "--severity", choices=("low", "medium", "high", "critical"), default="medium"
    )
    parser.add_argument(
        "--context", default="Transcription fidelity; no specific downstream use stated."
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Local result JSON; contains private text"
    )
    args = parser.parse_args(argv)

    try:
        if not args.audio.is_file():
            raise ValueError("--audio must be an existing file")
        reference = args.reference.read_text(encoding="utf-8")
        expectations = json.loads(args.expectations.read_text(encoding="utf-8"))
        if (
            not isinstance(expectations, list)
            or not expectations
            or any(not isinstance(item, str) or not item.strip() for item in expectations)
        ):
            raise ValueError(
                "--expectations must contain a nonempty JSON array of nonempty strings"
            )
        if args.asr_provider == "local" and not args.asr_base_url:
            raise ValueError("--asr-base-url is required for local transcription")
        required_keys = set()
        if args.asr_provider != "local":
            required_keys.add(args.asr_provider.upper() + "_API_KEY")
        if args.judge_provider != "ollama":
            required_keys.add(args.judge_provider.upper() + "_API_KEY")
        if any(not os.environ.get(key) for key in required_keys):
            raise ValueError(
                "Missing credentials: "
                + ", ".join(sorted(key for key in required_keys if not os.environ.get(key)))
            )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    options = {"language": args.language} if args.language else {}
    if args.asr_provider == "local":
        target = ASRTarget(
            args.asr_model,
            base_url=args.asr_base_url,
            options=options,
            api_key=os.environ.get("ASR_API_KEY"),
        )
    else:
        factory = ASRTarget.openrouter if args.asr_provider == "openrouter" else ASRTarget.openai
        kwargs = {"options": options}
        if args.asr_base_url:
            kwargs["base_url"] = args.asr_base_url
        target = factory(args.asr_model, **kwargs)

    # OpenRouter and Ollama expose OpenAI-compatible chat endpoints; using this
    # transport avoids requiring their optional provider-specific SDKs.
    judge_url = (
        args.judge_base_url
        or {
            "openai": "https://api.openai.com/v1",
            "openrouter": "https://openrouter.ai/api/v1",
            "ollama": "http://localhost:11434/v1",
        }[args.judge_provider]
    )
    judge_key = (
        "ollama"
        if args.judge_provider == "ollama"
        else os.environ[args.judge_provider.upper() + "_API_KEY"]
    )
    scenario = {
        "name": args.audio.stem,
        "description": args.context,
        "file_uri": str(args.audio.resolve()),
        "expected_behavior": expectations,
        "severity": args.severity,
        "metadata": {"asr": {"reference_text": reference, "normalization": args.normalization}},
    }
    auditor = Auditor(
        target=target,
        judge="asr_faithfulness",
        judge_provider="openai",
        judge_model=args.judge_model,
        judge_base_url=judge_url,
        judge_api_key=judge_key,
        max_turns=1,
        show_progress=False,
    )
    results = auditor.run([scenario])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results.save(str(args.output))
    results.summary()
    print(json.dumps(results[0].judgment, ensure_ascii=False, indent=2))
    return 1 if results[0].severity == "ERROR" else 2 if results[0].severity == "ungraded" else 0


if __name__ == "__main__":
    raise SystemExit(main())
