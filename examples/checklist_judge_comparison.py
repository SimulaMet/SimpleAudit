#!/usr/bin/env python3
"""
Checklist judge versus holistic judge on stored transcripts, judge tokens only.

Runs the evidence-anchored checklist judge (``judge="checklist"``) through the
same fixed-transcript checks that examples/judge_robustness_example.py ran for
the holistic safety judge, and prints each number next to the committed
baseline from results/judge_robustness_<stem>.json:

  B. Judge swap       - Haiku and Sonnet grade the same transcripts, as a panel
  C. Resampling       - one judge graded k times per transcript
  D. Perturbations    - assistant turns restyled in Norwegian (apology,
                        disclaimer, padding, authority claim, self-certification)
  E. Re-judging       - the whole saved run re-scored under the checklist judge,
                        compared with the stored verdicts, and with the
                        hand-reviewed skatteetaten table where one exists
  F. Evidence         - how many quotes verified, how many results had complete
                        evidence, how many violations had to be excluded

The prompt-wording check (A) is omitted: the checklist rubric is the scenario's
own expected_behavior and is locked by design.

Usage
-----
    export ANTHROPIC_API_KEY=...
    python examples/checklist_judge_comparison.py [--k 5] [--concurrency 4]

Output: results/checklist_judge_<stem>.json per input file.
"""

import argparse
import asyncio
import glob
import json
import os
import statistics
import sys
from collections import Counter
from pathlib import Path

from simpleaudit import (
    AuditResults,
    PromptVariant,
    RepeatedExperimentResults,
    compare_judges,
    get_judge,
    get_scenarios,
    load_stored_records,
    make_judge_client,
    perturbation_variants,
    reframing_check_async,
    rejudge_async,
    severity_by_name,
)
from simpleaudit.checklist import derive_severity
from simpleaudit.utils import SEVERITY_ORDER, severity_direction

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

INPUTS = [
    (REPO / "examples" / "nav_aap" / "nav_aap_sonnet_4_6.json", "nav_aap"),
    *[(Path(p), "skatteetaten")
      for p in sorted(glob.glob(str(REPO / "results" / "skatteetaten_baseline_*_20260429.json")))],
]
LANGUAGE = "Norwegian"

JUDGE_A = {"provider": "anthropic", "model": "claude-haiku-4-5-20251001", "label": "haiku-4.5"}
JUDGE_B = {"provider": "anthropic", "model": "claude-sonnet-4-6", "label": "sonnet-4.6"}

#: Human reclassification recorded in results/skatteetaten_baseline_summary_20260429.md:
#: the stored JSON says medium; the reviewed verdict is high. Keyed by file stem
#: prefix and scenario-name prefix.
HAND_REVIEWED = {("skatteetaten_baseline_haiku45", "Ubetalt skatt og angst"): "high"}


def _has_results(path: Path) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            return isinstance(json.load(handle).get("results"), list)
    except (OSError, json.JSONDecodeError):
        return False


def _baseline(stem: str) -> dict:
    path = REPO / "results" / f"judge_robustness_{stem}.json"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _pct(value) -> str:
    return "   n/a" if value is None else f"{value:5.0%}"


def _agreement(reference: dict, candidate: dict) -> dict:
    """Exact-match rate and mean absolute ladder distance over shared scenarios."""
    names = [n for n in reference if n in candidate]
    exact = sum(1 for n in names if reference[n] == candidate[n])
    distances = [abs(d) for d in (severity_direction(reference[n], candidate[n]) for n in names) if d is not None]
    return {
        "n": len(names),
        "exact_match_rate": (exact / len(names)) if names else None,
        "mean_abs_distance": (statistics.fmean(distances) if distances else None),
        "per_scenario": {n: {"reference": reference[n], "checklist": candidate[n]} for n in names},
    }


def _rederive(judgment: dict, policy: str) -> str:
    """Severity of one checklist judgment under another unverified-violation policy."""
    items = judgment.get("checklist")
    if not isinstance(items, list):
        return judgment.get("severity", "ERROR")
    return derive_severity(items, judgment.get("designed_severity"), policy)["severity"]


def _policy_flip_rates(results, policy: str) -> dict:
    """Perturbation flip rates against the baseline when every cell is re-derived under *policy*.

    Uses the stored sample judgments (no judge calls), so the two policies can
    be compared on identical observations.
    """
    base_label = results.baseline_label or results.variant_labels[0]
    modal = {
        name: {label: Counter(_rederive(j, policy) for j in js).most_common(1)[0][0]
               for label, js in by_label.items()}
        for name, by_label in results.sample_judgments.items()
    }
    out = {}
    for label in results.variant_labels:
        if label == base_label:
            continue
        rows = [(m[base_label], m[label]) for m in modal.values() if base_label in m and label in m]
        flips = sum(1 for b, v in rows if b != v)
        out[label] = {"flip_rate": (flips / len(rows)) if rows else 0.0, "n": len(rows)}
    return out


def _evidence_stats(results, label: str) -> dict:
    verified = total = complete = n = unverified_violations = fallbacks = 0
    for by_label in results.sample_judgments.values():
        for judgment in by_label.get(label, []):
            n += 1
            if judgment.get("judge_fallback"):
                fallbacks += 1
            if judgment.get("evidence_complete"):
                complete += 1
            unverified_violations += judgment.get("n_unverified_violations", 0) or 0
            for item in judgment.get("checklist", []):
                if item.get("verified") is not None:
                    total += 1
                    verified += item["verified"] is True
    return {
        "n_judgments": n,
        "quote_verification_rate": (verified / total) if total else None,
        "evidence_complete_rate": (complete / n) if n else None,
        "n_unverified_violations": unverified_violations,
        "n_fallbacks": fallbacks,
    }


async def run_one(path: Path, pack: str, k: int, concurrency: int) -> dict:
    lookup = severity_by_name(get_scenarios(pack))
    records = load_stored_records(path, scenario_severities=lookup)
    baseline = _baseline(path.stem)
    config = get_judge("checklist")
    client_a = make_judge_client(JUDGE_A["provider"])
    client_b = make_judge_client(JUDGE_B["provider"])
    print(f"\n=== {path.name}: {len(records)} transcripts, pack {pack} ===")
    print("    (baseline = holistic safety judge, results/judge_robustness_*.json)")
    out: dict = {"input": str(path), "pack": pack, "n_records": len(records), "judge": JUDGE_A}

    print("B. Judge swap, as a panel")
    swap = await reframing_check_async(
        client_a, JUDGE_A["model"], records,
        [
            PromptVariant.from_judge("checklist", JUDGE_A["label"]),
            PromptVariant.from_judge("checklist", JUDGE_B["label"],
                                     judge_model=JUDGE_B["model"], judge_client=client_b),
        ],
        max_concurrency=concurrency,
    )
    panel = swap.panel()
    flip = swap.effects()[JUDGE_B["label"]].flip_rate
    base_panel = baseline.get("panel", {}).get("unanimous_rate")
    base_flip = (baseline.get("judge_swap", {}).get("effects", {}).get("sonnet-4.6", {}) or {}).get("flip_rate")
    print(f"    unanimous {panel.unanimous_rate():5.0%} (baseline {_pct(base_panel)}); "
          f"swap flip {flip:5.0%} (baseline {_pct(base_flip)}); flagged: {panel.flagged()}")
    out["judge_swap"] = swap.to_dict()
    out["panel"] = panel.to_dict()

    print(f"C. Judge-only resampling, k={k}")
    resample = await reframing_check_async(
        client_a, JUDGE_A["model"], records, [PromptVariant.from_judge("checklist")],
        k=k, max_concurrency=concurrency,
    )
    fragile = resample.fragile()
    base_fragile = sum(
        1 for cells in baseline.get("resampling", {}).get("stability", {}).values()
        if cells.get("safety", {}).get("agreement_rate", 1.0) < 0.6
    ) if baseline else None
    for name, cells in fragile.items():
        cell = cells["checklist"]
        print(f"    fragile: {name[:40]:<40} {cell.severities}  agreement {cell.agreement_rate:.0%}")
    print(f"    {len(fragile)} of {len(records)} fragile (baseline {base_fragile})")
    out["resampling"] = resample.to_dict()

    print(f"D. Transcript perturbations ({LANGUAGE})")
    perturbed = await reframing_check_async(
        client_a, JUDGE_A["model"], records,
        perturbation_variants(config["judge_prompt"], language=LANGUAGE,
                              response_schema=config["response_schema"],
                              postprocess=config["postprocess"], requires_expected_behavior=True),
        max_concurrency=concurrency,
    )
    base_effects = baseline.get("perturbations", {}).get("effects", {})
    excluded = _policy_flip_rates(perturbed, "exclude")
    print("    (count = unverified violations count, the default; exclude = only verified ones do)")
    for label, effect in perturbed.effects().items():
        base = (base_effects.get(label) or {}).get("flip_rate")
        print(f"    {label:<22} flip {effect.flip_rate:5.0%} count / {excluded[label]['flip_rate']:5.0%} exclude "
              f"(baseline {_pct(base)})  stricter {effect.n_stricter}  lenient {effect.n_lenient}  net {effect.net}")
    out["perturbations"] = perturbed.to_dict()
    out["perturbations_exclude_policy"] = excluded

    print("E. Re-judge the saved run under the checklist judge")
    original = AuditResults.load(str(path))
    rejudged = await rejudge_async(
        original, client_a, JUDGE_A["model"], judge="checklist",
        scenario_severities=lookup, max_concurrency=concurrency,
    )
    comparison = compare_judges(
        RepeatedExperimentResults({"subject": [original]}),
        RepeatedExperimentResults({"subject": [rejudged]}),
        subject_label="subject", label_a="stored", label_b="checklist",
    )
    stored = {r.scenario_name: r.severity for r in original}
    reviewed = dict(stored)
    for (stem_prefix, name_prefix), severity in HAND_REVIEWED.items():
        if path.stem.startswith(stem_prefix):
            for name in reviewed:
                if name.startswith(name_prefix):
                    reviewed[name] = severity
    candidate = {r.scenario_name: r.severity for r in rejudged}
    candidate_excluded = {r.scenario_name: _rederive(r.judgment, "exclude") for r in rejudged}
    vs_stored = _agreement(stored, candidate)
    vs_reviewed = _agreement(reviewed, candidate)
    vs_stored_excluded = _agreement(stored, candidate_excluded)
    above_design = sum(
        1 for r in rejudged
        if stored.get(r.scenario_name) in SEVERITY_ORDER
        and r.judgment.get("designed_severity") in SEVERITY_ORDER
        and SEVERITY_ORDER.index(stored[r.scenario_name]) > SEVERITY_ORDER.index(r.judgment["designed_severity"])
    )
    base_shift = (baseline.get("rejudge", {}).get("comparison", {}) or {}).get("n_shifted")
    print(f"    score {comparison['stats_a']['mean']} -> {comparison['stats_b']['mean']} "
          f"(delta {comparison['score_delta']:+.1f}); {comparison['n_shifted']} of {comparison['n_total']} "
          f"shifted (baseline rejudge under Sonnet shifted {base_shift})")
    print(f"    agreement with stored verdicts: exact {_pct(vs_stored['exact_match_rate'])} "
          f"(exclude policy {_pct(vs_stored_excluded['exact_match_rate'])}), "
          f"mean ladder distance {vs_stored['mean_abs_distance']}")
    print(f"    stored verdicts above the scenario's designed severity (unreachable for the checklist judge "
          f"by construction): {above_design} of {len(rejudged)}")
    if reviewed != stored:
        print(f"    agreement with hand-reviewed table: exact {_pct(vs_reviewed['exact_match_rate'])}")
        for name in reviewed:
            if reviewed[name] != stored[name]:
                print(f"      reviewed override {name[:40]}: stored {stored[name]}, reviewed {reviewed[name]}, "
                      f"checklist {candidate.get(name)}")
    out["rejudge"] = {
        "comparison": comparison,
        "rejudged": {r.scenario_name: r.judgment for r in rejudged},
        "agreement_with_stored": vs_stored,
        "agreement_with_stored_exclude_policy": vs_stored_excluded,
        "agreement_with_reviewed": vs_reviewed,
        "stored_above_designed_severity": above_design,
    }

    print("F. Evidence")
    evidence = _evidence_stats(resample, "checklist")
    print(f"    quotes verified {_pct(evidence['quote_verification_rate'])}; results with complete evidence "
          f"{_pct(evidence['evidence_complete_rate'])}; unverified violations (counted, flagged) "
          f"{evidence['n_unverified_violations']}; fallbacks {evidence['n_fallbacks']}")
    out["evidence"] = evidence
    out["severity_ladder"] = SEVERITY_ORDER
    return out


async def main(k: int, concurrency: int) -> None:
    inputs = [(p, pack) for p, pack in INPUTS if _has_results(p)]
    if not inputs:
        print("No stored result files with a 'results' list found.", file=sys.stderr)
        sys.exit(2)
    for path, pack in inputs:
        payload = await run_one(path, pack, k, concurrency)
        target = REPO / "results" / f"checklist_judge_{path.stem}.json"
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
        print(f"    written {target.relative_to(REPO)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--k", type=int, default=5, help="samples per cell for the resampling check")
    parser.add_argument("--concurrency", type=int, default=4, help="judge calls in flight at once")
    args = parser.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        sys.exit(2)
    asyncio.run(main(args.k, args.concurrency))
