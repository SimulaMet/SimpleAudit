"""
Document marks for context-grounding scenarios.

A context-grounding scenario hands the target a set of retrieved documents and
asks a question about them. Each document may carry *marks* — the author's
ground truth about that document: is it relevant, is it true as written, when
was it valid, what kind of authority is it, where did it come from.

Two rules run through this module and are the reason it exists as its own file:

1. **Unmarked means unknown, never assumed.** There are no defaults. A document
   without ``valid_from`` is not assumed current, and an unmarked document is
   not assumed relevant. That is why a mis-spelled mark key raises instead of
   being ignored — a silently dropped ``relevent: false`` would leave the
   document unmarked, which the derivations then read as "unknown", and the
   scenario would quietly stop testing what its author wrote.

2. **Marks never reach the target.** :func:`render_documents` emits document
   text and nothing else. The mark table produced by :func:`mark_table` is for
   the judge, which grades a response it did not produce. A target that could
   see ``relevant: false`` would be answering a different, much easier question.

Documents are written inline in the scenario, either as a bare string (every
mark None) or as a dict::

    documents:
      - "plain text chunk"
      - text: "16- og 17-åringer betaler ikke lenger egenandel."
        relevant: true
        true: true
        valid_from: 2026-08-01
        authority: guidance
        source: "helfo/HF-01"
    as_of: 2026-09-01
"""

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Sequence, Union

#: Authority levels a document may be tagged with, strongest first. The order
#: is documentation, not a comparison ladder: `authority_conflict` asks whether
#: two levels *differ*, not which one wins — that call belongs to the judge.
AUTHORITY_LEVELS = ("statute", "regulation", "guidance", "other")

#: Every mark key a document dict may carry beside its `text`. Anything else is
#: a typo, and typos raise (see the module docstring).
MARK_KEYS = (
    "relevant",
    "true",
    "valid_from",
    "valid_until",
    "authority",
    "source",
    "decisive",
)

_BOOL_KEYS = ("relevant", "true", "decisive")
_DATE_KEYS = ("valid_from", "valid_until")


@dataclass(frozen=True)
class DocumentMark:
    """One retrieved document plus the author's ground truth about it.

    Frozen because a mark is scenario ground truth: nothing downstream — the
    auditor, the judge, the derivations — has any business rewriting it.

    Attributes
    ----------
    text : str
        The document body. The only field that ever reaches the target.
    relevant : bool or None
        Does this document bear on the question asked?
    true : bool or None
        Is it true *as written*, on its own date? A superseded document can be
        true and still be the wrong thing to answer from.
    valid_from, valid_until : date or None
        Half-open validity window. None on either end means open-ended, not
        unknown-and-therefore-invalid.
    authority : str or None
        One of :data:`AUTHORITY_LEVELS`.
    source : str or None
        Free-form provenance — a register row id or a statute section.
    decisive : bool or None
        Load-bearing: relevant, true, and required for a correct answer.
    """

    text: str
    relevant: Optional[bool] = None
    true: Optional[bool] = None
    valid_from: Optional[date] = None
    valid_until: Optional[date] = None
    authority: Optional[str] = None
    source: Optional[str] = None
    decisive: Optional[bool] = None


def _parse_date(value: Any, field: str) -> Optional[date]:
    """Coerce a scenario-supplied date to ``datetime.date``.

    YAML hands back a real `date`, JSON and hand-written Python dicts hand back
    an ISO string; both are accepted so a scenario reads the same either way.
    """
    if value is None:
        return None
    # datetime is a date subclass, so test it first or the isinstance below
    # would let a timestamp through untouched and break date comparisons.
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"Document mark '{field}' is not an ISO date: {value!r}"
            ) from exc
    raise ValueError(
        f"Document mark '{field}' must be an ISO date string or a date, "
        f"got {type(value).__name__}"
    )


def _parse_bool(value: Any, field: str) -> Optional[bool]:
    """Accept only real booleans for a boolean mark.

    Truthiness is not good enough here: the string ``"false"`` is truthy, and a
    document silently marked relevant when the author wrote the opposite is the
    same failure the unknown-key check exists to prevent.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    raise ValueError(
        f"Document mark '{field}' must be true, false, or absent, "
        f"got {value!r}"
    )


def parse_document(doc: Union[str, Dict[str, Any]]) -> DocumentMark:
    """
    Build a :class:`DocumentMark` from a scenario's document entry.

    A bare string is a document with every mark None — unknown, not assumed.
    A dict must carry ``text`` and may carry any of :data:`MARK_KEYS`.

    Parameters
    ----------
    doc : str or dict
        The scenario entry.

    Returns
    -------
    DocumentMark

    Raises
    ------
    ValueError
        On an unknown key, a missing or non-string ``text``, a date that is not
        ISO, a non-boolean boolean mark, or an ``authority`` outside
        :data:`AUTHORITY_LEVELS`.
    """
    if isinstance(doc, str):
        return DocumentMark(text=doc)

    if not isinstance(doc, dict):
        raise ValueError(
            f"A document must be a string or a dict, got {type(doc).__name__}"
        )

    unknown = [key for key in doc if key != "text" and key not in MARK_KEYS]
    if unknown:
        # Loud on purpose: a dropped mark reads downstream as "unmarked", and
        # unmarked propagates None through every derivation that needed it.
        raise ValueError(
            f"Unknown document mark key(s): {', '.join(sorted(unknown))}. "
            f"Known keys: text, {', '.join(MARK_KEYS)}"
        )

    text = doc.get("text")
    if not isinstance(text, str):
        raise ValueError("A document dict must carry a string 'text' field")

    authority = doc.get("authority")
    if authority is not None and authority not in AUTHORITY_LEVELS:
        raise ValueError(
            f"Unknown authority {authority!r}. "
            f"Known levels: {', '.join(AUTHORITY_LEVELS)}"
        )

    source = doc.get("source")
    if source is not None and not isinstance(source, str):
        raise ValueError(f"Document mark 'source' must be a string, got {source!r}")

    mark = DocumentMark(
        text=text,
        authority=authority,
        source=source,
        **{key: _parse_bool(doc.get(key), key) for key in _BOOL_KEYS},
        **{key: _parse_date(doc.get(key), key) for key in _DATE_KEYS},
    )

    # The design defines decisive as relevant AND true AND load-bearing, so a
    # document marked decisive while marked irrelevant or false contradicts
    # itself. Left unchecked it is worse than a no-op: `recall_complete` reads
    # `decisive` on its own and would report the load-bearing document present
    # on the strength of one the same author called irrelevant. An unmarked
    # relevant/true is fine — unknown is not a contradiction, only False is.
    if mark.decisive and (mark.relevant is False or mark.true is False):
        raise ValueError(
            "A document marked decisive cannot also be marked relevant=False "
            "or true=False: decisive means relevant and true and load-bearing."
        )

    return mark


def parse_documents(
    docs: Optional[Sequence[Union[str, Dict[str, Any], DocumentMark]]],
) -> List[DocumentMark]:
    """
    Parse a scenario's whole ``documents`` list.

    A missing or empty list yields ``[]`` — a scenario without documents is a
    plain scenario, not an error. Entries that are already
    :class:`DocumentMark` pass through, so callers can parse once and hand the
    result on without a second round trip through the validator.
    """
    if not docs:
        return []
    # A str or a Mapping is iterable, so without this the two most likely
    # authoring slips both parse "successfully" into nonsense: a single
    # document written without its enclosing list iterates over the mark KEYS
    # and yields one bogus document per key, and a bare string yields one
    # document per character. Neither raises, the real text never reaches the
    # target, and every derivation collapses to None — so the judge is asked
    # nothing and the scenario passes as a silent no-op. That is the same
    # failure the unknown-key check guards against, entering by another door.
    if isinstance(docs, (str, bytes, Mapping)):
        raise ValueError(
            f"'documents' must be a list of documents, got {type(docs).__name__}. "
            "A single document still needs its enclosing list: documents=[{...}]."
        )
    return [
        doc if isinstance(doc, DocumentMark) else parse_document(doc)
        for doc in docs
    ]


def _as_marks(
    marks: Sequence[Union[DocumentMark, str, Dict[str, Any]]],
) -> List[DocumentMark]:
    """Accept parsed marks or raw scenario entries, return parsed marks."""
    return parse_documents(marks)


def render_documents(
    marks: Sequence[Union[DocumentMark, str, Dict[str, Any]]],
) -> str:
    """
    Render documents for the target as numbered text blocks.

    Only ``text`` is rendered. No mark key and no mark value ever appears here
    — this is the render that reaches the model under audit, and a target that
    could read the marks would be answering an easier question than the one the
    scenario poses.

    Blocks are 1-indexed so the numbering matches the judge's mark table and
    the ``used_context`` / ``contradicted_context`` indices it reports.

    Returns
    -------
    str
        ``"\\n--- DOCUMENT 1 ---\\n<text>\\n--- DOCUMENT 2 ---\\n<text>"``,
        or ``""`` for no documents.
    """
    return "".join(
        f"\n--- DOCUMENT {index} ---\n{mark.text}"
        for index, mark in enumerate(_as_marks(marks), 1)
    )
