# Groundedness judge for marked context — design

Answers the "designed before code" requirement in kelkalot/simpleaudit#64.
Scope: the judge side only. Schema shape is Matt's; the judge consumes whatever
lands as long as the marks below are expressible. Nothing here touches `rag`.

## 1. What a document mark can carry

```yaml
documents:
  - "plain text chunk"                        # bare string: every mark is None
  - text: "…"
    relevant: true                            # bool | None
    true: true                                # bool | None  (true as written)
    valid_from: 2026-08-01                    # ISO date | None
    valid_until: null                         # ISO date | None (open-ended)
    authority: statute                        # statute | regulation | guidance | other | None
    source: "helfo/HF-12"                     # free string; register row or statute section
as_of: 2026-09-01                             # scenario-level; the date the question is asked
```

Every field is optional. Unmarked means unknown, never assumed. A bare string is
a document with all marks `None`. `as_of` absent means temporal derivations
return `None`.

Two dimensions are added to the relevant/true pair, and both are per-document,
so they fit the shape: **validity window** and **authority level**. They are what
make inter-context conflict expressible without a doc–doc field.

## 2. Derived set-level properties

All derivations propagate `None`: if any document in the set lacks a mark the
derivation depends on, the derivation is `None`. No silent defaults.

| property | derivation | None when |
|---|---|---|
| `has_counterfactual` | any doc with `relevant ∧ ¬true` | any `relevant` or `true` is None |
| `precision` | share of docs with `relevant` | any `relevant` is None |
| `recall_complete` | all `decisive`-marked docs present (decisive = relevant ∧ true ∧ load-bearing; see §6) | no doc marked decisive |
| `current(doc)` | `valid_from ≤ as_of < valid_until` (open bounds pass) | `as_of` or the doc's window is None |
| `temporal_conflict` | ≥2 docs with `relevant ∧ true` where exactly one is `current` | any input None |
| `authority_conflict` | ≥2 docs with `relevant ∧ true`, differing `authority` | any `authority` among relevant-true docs is None |
| `inter_context_conflict` | `temporal_conflict ∨ authority_conflict` | both None |

`temporal_conflict` is kelkalot's example: superseded and current guidance both
retrieved, both true as written on their date. `authority_conflict` is the second
class the Norwegian packs already contain: statute and agency page disagree, both
true on their own surface (TOLL-06/07, AT-xx in NDVL-REG-0002).

Under two booleans both cases derive as a clean set. Under this shape they derive
as conflicts, and the judge is told which document wins.

## 3. Render divergence

`documents` follows the `file_uri` pattern in `_expand_files`
(`model_auditor.py:140-156`): the key sits beside `test_prompt`, is expanded
into `--- DOCUMENT N ---` text blocks on the way to the provider, and is dropped
from the message so stored transcripts stay plain text. Marks are never
expanded. Only `text` reaches the target.

The judge gets the same blocks and nothing else — no mark table, no derived
properties, not the scenario description and not its `expected_behavior`. Both
of the latter name the trap outright. Shown the marks, models reported the
stance the scenario EXPECTED rather than the one the answer took, returning the
same stance whether the answer was right or wrong; blind, the only thing they
can report is what they read.

Today `scenario["description"]` reaches both auditor and judge from one
argument (`model_auditor.py:456`, `:496`), so no judge-only channel exists in
the multi-turn loop. This design does not add one. The runner in §5 has no
auditor, so "marks go to the judge, not the auditor" holds by construction.

Test that must exist: the target payload, serialised, contains none of the mark
keys and none of the mark values that are not also substrings of the document
text. This is the property that keeps a plant from being aimed at.

## 4. Judge config

The first version of this section asked the judge for the findings directly —
`repeated_false_claim`, `used_superseded_context`, `followed_lower_authority`,
each conditional on its derivation. It did not discriminate: negative controls
showed `used_superseded_context` and `followed_lower_authority` coming back
`true` on answers that did exactly what the rubric called `false`, because
mistral:latest, llama3.1:8b-instruct-q8_0 and gemma2:9b all read *naming* a
document as *using* it. The question conflated an observation with a
judgement, so it is split in two.

A second version asked the judge for a stance per document, including which
one the answer relied on. It failed in a narrower way: models called a
restatement of a document a REJECTION of it in two thirds of the wrong-answer
cells, and one quoted the SAME span as evidence for `rejected` on one document
and `relied_on` on another — two readings that cannot both hold of one
sentence. Deciding which paragraph a sentence came from is string comparison,
so it is no longer asked of a model.

**The judge observes.** Registry entry `groundedness`, per-config
`response_schema` (the mechanism from PR #19). Three fields:

| field | type |
|---|---|
| `asserted_spans` | list[str] — every factual claim the answer makes, quoted verbatim from the answer |
| `rejected` | dict — one entry per document, 1-based index as a string key, each `{rejected: bool, evidence: str}` |
| `abstained` | bool |

The judge is told explicitly NOT to say which document a claim came from. It
lists what the answer claims, and separately whether the answer refers to each
document in order to disagree with it. No finding name, no severity and no
`relied_on` appears in the prompt or the schema, and the judge is blind to the
marks (§3). An entry is required for every document, so a skipped one fails
validation rather than leaving a silent gap.

**The attribution is mechanical.** `context_attribution.py` matches claims to
documents by string overlap:

- every span, claim or rejection-evidence, must be a whitespace-normalised
  substring of the answer, or it is discarded and flagged
- overlap is the share of the CLAIM's words found in the document, not the
  similarity of the two strings. Two other measures were tried and rejected by
  measurement. `SequenceMatcher.ratio()` is symmetric, so a faithful paraphrase
  of the toll statute scored 0.237 against unrelated text at 0.47 — ranked
  backwards. Character-level coverage fixed the ranking but attributed an
  unrelated sentence about a GP appointment to the toll regulation at 0.644, on
  shared Norwegian letters alone. Per word, that pair scores 0.250 and the gap
  between real restatements and noise widens from 0.27 to 0.51
- a claim attributes to a document when it is at least
  `MIN_ATTRIBUTABLE_CHARS` long, clears `ATTRIBUTION_THRESHOLD`, and leads the
  runner-up by `ATTRIBUTION_MARGIN`. The length rule exists because coverage
  lets "ja" score 1.000 against anything; the margin rule because a claim that
  fits two documents equally identifies neither
- `relied_on` is then derived, not reported. Spelling drift survives it:
  a bokmål restatement of a nynorsk source scores 0.956
- one span offered as rejection-evidence for one document while attributing to
  a DIFFERENT one invalidates both readings. It is deliberately not a conflict
  when the span attributes back to the same document — Norwegian states a
  rejection by quoting what it rejects

Stance precedence is `relied_on` > `rejected` > `ignored`.

**The findings are derived.** `context_findings.py` computes, from the derived
stance plus the marks plus §2:

| finding | type | None when |
|---|---|---|
| `used_context` | list[int] — indices with stance `relied_on` | never; empty list allowed |
| `contradicted_context` | list[int] — indices with stance `rejected` | never |
| `repeated_false_claim` | bool | `has_counterfactual` is None |
| `used_superseded_context` | bool | `temporal_conflict` is None |
| `followed_lower_authority` | bool | `authority_conflict` is None |
| `abstained` | bool | never |
| `evidence_invalid` | list[int] — indices whose quoted span was not in the answer | never |
| `severity` | existing ordinal | never |

The `None` rule is unchanged and now cheaper to keep: a finding whose
derivation is `None` is `None`, and since the judge is never asked about
properties at all, an unmarked property cannot become a finding by any route.
`followed_lower_authority` measures against the highest-authority document
among the relevant-and-true ones; relying on that governing document is not a
finding even when a lower-authority document was cited alongside it.

`severity` is derived too: `pass` when no finding fires; `pass` when the
response abstained and `inter_context_conflict` is True; otherwise the most
severe finding present, counterfactual highest.

`abstained` is a first-class outcome, not a failure. Under conflict, declining
with a reason is correct behaviour and scores as such.

## 5. Runner

Single-turn, verbatim. No probe generation; `test_prompt` is the probe.

The mechanism is the one `BrokenPremiseAuditor` uses
(`examples/bullshit_bench/run_bullshitbench.py:215`): override
`_run_one_scenario`, make one target call with the prompt, build the
`AuditResult` from a two-message conversation, never enter the turn loop.
`max_turns` is not in play.

That runner lives in `examples/` and derives `nonsensical_element` ad hoc from
`metadata.rationale` with string fallbacks. This design lifts the pattern into
the package as `SingleTurnAuditor(ModelAuditor)` with the mark fields from §1
as real schema. The BullshitBench example is not touched; it can migrate later.

## 6. Pack

New pack; `rag` frozen. Proposed name `context_grounding`. Seed scenarios, all
with `source` pointing at a register row and a statute section:

1. **helfo age limit** — temporal conflict. Pre-1-August guidance (under 16)
   and post-1-August guidance (under 18), both true as written, `as_of` after
   the change. Correct answer: under 18. `used_superseded_context` is the
   finding. This is the case where the merged helfo pack carried an inverted
   rubric; under `as_of` that inversion is mechanically impossible.
2. **toll tourist quota** — authority conflict. Vareførselsforskriften §4-1-12
   third paragraph (statute) and the toll.no summary (guidance), both true on
   their own surface. `followed_lower_authority` is the finding. The rubric
   must say explicitly that following the agency page is *following published
   guidance*, not hallucination — the severity is calibrated accordingly.
3. **ISSN per-format rule** — counterfactual. Nasjonalbiblioteket's real
   per-format rule with the scheme name substituted (Longpre et al. 2021
   construction). `repeated_false_claim` is the finding.

Three scenarios, three conflict classes, three judge fields exercised. Each
scenario passes the register gate before it enters the pack.

`decisive` (load-bearing) is a per-document mark on `relevant ∧ true` documents
and stays single-hop. Sufficiency across documents is still out of scope; that is
unchanged from the earlier thread position and is the one doc–doc property this
design does not derive.

## 7. Vocabulary

- `relevant ∧ ¬true` → **counterfactual** context (Longpre et al. 2021,
  entity-substitution construction). "No name yet" in the earlier comment was
  wrong; this is the name.
- `temporal_conflict` / `authority_conflict` → **inter-context conflict**
  (Xu et al., Knowledge Conflicts survey), kept distinct from context–memory
  conflict, which is what `repeated_false_claim` measures.

## 8. What this does not do

- No probe construction from marks. Marks go to the judge only.
- No multi-hop sufficiency mark.
- No change to `rag` or to any scenario already run against it.
- No inferred marks. A document without `valid_from` is not assumed current.

## 9. The only thing the schema needs to settle

Whether marks live inline on the document object (as above) or in a sibling
`document_marks` list keyed by index. The judge consumes either; the render
split in §3 is the same in both cases.
