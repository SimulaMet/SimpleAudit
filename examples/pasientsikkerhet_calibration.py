#!/usr/bin/env python3
"""
Compare the pasientsikkerhet_no judge with physicians' labels.

Draelos et al. (npj Digital Medicine 9:241, 2026) had physicians rate 888
chatbot answers to 222 patient-posed questions as acceptable or problematic
and tag what was wrong. This script grades the same answers with the
pasientsikkerhet_no judge and prints, per tag, how often judge and physician
agree. It answers one question: can a judge model, with no reference answer
to compare against, find what a physician finds?

In our run (2026-11-19) the answer was no. With gpt-4.1-mini on 160 held-out
answers, Cohen's kappa for "problematic" was 0.13 and the judge found 1 of
the 21 answers a physician had tagged unsafe. The same judge separates good
from bad answers on pasientsporsmal scenarios, where it is given the
scenario's expectations. Use it that way. Run this script before you rely on
another judge model without expectations.

The data is downloaded at run time from the authors' repository, pinned to a
commit and checked against a SHA-256. The repository has no licence, so
nothing from it is shipped here, and this script stores the download and its
output in your cache directory, outside the repository. The output contains
the judge's remarks about the answers; do not publish it.

Each answer was rated by one physician. Two physicians would not agree on
every answer, so perfect agreement is not the ceiling, and the paper does not
say what the ceiling is.

Usage
-----
    export OPENAI_API_KEY=...
    python examples/pasientsikkerhet_calibration.py --judge-model gpt-4.1-mini --limit 160

    --split dev|test|all   questions are split 40/60 by a hash of the question text, so
                           the four answers to one question stay together. Tune a prompt
                           on dev, report on test. Default: test.
    --limit N              grade at most N answers (in a fixed pseudo-random order).
    --provider NAME        any-llm provider of the judge model. Default: openai.

About 2,100 input and 400 output tokens per answer.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from simpleaudit import make_judge_client, rejudge
from simpleaudit.results import AuditResult, AuditResults

COMMIT = "b805c636fd74a0e988ea33009a32cfdd359b3f4f"
URL = (
    "https://raw.githubusercontent.com/rachellea/LLMPatientSafety/"
    f"{COMMIT}/SupplementaryDocuments/HealthAdviceDatasetWithEvaluations.xlsx"
)
SHA256 = "c1c8e47d1e9fe73cd78f63e3a09c37879891955101d7ce357ad324113575c959"

#: Judge tag -> the physician's column in the workbook.
TAGS = {
    "unsafe": "Unsafe",
    "problematic_content": "Includes Problematic Content",
    "missing_important_info": "Missing Important Info",
    "missing_history_taking": "Missing History Taking",
    "poor_writing": "Poor Writing",
}

_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "simpleaudit" / "healthadvice"


def fetch() -> Path:
    """The workbook from the cache, downloaded and verified if needed."""
    path = cache_dir() / "HealthAdviceDatasetWithEvaluations.xlsx"
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == SHA256:
        return path
    with urllib.request.urlopen(URL, timeout=120) as resp:
        data = resp.read()
    digest = hashlib.sha256(data).hexdigest()
    if digest != SHA256:
        raise RuntimeError(f"{URL} has SHA-256 {digest}, expected {SHA256}.")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def _column(ref: str) -> int:
    n = 0
    for c in ref:
        if c.isalpha():
            n = n * 26 + (ord(c.upper()) - 64)
    return n - 1


def read_sheet(path: Path) -> list:
    """First worksheet as a list of dicts. Standard library only; no formulas are evaluated."""
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS):
                shared.append("".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t")))
        book = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels.findall("p:Relationship", _NS)}
        first = book.find("m:sheets", _NS).findall("m:sheet", _NS)[0]
        target = targets[first.get(f"{{{_NS['r']}}}id")].lstrip("/")
        sheet = ET.fromstring(z.read(target if target.startswith("xl/") else "xl/" + target))
    rows = []
    for row in sheet.iter(f"{{{_NS['m']}}}row"):
        cells = {}
        for c in row.findall("m:c", _NS):
            v = c.find("m:v", _NS)
            if c.get("t") == "s" and v is not None:
                value = shared[int(v.text)]
            elif c.get("t") == "inlineStr":
                value = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
            else:
                value = v.text if v is not None else ""
            cells[_column(c.get("r"))] = value
        if cells:
            rows.append([cells.get(i, "") for i in range(max(cells) + 1)])
    header = rows[0]
    return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in rows[1:]]


def split_of(question: str) -> str:
    return "dev" if int(hashlib.sha1(question.encode()).hexdigest(), 16) % 100 < 40 else "test"


def kappa(a: list, b: list) -> float:
    n = len(a)
    po = sum(x == y for x, y in zip(a, b)) / n
    pa, pb = sum(a) / n, sum(b) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def report_line(name: str, physician: list, judge: list) -> str:
    n = len(physician)
    tp = sum(p and j for p, j in zip(physician, judge))
    fp = sum((not p) and j for p, j in zip(physician, judge))
    fn = sum(p and (not j) for p, j in zip(physician, judge))
    tn = n - tp - fp - fn
    sens = f"{tp}/{tp + fn}" if tp + fn else "-"
    spec = f"{tn}/{tn + fp}" if tn + fp else "-"
    return (
        f"  {name:<24} physician {100 * sum(physician) / n:5.1f}%   judge {100 * sum(judge) / n:5.1f}%   "
        f"agree {100 * (tp + tn) / n:5.1f}%   found {sens:>7}   cleared {spec:>9}   kappa {kappa(physician, judge):5.2f}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--judge-model", default="gpt-4.1-mini")
    ap.add_argument("--provider", default="openai")
    ap.add_argument("--split", default="test", choices=["dev", "test", "all"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    rows = [r for r in read_sheet(fetch()) if args.split in ("all", split_of(r["PatientQuestion"]))]
    rows.sort(key=lambda r: hashlib.sha1(r["UUID"].encode()).hexdigest())
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        sys.exit("No answers selected.")

    results = AuditResults([
        AuditResult(
            scenario_name=r["UUID"],
            scenario_description="A patient-posed medical question answered by a chatbot. No scenario expectations are given.",
            conversation=[
                {"role": "user", "content": r["PatientQuestion"]},
                {"role": "assistant", "content": r["ChatbotResponse"]},
            ],
            severity="pass", issues_found=[], positive_behaviors=[], summary="", recommendations=[],
        )
        for r in rows
    ])
    graded = rejudge(
        results, make_judge_client(args.provider), args.judge_model,
        judge="pasientsikkerhet_no", max_concurrency=args.concurrency, max_retries=2,
    )
    by_name = {g.scenario_name: g for g in graded.results}

    kept, records = [], []
    for r in rows:
        g = by_name[r["UUID"]]
        issues = (g.judgment or {}).get("issues")
        records.append({"uuid": r["UUID"], "chatbot": r["Chatbot"], "severity": g.severity, "judgment": g.judgment})
        if isinstance(issues, dict):
            kept.append((r, issues, g.severity))

    out = cache_dir() / f"calibration_{args.judge_model.replace('/', '_')}_{args.split}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=1))

    tokens_in = sum(g.judge_input_tokens for g in graded.results)
    tokens_out = sum(g.judge_output_tokens for g in graded.results)
    print(f"\npasientsikkerhet_no with {args.judge_model}, split={args.split}: "
          f"{len(kept)} graded, {len(rows) - len(kept)} without a usable reply")
    print(f"judge tokens: {tokens_in} in, {tokens_out} out")
    labels = {"acceptable": False, "problematic": True}
    unlabelled = [k for k in kept if k[0]["AcceptableVersusProblematic"].strip().lower() not in labels]
    kept = [k for k in kept if k not in unlabelled]
    if unlabelled:
        print(f"{len(unlabelled)} answers without an acceptable/problematic label left out")
    if not kept:
        return
    problematic = [labels[r["AcceptableVersusProblematic"].strip().lower()] for r, _, _ in kept]
    print(report_line("problematic (any tag)", problematic, [any(i.get(k) for k in TAGS) for _, i, _ in kept]))
    for tag, column in TAGS.items():
        print(report_line(tag, [bool(r[column].strip()) for r, _, _ in kept], [bool(i.get(tag)) for _, i, _ in kept]))
    print("\n'found' = answers the physician tagged that the judge tagged too; 'cleared' = answers the")
    print("physician did not tag that the judge left alone. One physician rated each answer.")
    print(f"\nsaved {out} (contains the judge's remarks on the dataset's answers; do not publish)")


if __name__ == "__main__":
    main()
