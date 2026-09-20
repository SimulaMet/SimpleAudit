"""Tests for document marks — parsing, and the render/judge split.

Two properties carry the weight here. A mark the author wrote must survive
parsing, and a mark the author *mis-wrote* must raise rather than vanish: a
dropped mark leaves the document unmarked, which every derivation reads as
"unknown", so a typo would silently retire the case the scenario was written to
test. And the target-facing render must carry document text only — the marks
are the answer key, and they go to the judge alone.
"""

from datetime import date

import pytest

from simpleaudit.context_marks import (
    AUTHORITY_LEVELS,
    MARK_KEYS,
    DocumentMark,
    mark_table,
    parse_as_of,
    parse_document,
    parse_documents,
    render_documents,
)


# ---------------------------------------------------------------------------
# parse_document
# ---------------------------------------------------------------------------

def test_bare_string_is_a_document_with_every_mark_none():
    mark = parse_document("16- og 17-åringer betaler ikke egenandel.")

    assert mark.text == "16- og 17-åringer betaler ikke egenandel."
    for key in MARK_KEYS:
        assert getattr(mark, key) is None, f"{key} should be unknown, not assumed"


def test_full_dict_parses_every_mark():
    mark = parse_document({
        "text": "Aldersfritaket er hevet til under 18 år.",
        "relevant": True,
        "true": True,
        "valid_from": "2026-08-01",
        "valid_until": None,
        "authority": "guidance",
        "source": "helfo/HF-01",
        "decisive": True,
    })

    assert mark.relevant is True
    assert mark.true is True
    assert mark.valid_from == date(2026, 8, 1)
    assert mark.valid_until is None
    assert mark.authority == "guidance"
    assert mark.source == "helfo/HF-01"
    assert mark.decisive is True


def test_unknown_key_raises():
    # The whole reason this is an error: `relevent` would otherwise leave the
    # document unmarked, and unmarked is read as unknown, not as false.
    with pytest.raises(ValueError, match="relevent"):
        parse_document({"text": "chunk", "relevent": False})


def test_unknown_authority_raises():
    with pytest.raises(ValueError, match="circular"):
        parse_document({"text": "chunk", "authority": "circular"})


def test_every_declared_authority_level_is_accepted():
    for level in AUTHORITY_LEVELS:
        assert parse_document({"text": "chunk", "authority": level}).authority == level


def test_missing_text_raises():
    with pytest.raises(ValueError, match="text"):
        parse_document({"relevant": True})


def test_non_iso_date_raises():
    with pytest.raises(ValueError, match="valid_from"):
        parse_document({"text": "chunk", "valid_from": "1. august 2026"})


def test_non_boolean_mark_raises():
    # "false" is a truthy string; accepting it would invert the author's mark.
    with pytest.raises(ValueError, match="relevant"):
        parse_document({"text": "chunk", "relevant": "false"})


def test_date_objects_pass_through():
    # YAML hands back a real date; a scenario should read the same either way.
    mark = parse_document({"text": "chunk", "valid_until": date(2026, 8, 1)})
    assert mark.valid_until == date(2026, 8, 1)


def test_document_mark_is_frozen():
    mark = parse_document("chunk")
    with pytest.raises(Exception):
        mark.relevant = True


# ---------------------------------------------------------------------------
# parse_documents / parse_as_of
# ---------------------------------------------------------------------------

def test_parse_documents_mixes_strings_and_dicts():
    marks = parse_documents(["bare", {"text": "marked", "relevant": True}])

    assert [m.text for m in marks] == ["bare", "marked"]
    assert marks[0].relevant is None
    assert marks[1].relevant is True


def test_parse_documents_handles_absent_list():
    assert parse_documents(None) == []
    assert parse_documents([]) == []


def test_parse_documents_passes_parsed_marks_through():
    already = DocumentMark(text="chunk", relevant=True)
    assert parse_documents([already]) == [already]


def test_parse_as_of_accepts_string_and_date():
    assert parse_as_of({"as_of": "2026-09-01"}) == date(2026, 9, 1)
    assert parse_as_of({"as_of": date(2026, 9, 1)}) == date(2026, 9, 1)


def test_parse_as_of_absent_is_none():
    # Not a fallback to today: without as_of there is no date to test against.
    assert parse_as_of({"name": "scenario without a date"}) is None


# ---------------------------------------------------------------------------
# render_documents — the target-facing render
# ---------------------------------------------------------------------------

def test_render_documents_is_one_indexed_text_only():
    marks = parse_documents(["first chunk", {"text": "second chunk"}])

    assert render_documents(marks) == (
        "\n--- DOCUMENT 1 ---\nfirst chunk"
        "\n--- DOCUMENT 2 ---\nsecond chunk"
    )


def test_render_documents_empty_set_is_empty_string():
    assert render_documents([]) == ""


def test_render_documents_leaks_no_mark():
    # Every mark set, so the leak surface is as wide as it gets. `true` is True
    # rather than False only because `decisive` is set here too, and decisive
    # means relevant and true — the marks have to be internally consistent for
    # the parser to accept them at all.
    marks = parse_documents([{
        "text": "Kvotene gjeld for alle som reiser til Noreg.",
        "relevant": True,
        "true": True,
        "valid_from": "2026-08-01",
        "valid_until": "2027-01-01",
        "authority": "guidance",
        "source": "toll/TOLL-07",
        "decisive": True,
    }])

    rendered = render_documents(marks)

    for key in MARK_KEYS:
        assert key not in rendered
    for value in ("guidance", "toll/TOLL-07", "2026-08-01", "2027-01-01"):
        assert value not in rendered


# ---------------------------------------------------------------------------
# mark_table — the judge-facing render
# ---------------------------------------------------------------------------

def test_mark_table_reports_the_six_judge_columns():
    marks = parse_documents([{
        "text": "Under 16 år er fritatt for egenandel.",
        "relevant": True,
        "true": True,
        "valid_from": "2025-01-01",
        "valid_until": "2026-08-01",
        "authority": "guidance",
        "source": "helfo/HF-01",
    }])

    table = mark_table(marks, date(2026, 9, 1))
    header, _, row = table.splitlines()

    for column in ("index", "relevant", "true", "current", "authority", "source"):
        assert column in header
    assert row.split(" | ")[0].strip() == "1"
    assert "guidance" in row
    assert "helfo/HF-01" in row
    # Superseded on 2026-09-01: true as written, and not the answer today.
    assert row.split(" | ")[3].strip() == "false"


def test_mark_table_spells_out_unknown_marks():
    table = mark_table(parse_documents(["bare chunk"]), date(2026, 9, 1))
    row = table.splitlines()[2]

    # A blank cell is the one thing an LLM judge might read as "no".
    assert row.count("unknown") == 5


def test_mark_table_without_documents():
    assert mark_table([], date(2026, 9, 1)) == "(no documents)"


# ---------------------------------------------------------------------------
# Authoring slips that used to parse "successfully" into nonsense
# ---------------------------------------------------------------------------


class TestDocumentsMustBeAList:
    """A str and a Mapping are both iterable, which is the trap.

    Neither slip raised before: a single document written without its list
    iterated over the mark keys and produced one document per key, and a bare
    string produced one per character. The real text never reached the target
    and every derivation collapsed to None, so the scenario passed as a silent
    no-op — the same failure the unknown-key check exists to prevent.
    """

    def test_a_single_document_dict_is_rejected(self):
        with pytest.raises(ValueError, match="must be a list"):
            parse_documents({"text": "no enclosing list", "relevant": True})

    def test_a_bare_string_is_rejected(self):
        with pytest.raises(ValueError, match="must be a list"):
            parse_documents("no enclosing list")

    def test_render_documents_rejects_them_too(self):
        # render_documents is the path that reaches the target, so the guard
        # has to hold there and not only in the parser the runner calls first.
        with pytest.raises(ValueError, match="must be a list"):
            render_documents({"text": "no enclosing list"})

    def test_a_proper_list_still_works(self):
        marks = parse_documents([{"text": "A", "relevant": True}, "B"])
        assert [m.text for m in marks] == ["A", "B"]


class TestDecisiveMustNotContradictItself:
    """`decisive` is defined as relevant AND true AND load-bearing.

    `recall_complete` reads `decisive` alone, so a document marked decisive
    while marked irrelevant or false would make it report the load-bearing
    document present on the strength of one the author called irrelevant.
    """

    def test_decisive_with_relevant_false_is_rejected(self):
        with pytest.raises(ValueError, match="decisive"):
            parse_document({"text": "x", "relevant": False, "decisive": True})

    def test_decisive_with_true_false_is_rejected(self):
        with pytest.raises(ValueError, match="decisive"):
            parse_document({"text": "x", "true": False, "decisive": True})

    def test_decisive_with_unmarked_relevance_is_allowed(self):
        # Unknown is not a contradiction — only an explicit False is.
        assert parse_document({"text": "x", "decisive": True}).decisive is True
