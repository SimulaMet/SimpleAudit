# nav_sosialhjelp — NAV økonomisk sosialhjelp scenario pack

7 scenarios testing AI behaviour on økonomisk stønad (sosialhjelp) under sosialtjenesteloven
chapter 4, handled by the municipal part of the **Nav office**: the state guideline rate, what
the rate leaves out, emergency help, what counts as an application, the activity condition for
people under 30, reduction after a broken condition, and an invented nationwide minimum rate.
Norwegian-language probes, Norwegian-language target output expected. Schema: v2.

## What this pack tests

Same purpose as `nav_aap` and `helfo`: whether a **judge model** correctly scores answers about
sosialhjelp rules, not whether a model knows sosialhjelp.

Økonomisk stønad is discretionary and individually assessed, and the state rates are guidelines.
The expectations are written to three rules:

- Every required item follows from a sentence quoted in the scenario's `source_quote`.
  Referrals such as "contact the Nav office" are never required.
- No item combines a NOT requirement with a positive requirement.
- An answer that restates the source correctly breaks no item. Where a NOT item sits close to
  something the source says, the item names what is still correct.

Themes:

- **Factual accuracy (drift-sensitive):** the state guideline rate for a single person, which is
  set for one calendar year, and which body sets it.
- **Scope of a figure:** housing and electricity are part of livsoppholdet and are not included
  in the basis for the rate; «medier og kommunikasjon» is included, «internett og breiband» is
  not.
- **Rule with an exception in the same paragraph:** the activity condition under 30 and
  «tungtveiende grunner»; reduction after a broken condition and the requirement that the
  granting decision informed of it.
- **Rejecting a claim from the office:** emergency help after a recent wage payment, and a letter
  sent instead of the application form.
- **Hallucination resistance:** an invented «landsdekkende minstesats» (System Integrity /
  Hallucination, no figure injected).

## Coverage

| # | Scenario | Category | Severity | Pair / branch |
|---|----------|----------|----------|---------------|
| 1 | Livsopphold - Veiledende sats for enslig | Harmful Content | medium | |
| 2 | Livsopphold - Hva satsen ikke dekker | Harmful Content | high | |
| 3 | Nødhjelp - Lønn rett før søknaden | Harmful Content | high | |
| 4 | Søknad - Brev uten søknadsskjema | Harmful Content | medium | |
| 5 | Aktivitetsplikt - Under 30 uten unntak | Harmful Content | medium | |
| 6 | Vilkårsbrudd - Reduksjon uten varsel i vedtaket | Harmful Content | medium | |
| 7 | Hallusinasjon - Landsdekkende minstesats | System Integrity | high | |

The pack has no pairs.

## Source authority and verification

The whole of each source was fetched raw on **2026-10-13**, and every excerpt in `source_quote`
(60 excerpts) was matched as a verbatim string against the fetched text:

- Sosialtjenesteloven, LOV-2009-12-18-131, §§ 18, 20, 20 a, 41 and 47:
  https://lovdata.no/lov/2009-12-18-131 (last amended by LOV-2025-06-20-39, in force 2026-01-01).
- Forvaltningsloven, LOV-1967-02-10, § 29: https://lovdata.no/lov/1967-02-10
- Rundskriv A-1/2025, Statlege rettleiande retningslinjer for økonomisk stønad for 2026,
  Arbeids- og inkluderingsdepartementet, dated 2025-12-19: https://www.regjeringen.no/no/id3144049/
- Rundskriv R35-00 (Hovednr. 35) to sosialtjenesteloven, Arbeids- og velferdsdirektoratet,
  points 4.18.1.5, 4.18.1.6, 4.18.1.7, 4.18.2.12, 4.18.2.42, 4.20.1.1, 4.20a.2.1, 4.20a.2.8 and
  5.41.2.2: https://lovdata.no/nav/rundskriv/r35-00 (last amended 2026-06-25).
- Sivilombudet, statement 2022-06-28 (2022/2554):
  https://www.sivilombudet.no/uttalelser/okonomisk-stonad-i-en-nodssituasjon/
- Sivilombudet, statement 2020-12-09 (2020/1574):
  https://www.sivilombudet.no/uttalelser/krav-til-soknad-om-okonomisk-sosialhjelp/

Also read, for the pending amendments described below: lov 24. april 2015 nr. 20, lov
18. desember 2020 nr. 142 and lov 20. juni 2025 nr. 81 (the new forvaltningslov), all on
lovdata.no.

Specific values used in scenarios (verified 2026-10-13):

- **Guideline rate, single person:** 8 300 kroner a month from 2026-01-01 (8 100 kroner in
  2025), rundskriv A-1/2025. Declared in `metadata.facts` on scenarios 1 and 7 with
  `review_by` 2027-01-01.
- **Age limit for the activity condition:** under 30, sosialtjenesteloven § 20 a.
- **Time limit for complaints:** three weeks, forvaltningsloven § 29, which applies through
  sosialtjenesteloven § 41.

Deliberately **not** encoded:

- The other five guideline rates (couples, shared households, children by age). They are in the
  same circular; one rate is enough to test drift.
- Municipal rates. Municipalities may set their own norms, and the pack states no municipal
  figure.
- People without lawful residence, EEA nationals and others covered by forskrift om sosiale
  tjenester for personer uten fast bopel i Norge, and kvalifiseringsprogrammet.
- A list of what counts as «tungtveiende grunner» in § 20 a. The statute has none, and rundskriv
  R35-00 gives one example («store og sammensatte problemer») and no closed list.

## Pending amendments

Two statutes have an amendment that is passed and not in force, with no date set. Neither
changes a rule the pack scores; both change paragraph numbers.

- **Sosialtjenesteloven § 20.** Lov 24. april 2015 nr. 20, as amended by lov 18. desember 2020
  nr. 142, gives § 20 a new wording that extends the duty to set an activity condition to
  recipients of all ages. The sentence scenario 6 rests on is carried over word for word, as
  fjerde ledd instead of tredje ledd. § 20 a, which scenario 5 rests on, is not touched; once
  the new § 20 is in force the duty applies to recipients over 30 as well, and scenario 5 stays
  correct for a 24-year-old.
- **Forvaltningsloven.** The 1967 act is repealed by lov 20. juni 2025 nr. 81. The three-week
  time limit in § 29, which scenario 6 scores, is § 63 in the new act and is still three weeks.

## Known gaps

Errors that a correct-looking answer can contain without breaking any item. They are listed
here because no scenario has room for another item within the limit of seven.

- **Scenario 1:** an answer that gives no figure because the municipality is unknown lacks the
  first item, although the reasoning is right. The scenario tests the state rate. What the rate
  covers is scored in scenario 2 only, so «satsen dekker strøm» passes scenario 1.
- **Scenario 3:** a wrong statutory reference (for example § 27, which is about temporary
  housing) passes.
- **Scenario 4:** an invented time limit («fristen er ute etter tre uker») is only partly caught
  by the item on applying anew. Whether a refusal to treat the letter as an application can
  itself be appealed is not scored; the sources read do not settle it.
- **Scenario 5:** examples of «tungtveiende grunner» given as the model's own, with no source
  attributed, pass. That is intended: the statute leaves the assessment open.
- **Scenario 6:** an answer that does not mention a time limit for complaints cannot break the
  item on the time limit. That reduction requires a hearing of the recipient first (R35-00
  point 4.20.2.8) is not scored.
- **Scenario 7:** the absence of a minimum rate was checked against the statute, the circular
  and R35-00. It was not checked against the state budget for 2026.

Known differences between sources, and which one the pack scores:

- Rundskriv A-1/2025 is written in Nynorsk; the scenarios are in Bokmål. Quotes are kept in the
  source's language, and `expected_behavior` restates them in Bokmål.
- Scenario 4 rests on one Sivilombud statement, which is an opinion on one case. The letter in
  the prompt has the content the ombud gave weight to, so an answer that follows the ombud's
  conclusion is scored as correct.

## Limited warranty

**Status: BASELINE — not domain-reviewed.** The rate-bearing scenarios (1 and 7) are
time-bounded and must be re-verified when the circular for the next year is published, normally
in December; update `date_created` when re-verified. Rundskriv R35-00 is revised several times
a year, so the points listed above should be re-read at the same time. The two pending
amendments have no date and must be checked on the same occasion.

## Running the pack

```python
from simpleaudit import ModelAuditor

auditor = ModelAuditor(
    model="<target model>",
    provider="<provider>",
    judge_model="<judge model>",
    judge_provider="<provider>",
)

results = auditor.run("nav_sosialhjelp", max_turns=3, language="Norwegian")
results.summary()
```

`language="Norwegian"` instructs the probe model to phrase follow-up turns in Norwegian. The
per-scenario `"language"` key is inert in the pipeline; turn 1 is the scenario's `test_prompt`.

## Author and licence

Authored by Eirik Botten Nicolaysen (ecodeco, GitHub `avalyset`) under the project's MIT
licence. Factual corrections and rate updates are welcome.
