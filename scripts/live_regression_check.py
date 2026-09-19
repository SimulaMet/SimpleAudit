#!/usr/bin/env python3
"""
Live regression check: the classic SimpleAudit paths still work end to end.

Runs a handful of small real calls against a provider and checks that the
long-standing entry points behave as before, next to the newer judge-only
paths. Meant to be run by a maintainer before a release; the unit tests cover
the same behaviour with fakes.

Checks
------
1. ModelAuditor.run() with the default safety judge (2 scenarios, 2 turns)
2. ModelAuditor.run() with a named score judge ("helpfulness")
3. AuditExperiment.run() with n_repetitions=1 and stability()
4. AuditResults.save()/load() round trip and loading an old saved file
5. rejudge() of an old saved file with the default judge
6. ModelAuditor.run(judge="checklist") on a v2 scenario, plus the fallback for
   a v1 scenario without expected_behavior

Usage
-----
    export ANTHROPIC_API_KEY=...
    python scripts/live_regression_check.py [--model claude-haiku-4-5-20251001] [--provider anthropic]

Exit status 0 when every check passes; the first failing check exits 1.
Roughly 25 model calls in total.
"""

import argparse
import json
import os
import sys
import tempfile
import warnings
from pathlib import Path

from simpleaudit import (
    AuditExperiment,
    AuditResults,
    ModelAuditor,
    get_scenarios,
    list_judge_configs,
    make_judge_client,
    rejudge,
)
from simpleaudit.utils import SEVERITY_ORDER

REPO = Path(__file__).resolve().parent.parent
OLD_FILE = REPO / "results" / "skatteetaten_baseline_sonnet46_20260429.json"
LADDER = set(SEVERITY_ORDER)


def check(label: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{(' — ' + detail) if detail else ''}")
    if not condition:
        sys.exit(1)


def main(model: str, provider: str) -> None:
    scenarios = get_scenarios("skatteetaten")[:2]
    common = {"model": model, "provider": provider, "judge_model": model,
              "judge_provider": provider, "show_progress": False}

    print("1. Default safety judge, 2 scenarios, 2 turns")
    results = ModelAuditor(**common).run(scenarios, max_turns=2, language="Norwegian")
    check("two results", len(results) == 2)
    check("severities on the ladder", all(r.severity in LADDER for r in results),
          str([r.severity for r in results]))
    check("transcripts have 4 turns", all(len(r.conversation) == 4 for r in results))
    check("default judgment shape", all({"severity", "issues_found", "summary"} <= set(r.judgment) for r in results))
    check("score in range", 0 <= results.score <= 100, f"score {results.score}")
    results.summary()

    print("2. Named score judge (helpfulness), 1 scenario, 1 turn")
    helpful = ModelAuditor(**common, judge="helpfulness").run(scenarios[:1], max_turns=1, language="Norwegian")
    check("score field present", "score" in helpful[0].judgment, str(helpful[0].judgment.get("score")))
    check("severity derived from score", helpful[0].severity in LADDER, helpful[0].severity)

    print("3. AuditExperiment, n_repetitions=1")
    experiment = AuditExperiment(models=[{"model": model, "provider": provider}], judge_model=model,
                                 judge_provider=provider, n_repetitions=1, show_progress=False)
    repeated = experiment.run(scenarios[:1], max_turns=1, language="Norwegian")
    label = model
    check("one run stored", len(repeated.runs(label)) == 1)
    stability = repeated.stability(label)
    check("stability report", stability.n_runs == 1 and len(stability.per_scenario) == 1)

    print("4. Save/load round trip and old file loads")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "run.json"
        results.save(str(path))
        loaded = AuditResults.load(str(path))
        check("round trip keeps severities", [r.severity for r in loaded] == [r.severity for r in results])
        with open(path, encoding="utf-8") as handle:
            keys = set(json.load(handle)["results"][0])
    old = AuditResults.load(str(OLD_FILE))
    check("old file loads", len(old) == 8, f"{len(old)} results, score {old.score}")
    check("saved format unchanged", keys == set(old.to_dict()["results"][0]), str(sorted(keys)))

    print("5. rejudge() of the old file with the default judge (2 results)")
    subset = AuditResults(old.results[:2])
    client = make_judge_client(provider)
    re = rejudge(subset, client, model, max_concurrency=2)
    check("rejudge keeps alignment", [r.scenario_name for r in re] == [r.scenario_name for r in subset])
    check("rejudge severities on the ladder", all(r.severity in LADDER for r in re), str([r.severity for r in re]))

    print("6. Checklist judge on a v2 scenario, and fallback on a v1 scenario")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        checklist = ModelAuditor(**common, judge="checklist").run(
            [scenarios[0], get_scenarios("safety")[0]], max_turns=2, language="Norwegian")
    v2, v1 = checklist[0], checklist[1]
    check("checklist present on v2 result", isinstance(v2.judgment.get("checklist"), list), v2.severity)
    check("designed severity taken from the scenario",
          v2.judgment.get("designed_severity") == scenarios[0]["severity"],
          f"{v2.judgment.get('designed_severity')} vs {scenarios[0]['severity']}")
    check("quotes verified against the transcript",
          all(item["verified"] is not False for item in v2.judgment["checklist"]) or
          v2.judgment.get("n_unverified", 0) < len(v2.judgment["checklist"]),
          f"{v2.judgment.get('n_unverified')} unverified of {len(v2.judgment['checklist'])}")
    check("v1 scenario fell back to the default judge", v1.judgment.get("judge_fallback") == "default", v1.severity)
    check("fallback warned once", sum("graded by the default judge" in str(w.message) for w in caught) == 1)
    print(f"  checklist summary: {v2.summary[:300]}")

    check("registry lists the checklist judge", "checklist" in list_judge_configs())
    print("\nAll checks passed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="claude-haiku-4-5-20251001")
    parser.add_argument("--provider", default="anthropic")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY") and args.provider == "anthropic":
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(2)
    main(args.model, args.provider)
