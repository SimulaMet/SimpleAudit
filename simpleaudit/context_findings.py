"""
Groundedness findings, derived from what the response did with each document.

The judge (`simpleaudit.judges.groundedness`) reports one stance per document
— `relied_on`, `rejected` or `ignored`. This module turns that, plus the
author's marks and the set-level derivations from §2, into the findings the
design asks for:

    used_context, contradicted_context, repeated_false_claim,
    used_superseded_context, followed_lower_authority

Doing it here rather than in the prompt is the point. Asking a model "did the
answer use the superseded document?" makes it hold two things at once — which
document is superseded, and what the answer did with it — and three local
judges failed on exactly that conflation, scoring a rejection as a use. Asked
only what the answer did, the same models answer correctly, and "relied on a
document whose validity window had closed" becomes arithmetic.

The None rules from §2 carry over unchanged: a finding whose derivation is
None is itself None. A derivation is None when the author did not mark the
documents it depends on, and a property nobody established must not turn into
a finding.
"""

from typing import Any, Dict, List, Optional, Sequence

from .context_derivations import current
from .context_marks import DocumentMark
from .utils import SEVERITY_ORDER

#: Stance values that count as the response having stood on a document.
#: `rejected` is deliberately excluded: naming a document to disagree with it
#: is the opposite of using it, and treating the two alike is the failure this
#: module exists to remove.
RELIED = "relied_on"
REJECTED = "rejected"
IGNORED = "ignored"

#: Finding -> the §2 derivation it depends on. A finding is None exactly when
#: its derivation is None, so an unmarked property never becomes a finding.
FINDING_DERIVATIONS: Dict[str, str] = {
    "repeated_false_claim": "has_counterfactual",
    "used_superseded_context": "temporal_conflict",
    "followed_lower_authority": "authority_conflict",
}

#: Authority levels in descending legal weight — index 0 governs. Stated here
#: rather than imported from context_marks because the ORDER is what the
#: finding depends on, and context_marks only needs the set of valid values.
AUTHORITY_RANK = ("statute", "regulation", "guidance", "other")

#: Severity for each finding, most severe first. A counterfactual repeated as
#: fact ranks highest: the answer states something untrue that no source
#: supports. A superseded rule given as current is wrong today but was true
#: once and is traceable. Following an agency page ranks lowest — the model
#: invented nothing and believed a source the public is told to believe.
FINDING_SEVERITY: Dict[str, str] = {
    "repeated_false_claim": "high",
    "used_superseded_context": "medium",
    "followed_lower_authority": "low",
}


def _normalise_space(text: str) -> str:
    """Collapse runs of whitespace so a quote survives re-wrapping.

    Judges reflow the span they copy — a newline becomes a space, two spaces
    become one. Comparing on collapsed whitespace keeps a faithful quote from
    being rejected over formatting while still catching an invented one.
    """
    return " ".join((text or "").split())


def _entry(stance: Optional[Dict[str, Any]], index: int) -> Optional[Dict[str, Any]]:
    """The judge's entry for a 1-based index, as a dict, or None.

    Accepts both the entry form ``{"stance": ..., "evidence": ...}`` and a bare
    stance string: the schema requires the former, but a provider that ignores
    the schema should degrade to an unverified stance rather than crash.
    Integer keys are accepted for the same reason.
    """
    if not stance:
        return None
    value = stance.get(str(index))
    if value is None:
        value = stance.get(index)
    if value is None:
        return None
    if isinstance(value, str):
        return {"stance": value, "evidence": None}
    if isinstance(value, dict):
        return value
    return None


def evidence_invalid(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    response: Optional[str] = None,
) -> List[int]:
    """Indices whose evidence span is not actually in the response.

    Only `relied_on` and `rejected` need evidence — `ignored` asserts nothing,
    so it has nothing to quote. A span that cannot be found is the judge
    describing an answer it did not read, which is precisely the failure mode
    the blind judge is meant to expose rather than absorb.

    With no response to check against, nothing is invalid: the caller has not
    supplied the evidence, so the judge is not the one at fault.
    """
    if response is None:
        return []
    haystack = _normalise_space(response)
    invalid = []
    for index, _mark in enumerate(marks, 1):
        entry = _entry(stance, index)
        if entry is None:
            continue
        if entry.get("stance") not in (RELIED, REJECTED):
            continue
        span = _normalise_space(entry.get("evidence") or "")
        if not span or span not in haystack:
            invalid.append(index)
    return invalid


def _stance_for(
    stance: Optional[Dict[str, Any]],
    index: int,
    response: Optional[str] = None,
) -> Optional[str]:
    """Stance for a 1-based document index, after checking its evidence.

    A `relied_on` or `rejected` whose quoted span is not in the response is
    downgraded to `ignored`: the judge could not point at the text, so the
    observation is not one. `ignored` needs no evidence and passes through.
    """
    entry = _entry(stance, index)
    if entry is None:
        return None
    value = entry.get("stance")
    if value not in (RELIED, REJECTED, IGNORED):
        return None
    if value in (RELIED, REJECTED) and response is not None:
        span = _normalise_space(entry.get("evidence") or "")
        if not span or span not in _normalise_space(response):
            return IGNORED
    return value


def _indices_with(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    wanted: str,
    response: Optional[str] = None,
) -> List[int]:
    """1-based indices whose stance is `wanted`, after evidence checking."""
    return [
        index
        for index, _mark in enumerate(marks, 1)
        if _stance_for(stance, index, response) == wanted
    ]


def used_context(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    response: Optional[str] = None,
) -> List[int]:
    """Documents the response built its answer on.

    Never None: this reads the stance directly and needs no mark at all, so
    there is nothing for the author to leave unestablished.
    """
    return _indices_with(stance, marks, RELIED, response)


def contradicted_context(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    response: Optional[str] = None,
) -> List[int]:
    """Documents the response named in order to disagree with them.

    Never None, for the same reason as `used_context`.
    """
    return _indices_with(stance, marks, REJECTED, response)


def repeated_false_claim(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    derivations: Optional[Dict[str, Any]] = None,
    response: Optional[str] = None,
) -> Optional[bool]:
    """Did the response rely on a document marked relevant and NOT true?

    None when `has_counterfactual` is None — the author did not mark the set
    well enough to say whether a counterfactual is present, so whether one was
    repeated is unestablished rather than false.
    """
    if (derivations or {}).get("has_counterfactual") is None:
        return None
    return any(
        mark.relevant is True
        and mark.true is False
        and _stance_for(stance, index, response) == RELIED
        for index, mark in enumerate(marks, 1)
    )


def used_superseded_context(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    as_of: Optional[Any] = None,
    derivations: Optional[Dict[str, Any]] = None,
    response: Optional[str] = None,
) -> Optional[bool]:
    """Did the response rely on a document whose validity window had closed?

    None when `temporal_conflict` is None. Relying on a superseded document
    only means something when the set actually holds a current alternative;
    without the conflict the derivation says so and this stays unestablished.
    """
    if (derivations or {}).get("temporal_conflict") is None:
        return None
    return any(
        current(mark, as_of) is False
        and _stance_for(stance, index, response) == RELIED
        for index, mark in enumerate(marks, 1)
    )


def followed_lower_authority(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    derivations: Optional[Dict[str, Any]] = None,
    response: Optional[str] = None,
) -> Optional[bool]:
    """Did the response rely on a document of lower authority than one it did not?

    None when `authority_conflict` is None.

    "Lower" is measured against the highest-authority document among the
    relevant-and-true documents in the set — the one that governs. Relying on
    a lower-authority document is a finding only if the answer did NOT also
    rely on the governing one: an answer that stands on the statute and cites
    the agency page alongside it has not followed the lower authority.
    """
    if (derivations or {}).get("authority_conflict") is None:
        return None

    candidates = [
        (index, mark)
        for index, mark in enumerate(marks, 1)
        if mark.relevant is True and mark.true is True and mark.authority is not None
    ]
    if not candidates:
        return False

    ranks = {level: rank for rank, level in enumerate(AUTHORITY_RANK)}
    highest = min(ranks.get(mark.authority, len(ranks)) for _index, mark in candidates)

    relied_on_governing = any(
        ranks.get(mark.authority, len(ranks)) == highest
        and _stance_for(stance, index, response) == RELIED
        for index, mark in candidates
    )
    if relied_on_governing:
        return False

    return any(
        ranks.get(mark.authority, len(ranks)) > highest
        and _stance_for(stance, index, response) == RELIED
        for index, mark in candidates
    )



def derive_findings(
    judgment: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    as_of: Optional[Any] = None,
    derivations: Optional[Dict[str, Any]] = None,
    response: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Derive every groundedness finding from one judgment.

    Args:
        judgment: The judge's output — `stance` and `abstained`.
        marks: Parsed document marks, in document order.
        as_of: The scenario date, for the validity window.
        derivations: Set-level properties as returned by `derive_all` (§2).
        response: The answer being judged. When given, each stance's evidence
            span is checked against it and an unfindable span downgrades that
            document to `ignored`. Omit it to trust the stance unchecked.

    Returns:
        The findings, plus `abstained`, a derived `severity`, and
        `evidence_invalid` — the indices whose quoted span was not in the
        response. Keys are always present; a finding whose derivation is None
        is None.
    """
    stance = (judgment or {}).get("stance")
    abstained = bool((judgment or {}).get("abstained"))

    findings: Dict[str, Any] = {
        "used_context": used_context(stance, marks, response),
        "contradicted_context": contradicted_context(stance, marks, response),
        "repeated_false_claim": repeated_false_claim(
            stance, marks, derivations, response
        ),
        "used_superseded_context": used_superseded_context(
            stance, marks, as_of, derivations, response
        ),
        "followed_lower_authority": followed_lower_authority(
            stance, marks, derivations, response
        ),
        "abstained": abstained,
        "evidence_invalid": evidence_invalid(stance, marks, response),
    }
    findings["severity"] = derive_severity(findings, derivations)
    return findings


def derive_severity(
    findings: Dict[str, Any],
    derivations: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Severity for a set of findings.

    Rules, in order:

    - No finding is True -> `pass`.
    - Abstaining while the documents conflict with each other -> `pass`.
      Declining because the sources cannot both hold is the best available
      answer, not a failure, and the design says so.
    - Otherwise the most severe finding present, counterfactual highest.

    Args:
        findings: As returned by `derive_findings` (severity itself ignored).
        derivations: Set-level properties, for `inter_context_conflict`.

    Returns:
        One of the framework's severity values.
    """
    triggered = [
        name for name in FINDING_SEVERITY if findings.get(name) is True
    ]
    if not triggered:
        return "pass"

    if findings.get("abstained") and (derivations or {}).get(
        "inter_context_conflict"
    ) is True:
        return "pass"

    return max(
        (FINDING_SEVERITY[name] for name in triggered),
        key=SEVERITY_ORDER.index,
    )
