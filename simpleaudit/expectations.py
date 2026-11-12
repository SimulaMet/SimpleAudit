"""Expectation objects: a scenario's ``expected_behavior`` with explicit flags.

An expectation is either a plain string or an object::

    {"id": "G1", "text": "...", "required": True, "source": "shared"}

Only ``text`` is required. A plain string means ``{"text": ..., "required": True}``.
``required`` decides whether a violation can change the derived severity
(:func:`simpleaudit.checklist.derive_severity`); a non-required item is graded
and reported but never counts. The flag is applied in code and is never shown
to the judge model, which sees the text only.

``id`` is optional. When it is missing, a stable id is generated from the
text, so every item in a result can be matched across runs and judges.
Explicit ids are recommended for shared rules and for items that may be
reworded (a generated id changes when the text does).

``source`` is ``"scenario"`` for a scenario's own items and ``"shared"`` for
rules merged into every scenario of a set (:func:`merge_shared`).

Optional items used to be marked by wording (a line starting with
``Kan nevne (ikke påkrevd)``, ``Optionally``, ``Ideally`` or ``May``). Grading
no longer reads the wording: :func:`upgrade_expectations` converts such legacy
strings into objects once, for scenario data and saved results written before
expectation objects existed.
"""

import hashlib
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from simpleaudit.spans import normalise

SOURCES = ("scenario", "shared")

#: Sentence-initial markers that made an item optional before expectation
#: objects existed. Used only by :func:`upgrade_expectations`, never by grading.
LEGACY_OPTIONAL_RE = re.compile(
    # "may " but not "may not" / "may never": those are prohibitions.
    r"^\s*(?:kan nevne \(ikke påkrevd\)|optionally\b|ideally\b|may\s(?!not\b|never\b))",
    re.IGNORECASE,
)

Expectation = Dict[str, Any]


def expectation_id(text: str) -> str:
    """A stable id for an expectation without one: a short hash of its normalised text."""
    digest = hashlib.sha1(normalise(text).encode("utf-8")).hexdigest()[:8]
    return f"e-{digest}"


def _one(entry: Any, source: str) -> Expectation:
    if isinstance(entry, str):
        text, required, given_id, given_source = entry, True, None, None
    elif isinstance(entry, Mapping):
        text = entry.get("text")
        required = entry.get("required", True)
        given_id = entry.get("id")
        given_source = entry.get("source")
        if not isinstance(required, bool):
            raise ValueError(f"expectation 'required' must be true or false, got {required!r}")
    else:
        raise ValueError(f"an expectation must be a string or an object with 'text', got {type(entry).__name__}")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("an expectation needs a non-empty 'text'")
    item_source = given_source if given_source is not None else source
    if item_source not in SOURCES:
        raise ValueError(f"expectation 'source' must be one of {SOURCES}, got {item_source!r}")
    item_id = str(given_id).strip() if given_id is not None and str(given_id).strip() else expectation_id(text)
    return {"id": item_id, "text": text, "required": required, "source": item_source}


def normalize_expectations(entries: Any, *, source: str = "scenario") -> List[Expectation]:
    """Expectation objects from strings, objects or a mix, in order.

    A bare string in place of the list is one expectation (iterating it would
    give one per character). ``None`` gives an empty list. Ids must be unique.
    """
    if entries is None:
        return []
    if isinstance(entries, (str, Mapping)):
        entries = [entries]
    items = [_one(entry, source) for entry in entries]
    seen: Dict[str, str] = {}
    for item in items:
        if item["id"] in seen:
            raise ValueError(
                f"duplicate expectation id {item['id']!r} "
                f"({seen[item['id']]!r} and {item['text']!r})"
            )
        seen[item["id"]] = item["text"]
    return items


def expectation_texts(expectations: Iterable[Mapping[str, Any]]) -> List[str]:
    """The texts, in order: what the judge model is shown."""
    return [item["text"] for item in expectations]


def merge_shared(
    expectations: Any,
    shared: Any,
) -> List[Expectation]:
    """A scenario's expectations followed by the set's shared rules.

    Shared rules get ``source="shared"`` unless they say otherwise. A shared
    rule whose id the scenario already uses is skipped, so a scenario can
    override a shared rule by defining an item with the same id.
    """
    own = normalize_expectations(expectations)
    extra = normalize_expectations(shared, source="shared")
    taken = {item["id"] for item in own}
    return own + [item for item in extra if item["id"] not in taken]


def apply_shared_expectations(
    scenarios: Sequence[Mapping[str, Any]],
    shared: Any,
) -> List[Dict[str, Any]]:
    """Copies of *scenarios* with *shared* merged into each one's ``expected_behavior``."""
    return [
        {**scenario, "expected_behavior": merge_shared(scenario.get("expected_behavior"), shared)}
        for scenario in scenarios
    ]


def is_legacy_optional(text: Optional[str]) -> bool:
    """True when *text* starts with a pre-object optional marker."""
    return bool(text) and LEGACY_OPTIONAL_RE.match(str(text)) is not None


def upgrade_expectations(entries: Any, *, source: str = "scenario") -> List[Expectation]:
    """Convert legacy data: plain strings with an optional marker become ``required: False``.

    The text is kept as written, so a re-grade shows the judge exactly what it
    saw before and the grade does not move. Objects are normalised unchanged.
    Use this only for scenario data or saved results written before expectation
    objects existed; new data should set ``required`` explicitly.
    """
    if entries is None:
        return []
    if isinstance(entries, (str, Mapping)):
        entries = [entries]
    upgraded: List[Any] = []
    for entry in entries:
        if isinstance(entry, str) and is_legacy_optional(entry):
            upgraded.append({"text": entry, "required": False})
        else:
            upgraded.append(entry)
    return normalize_expectations(upgraded, source=source)


def stored_expectations(
    objects: Any,
    strings: Any,
    *,
    legacy_optional_markers: bool = True,
) -> List[Expectation]:
    """Expectation objects for a saved result: its objects if it has them, else its strings.

    Strings come from data written before expectation objects existed, so by
    default they are read with the old optional markers (see
    :func:`upgrade_expectations`).
    """
    if objects:
        return normalize_expectations(objects)
    return (upgrade_expectations if legacy_optional_markers else normalize_expectations)(strings)


def objects_worth_storing(raw: Any, expectations: List[Expectation]) -> Optional[List[Expectation]]:
    """*expectations* when they carry more than plain required strings would, else None.

    Results of scenarios written as plain strings keep their old shape; the
    objects are stored when the scenario used objects, or when an item does
    not count or is a shared rule.
    """
    entries = [raw] if isinstance(raw, (str, Mapping)) else list(raw or [])
    if any(isinstance(entry, Mapping) for entry in entries) or any(
        not item["required"] or item["source"] != "scenario" for item in expectations
    ):
        return expectations
    return None


def shared_rule_summary(results: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    """Per shared rule: how often it was met, violated, not applicable or missing.

    *results* are :class:`~simpleaudit.results.AuditResult` objects (or dicts
    with a ``judgment``) graded by a checklist judge. Rules are keyed by id.
    """
    summary: Dict[str, Dict[str, Any]] = {}
    for result in results:
        judgment = result.get("judgment") if isinstance(result, Mapping) else getattr(result, "judgment", None)
        if not isinstance(judgment, Mapping):
            continue
        for item in judgment.get("checklist") or []:
            if not isinstance(item, Mapping) or item.get("source") != "shared":
                continue
            row = summary.setdefault(
                str(item.get("id")),
                {"text": item.get("expectation"), "required": item.get("required", True),
                 "met": 0, "violated": 0, "not_applicable": 0, "missing": 0},
            )
            status = item.get("status")
            row[status if status in ("met", "violated", "not_applicable") else "missing"] += 1
    return summary
