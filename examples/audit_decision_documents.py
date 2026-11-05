#!/usr/bin/env python3
"""Audit a System One model on user-supplied document/question scenarios.

Input: a JSON array of SimpleAudit scenarios, each containing documents and a
labelled decision block. No chat or judge model is called. Hosted decision
requests incur provider charges. Run with --help for arguments.

Each input scenario needs name, description, documents (a list of text strings),
and decision with instructions, criteria (option keys to descriptions) and
accepted (correct option keys). The answer key is excluded from model requests.
Use checked real documents and include an insufficient-information option where
appropriate. See the main README's Decision Models section for the JSON format.

Run from the repository root:

    python examples/audit_decision_documents.py --scenarios /path/scenarios.json \
        --provider ollama --base-url http://SERVER:11434 --model clef-64k:latest \
        --output /path/local-results/decisions.json

For OpenRouter, use --provider openrouter --model typesafe/jev-1.13 with
OPENROUTER_API_KEY set. Model availability depends on the provider. Results
contain source text: keep private results outside the repository.
Exit codes: 0 = graded audit (including wrong answers), 1 = execution error,
2 = invalid input or ungraded result. A zero exit does not imply correct decisions.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from simpleaudit import Auditor, DecisionTarget
from simpleaudit.decision import validate_decision


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", required=True, type=Path)
    parser.add_argument("--provider", choices=("ollama", "openrouter"), default="ollama")
    parser.add_argument(
        "--model", required=True, help="Installed Ollama name or OpenRouter model ID"
    )
    parser.add_argument("--base-url", default="http://localhost:11434", help="Ollama server URL")
    parser.add_argument(
        "--output", required=True, type=Path, help="Local result JSON; contains source text"
    )
    args = parser.parse_args(argv)
    try:
        scenarios = json.loads(args.scenarios.read_text(encoding="utf-8"))
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError("--scenarios must contain a nonempty JSON array")
        for scenario in scenarios:
            if not isinstance(scenario, dict) or not all(
                isinstance(scenario.get(key), str) and scenario[key].strip()
                for key in ("name", "description")
            ):
                raise ValueError("Each scenario needs a nonempty name and description")
            documents = scenario.get("documents")
            if (
                not isinstance(documents, list)
                or not documents
                or any(not isinstance(doc, str) or not doc.strip() for doc in documents)
            ):
                raise ValueError(
                    "Each scenario needs documents as a nonempty array of text strings"
                )
            validate_decision(scenario.get("decision"))
            if not scenario["decision"].get("accepted"):
                raise ValueError("Each decision needs accepted answers for code-only grading")
        if args.provider == "openrouter" and not os.environ.get("OPENROUTER_API_KEY"):
            raise ValueError("OPENROUTER_API_KEY is required")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(str(exc))

    target = (
        DecisionTarget.openrouter(args.model)
        if args.provider == "openrouter"
        else DecisionTarget.ollama(args.model, base_url=args.base_url)
    )
    results = Auditor(target=target, judge="choice_match", max_turns=1, show_progress=False).run(
        scenarios
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results.save(str(args.output))
    for result in results:
        print(
            json.dumps(
                {
                    "scenario": result.scenario_name,
                    "severity": result.severity,
                    "choice": result.judgment.get("choice"),
                    "accepted": result.judgment.get("accepted"),
                    "confidence": result.judgment.get("confidence"),
                    "probabilities": result.judgment.get("probabilities"),
                },
                ensure_ascii=False,
            )
        )
    results.summary()
    return (
        1
        if any(r.severity == "ERROR" for r in results)
        else 2 if any(r.severity == "ungraded" for r in results) else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
