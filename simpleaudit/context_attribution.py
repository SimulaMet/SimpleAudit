"""
Attributing an answer's claims to the documents it was given — mechanically.

The judge no longer says which document an answer relied on. It says what the
answer CLAIMS, quoted verbatim, and separately which documents it argues
against. Matching a claim to its source is done here, by string overlap.

Why the model stopped doing it. Asked directly for a stance per document,
local judges called an answer that restates a document a REJECTION of it in 6
of 9 wrong-answer cells, and one of them quoted the SAME span as evidence for
`rejected` on one document and `relied_on` on another — two stances that
cannot both hold of one sentence. Listing the claims is something a model can
do; deciding which paragraph a sentence came from is string comparison, and
string comparison does not have opinions about which document ought to be the
right one.

The overlap is deliberately fuzzy, and asymmetric. A model answering in
Norwegian restates a nynorsk source in bokmål — "gjeld for alle som reiser til
Noreg, også turistar" becomes "gjelder for alle som reiser til Norge, også
turister" — and an exact-match rule would score that as no attribution at all.
The score is the share of the CLAIM found in the document, not the similarity
of the two strings: see ATTRIBUTION_THRESHOLD for why the symmetric measure
was measured and rejected.
"""

import difflib
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .context_marks import DocumentMark

#: Share of a claim's words that must be traceable in a document for the claim
#: to count as coming from it.
#:
#: One constant, because two would invite tuning each finding separately until
#: the table looks right. Calibrated against known-real restatements and
#: known-unrelated text rather than picked: over the pack, real restatements
#: cover 0.85-1.00 of the claim's words while unrelated text reaches at most
#: 0.33, so 0.6 sits in the middle of a 0.51-wide gap.
#:
#: The calibration also ruled out two other measures. `SequenceMatcher.ratio()`
#: is symmetric, so a one-sentence claim against a paragraph-long source is
#: divided by the length difference — a faithful paraphrase of the toll statute
#: scored 0.237 against noise at 0.47, ranking them backwards. Character-level
#: coverage fixed that but left a narrower gap and one outright false
#: attribution: an unrelated sentence about a GP appointment scored 0.644
#: against the toll regulation on shared letters alone.
ATTRIBUTION_THRESHOLD = 0.6

#: Shortest claim, in normalised characters, that may attribute at all.
#:
#: Coverage measures the share of the CLAIM found in the document, so a short
#: claim finds its own few characters again almost anywhere: "ja" scores 1.000
#: against every document in the pack, "for deg" likewise. Measured on the pack,
#: real claims run 41-101 characters and the spans that attributed to everything
#: were 2-15, so 25 sits clear of both.
MIN_ATTRIBUTABLE_CHARS = 25

#: How far ahead of the runner-up the winning document must be.
#:
#: A claim that fits two documents equally identifies neither. The two helfo
#: chunks are one rule before and after a change, so they share most of their
#: wording: "Aldersfritaket for egenandel" scores 1.000 against BOTH. Without a
#: margin the winner would be whichever document sorted first. Calibrated over
#: the pack: 0.05-0.15 all score the same, so 0.10 is the middle of the range
#: that works.
ATTRIBUTION_MARGIN = 0.10

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)


def normalise(text: Optional[str]) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    Comparison happens on this form so a quote survives re-wrapping, a stray
    comma, or the « » a Norwegian source quotes with.
    """
    if not text:
        return ""
    return " ".join(_PUNCTUATION.sub(" ", text.lower()).split())


#: How close two words must be to count as the same word. Norwegian inflection
#: and the nynorsk/bokmål split are both small edits — gjeld/gjelder,
#: Noreg/Norge, turistar/turister — while genuinely different words are not.
_WORD_MATCH = 0.85


def _same_word(word: str, candidates: Sequence[str]) -> bool:
    """Is this word in the document, allowing for inflection and målform?"""
    if word in candidates:
        return True
    return any(
        difflib.SequenceMatcher(None, word, other).ratio() >= _WORD_MATCH
        for other in candidates
    )


def best_overlap(span: str, document_text: str) -> float:
    """Share of the words in `span` that appear in the document.

    Asymmetric on purpose: the score answers "how much of this claim is in that
    document", not "how similar are these two strings". A document that says a
    great deal more than the claim is not penalised for it, which is the normal
    case — claims are sentences, retrieved chunks are paragraphs.

    Compared per word rather than per character, and that choice was forced by
    measurement. Character-level coverage lets an arbitrary sentence find its
    own letters scattered through a long document: "Datteren min er 16 år og
    skal til fastlegen i morgen tidlig" scored 0.644 against the toll
    regulation, over the threshold, on nothing but shared Norwegian letters.
    Word-level comparison cannot do that — it scores the same pair at 0.250 —
    and widens the gap between real restatements and noise from 0.27 to 0.51.

    Fuzzy within a word, so spelling drift survives: a bokmål restatement of a
    nynorsk source scores 0.867 where an exact rule would score nothing.

    Returns 0.0 when either side is empty.
    """
    needle = normalise(span).split()
    haystack = normalise(document_text).split()
    if not needle or not haystack:
        return 0.0
    return sum(1 for word in needle if _same_word(word, haystack)) / len(needle)


def _found_in(span: str, response: str) -> bool:
    """Is this span actually in the response, ignoring whitespace differences?"""
    return bool(normalise(span)) and normalise(span) in normalise(response)


def verify_spans(
    asserted_spans: Optional[Sequence[str]],
    response: Optional[str],
) -> Tuple[List[str], List[str]]:
    """Split the judge's quoted claims into those in the answer and those not.

    A span the judge could not copy correctly is a claim about an answer it did
    not read. With no response to check against nothing is invalid — the caller
    withheld the evidence, so the judge is not at fault.
    """
    spans = [s for s in (asserted_spans or []) if isinstance(s, str)]
    if response is None:
        return list(spans), []
    valid, invalid = [], []
    for span in spans:
        (valid if _found_in(span, response) else invalid).append(span)
    return valid, invalid


def attribute_span(
    span: str,
    marks: Sequence[DocumentMark],
    threshold: float = ATTRIBUTION_THRESHOLD,
) -> Tuple[Optional[int], Dict[int, float]]:
    """Which document a single claim came from, and its overlap with each.

    Returns ``(index_or_None, ratios)``. The index is None when the claim is
    too short to attribute, when nothing clears the threshold, or when the two
    best documents are within `ATTRIBUTION_MARGIN` of each other — a claim
    that fits two sources equally is evidence about neither.
    """
    ratios = {
        index: best_overlap(span, mark.text)
        for index, mark in enumerate(marks, 1)
    }
    if len(normalise(span)) < MIN_ATTRIBUTABLE_CHARS or not ratios:
        return None, ratios

    ranked = sorted(ratios.items(), key=lambda kv: kv[1], reverse=True)
    best_index, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if best_score < threshold or (best_score - runner_up) < ATTRIBUTION_MARGIN:
        return None, ratios
    return best_index, ratios


def attribution_ratios(
    spans: Sequence[str],
    marks: Sequence[DocumentMark],
) -> Dict[int, float]:
    """Best overlap between any claim and each document, by 1-based index.

    Reported for the record; the stance is decided by `attribute_span`, which
    also applies the length and margin rules this does not.
    """
    return {
        index: max((best_overlap(span, mark.text) for span in spans), default=0.0)
        for index, mark in enumerate(marks, 1)
    }


def attribute(
    spans: Sequence[str],
    marks: Sequence[DocumentMark],
    threshold: float = ATTRIBUTION_THRESHOLD,
) -> Tuple[Dict[int, List[str]], Dict[int, float]]:
    """Group claims by the document each came from.

    Returns ``(claims_by_index, ratios)``. A claim that attributes to nothing
    appears in neither — an unattributable claim is not evidence that the
    answer relied on any particular document.
    """
    by_index: Dict[int, List[str]] = {}
    for span in spans:
        index, _ratios = attribute_span(span, marks, threshold)
        if index is not None:
            by_index.setdefault(index, []).append(span)
    return by_index, attribution_ratios(spans, marks)


def _rejected_entry(
    rejected: Optional[Dict[str, Any]],
    index: int,
) -> Tuple[bool, Optional[str]]:
    """The judge's rejection claim for one document, as (claimed, evidence)."""
    if not rejected:
        return False, None
    entry = rejected.get(str(index))
    if entry is None:
        entry = rejected.get(index)
    if entry is None:
        return False, None
    if isinstance(entry, bool):
        return entry, None
    if isinstance(entry, dict):
        return bool(entry.get("rejected")), entry.get("evidence")
    return False, None


def derive_stance(
    judgment: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    response: Optional[str] = None,
    threshold: float = ATTRIBUTION_THRESHOLD,
) -> Dict[str, Any]:
    """
    Work out, per document, what the answer did with it.

    Args:
        judgment: The judge's output — `asserted_spans` and a per-document
            `{rejected, evidence}` map (the map may be keyed `stance` or
            `rejected`; both are read).
        marks: Parsed document marks, in document order.
        response: The answer. Every span is checked against it.
        threshold: Overlap at which a claim counts as coming from a document.

    Returns:
        ``{"stance": {index: {"stance": ..., "evidence": ...}}, "ratios": ...,
        "invalid_spans": [...], "evidence_invalid": [...],
        "conflicting_spans": [...]}``

        `stance` is in the form `context_findings` already consumes, so
        nothing downstream changes.
    """
    judgment = judgment or {}
    rejected_map = judgment.get("rejected") or judgment.get("stance") or {}

    valid_spans, invalid_spans = verify_spans(
        judgment.get("asserted_spans"), response
    )
    claims_by_index, ratios = attribute(valid_spans, marks, threshold)

    evidence_invalid: List[int] = []
    conflicting: List[str] = []
    stance: Dict[str, Dict[str, Any]] = {}

    for index, mark in enumerate(marks, 1):
        claimed, evidence = _rejected_entry(rejected_map, index)
        attributed = claims_by_index.get(index) or []
        relied = bool(attributed)

        if claimed and response is not None and not _found_in(evidence or "", response):
            # Same rule as before: a rejection the judge cannot point at is not
            # an observation.
            evidence_invalid.append(index)
            claimed = False

        if claimed and evidence is not None:
            # The conflict is one span doing duty for two DIFFERENT documents:
            # offered as proof the answer disagrees with this one while also
            # reading as a restatement of another. That span cannot be evidence
            # of both, so neither reading is kept.
            #
            # Deliberately not triggered when the span attributes back to THIS
            # document. Norwegian states a rejection by quoting what it rejects
            # — "Påstanden om at kvart format skal tildelast eit eige ISSN er
            # feil for ISSN" necessarily contains the claim it denies — and
            # treating that as a contradiction discards correct rejections.
            other, _ratios = attribute_span(evidence, marks, threshold)
            if other is not None and other != index:
                conflicting.append(evidence)
                evidence_invalid.append(index)
                claimed = False

        # relied_on outranks rejected: an answer that restates a document has
        # adopted it whatever else it says about it. rejected outranks ignored.
        if relied:
            value = "relied_on"
        elif claimed:
            value = "rejected"
        else:
            value = "ignored"

        # The evidence recorded is the span the stance actually rests on: the
        # claim that attributed for relied_on, the disagreement for rejected.
        # Downstream re-verifies it against the answer, and a relied_on with an
        # empty span would be discarded there.
        if value == "relied_on":
            span_used = attributed[0]
        elif value == "rejected":
            span_used = evidence or ""
        else:
            span_used = ""
        stance[str(index)] = {"stance": value, "evidence": span_used}

    return {
        "stance": stance,
        "ratios": ratios,
        "claims_by_index": claims_by_index,
        "valid_spans": valid_spans,
        "invalid_spans": invalid_spans,
        "evidence_invalid": sorted(set(evidence_invalid)),
        "conflicting_spans": conflicting,
    }
