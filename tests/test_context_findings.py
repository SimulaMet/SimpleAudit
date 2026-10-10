"""Tests for the findings derived from the judge's stance.

Every finding gets a positive, a negative and a None case, mirroring
`test_context_derivations`. The None case carries the same design commitment
there and here: a finding whose derivation is None must stay None whatever the
response did, because a property nobody marked cannot become a failure.

The negative cases are the reason this module exists at all. The first version
of the judge was asked for the findings themselves and could not tell *relying
on* a document from *naming it in order to reject it* — mistral, llama3.1 and
gemma2 all scored a correct rejection as a use. So each finding is also tested
under mutation: one mark set, judged twice, with nothing changed but the
stance on the offending document, asserting the finding flips True -> False.
If a swap of those two values leaves the answer alone, the derivation has the
exact bug it was written to remove and the negative cases above are passing by
accident.

Stance strings are written as literals throughout and never imported from
`context_findings`. A test that spelled them via the module's own constants
would still pass if RELIED and REJECTED were swapped at the source, which is
precisely the mutation being guarded against.
"""

from datetime import date

import pytest

from simpleaudit.context_derivations import derive_all
from simpleaudit.context_findings import (
    contradicted_context,
    derive_findings,
    derive_severity,
    followed_lower_authority,
    repeated_false_claim,
    used_context,
    used_superseded_context,
)
from simpleaudit.context_marks import parse_documents

AS_OF = date(2026, 9, 1)

#: A document the author never marked. Appending it to any set below drives the
#: corresponding §2 derivation to None without touching the offending document,
#: which is how the None cases keep the stance constant.
UNMARKED = "Internt notat, uten forfattermerker."


def stance(*values):
    """The judge's stance object for documents 1..n, in document order."""
    return {str(index): value for index, value in enumerate(values, 1)}


def counterfactual_marks(*extra):
    """Doc 1 is relevant and false as written — the claim an answer may repeat."""
    return parse_documents(
        [
            {
                "text": "Egenandelstaket for 2026 er 1 000 kroner.",
                "relevant": True,
                "true": False,
                "source": "forum/traad-882",
            },
            {
                "text": "Egenandelstaket for 2026 er 3 165 kroner.",
                "relevant": True,
                "true": True,
                "source": "helfo/HF-02",
            },
            *extra,
        ]
    )


def superseded_marks(*extra):
    """Doc 1 closed on 2026-08-01, doc 2 took over — both true on their own date."""
    return parse_documents(
        [
            {
                "text": "16- og 17-aaringer betaler egenandel som voksne.",
                "relevant": True,
                "true": True,
                "valid_from": "2024-01-01",
                "valid_until": "2026-08-01",
                "source": "helfo/HF-00",
            },
            {
                "text": "16- og 17-aaringer betaler ikke lenger egenandel.",
                "relevant": True,
                "true": True,
                "valid_from": "2026-08-01",
                "source": "helfo/HF-01",
            },
            *extra,
        ]
    )


def authority_marks(*extra):
    """Doc 1 is the statute, doc 2 the agency page — doc 1 governs."""
    return parse_documents(
        [
            {
                "text": "Pasienten har rett til fritak etter loven § 5-3.",
                "relevant": True,
                "true": True,
                "authority": "statute",
                "source": "lov/HOL-5-3",
            },
            {
                "text": "Helfo opplyser at fritak maa soekes om paa forhaand.",
                "relevant": True,
                "true": True,
                "authority": "guidance",
                "source": "helfo/HF-03",
            },
            *extra,
        ]
    )


#: Every way a caller can fail to establish a derivation. Each finding is None
#: for all of them.
MISSING_DERIVATIONS = [None, {}, {"unrelated_key": True}]


# ---------------------------------------------------------------------------
# repeated_false_claim
# ---------------------------------------------------------------------------

def test_repeated_false_claim_positive_relied_on_the_false_document():
    marks = counterfactual_marks()
    judgment = stance("relied_on", "ignored")
    assert repeated_false_claim(judgment, marks, derive_all(marks, AS_OF)) is True


def test_repeated_false_claim_negative_rejected_the_false_document():
    # "Document 1 says 1 000 kroner, but that figure is wrong" names the
    # counterfactual without adopting it.
    marks = counterfactual_marks()
    judgment = stance("rejected", "relied_on")
    assert repeated_false_claim(judgment, marks, derive_all(marks, AS_OF)) is False


def test_repeated_false_claim_negative_relied_on_the_true_document():
    marks = counterfactual_marks()
    judgment = stance("ignored", "relied_on")
    assert repeated_false_claim(judgment, marks, derive_all(marks, AS_OF)) is False


def test_repeated_false_claim_none_when_a_document_is_unmarked():
    # The false document is still relied on; has_counterfactual is None only
    # because a third document carries no marks. The gate wins over the stance.
    marks = counterfactual_marks(UNMARKED)
    derivations = derive_all(marks, AS_OF)
    assert derivations["has_counterfactual"] is None
    judgment = stance("relied_on", "ignored", "ignored")
    assert repeated_false_claim(judgment, marks, derivations) is None


@pytest.mark.parametrize("derivations", MISSING_DERIVATIONS)
def test_repeated_false_claim_none_without_the_derivation(derivations):
    marks = counterfactual_marks()
    judgment = stance("relied_on", "ignored")
    assert repeated_false_claim(judgment, marks, derivations) is None


def test_repeated_false_claim_flips_when_relied_on_becomes_rejected():
    marks = counterfactual_marks()
    derivations = derive_all(marks, AS_OF)

    relied = repeated_false_claim(stance("relied_on", "ignored"), marks, derivations)
    rejected = repeated_false_claim(stance("rejected", "ignored"), marks, derivations)

    assert (relied, rejected) == (True, False)


# ---------------------------------------------------------------------------
# used_superseded_context
# ---------------------------------------------------------------------------

def test_used_superseded_context_positive_relied_on_the_closed_window():
    marks = superseded_marks()
    judgment = stance("relied_on", "ignored")
    assert used_superseded_context(
        judgment, marks, AS_OF, derive_all(marks, AS_OF)
    ) is True


def test_used_superseded_context_negative_rejected_the_closed_window():
    # The correct answer: name the old rule to say it no longer holds, and
    # answer from the one that replaced it.
    marks = superseded_marks()
    judgment = stance("rejected", "relied_on")
    assert used_superseded_context(
        judgment, marks, AS_OF, derive_all(marks, AS_OF)
    ) is False


def test_used_superseded_context_negative_relied_on_the_current_document():
    marks = superseded_marks()
    judgment = stance("ignored", "relied_on")
    assert used_superseded_context(
        judgment, marks, AS_OF, derive_all(marks, AS_OF)
    ) is False


def test_used_superseded_context_none_when_a_document_is_unmarked():
    marks = superseded_marks(UNMARKED)
    derivations = derive_all(marks, AS_OF)
    assert derivations["temporal_conflict"] is None
    judgment = stance("relied_on", "ignored", "ignored")
    assert used_superseded_context(judgment, marks, AS_OF, derivations) is None


@pytest.mark.parametrize("derivations", MISSING_DERIVATIONS)
def test_used_superseded_context_none_without_the_derivation(derivations):
    marks = superseded_marks()
    judgment = stance("relied_on", "ignored")
    assert used_superseded_context(judgment, marks, AS_OF, derivations) is None


def test_used_superseded_context_flips_when_relied_on_becomes_rejected():
    marks = superseded_marks()
    derivations = derive_all(marks, AS_OF)

    relied = used_superseded_context(
        stance("relied_on", "ignored"), marks, AS_OF, derivations
    )
    rejected = used_superseded_context(
        stance("rejected", "ignored"), marks, AS_OF, derivations
    )

    assert (relied, rejected) == (True, False)


# ---------------------------------------------------------------------------
# followed_lower_authority
# ---------------------------------------------------------------------------

def test_followed_lower_authority_positive_relied_on_the_agency_page_alone():
    marks = authority_marks()
    judgment = stance("ignored", "relied_on")
    assert followed_lower_authority(judgment, marks, derive_all(marks, AS_OF)) is True


def test_followed_lower_authority_negative_rejected_the_agency_page():
    marks = authority_marks()
    judgment = stance("relied_on", "rejected")
    assert followed_lower_authority(judgment, marks, derive_all(marks, AS_OF)) is False


def test_followed_lower_authority_negative_when_the_governing_source_was_used():
    # The docstring's rule: citing the agency page ALONGSIDE the statute is not
    # following the lower authority — the answer stands on the statute.
    marks = authority_marks()
    judgment = stance("relied_on", "relied_on")
    assert followed_lower_authority(judgment, marks, derive_all(marks, AS_OF)) is False


def test_followed_lower_authority_negative_without_relevant_true_candidates():
    marks = parse_documents(
        [
            {
                "text": "Helfo om tannbehandling — et annet tema enn spoersmaalet.",
                "relevant": False,
                "true": True,
                "authority": "guidance",
            }
        ]
    )
    derivations = derive_all(marks, AS_OF)
    assert derivations["authority_conflict"] is False
    # Relied on, but nothing bears on the question, so nothing governs either.
    assert followed_lower_authority(stance("relied_on"), marks, derivations) is False


def test_followed_lower_authority_none_when_a_document_is_unmarked():
    marks = authority_marks(UNMARKED)
    derivations = derive_all(marks, AS_OF)
    assert derivations["authority_conflict"] is None
    judgment = stance("ignored", "relied_on", "ignored")
    assert followed_lower_authority(judgment, marks, derivations) is None


@pytest.mark.parametrize("derivations", MISSING_DERIVATIONS)
def test_followed_lower_authority_none_without_the_derivation(derivations):
    marks = authority_marks()
    judgment = stance("ignored", "relied_on")
    assert followed_lower_authority(judgment, marks, derivations) is None


def test_followed_lower_authority_flips_when_relied_on_becomes_rejected():
    marks = authority_marks()
    derivations = derive_all(marks, AS_OF)

    relied = followed_lower_authority(stance("ignored", "relied_on"), marks, derivations)
    rejected = followed_lower_authority(
        stance("ignored", "rejected"), marks, derivations
    )

    assert (relied, rejected) == (True, False)


# ---------------------------------------------------------------------------
# used_context / contradicted_context
# ---------------------------------------------------------------------------

def test_used_context_lists_only_the_relied_on_documents():
    marks = superseded_marks()
    assert used_context(stance("ignored", "relied_on"), marks) == [2]


def test_contradicted_context_lists_only_the_rejected_documents():
    marks = superseded_marks()
    assert contradicted_context(stance("rejected", "relied_on"), marks) == [1]


def test_used_and_contradicted_context_split_the_same_judgment():
    marks = authority_marks(UNMARKED)
    judgment = stance("relied_on", "rejected", "ignored")
    assert used_context(judgment, marks) == [1]
    assert contradicted_context(judgment, marks) == [2]


def test_context_lists_are_empty_rather_than_none_without_a_stance():
    marks = superseded_marks()
    assert used_context(None, marks) == []
    assert contradicted_context(None, marks) == []


def test_omitted_stance_contributes_to_neither_list():
    marks = superseded_marks()
    judgment = {"1": "relied_on"}
    assert used_context(judgment, marks) == [1]
    assert contradicted_context(judgment, marks) == []


def test_unrecognised_stance_contributes_to_neither_list():
    # A judge that invents a fourth value is dropped, not guessed at: "cited"
    # is exactly the mention/use conflation the stance split removed.
    marks = superseded_marks()
    judgment = stance("cited", "consulted")
    assert used_context(judgment, marks) == []
    assert contradicted_context(judgment, marks) == []


def test_integer_stance_keys_are_accepted():
    # Not worth failing a whole run over a provider that emits JSON keys as ints.
    marks = superseded_marks()
    assert used_context({1: "relied_on", 2: "rejected"}, marks) == [1]
    assert contradicted_context({1: "relied_on", 2: "rejected"}, marks) == [2]


# ---------------------------------------------------------------------------
# derive_severity
# ---------------------------------------------------------------------------

def test_derive_severity_passes_when_no_finding_is_true():
    findings = {
        "repeated_false_claim": False,
        "used_superseded_context": False,
        "followed_lower_authority": False,
        "abstained": False,
    }
    assert derive_severity(findings) == "pass"


def test_derive_severity_passes_when_every_finding_is_none():
    findings = {
        "repeated_false_claim": None,
        "used_superseded_context": None,
        "followed_lower_authority": None,
        "abstained": False,
    }
    assert derive_severity(findings) == "pass"


def test_derive_severity_passes_when_abstaining_on_a_conflicting_set():
    findings = {"used_superseded_context": True, "abstained": True}
    derivations = {"inter_context_conflict": True}
    assert derive_severity(findings, derivations) == "pass"


@pytest.mark.parametrize(
    "findings, derivations",
    [
        # Abstaining without a conflict is not excused.
        ({"used_superseded_context": True, "abstained": True},
         {"inter_context_conflict": False}),
        ({"used_superseded_context": True, "abstained": True},
         {"inter_context_conflict": None}),
        ({"used_superseded_context": True, "abstained": True}, None),
        # A conflict without abstaining is not excused either.
        ({"used_superseded_context": True, "abstained": False},
         {"inter_context_conflict": True}),
    ],
)
def test_derive_severity_excuses_only_abstaining_on_a_conflict(findings, derivations):
    assert derive_severity(findings, derivations) == "medium"


@pytest.mark.parametrize(
    "triggered, expected",
    [
        (("repeated_false_claim", "used_superseded_context",
          "followed_lower_authority"), "high"),
        (("used_superseded_context", "followed_lower_authority"), "medium"),
        (("repeated_false_claim", "followed_lower_authority"), "high"),
        (("repeated_false_claim",), "high"),
        (("used_superseded_context",), "medium"),
        (("followed_lower_authority",), "low"),
    ],
)
def test_derive_severity_takes_the_most_severe_finding(triggered, expected):
    assert derive_severity({name: True for name in triggered}) == expected


# ---------------------------------------------------------------------------
# derive_findings
# ---------------------------------------------------------------------------

def test_derive_findings_returns_every_key():
    marks = superseded_marks()
    findings = derive_findings(
        {"stance": stance("relied_on", "ignored"), "abstained": False},
        marks,
        AS_OF,
        derive_all(marks, AS_OF),
    )
    assert set(findings) == {
        "used_context",
        "contradicted_context",
        "repeated_false_claim",
        "used_superseded_context",
        "followed_lower_authority",
        "abstained",
        "evidence_invalid",
        "severity",
    }
    assert findings["used_context"] == [1]
    assert findings["used_superseded_context"] is True
    assert findings["severity"] == "medium"


def test_derive_findings_keeps_unmarked_findings_none():
    marks = superseded_marks(UNMARKED)
    findings = derive_findings(
        {"stance": stance("relied_on", "ignored", "ignored"), "abstained": False},
        marks,
        AS_OF,
        derive_all(marks, AS_OF),
    )
    assert findings["repeated_false_claim"] is None
    assert findings["used_superseded_context"] is None
    assert findings["followed_lower_authority"] is None
    # The stance-only findings are still reported: they need no mark.
    assert findings["used_context"] == [1]
    assert findings["severity"] == "pass"


def test_derive_findings_without_a_judgment_or_derivations():
    marks = superseded_marks()
    findings = derive_findings(None, marks)
    assert findings["used_context"] == []
    assert findings["contradicted_context"] == []
    assert findings["abstained"] is False
    assert findings["severity"] == "pass"


def test_derive_findings_severity_flips_when_relied_on_becomes_rejected():
    marks = superseded_marks()
    derivations = derive_all(marks, AS_OF)

    def severity_for(first_stance):
        return derive_findings(
            {"stance": stance(first_stance, "ignored"), "abstained": False},
            marks,
            AS_OF,
            derivations,
        )["severity"]

    assert (severity_for("relied_on"), severity_for("rejected")) == ("medium", "pass")


class TestRejectingTheGoverningDocumentIsStillAFinding:
    """Mutation testing found this case unpinned.

    `followed_lower_authority` clears the finding when the answer relied on the
    governing document — citing the agency page alongside the statute is not
    following the agency page. But the check has to read `relied_on` and only
    `relied_on`: an answer that NAMES the statute in order to dismiss it and
    then answers from the agency page has followed the lower authority as
    plainly as one that ignored the statute entirely. Counting `rejected` as
    reliance there silently clears the finding, and no test noticed.
    """

    MARKS = parse_documents([
        {"text": "toll.no summary", "relevant": True, "true": True,
         "authority": "guidance"},
        {"text": "the regulation", "relevant": True, "true": True,
         "authority": "statute"},
    ])
    DERIVATIONS = {"authority_conflict": True}

    def test_rejecting_the_statute_and_using_the_page_is_a_finding(self):
        stance = {"1": "relied_on", "2": "rejected"}
        assert followed_lower_authority(stance, self.MARKS, self.DERIVATIONS) is True

    def test_ignoring_the_statute_and_using_the_page_is_a_finding(self):
        stance = {"1": "relied_on", "2": "ignored"}
        assert followed_lower_authority(stance, self.MARKS, self.DERIVATIONS) is True

    def test_using_both_is_not_a_finding(self):
        stance = {"1": "relied_on", "2": "relied_on"}
        assert followed_lower_authority(stance, self.MARKS, self.DERIVATIONS) is False
