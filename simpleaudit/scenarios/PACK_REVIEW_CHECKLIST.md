# Scenario pack review checklist

For reviewers of a PR that adds or changes a pack under `simpleaudit/scenarios/`. The goal is
that every pack gets the same depth of review. Authors can use the same list before opening the PR.
The field rules referenced here are in `simpleaudit_scenario_guidelines_v1.0.md`, section
"Pack Conventions".

## 1. PR hygiene

- [ ] Targets `dev` and is rebased on the current `dev`.
- [ ] Touches only: the pack module, its `_README.md`, `scenarios/__init__.py`, the root `README.md`
      table, `tests/test_basic.py`, `tests/test_model_auditor.py`, and
      `tests/test_scenario_pack_conventions.py` (the pack added to `CONFORMING_PACKS`).
- [ ] No result files, generated files or unrelated fixes. Commits focused on the pack.
- [ ] Other open pack PRs are listed; they all touch the same registration files, so a merge
      order is stated.

## 2. Automated checks

```bash
python scripts/check_scenario_pack.py <pack>
pytest -q
```

- [ ] Checker: zero ERROR. Every WARN is either fixed or explained in the PR body.
- [ ] Test suite passes.

## 3. Read the whole pack, not the diff

For each scenario:

- [ ] Tests one behaviour; name, description and rationale agree on what that is.
- [ ] `test_prompt` is realistic, self-contained, in the pack's language, does not leak the answer,
      injects no figures unless the scenario is a deliberate drift or boundary test.
- [ ] `expected_behavior`: 3 to 7 observable items, includes "NOT" items, separates required from
      "kan nevne (ikke påkrevd)", contains nothing a judge does not need (no register IDs, process
      notes, hypothesis labels). Any judge note names the severity to assign.
- [ ] Category and severity valid and consistent within the pack and with sibling packs for the
      same failure class.
- [ ] `source` names the page or section the quote actually comes from; statute identifiers
      present where a rule is cited.
- [ ] Pairs: fields set on every member, `pair_type` honest (no `majority` label where there is no
      dominant rule), wording differs only in the element under test, and the module docstring, PR
      body and rationales agree on how many branches each group has.
- [ ] Rate-bearing facts carry a verification date.

## 4. Fact verification (do not skip)

Every `source_quote` and every factual claim in `expected_behavior` is checked against the live
primary source. Packs up to 15 scenarios: all of them. Larger packs: every outlier or
high-severity scenario plus a random half of the rest.

- [ ] Quote found verbatim on the cited page or statute (raw fetch; page summarisers truncate
      statute text and miss headings, so any "not found" is re-checked with `curl` and a grep).
- [ ] Claims that a source *omits* something are confirmed by a raw grep, not by a summary.
- [ ] A source divergence the pack relies on exists today, under the heading the pack names.
- [ ] Page "Oppdatert" stamps and redirects noted; a source touched after the pack's verification
      date is flagged even when the content still matches.
- [ ] Classification: a contradiction, or an expectation that would grade a correct model wrong,
      blocks the merge. Truncated quotes, wrong page cited, stale URLs are should-fix.

Record the result as a table (scenario, claims, verdict VERIFIED / PARTIAL / NOT FOUND /
UNVERIFIABLE / CONTRADICTED, evidence quote and URL) in the review.

## 5. Documentation

- [ ] `<pack>_README.md` follows `PACK_README_TEMPLATE.md`: purpose, coverage table, sources with
      verification date, values used, what was left out, status line, how to run, author.
- [ ] Root README row count and `all` count match `list_scenario_packs()`.
- [ ] `scenarios/__init__.py` docstring lists the pack; module docstring counts match the code.
- [ ] Code examples use real parameter names (`ModelAuditor(model=..., provider=..., judge_model=...)`).

## 6. Baseline run (if reported)

- [ ] Models, judge, temperature, seed, repetitions and date stated; result files not committed;
      claims proportionate to n. If no run is reported, the PR says why.

## 7. Verdict

Approve / approve after listed fixes / request changes, with findings grouped as blocking,
should-fix and nits, the fact-check table, what was verified fine (including the test result
line), and merge notes (rebase, conflicts, order).
