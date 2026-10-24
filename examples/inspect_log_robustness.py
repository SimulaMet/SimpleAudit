"""Judge-robustness checks on an Inspect AI eval log.

Reads any Inspect log, rebuilds each sample's transcript (tool calls included),
and runs SimpleAudit's perturbation check on it: the same transcripts are
graded once as stored and once per style perturbation (an apology, a
disclaimer, padding, a claim of authority, self-certification), and the script
reports how often each perturbation flips the judge's verdict. Only judge calls
are made; the model under test is never called again.

    # A log from SimpleAudit's own Inspect task ...
    inspect eval simpleaudit/audit -T pack=nav_aap -T language=Norwegian --limit 5 \\
        --model anthropic/claude-haiku-5-5 --model-role grader=anthropic/claude-haiku-5-5

    # ... or from any other Inspect eval
    python examples/inspect_log_robustness.py logs/<file>.eval \\
        --judge-provider anthropic --judge-model claude-haiku-5-5 --language Norwegian

Needs ``pip install 'simpleaudit[inspect]'`` and the judge provider's API key.
"""

import argparse

from simpleaudit import (
    load_stored_records,
    make_judge_client,
    perturbation_variants,
    reframing_check,
)
from simpleaudit.integrations.inspect import load_inspect_log
from simpleaudit.judges import get_judge


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("log", help="Inspect log file (.eval or .json)")
    parser.add_argument("--judge-provider", default="anthropic")
    parser.add_argument("--judge-model", default="claude-haiku-5-5")
    parser.add_argument("--judge", default="safety", help="SimpleAudit judge config to grade with")
    parser.add_argument("--language", default="English", help="language of the perturbation strings")
    parser.add_argument("--epoch", type=int, default=1)
    args = parser.parse_args()

    runs = load_inspect_log(args.log)
    model = next(iter(runs.keys()))
    epochs = runs.runs(model)
    if not 1 <= args.epoch <= len(epochs):
        parser.error(f"--epoch must be between 1 and {len(epochs)}")
    run = epochs[args.epoch - 1]
    records = load_stored_records(run.to_dict())
    print(f"{len(records)} transcripts from {model}, epoch {args.epoch}")

    config = get_judge(args.judge)
    variants = perturbation_variants(
        config["judge_prompt"],
        language=args.language,
        response_schema=config.get("response_schema"),
        postprocess=config.get("postprocess"),
        requires_expected_behavior=bool(config.get("requires_expected_behavior")),
    )
    results = reframing_check(
        make_judge_client(args.judge_provider), args.judge_model, records, variants, max_concurrency=4
    )
    for label, effect in results.effects().items():
        print(f"{label:22} {effect.flip_rate:4.0%} of verdicts flipped ({effect.n_shifted}/{effect.n_compared})")


if __name__ == "__main__":
    main()
