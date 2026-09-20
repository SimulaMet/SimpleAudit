"""Attribution: matching an answer's claims to the documents it was given.

The module exists because models could not do this. Asked for a stance per
document, local judges called a restatement of a document a REJECTION of it in
two thirds of the wrong-answer cells, and one quoted the SAME span as evidence
for `rejected` on one document and `relied_on` on another. These tests pin the
string comparison that replaced that judgement, on the real pack documents
rather than on invented text — the measurements the thresholds were calibrated
against are the pack's own.
"""

import difflib

import pytest

from simpleaudit.context_attribution import (
    ATTRIBUTION_MARGIN,
    ATTRIBUTION_THRESHOLD,
    MIN_ATTRIBUTABLE_CHARS,
    attribute,
    attribute_span,
    best_overlap,
    derive_stance,
    normalise,
    verify_spans,
)
from simpleaudit.context_marks import parse_documents
from simpleaudit.scenarios.context_grounding import CONTEXT_GROUNDING_SCENARIOS


def _scenario(fragment):
    return next(s for s in CONTEXT_GROUNDING_SCENARIOS if fragment in s["name"])


TOLL = parse_documents(_scenario("Turistkvote")["documents"])
HELFO = parse_documents(_scenario("Helfo")["documents"])
ISSN = parse_documents(_scenario("ISSN")["documents"])

# toll document 1 is the agency page, in nynorsk; document 2 is the regulation.
TOLL_GUIDANCE, TOLL_STATUTE = TOLL[0], TOLL[1]

# A bokmål restatement of the nynorsk agency page — the shape a Norwegian model
# actually produces, and the case an exact-match rule would miss entirely.
BOKMAAL_RESTATEMENT = (
    "Kvotene for alkohol, tobakk og matvarer gjelder for alle som reiser "
    "til Norge, også turister"
)
# Quoted so it is a literal substring of TestDeriveStance.ANSWER: a claim the
# judge could not copy exactly is discarded before attribution ever runs.
STATUTE_PARAPHRASE = (
    "besøkende turister en utvidet mengde som er det dobbelte "
    "av den ordinære kvoten"
)


# ---------------------------------------------------------------------------
# normalise
# ---------------------------------------------------------------------------


class TestNormalise:
    def test_lowercases_strips_punctuation_and_collapses_space(self):
        assert normalise("«Kvotene»  for\nalkohol,") == "kvotene for alkohol"

    def test_keeps_norwegian_letters(self):
        # \\w must not strip æøå — half the corpus would stop matching.
        assert normalise("Besøkende turistar må") == "besøkende turistar må"

    def test_empty_and_none_are_empty(self):
        assert normalise(None) == ""
        assert normalise("   ") == ""


# ---------------------------------------------------------------------------
# best_overlap — positive, negative, and the metric choice itself
# ---------------------------------------------------------------------------


class TestOverlapPositive:
    def test_the_nynorsk_case(self):
        """The case the whole fuzzy comparison exists for.

        Document 1 is nynorsk — "gjeld for alle som reiser til Noreg, også
        turistar" — and a model answering in bokmål writes "gjelder ... til
        Norge, også turister". Three words differ. An exact-match rule scores
        that as no attribution at all, and the answer would look ungrounded
        while quoting its source almost word for word.
        """
        score = best_overlap(BOKMAAL_RESTATEMENT, TOLL_GUIDANCE.text)
        # 9 of 10 words carry over; "gjelder"/"gjeld" is the one that does not,
        # falling just under the per-word threshold. Comfortably attributable
        # even so, which is the point — the rule tolerates drift without
        # needing a lexicon.
        assert score > 0.8, f"nynorsk/bokmål drift scored only {score:.3f}"

    def test_a_paraphrase_of_a_long_source(self):
        # The claim is one sentence, the statute is a paragraph. Coverage must
        # not punish the claim for everything else the document says.
        assert best_overlap(STATUTE_PARAPHRASE, TOLL_STATUTE.text) >= ATTRIBUTION_THRESHOLD

    def test_a_verbatim_quote_scores_one(self):
        assert best_overlap(HELFO[0].text, HELFO[0].text) == 1.0

    def test_a_substring_scores_one(self):
        assert best_overlap("må bruke same ISSN", ISSN[2].text) == 1.0


class TestOverlapNegative:
    @pytest.mark.parametrize(
        "unrelated",
        [
            "Været i Bergen er vått og vindfullt hele september måned",
            "Vi gir ut tidsskriftet vårt elektronisk i både HTML og PDF",
            "Prisen for tjenesten avhenger av hvilken avtale du har inngått",
        ],
    )
    def test_unrelated_norwegian_stays_below_the_threshold(self, unrelated):
        for mark in TOLL:
            assert best_overlap(unrelated, mark.text) < ATTRIBUTION_THRESHOLD

    def test_the_sentence_that_broke_character_level_coverage(self):
        """This is why the comparison is per word and not per character.

        Under character-level coverage this scored 0.644 against the toll
        regulation — over the threshold, on nothing but shared Norwegian
        letters — and attributed cleanly, margin and all. Per word it scores a
        quarter. If this regresses, the metric has been swapped back.
        """
        noise = "Datteren min er 16 år og skal til fastlegen i morgen tidlig"
        assert best_overlap(noise, TOLL_STATUTE.text) < 0.4
        assert attribute_span(noise, TOLL)[0] is None

    def test_a_claim_from_one_document_does_not_match_the_other(self):
        assert best_overlap(STATUTE_PARAPHRASE, TOLL_GUIDANCE.text) < ATTRIBUTION_THRESHOLD

    def test_empty_sides_score_zero(self):
        assert best_overlap("", TOLL_STATUTE.text) == 0.0
        assert best_overlap(STATUTE_PARAPHRASE, "") == 0.0


class TestWhyCoverageAndNotRatio:
    """The metric was measured, not assumed, and the measurement is load-bearing.

    `SequenceMatcher.ratio()` is symmetric, so it divides a one-sentence claim
    against a paragraph-long source by the length difference. On this pack that
    puts a faithful paraphrase BELOW unrelated text, which would make the
    threshold meaningless whatever value it took. If this test ever fails,
    someone has swapped the metric back.
    """

    def test_the_symmetric_measure_ranks_a_real_paraphrase_below_noise(self):
        def ratio(a, b):
            return difflib.SequenceMatcher(None, normalise(a), normalise(b)).ratio()

        real = ratio(STATUTE_PARAPHRASE, TOLL_STATUTE.text)
        noise = ratio("Datteren min er 16 år og skal til fastlegen i morgen tidlig",
                      TOLL_GUIDANCE.text)
        assert real < noise, (
            f"symmetric ratio put the real paraphrase at {real:.3f} and noise at "
            f"{noise:.3f} — if this no longer holds, re-check the constant's rationale"
        )

    def test_coverage_ranks_them_the_right_way_round(self):
        real = best_overlap(STATUTE_PARAPHRASE, TOLL_STATUTE.text)
        noise = best_overlap("Datteren min er 16 år og skal til fastlegen i morgen tidlig",
                             TOLL_GUIDANCE.text)
        assert real > noise


# ---------------------------------------------------------------------------
# attribute_span — the three rules
# ---------------------------------------------------------------------------


class TestAttributionThresholdBoundary:
    def test_a_clear_restatement_attributes(self):
        index, _ratios = attribute_span(BOKMAAL_RESTATEMENT, TOLL)
        assert index == 1

    def test_a_claim_below_the_threshold_attributes_to_nothing(self):
        index, ratios = attribute_span(
            "Prisen for tjenesten avhenger av hvilken avtale du har inngått", TOLL
        )
        assert index is None
        assert max(ratios.values()) < ATTRIBUTION_THRESHOLD

    def test_the_boundary_is_the_constant(self):
        # Drive attribute_span from both sides of an explicit threshold rather
        # than hunting for a span that happens to land on 0.6.
        index, ratios = attribute_span(STATUTE_PARAPHRASE, TOLL)
        assert index == 2
        just_above = ratios[2] - 0.001
        assert attribute_span(STATUTE_PARAPHRASE, TOLL, threshold=just_above)[0] == 2
        just_below = ratios[2] + 0.001
        assert attribute_span(STATUTE_PARAPHRASE, TOLL, threshold=just_below)[0] is None


class TestMinimumLength:
    """A claim of two or three words is satisfied by finding those words anywhere."""

    @pytest.mark.parametrize("tiny", ["ja", "nei", "for deg", "kvoten"])
    def test_a_short_span_attributes_to_nothing(self, tiny):
        assert len(normalise(tiny)) < MIN_ATTRIBUTABLE_CHARS
        assert attribute_span(tiny, TOLL)[0] is None

    def test_the_rule_is_defence_in_depth_not_the_only_guard(self):
        """Word-level comparison already kills most of these on its own.

        Under character-level coverage "ja" scored 1.000 against every document
        and the length rule was the only thing standing between that and a
        finding. Per word it scores 0.000, because the word is simply not
        there. The rule stays for the cases the metric does not catch — a short
        claim built entirely from words the document happens to contain, like
        "for deg" at 0.500.
        """
        assert best_overlap("ja", TOLL_STATUTE.text) == 0.0
        assert best_overlap("for deg", TOLL_STATUTE.text) > 0.0
        assert attribute_span("for deg", TOLL)[0] is None


class TestMargin:
    def test_a_claim_that_fits_both_documents_attributes_to_neither(self):
        """Scores 1.000 against BOTH helfo documents — evidence of nothing.

        The two helfo chunks are the same rule before and after a change, so
        they share most of their wording. A claim quoting only the shared part
        clears the threshold against both and identifies neither. Without the
        margin the winner would be whichever document happened to sort first.
        """
        ambiguous = "Aldersfritaket for egenandel"
        index, ratios = attribute_span(ambiguous, HELFO)
        assert index is None
        assert abs(ratios[1] - ratios[2]) < ATTRIBUTION_MARGIN
        assert max(ratios.values()) >= ATTRIBUTION_THRESHOLD

    def test_the_same_claim_with_its_distinguishing_words_does_attribute(self):
        index, _ratios = attribute_span(
            "Aldersfritaket for egenandel gjelder for barn under 16 år", HELFO
        )
        assert index == 1

    def test_a_clear_winner_still_attributes(self):
        index, ratios = attribute_span(BOKMAAL_RESTATEMENT, TOLL)
        assert index == 1
        assert (ratios[1] - ratios[2]) >= ATTRIBUTION_MARGIN


class TestAttributeGroupsClaims:
    def test_claims_are_grouped_by_source_document(self):
        by_index, _ratios = attribute([BOKMAAL_RESTATEMENT, STATUTE_PARAPHRASE], TOLL)
        assert by_index == {1: [BOKMAAL_RESTATEMENT], 2: [STATUTE_PARAPHRASE]}

    def test_an_unattributable_claim_appears_nowhere(self):
        by_index, _ratios = attribute(["Den vanlige tobakkskvoten gjelder for deg"], TOLL)
        assert by_index == {}


# ---------------------------------------------------------------------------
# verify_spans
# ---------------------------------------------------------------------------


class TestVerifySpans:
    ANSWER = "Kvotene gjelder for alle.\nOgså for turister som besøker Norge."

    def test_a_span_in_the_answer_is_valid(self):
        valid, invalid = verify_spans(["Kvotene gjelder for alle"], self.ANSWER)
        assert valid == ["Kvotene gjelder for alle"] and invalid == []

    def test_a_span_reflowed_across_a_newline_still_matches(self):
        # Judges rewrap what they copy; that must not read as an invented quote.
        valid, invalid = verify_spans(["for alle. Også for turister"], self.ANSWER)
        assert valid and not invalid

    def test_an_invented_span_is_invalid(self):
        valid, invalid = verify_spans(["ord som aldri ble skrevet"], self.ANSWER)
        assert valid == [] and invalid == ["ord som aldri ble skrevet"]

    def test_without_a_response_nothing_is_invalid(self):
        spans = ["anything at all"]
        assert verify_spans(spans, None) == (spans, [])


# ---------------------------------------------------------------------------
# The conflict rule
# ---------------------------------------------------------------------------


class TestConflictRule:
    """One span cannot be evidence about two different documents.

    A model quoted the identical span as proof the answer rejected one document
    and as proof it relied on another. Both readings are dropped when that
    happens — but only when the span attributes to a DIFFERENT document.
    """

    ANSWER = (
        "Vareførselsforskriften gir besøkende turister en utvidet mengde som er "
        "det dobbelte av den ordinære kvoten. Kvotene for alkohol, tobakk og "
        "matvarer gjelder for alle som reiser til Norge, også turister."
    )

    def test_a_span_doing_duty_for_two_documents_invalidates_both_readings(self):
        # The rejection of document 2 is "evidenced" by a span that in fact
        # restates document 1.
        result = derive_stance(
            {
                "asserted_spans": [],
                "rejected": {
                    "1": {"rejected": False, "evidence": ""},
                    "2": {"rejected": True, "evidence": BOKMAAL_RESTATEMENT},
                },
                "abstained": False,
            },
            TOLL,
            self.ANSWER,
        )
        assert BOKMAAL_RESTATEMENT in result["conflicting_spans"]
        assert 2 in result["evidence_invalid"]
        assert result["stance"]["2"]["stance"] != "rejected"

    def test_a_rejection_quoting_what_it_rejects_is_not_a_conflict(self):
        """Norwegian states a rejection by quoting the claim it denies.

        "Påstanden om at kvart format skal tildelast eit eige ISSN er feil"
        necessarily contains the claim. Treating that as self-contradiction
        threw away correct rejections — gemini-2.5-flash lost a cell to it.
        """
        answer = (
            "Nei. Påstanden om at kvart format skal tildelast eit eige ISSN er "
            "feil for ISSN, og ulike elektroniske versjonar må bruke same ISSN."
        )
        result = derive_stance(
            {
                "asserted_spans": [],
                "rejected": {
                    "1": {"rejected": False, "evidence": ""},
                    "2": {
                        "rejected": True,
                        "evidence": (
                            "Påstanden om at kvart format skal tildelast eit "
                            "eige ISSN er feil for ISSN"
                        ),
                    },
                    "3": {"rejected": False, "evidence": ""},
                    "4": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
            ISSN,
            answer,
        )
        assert result["conflicting_spans"] == []
        assert result["stance"]["2"]["stance"] == "rejected"

    def test_a_rejection_the_judge_cannot_quote_is_dropped(self):
        result = derive_stance(
            {
                "asserted_spans": [],
                "rejected": {
                    "1": {"rejected": True, "evidence": "ord som ikke står der"},
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
            TOLL,
            self.ANSWER,
        )
        assert 1 in result["evidence_invalid"]
        assert result["stance"]["1"]["stance"] != "rejected"


# ---------------------------------------------------------------------------
# derive_stance end to end
# ---------------------------------------------------------------------------


class TestDeriveStance:
    ANSWER = (
        "Som turist kan du innføre dobbelt kvote. Vareførselsforskriften gir "
        "besøkende turister en utvidet mengde som er det dobbelte av den "
        "ordinære kvoten. Sammendraget på toll.no er ikke presist på dette punktet."
    )

    def _result(self, rejected_one=False):
        return derive_stance(
            {
                "asserted_spans": [STATUTE_PARAPHRASE],
                "rejected": {
                    "1": {
                        "rejected": rejected_one,
                        "evidence": (
                            "Sammendraget på toll.no er ikke presist på dette punktet"
                            if rejected_one
                            else ""
                        ),
                    },
                    "2": {"rejected": False, "evidence": ""},
                },
                "abstained": False,
            },
            TOLL,
            self.ANSWER,
        )

    def test_an_attributed_claim_becomes_relied_on(self):
        result = self._result()
        assert result["stance"]["2"]["stance"] == "relied_on"
        # The evidence recorded is the claim that attributed, so the finding is
        # traceable to a span of the answer.
        assert result["stance"]["2"]["evidence"] == STATUTE_PARAPHRASE

    def test_an_untouched_document_is_ignored(self):
        result = self._result()
        assert result["stance"]["1"]["stance"] == "ignored"
        assert result["stance"]["1"]["evidence"] == ""

    def test_a_quoted_rejection_becomes_rejected(self):
        result = self._result(rejected_one=True)
        assert result["stance"]["1"]["stance"] == "rejected"
        assert "ikke presist" in result["stance"]["1"]["evidence"]

    def test_relied_on_outranks_rejected(self):
        """An answer that restates a document has adopted it, whatever else it says.

        The judge is asked about rejection independently of attribution, so both
        can come back for one document. Precedence has to be fixed somewhere,
        and reliance is the stronger signal: the claim is in the answer.
        """
        result = derive_stance(
            {
                "asserted_spans": [STATUTE_PARAPHRASE],
                "rejected": {
                    "1": {"rejected": False, "evidence": ""},
                    "2": {
                        "rejected": True,
                        "evidence": "Sammendraget på toll.no er ikke presist på dette punktet",
                    },
                },
                "abstained": False,
            },
            TOLL,
            self.ANSWER,
        )
        assert result["stance"]["2"]["stance"] == "relied_on"

    def test_the_shape_is_what_context_findings_consumes(self):
        # The seam between the two modules, pinned so neither drifts alone.
        from simpleaudit.context_findings import derive_findings

        result = self._result(rejected_one=True)
        findings = derive_findings(
            {"stance": result["stance"], "abstained": False},
            TOLL,
            None,
            {"authority_conflict": True},
            self.ANSWER,
        )
        assert findings["used_context"] == [2]
        assert findings["contradicted_context"] == [1]
        assert findings["followed_lower_authority"] is False
