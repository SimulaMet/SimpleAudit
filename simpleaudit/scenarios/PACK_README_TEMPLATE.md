# <pack_name> — <one-line title>

<N> scenarios testing AI behaviour on <domain> administered by **<agency>**: <list the themes in
one sentence>. <Language>-language probes, <language>-language target output expected. Schema: v2.

## What this pack tests

State the purpose in one paragraph. For the public-sector packs: whether a **judge model**
correctly scores answers about <agency> rules, not whether a model knows <agency>.

- **<theme 1>:** what is probed and why it matters.
- **<theme 2>:** …
- **Hallucination resistance / boundary keeping / vulnerable-user routing:** if present.

If the pack uses pairs, explain the pairing and the reading rule here (see the guidelines,
"Paired scenarios").

## Coverage

| # | Scenario | Category | Severity | Pair / branch |
|---|----------|----------|----------|---------------|
| 1 | <name> | <category> | <severity> | <pair_id / branch or blank> |

## Source authority and verification

All factual claims are anchored to the following primary sources and were verified verbatim on
**<YYYY-MM-DD>**:

- <statute / regulation, with LOV- or FOR- identifier>
- <agency page(s), full URL>

Specific values used in scenarios (verified <YYYY-MM-DD>):

- **<fact>:** <value>, <source reference>.

Deliberately **not** encoded, because they could not be verified from a citable source or the
sources disagree: <list, with the reason>.

Known differences between sources (agency page vs statute), and which one the pack scores: <list>.

## Limited warranty

**Status: BASELINE — not domain-reviewed** (or: reviewed by <role>, <date>). Rate-bearing
scenarios (<list>) are time-bounded and must be re-verified <per year / per undervisningsår>;
update `date_created` when re-verified. Structural-rule scenarios age more slowly.

## Running the pack

```python
from simpleaudit import ModelAuditor

auditor = ModelAuditor(
    model="<target model>",
    provider="<provider>",
    judge_model="<judge model>",
    judge_provider="<provider>",
)

results = auditor.run("<pack_name>", max_turns=3, language="Norwegian")
results.summary()
```

`language="Norwegian"` instructs the probe model to phrase follow-up turns in Norwegian. The
per-scenario `"language"` key is inert in the pipeline; turn 1 is the scenario's `test_prompt`.

## Baseline (optional)

If a run is reported: target model(s), judge model, temperature, seed, repetitions, date. Result
files stay out of the tree. Keep claims proportionate to the number of runs.

## Author and licence

Authored by <Name> (<affiliation>) under the project's MIT licence. Factual corrections and rate
updates are welcome.
