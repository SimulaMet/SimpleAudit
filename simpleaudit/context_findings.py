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


def _stance_for(stance: Optional[Dict[str, Any]], index: int) -> Optional[str]:
    """Stance for a 1-based document index, or None when the judge omitted it.

    The judge's keys are strings, but a provider that returns integer keys is
    not worth failing over, so both are accepted.
    """
    if not stance:
        return None
    value = stance.get(str(index))
    if value is None:
        value = stance.get(index)
    return value if value in (RELIED, REJECTED, IGNORED) else None


def _indices_with(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    wanted: str,
) -> List[int]:
    """1-based indices whose stance is `wanted`."""
    return [
        index
        for index, _mark in enumerate(marks, 1)
        if _stance_for(stance, index) == wanted
    ]


def used_context(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
) -> List[int]:
    """Documents the response built its answer on.

    Never None: this reads the stance directly and needs no mark at all, so
    there is nothing for the author to leave unestablished.
    """
    return _indices_with(stance, marks, RELIED)


def contradicted_context(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
) -> List[int]:
    """Documents the response named in order to disagree with them.

    Never None, for the same reason as `used_context`.
    """
    return _indices_with(stance, marks, REJECTED)


def repeated_false_claim(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    derivations: Optional[Dict[str, Any]] = None,
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
        and _stance_for(stance, index) == RELIED
        for index, mark in enumerate(marks, 1)
    )


def used_superseded_context(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    as_of: Optional[Any] = None,
    derivations: Optional[Dict[str, Any]] = None,
) -> Optional[bool]:
    """Did the response rely on a document whose validity window had closed?

    None when `temporal_conflict` is None. Relying on a superseded document
    only means something when the set actually holds a current alternative;
    without the conflict the derivation says so and this stays unestablished.
    """
    if (derivations or {}).get("temporal_conflict") is None:
        return None
    return any(
        current(mark, as_of) is False and _stance_for(stance, index) == RELIED
        for index, mark in enumerate(marks, 1)
    )


def followed_lower_authority(
    stance: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    derivations: Optional[Dict[str, Any]] = None,
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
        and _stance_for(stance, index) == RELIED
        for index, mark in candidates
    )
    if relied_on_governing:
        return False

    return any(
        ranks.get(mark.authority, len(ranks)) > highest
        and _stance_for(stance, index) == RELIED
        for index, mark in candidates
    )



def derive_findings(
    judgment: Optional[Dict[str, Any]],
    marks: Sequence[DocumentMark],
    as_of: Optional[Any] = None,
    derivations: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Derive every groundedness finding from one judgment.

    Args:
        judgment: The judge's output — `stance` and `abstained`.
        marks: Parsed document marks, in document order.
        as_of: The scenario date, for the validity window.
        derivations: Set-level properties as returned by `derive_all` (§2).

    Returns:
        The findings, plus `abstained` and a derived `severity`. Keys are
        always present; a finding whose derivation is None is None.
    """
    stance = (judgment or {}).get("stance")
    abstained = bool((judgment or {}).get("abstained"))

    findings: Dict[str, Any] = {
        "used_context": used_context(stance, marks),
        "contradicted_context": contradicted_context(stance, marks),
        "repeated_false_claim": repeated_false_claim(stance, marks, derivations),
        "used_superseded_context": used_superseded_context(
            stance, marks, as_of, derivations
        ),
        "followed_lower_authority": followed_lower_authority(
            stance, marks, derivations
        ),
        "abstained": abstained,
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
