#!/usr/bin/env python3
"""
Calibrate a judge against the answer key of the normedqa_triage pack.

The pack's questions come with the exam's key and a direction for every wrong option,
so the right verdict for any reply is known. This script puts every option in front of
a judge as if a model had answered with it, and prints how the judge's verdicts compare
with the key's: how many wrong answers it fails, how many right answers it passes, and
what it says about under-triage and over-triage answers.

Run it before you rely on a judge model for this pack, and above all before you rely on
one without the key (``--reveal none``). See the pack README, "Judges on this pack", for
what we found.

Usage
-----
    export OPENAI_API_KEY=...
    python examples/answer_key_calibration.py --judge-model gpt-4.1-mini
    python examples/answer_key_calibration.py --judge checklist --judge-model gpt-4.1-mini
    python examples/answer_key_calibration.py --judge-model gpt-4.1-mini --reveal none
    python examples/answer_key_calibration.py --judge-model gpt-4.1-mini --replay my_run.json

    --judge NAME           a registered judge; omit it for the default judge.
    --judge-model MODEL    the judge model to calibrate. Required.
    --provider NAME        any-llm provider of the judge model. Default: openai.
    --split dev|test|train exam sittings to use. Tune a prompt on dev (2025V, 43 Bokmål
                           questions), report on test (2025H and 2026V, 95). Default: dev.
    --language nb|nn       Bokmål or Nynorsk versions. Default: nb.
    --reveal full|key|none how much of the key the judge is shown. "full" is the scenario
                           as it ships. "key" shows the right option only. "none" shows
                           nothing, so the judge has to know the answer. Default: full.
    --style letter|text    a planted reply opens with the option letter, or states the
                           option's text without it. Default: letter.
    --replay FILE          judge the replies of a stored run of the pack (any judge) in
                           place of planted ones. They carry the target's own reasoning.
    --limit N              use the first N questions.
    --workers N            judge calls in parallel. Default: 8.
    --out FILE             save the judged run.

A planted reply has no reasoning of its own, only a fixed sentence. A judge that weighs
the justification marks some right planted replies down for that, so read the pass rate
for right answers from a ``--replay`` run and the rest from planted ones.

Four replies per question, one judge call per reply. In our runs a call took about 900
input and 200 output tokens with the default judge, 1,200 and 400 with ``checklist``, and
2,400 and 400 with ``pasientsikkerhet_no``.
"""

import argparse

from simpleaudit import Auditor, calibrate, get_scenarios, planted_answers, replayed_answers

#: The planted replies' justification line. The pack's prompt asks for one, and a judge
#: can fail a bare letter for leaving it out.
REASON = {
    "nb": "Dette er etter min vurdering det riktige tiltaket for denne pasienten.",
    "nn": "Dette er etter mi vurdering det rette tiltaket for denne pasienten.",
}
SEVERITIES = ["pass", "low", "medium", "high", "critical", "ERROR"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--judge", default=None)
    ap.add_argument("--judge-model", required=True)
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--split", default="dev", choices=["dev", "test", "train"])
    ap.add_argument("--language", default="nb", choices=["nb", "nn"])
    ap.add_argument("--reveal", default="full", choices=["full", "key", "none"])
    ap.add_argument("--style", default="letter", choices=["letter", "text"])
    ap.add_argument("--replay", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    scenarios = [
        s for s in get_scenarios("normedqa_triage")
        if s["language"] == args.language and s["metadata"]["normedqa"]["split"] == args.split
    ][: args.limit]
    if args.replay:
        answers = replayed_answers(args.replay, scenarios, reveal=args.reveal)
    else:
        answers = planted_answers(scenarios, reveal=args.reveal, style=args.style, reason=REASON[args.language])

    judge = {"judge": args.judge} if args.judge else {}
    auditor = Auditor(answers.target, judge_model=args.judge_model, judge_provider=args.provider, max_turns=1, **judge)
    results = auditor.run(answers.scenarios, language="Norwegian", max_workers=args.workers)
    if args.out:
        results.save(args.out)

    calibrate(results, answers.reference, judge_model=args.judge_model).summary()

    print("\nWhat the judge said, by what the key says about the reply:")
    print(f"  {'':<14}" + "".join(f"{s:>9}" for s in SEVERITIES))
    table = answers.verdicts_by_outcome(results)
    for outcome in ("correct", "under_triage", "over_triage", "other_wrong", "no_answer"):
        if outcome in table:
            print(f"  {outcome:<14}" + "".join(f"{table[outcome].get(s, 0):>9}" for s in SEVERITIES))


if __name__ == "__main__":
    main()
