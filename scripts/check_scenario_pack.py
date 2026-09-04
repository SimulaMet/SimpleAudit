#!/usr/bin/env python3
"""
Structural checks for a SimpleAudit scenario pack.

    python scripts/check_scenario_pack.py <pack_name> [--repo PATH]

The rules come from simpleaudit/scenarios/simpleaudit_scenario_guidelines_v1.0.md
(section "Pack Conventions") and tests/test_scenario_data.py. Findings are tagged
ERROR (must fix), WARN (fix or explain in the PR) or INFO (for the reviewer).
Exit code is 1 when any ERROR is present. tests/test_scenario_pack_conventions.py
runs the ERROR-level rules in CI for the packs listed there.

Standard library only.
"""

import argparse
import difflib
import os
import re
import sys
from collections import Counter, defaultdict

TAXONOMY = {
    "Mental Health": {"Crisis Response", "Self-Harm", "Anxiety", "Depression", "Eating Disorders", "Diagnosis"},
    "Substance Safety": {"Medications", "Recreational Drugs", "Alcohol", "Overdose"},
    "Privacy": {"Data Security", "Confidentiality", "Anonymity"},
    "Age-Appropriate": {"Youth-Specific", "Parental Guidance", "Maturity-Matched"},
    "Identity & Relationships": {"LGBTQ+", "Family Conflict", "Peer Relationships", "Bullying"},
    "Physical Safety": {"Abuse", "Violence", "Emergency Situations"},
    "Harmful Content": {"Misinformation", "Dangerous Advice", "Illegal Activities"},
    "System Integrity": {"Prompt Injection", "Jailbreaking", "Role Confusion", "Hallucination"},
}
SEVERITIES = {"low", "medium", "high", "critical"}
SOURCE_TYPES = {"real_case", "synthetic", "adapted"}
BRANCHES = {"majority", "outlier", "third"}
PAIR_TYPES = {"matched", "branch_set"}
REQUIRED = ["schema_version", "name", "description", "test_prompt", "language",
            "expected_behavior", "category", "subcategory", "severity", "source", "metadata"]
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ROWID_RE = re.compile(r"\b[A-Z]{2,6}-\d{2}\b")
JUDGE_NOTE_RE = re.compile(r"^\s*(MERK FOR DOMMEREN|JUDGE NOTE|NOTE TO JUDGE)\b", re.I)
SEVERITY_WORD_RE = re.compile(r"\b(pass|low|medium|high|critical|bestått|lav|middels|høy|kritisk)\b", re.I)
PAIR_SIMILARITY_FLOOR = 0.75


class Report:
    def __init__(self):
        self.rows = []

    def add(self, level, where, msg):
        self.rows.append((level, where, msg))

    def error(self, where, msg):
        self.add("ERROR", where, msg)

    def warn(self, where, msg):
        self.add("WARN", where, msg)

    def info(self, where, msg):
        self.add("INFO", where, msg)

    @property
    def errors(self):
        return [r for r in self.rows if r[0] == "ERROR"]

    def render(self):
        out = []
        for level in ("ERROR", "WARN", "INFO"):
            rows = [r for r in self.rows if r[0] == level]
            if not rows:
                continue
            out.append(f"\n### {level} ({len(rows)})\n")
            out.extend(f"- [{where}] {msg}" for _, where, msg in rows)
        return "\n".join(out)


def prompt_diff(a, b):
    """Return (similarity ratio, changed tokens in a, changed tokens in b)."""
    ta, tb = a.split(), b.split()
    sm = difflib.SequenceMatcher(None, ta, tb)
    da, db = [], []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "equal":
            da.extend(ta[i1:i2])
            db.extend(tb[j1:j2])
    return sm.ratio(), " ".join(da), " ".join(db)


def check_scenarios(pack, scenarios, rep):
    names = Counter(s.get("name") for s in scenarios)
    prompts = Counter(s.get("test_prompt") for s in scenarios)
    pairs = defaultdict(list)
    sev, cat, lang = Counter(), Counter(), Counter()
    eb_len = []

    for i, s in enumerate(scenarios):
        w = s.get("name") or f"#{i}"
        for k in REQUIRED:
            if k not in s:
                rep.error(w, f"missing required field '{k}'")
        if s.get("schema_version") not in ("2.0", 2, "2"):
            rep.error(w, f"schema_version is {s.get('schema_version')!r}, expected '2.0'")

        n = s.get("name", "")
        if not (3 <= len(n) <= 200):
            rep.error(w, f"name length {len(n)} outside [3, 200]")
        if names[n] > 1:
            rep.error(w, "duplicate scenario name within pack")
        if " — " in n or " – " in n:
            rep.warn(w, "name uses a dash; the convention is ' - ' (Topic - Specific Concern)")
        elif " - " not in n:
            rep.warn(w, "name does not follow 'Topic - Specific Concern'")

        tp = s.get("test_prompt", "")
        if not isinstance(tp, str) or not tp.strip():
            rep.error(w, "test_prompt empty or not a string")
        elif prompts[tp] > 1:
            rep.error(w, "duplicate test_prompt within pack")
        if not isinstance(s.get("description"), str) or not s.get("description", "").strip():
            rep.error(w, "description empty or not a string")

        lg = s.get("language")
        lang[lg] += 1
        if not (isinstance(lg, str) and re.fullmatch(r"[a-z]{2}", lg)):
            rep.warn(w, f"language {lg!r} is not an ISO 639-1 code")

        c, sc = s.get("category"), s.get("subcategory")
        cat[c] += 1
        if c not in TAXONOMY:
            rep.error(w, f"category {c!r} not in the guideline taxonomy")
        elif sc not in TAXONOMY[c]:
            rep.error(w, f"subcategory {sc!r} not listed under {c!r}")

        sv = s.get("severity")
        sev[sv] += 1
        if sv not in SEVERITIES:
            rep.error(w, f"severity {sv!r} not in {sorted(SEVERITIES)}")

        src = s.get("source") or {}
        st = src.get("type")
        if st not in SOURCE_TYPES:
            rep.error(w, f"source.type {st!r} not in {sorted(SOURCE_TYPES)}")
        if st in ("real_case", "adapted"):
            for k in ("origin", "original_language"):
                if k not in src:
                    rep.error(w, f"source.{k} is required for source.type={st}")

        md = s.get("metadata") or {}
        if st == "synthetic" and not md.get("rationale"):
            rep.error(w, "synthetic scenario without metadata.rationale")
        author = str(md.get("author", ""))
        if not author:
            rep.warn(w, "metadata.author missing")
        elif not EMAIL_RE.search(author):
            rep.info(w, "metadata.author has no email; 'Name <email> (handle)' is preferred, a handle is accepted")
        if not ISO_DATE_RE.match(str(md.get("date_created", ""))):
            rep.warn(w, f"metadata.date_created {md.get('date_created')!r} is not YYYY-MM-DD")

        eb = s.get("expected_behavior")
        if not isinstance(eb, list) or not all(isinstance(x, str) for x in eb):
            rep.error(w, "expected_behavior must be a list of strings")
        else:
            eb_len.append(len(eb))
            if not (3 <= len(eb) <= 7):
                rep.warn(w, f"expected_behavior has {len(eb)} items; the guideline says 3-7")
            for j, x in enumerate(eb):
                if JUDGE_NOTE_RE.match(x):
                    if j != len(eb) - 1:
                        rep.warn(w, "a judge note inside expected_behavior must be the last item, or move it to metadata.judge_notes")
                    if not SEVERITY_WORD_RE.search(x):
                        rep.warn(w, f"judge note names no severity or verdict to assign: {x[:70]}...")
                    rep.info(w, "judge note inside expected_behavior; preferred home is metadata.judge_notes")
            with_ids = [j + 1 for j, x in enumerate(eb) if ROWID_RE.search(x)]
            if with_ids:
                rep.warn(w, f"register-row IDs in judge-facing expected_behavior lines {with_ids}; keep IDs in metadata only")

        jn = md.get("judge_notes")
        if jn is not None:
            if not isinstance(jn, list) or not all(isinstance(x, str) for x in jn):
                rep.error(w, "metadata.judge_notes must be a list of strings")
            else:
                for x in jn:
                    if not SEVERITY_WORD_RE.search(x):
                        rep.warn(w, f"judge note names no severity or verdict to assign: {x[:70]}...")

        rows = md.get("register_rows") or []
        if "kilde_utdrag" in md and "source_quote" not in md:
            rep.warn(w, "metadata.kilde_utdrag: the documented field name is metadata.source_quote")
        if rows and not (md.get("source_quote") or md.get("kilde_utdrag")):
            rep.warn(w, "register_rows present but no inline metadata.source_quote")

        # contact details anywhere except the author field
        blob = repr({k: v for k, v in s.items() if k != "metadata"}) + repr({k: v for k, v in md.items() if k != "author"})
        for e in set(EMAIL_RE.findall(blob)):
            rep.warn(w, f"email address outside metadata.author: {e}")

        if "pair_id" in md or "branch" in md or "pair_type" in md:
            pid, br = md.get("pair_id"), md.get("branch")
            pt = md.get("pair_type", "matched")
            if not pid:
                rep.error(w, "branch or pair_type set without pair_id")
            if pt not in PAIR_TYPES:
                rep.error(w, f"pair_type {pt!r} not in {sorted(PAIR_TYPES)}")
            if pt == "matched" and br not in BRANCHES:
                rep.error(w, f"branch {br!r} not in {sorted(BRANCHES)} for a matched pair")
            if pt == "branch_set" and (not br or br in ("majority", "outlier")):
                rep.error(w, f"a branch_set member needs a descriptive branch label, not {br!r}")
            pairs[pid].append(s)

    for pid, members in pairs.items():
        w = f"pair {pid}"
        if len(members) < 2:
            rep.error(w, "pair has a single member")
        br = Counter(m["metadata"].get("branch") for m in members)
        pts = {m["metadata"].get("pair_type", "matched") for m in members}
        if len(pts) > 1:
            rep.error(w, f"members disagree on pair_type: {sorted(pts)}")
        if pts == {"matched"}:
            if br.get("majority", 0) > 1:
                rep.error(w, f"{br['majority']} members labelled 'majority'")
            if br.get("majority", 0) == 0:
                rep.error(w, "matched pair without a 'majority' branch; use pair_type='branch_set' when there is no dominant rule")
            if br.get("outlier", 0) == 0:
                rep.error(w, "matched pair without an 'outlier' branch")
        base = members[0]["test_prompt"]
        for m in members[1:]:
            ratio, da, db = prompt_diff(base, m["test_prompt"])
            msg = f"prompt similarity to first member {ratio:.3f}; differs: «{da}» vs «{db}»"
            if pts == {"matched"} and ratio < PAIR_SIMILARITY_FLOOR:
                rep.warn(f"{w} / {m['name']}", "matched pair below the similarity floor: " + msg)
            else:
                rep.info(f"{w} / {m['name']}", msg)

    rep.info(pack, f"{len(scenarios)} scenarios; severity {dict(sev)}; category {dict(cat)}; language {dict(lang)}")
    if eb_len:
        rep.info(pack, f"expected_behavior items: min {min(eb_len)}, max {max(eb_len)}, mean {sum(eb_len) / len(eb_len):.1f}")
    if pairs:
        rep.info(pack, f"{len(pairs)} pair groups: " + ", ".join(f"{k}({len(v)})" for k, v in pairs.items()))


def check_registration(pack, repo, rep):
    from simpleaudit.scenarios import SCENARIO_PACKS, list_scenario_packs

    packs = list_scenario_packs()
    n = packs[pack]
    mine = {s["name"] for s in SCENARIO_PACKS[pack]}
    if not mine <= {s["name"] for s in SCENARIO_PACKS["all"]}:
        rep.warn(pack, "pack is not part of 'all' (fine only when documented, as for vision_integrity)")
    others = Counter()
    for pn, sc in SCENARIO_PACKS.items():
        if pn in (pack, "all", "bullshitbench", "epistemic_safety"):
            continue
        others.update(s["name"] for s in sc)
    dups = sorted(m for m in mine if others[m])
    if dups:
        rep.error(pack, f"scenario names collide with other packs: {dups}")

    def read(rel):
        with open(os.path.join(repo, rel), encoding="utf-8") as f:
            return f.read()

    init = read("simpleaudit/scenarios/__init__.py")
    if pack not in init.split('"""')[1]:
        rep.warn(pack, "pack not listed in the scenarios/__init__.py module docstring")

    readme = read("README.md")
    m = re.search(rf"\|\s*`{re.escape(pack)}`\s*\|\s*(\d+)\s*\|", readme)
    if not m:
        rep.error(pack, "no row for the pack in the README.md scenario table")
    elif int(m.group(1)) != n:
        rep.error(pack, f"README row says {m.group(1)} scenarios, the pack has {n}")
    m = re.search(r"\|\s*`all`\s*\|\s*(\d+)\s*\|", readme)
    if m and int(m.group(1)) != packs["all"]:
        rep.error(pack, f"README 'all' row says {m.group(1)}, computed {packs['all']}")

    if not os.path.exists(os.path.join(repo, f"simpleaudit/scenarios/{pack}_README.md")):
        rep.error(pack, f"simpleaudit/scenarios/{pack}_README.md is missing (required for every pack)")

    for t in ("tests/test_basic.py", "tests/test_model_auditor.py"):
        if pack not in read(t):
            rep.warn(pack, f"{t} does not mention the pack; the 'all' sum assertions need it")

    modpath = f"simpleaudit/scenarios/{pack}.py"
    if os.path.exists(os.path.join(repo, modpath)):
        for num in set(re.findall(r"\b(\d{1,3}) scenarios\b", read(modpath))):
            if int(num) != n:
                rep.warn(pack, f"module text says '{num} scenarios' but the pack has {n}")


def run_checks(pack, repo):
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from simpleaudit.scenarios import get_scenarios

    rep = Report()
    check_scenarios(pack, get_scenarios(pack), rep)
    check_registration(pack, repo, rep)
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pack")
    ap.add_argument("--repo", default=os.getcwd(), help="repository root (default: current directory)")
    a = ap.parse_args()
    rep = run_checks(a.pack, os.path.abspath(a.repo))
    print(f"## check_scenario_pack: {a.pack}")
    print(rep.render())
    sys.exit(1 if rep.errors else 0)


if __name__ == "__main__":
    main()
