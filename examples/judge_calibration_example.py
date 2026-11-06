#!/usr/bin/env python3
"""
Calibrate a judge against a reference, two ways, on stored transcripts.

  A. Human labels   - label a few of the 15 nav_aap transcripts in the
                      terminal, then score the stored verdicts against them.
  B. Second family  - re-grade the same transcripts with a judge from another
                      model family (OpenAI here) and use that as the reference.
                      Judge tokens only; no target or auditor is called.

Path A needs no API key. Path B needs OPENAI_API_KEY and runs only when it
is set. Both write a report under results/calibration/.

Usage
-----
    python examples/judge_calibration_example.py            # label, then score
    python examples/judge_calibration_example.py --no-label # reuse the labels file
"""

import os
import sys
from pathlib import Path

from simpleaudit import calibrate, label, load_labels, make_judge_client, rejudge

STORED = Path(__file__).parent / "nav_aap" / "nav_aap_sonnet_4_6.json"
LABELS = STORED.with_name(STORED.stem + ".labels.json")
# The stored file predates judge metadata logging (0.5.1), so say which judge graded it.
STORED_JUDGE = "claude-haiku-4-5-20251001"


def main() -> None:
    if "--no-label" in sys.argv and LABELS.exists():
        labels = load_labels(LABELS)
    else:
        labels = label(STORED, LABELS)
    if labels:
        report = calibrate(STORED, labels, judge_model=STORED_JUDGE)
        report.summary()
        print(f"\nsaved {report.save()}\n")
    else:
        print("No labels given; skipping the human-reference report.\n")

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set; skipping the cross-family reference.")
        return
    other = make_judge_client("openai")
    reference = rejudge(STORED, other, "gpt-5.2", max_concurrency=4)
    report = calibrate(STORED, reference, judge_model=STORED_JUDGE)
    report.summary()
    print(f"\nsaved {report.save()}")


if __name__ == "__main__":
    main()
