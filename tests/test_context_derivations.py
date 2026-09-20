"""Tests for the set-level derivations.

Every derivation gets three cases — positive, negative, and None — because the
None case is the one that carries a design commitment: a derivation that cannot
be computed must not collapse to False. False asserts "checked, no conflict";
None means "nobody marked this", and it is what makes the judge prompt drop the
question instead of asking the judge to rule on something the author never
established. One unmarked document is enough to force it.
"""

from datetime import date

from simpleaudit.context_derivations import (
    authority_conflict,
    current,
    derive_all,
    has_counterfactual,
    inter_context_conflict,
    precision,
    recall_complete,
    temporal_conflict,
)
from simpleaudit.context_marks import DocumentMark, parse_documents

AS_OF = date(2026, 9, 1)


def doc(text="chunk", **marks):
    """A DocumentMark with everything unmarked except what the test sets."""
    return DocumentMark(text=text, **marks)


# ---------------------------------------------------------------------------
# current
# ---------------------------------------------------------------------------

def test_current_positive_inside_the_window():
    assert current(doc(valid_from=date(2026, 8, 1), valid_until=date(2027, 1, 1)), AS_OF) is True


def test_current_negative_superseded():
    assert current(doc(valid_from=date(2025, 1, 1), valid_until=date(2026, 8, 1)), AS_OF) is False


def test_current_negative_not_yet_in_force():
    assert current(doc(valid_from=date(2026, 10, 1)), AS_OF) is False


def test_current_open_lower_bound_passes():
    # No valid_from: the document was not born on a date that disqualifies it.
    assert current(doc(valid_until=date(2027, 1, 1)), AS_OF) is True


def test_current_open_upper_bound_passes():
    # No valid_until: nothing has superseded it.
    assert current(doc(valid_from=date(2026, 8, 1)), AS_OF) is True


def test_current_both_bounds_open_is_none():
    # No window at all is not "always in force" — it is unmarked.
    assert current(doc(), AS_OF) is None


def test_current_without_as_of_is_none():
    assert current(doc(valid_from=date(2026, 8, 1)), None) is None


def test_current_window_is_half_open():
    window = doc(valid_from=date(2026, 8, 1), valid_until=date(2026, 9, 1))
    assert current(window, date(2026, 8, 1)) is True
    assert current(window, date(2026, 9, 1)) is False


# ---------------------------------------------------------------------------
# has_counterfactual
# ---------------------------------------------------------------------------

def test_has_counterfactual_positive():
    marks = [
        doc(relevant=True, true=True),
        doc(relevant=True, true=False),
    ]
    assert has_counterfactual(marks) is True


def test_has_counterfactual_negative():
    # An irrelevant false document is not a counterfactual: nothing on topic
    # invites the model to repeat it.
    marks = [
        doc(relevant=True, true=True),
        doc(relevant=False, true=False),
    ]
    assert has_counterfactual(marks) is False


def test_has_counterfactual_none_when_a_document_is_unmarked():
    marks = [doc(relevant=True, true=False), doc()]
    assert has_counterfactual(marks) is None


# ---------------------------------------------------------------------------
# precision
# ---------------------------------------------------------------------------

def test_precision_positive_all_relevant():
    assert precision([doc(relevant=True), doc(relevant=True)]) == 1.0


def test_precision_negative_partial_and_none_relevant():
    assert precision([doc(relevant=True), doc(relevant=False)]) == 0.5
    assert precision([doc(relevant=False), doc(relevant=False)]) == 0.0


def test_precision_none_when_a_document_is_unmarked():
    assert precision([doc(relevant=True), doc()]) is None


def test_precision_of_an_empty_set_is_none():
    # 0.0 would read as "retrieval returned only junk" rather than "nothing".
    assert precision([]) is None


# ---------------------------------------------------------------------------
# recall_complete
# ---------------------------------------------------------------------------

def test_recall_complete_positive():
    marks = [doc(decisive=True), doc(decisive=False)]
    assert recall_complete(marks) is True


def test_recall_complete_negative():
    marks = [doc(decisive=False), doc(decisive=False)]
    assert recall_complete(marks) is False


def test_recall_complete_none_when_nothing_is_marked_decisive():
    # Nobody said which document the answer turns on.
    assert recall_complete([doc(relevant=True, true=True), doc()]) is None


# ---------------------------------------------------------------------------
# temporal_conflict
# ---------------------------------------------------------------------------

def _superseded_and_current():
    """The helfo age-limit shape: both true as written, one of them stale."""
    return [
        doc("under 16", relevant=True, true=True,
            valid_from=date(2025, 1, 1), valid_until=date(2026, 8, 1)),
        doc("under 18", relevant=True, true=True, valid_from=date(2026, 8, 1)),
    ]


def test_temporal_conflict_positive():
    assert temporal_conflict(_superseded_and_current(), AS_OF) is True


def test_temporal_conflict_negative_when_both_are_current():
    marks = [
        doc(relevant=True, true=True, valid_from=date(2026, 1, 1)),
        doc(relevant=True, true=True, valid_from=date(2026, 8, 1)),
    ]
    assert temporal_conflict(marks, AS_OF) is False


def test_temporal_conflict_none_when_a_document_is_unmarked():
    assert temporal_conflict(_superseded_and_current() + [doc()], AS_OF) is None


def test_temporal_conflict_none_without_as_of():
    assert temporal_conflict(_superseded_and_current(), None) is None


def test_temporal_conflict_none_when_a_window_is_missing():
    # The second document is relevant and true but carries no window, so its
    # currency is unknown — and so is the conflict.
    marks = [
        doc(relevant=True, true=True,
            valid_from=date(2025, 1, 1), valid_until=date(2026, 8, 1)),
        doc(relevant=True, true=True),
    ]
    assert temporal_conflict(marks, AS_OF) is None


# ---------------------------------------------------------------------------
# authority_conflict
# ---------------------------------------------------------------------------

def _statute_against_guidance():
    """The toll quota shape: statute and agency page, both true on their face."""
    return [
        doc("§ 4-1-12 tredje ledd", relevant=True, true=True, authority="statute"),
        doc("toll.no summary", relevant=True, true=True, authority="guidance"),
    ]


def test_authority_conflict_positive():
    assert authority_conflict(_statute_against_guidance()) is True


def test_authority_conflict_negative_when_levels_agree():
    marks = [
        doc(relevant=True, true=True, authority="guidance"),
        doc(relevant=True, true=True, authority="guidance"),
    ]
    assert authority_conflict(marks) is False


def test_authority_conflict_none_when_an_authority_is_missing():
    marks = [
        doc(relevant=True, true=True, authority="statute"),
        doc(relevant=True, true=True),
    ]
    assert authority_conflict(marks) is None


def test_authority_conflict_none_when_a_document_is_unmarked():
    assert authority_conflict(_statute_against_guidance() + [doc()]) is None


# ---------------------------------------------------------------------------
# inter_context_conflict
# ---------------------------------------------------------------------------

def test_inter_context_conflict_positive():
    assert inter_context_conflict(_statute_against_guidance(), AS_OF) is True


def test_inter_context_conflict_positive_survives_an_underivable_sibling():
    # Temporal is None (no windows anywhere), authority is True. A conflict
    # that is established does not become unknown because a different class
    # could not be checked.
    marks = _statute_against_guidance()
    assert temporal_conflict(marks, AS_OF) is None
    assert inter_context_conflict(marks, AS_OF) is True


def test_inter_context_conflict_negative():
    marks = [
        doc(relevant=True, true=True, authority="guidance",
            valid_from=date(2026, 1, 1)),
        doc(relevant=True, true=True, authority="guidance",
            valid_from=date(2026, 8, 1)),
    ]
    assert temporal_conflict(marks, AS_OF) is False
    assert authority_conflict(marks) is False
    assert inter_context_conflict(marks, AS_OF) is False


def test_inter_context_conflict_none_only_when_both_are_none():
    marks = [doc(), doc()]
    assert inter_context_conflict(marks, AS_OF) is None


# ---------------------------------------------------------------------------
# derive_all
# ---------------------------------------------------------------------------

def test_derive_all_returns_exactly_the_six_set_level_keys():
    assert set(derive_all([doc()], AS_OF)) == {
        "has_counterfactual",
        "precision",
        "recall_complete",
        "temporal_conflict",
        "authority_conflict",
        "inter_context_conflict",
    }


def test_derive_all_matches_the_individual_derivations():
    marks = _superseded_and_current()
    derived = derive_all(marks, AS_OF)

    assert derived["has_counterfactual"] is False
    assert derived["precision"] == 1.0
    assert derived["recall_complete"] is None
    assert derived["temporal_conflict"] is True
    assert derived["authority_conflict"] is None
    assert derived["inter_context_conflict"] is True


def test_derive_all_on_an_unmarked_set_is_all_none():
    # The judge prompt then asks none of the mark-dependent questions.
    assert all(value is None for value in derive_all([doc(), doc()], AS_OF).values())


# ---------------------------------------------------------------------------
# Guards that mutation testing showed no test was pinning
# ---------------------------------------------------------------------------


class TestTemporalConflictGuardsArePinned:
    """Two rules in `temporal_conflict` survived mutation with a green suite.

    Both existing tests reach `None` through `current()` returning `None` for
    every document, so they pass whether or not the guard they name is there.
    These isolate each rule on an input where nothing else produces the answer.
    """

    def test_as_of_none_is_what_makes_it_none_here(self):
        # No relevant-true documents at all, so the ">= 2 candidates" branch
        # would return False on its own. Only the as_of guard yields None.
        marks = parse_documents([
            {"text": "irrelevant", "relevant": False, "true": True},
        ])
        assert temporal_conflict(marks, None) is None

    def test_a_single_current_document_is_not_a_conflict(self):
        # One relevant-true document that is current. Exactly one is current,
        # so only the ">= 2" floor keeps this from reporting a conflict.
        marks = parse_documents([
            {"text": "current", "relevant": True, "true": True,
             "valid_from": "2026-01-01"},
        ])
        assert temporal_conflict(marks, date(2026, 9, 3)) is False

    def test_two_current_and_one_stale_is_not_exactly_one(self):
        marks = parse_documents([
            {"text": "current A", "relevant": True, "true": True,
             "valid_from": "2026-01-01"},
            {"text": "current B", "relevant": True, "true": True,
             "valid_from": "2026-02-01"},
            {"text": "stale", "relevant": True, "true": True,
             "valid_until": "2025-12-31"},
        ])
        assert temporal_conflict(marks, date(2026, 9, 3)) is False


class TestAuthorityConflictFloorIsPinned:
    def test_a_single_relevant_true_document_is_not_a_conflict(self):
        marks = parse_documents([
            {"text": "only one", "relevant": True, "true": True,
             "authority": "statute"},
        ])
        assert authority_conflict(marks) is False
