"""
Set-level properties derived from document marks.

The marks in :mod:`simpleaudit.context_marks` are per-document. What a judge
needs is per-*set*: does this document set contain a counterfactual, do two of
its documents conflict, is the answer even derivable from what was retrieved.
This module computes those, and each function here is one row of the design's
derivation table.

Every derivation propagates None. If a single document lacks a mark the
derivation depends on, the derivation is None — not False, not a default. That
is the whole point: a None derivation causes the judge prompt to *omit* the
corresponding question, so the judge is never asked something the scenario
author did not actually mark. A False in that position would instead assert
"no conflict here", which nobody established.

The asymmetry worth knowing about is :func:`inter_context_conflict`: it returns
True as soon as either conflict is True, even when the other is None. A known
conflict does not become unknown because a second, different conflict could not
be checked.
"""

from datetime import date
from typing import Any, Dict, List, Optional, Sequence

from .context_marks import DocumentMark


def current(mark: DocumentMark, as_of: Optional[date]) -> Optional[bool]:
    """
    Is *mark* in force on *as_of*?

    The window is half-open: ``valid_from <= as_of < valid_until``. An open
    bound passes — a document with no ``valid_from`` was not born on a date
    that disqualifies it, and one with no ``valid_until`` has not been
    superseded. But a document with *neither* bound has no window at all, and
    an unmarked document is never assumed current, so that returns None.

    Returns
    -------
    bool or None
        None when *as_of* is None or the document carries no window at all.
    """
    if as_of is None:
        return None
    if mark.valid_from is None and mark.valid_until is None:
        return None
    if mark.valid_from is not None and mark.valid_from > as_of:
        return False
    if mark.valid_until is not None and as_of >= mark.valid_until:
        return False
    return True


def _relevant_true(marks: Sequence[DocumentMark]) -> Optional[List[DocumentMark]]:
    """The documents marked relevant *and* true, or None if any is unmarked.

    Both conflict derivations rest on this set: a conflict is only interesting
    between documents that both bear on the question and are both true as
    written. One unknown mark anywhere makes the set itself unknown, because
    the missing document might have belonged in it.
    """
    for mark in marks:
        if mark.relevant is None or mark.true is None:
            return None
    return [mark for mark in marks if mark.relevant and mark.true]


def has_counterfactual(marks: Sequence[DocumentMark]) -> Optional[bool]:
    """
    Does the set contain a relevant document that is false as written?

    That construction — relevant, on topic, and wrong — is the counterfactual
    context of Longpre et al. (2021). Repeating it is the failure
    ``repeated_false_claim`` grades.

    Returns
    -------
    bool or None
        None if any document's ``relevant`` or ``true`` is unmarked.
    """
    for mark in marks:
        if mark.relevant is None or mark.true is None:
            return None
    return any(mark.relevant and not mark.true for mark in marks)


def precision(marks: Sequence[DocumentMark]) -> Optional[float]:
    """
    Share of the retrieved documents that are relevant.

    Returns
    -------
    float or None
        None if any ``relevant`` is unmarked, or if the set is empty — there is
        no share of nothing, and 0.0 would read as "retrieval returned only
        junk" rather than "retrieval returned nothing".
    """
    if not marks:
        return None
    for mark in marks:
        if mark.relevant is None:
            return None
    return sum(1 for mark in marks if mark.relevant) / len(marks)


def recall_complete(marks: Sequence[DocumentMark]) -> Optional[bool]:
    """
    Is a load-bearing document present in the set?

    ``decisive`` is single-hop by design: it says this one document is required
    for a correct answer, not that the set jointly suffices. Sufficiency across
    documents is out of scope.

    Returns
    -------
    bool or None
        None when no document carries ``decisive`` at all — nobody said which
        document the answer turns on, so nothing can be concluded about whether
        it made it into the set.
    """
    if all(mark.decisive is None for mark in marks):
        return None
    return any(mark.decisive for mark in marks)


def temporal_conflict(
    marks: Sequence[DocumentMark],
    as_of: Optional[date],
) -> Optional[bool]:
    """
    Are a superseded and a current document both in the set?

    The shape is two or more relevant-and-true documents of which *exactly one*
    is current on *as_of*. Both are true as written on their own date; only one
    is the right thing to answer from today. Two current documents are not a
    temporal conflict, and neither are two stale ones.

    Returns
    -------
    bool or None
        None if any ``relevant`` or ``true`` is unmarked, if *as_of* is None,
        or if any of the relevant-true documents has no derivable currency.
    """
    candidates = _relevant_true(marks)
    if candidates is None or as_of is None:
        return None
    currencies = [current(mark, as_of) for mark in candidates]
    if any(value is None for value in currencies):
        return None
    return len(candidates) >= 2 and sum(currencies) == 1


def authority_conflict(marks: Sequence[DocumentMark]) -> Optional[bool]:
    """
    Do two documents of different authority level both bear on the question?

    Statute against agency guidance, both true on their own surface, is the
    second conflict class the Norwegian register packs already contain. Which
    one wins is the judge's call; this only reports that the set forces one.

    Returns
    -------
    bool or None
        None if any ``relevant`` or ``true`` is unmarked, or if any
        relevant-true document has no ``authority``.
    """
    candidates = _relevant_true(marks)
    if candidates is None:
        return None
    if any(mark.authority is None for mark in candidates):
        return None
    return len(candidates) >= 2 and len({mark.authority for mark in candidates}) >= 2


def inter_context_conflict(
    marks: Sequence[DocumentMark],
    as_of: Optional[date],
) -> Optional[bool]:
    """
    Does the set conflict with itself, temporally or by authority?

    Any-True-wins: a conflict that *is* established stays established even when
    the other class could not be checked. None only when neither class is
    derivable, which is the one case where the set says nothing at all.
    """
    temporal = temporal_conflict(marks, as_of)
    authority = authority_conflict(marks)
    if temporal or authority:
        return True
    if temporal is None and authority is None:
        return None
    return False


def derive_all(
    marks: Sequence[DocumentMark],
    as_of: Optional[date],
) -> Dict[str, Any]:
    """
    Compute every set-level derivation at once.

    The keys match the judge fields that depend on them, so the prompt builder
    can walk this dict and drop the question for any derivation that is None.

    Returns
    -------
    dict
        Keys ``has_counterfactual``, ``precision``, ``recall_complete``,
        ``temporal_conflict``, ``authority_conflict``,
        ``inter_context_conflict``.
    """
    return {
        "has_counterfactual": has_counterfactual(marks),
        "precision": precision(marks),
        "recall_complete": recall_complete(marks),
        "temporal_conflict": temporal_conflict(marks, as_of),
        "authority_conflict": authority_conflict(marks),
        "inter_context_conflict": inter_context_conflict(marks, as_of),
    }
