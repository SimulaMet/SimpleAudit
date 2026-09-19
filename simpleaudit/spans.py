"""
Locate quoted spans in stored transcripts.

A judge that must prove an observation quotes a span of the transcript. The
quote is copied by a model from a rendered transcript, so it arrives with the
usual drift: different casing, curly quotes folded to straight ones, markdown
emphasis kept or dropped, a non-breaking space in ``50 000`` turned into a
plain one. Verification therefore compares *normalised* forms: the span and the
candidate text are both reduced to lowercase words separated by single spaces,
with every non-word character (punctuation, markdown symbols, emoji) treated as
a separator. What survives is the wording; what is dropped is formatting.

The functions here import nothing from the rest of the package so that any
judge which returns spans can use them.
"""

import re
import unicodedata
from typing import Any, List, Mapping, Optional, Sequence

_FOLD = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "«": '"',
        "»": '"',
        "–": "-",
        "—": "-",
        "−": "-",
    }
)

#: Anything that is not a word character or whitespace, plus the underscore,
#: which ``\w`` keeps but markdown uses for emphasis.
_SEPARATOR = re.compile(r"[^\w\s]|_", re.UNICODE)

#: Default minimum length (in normalised characters) for a span to count as
#: evidence. Shorter strings ("ja", "nei", "ok") occur in almost any turn.
MIN_SPAN_CHARS = 8


def normalise(text: Optional[str]) -> str:
    """Reduce *text* to lowercase words separated by single spaces.

    NFKC folds compatibility characters (non-breaking space, full-width
    digits); typographic quotes and dashes are folded to ASCII; every
    remaining non-word character becomes a separator; whitespace collapses.
    Letters outside ASCII (å, ø, æ) are word characters and survive.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKC", str(text)).translate(_FOLD).casefold()
    return " ".join(_SEPARATOR.sub(" ", folded).split())


def find_span(
    span: Optional[str],
    texts: Sequence[str],
    *,
    min_chars: int = MIN_SPAN_CHARS,
) -> Optional[int]:
    """Index of the first text that contains *span*, or None.

    Both sides are normalised. A span shorter than *min_chars* after
    normalisation is never matched: it is too short to be evidence. A span is
    matched against one text at a time, so a quote stitched together from two
    turns does not verify.
    """
    needle = normalise(span)
    if len(needle) < min_chars:
        return None
    for index, text in enumerate(texts):
        if needle in normalise(text):
            return index
    return None


def assistant_texts(conversation: Sequence[Mapping[str, Any]]) -> List[str]:
    """The string content of every assistant turn, in order.

    Non-string content (structured blocks) yields an empty string for that
    turn so indices stay aligned with the conversation's assistant turns.
    """
    out: List[str] = []
    for message in conversation:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        out.append(content if isinstance(content, str) else "")
    return out
