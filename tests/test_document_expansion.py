"""Tests for the scenario `documents` field.

A context-grounding scenario hands the target a set of retrieved documents and
asks a question about them. Each document may carry *marks* — the author's
ground truth about whether it is relevant, true as written, still valid, and
what kind of authority it is. Those marks are the answer key. They belong to
the judge, which grades a response it did not produce; a target that could read
them would be told which document to trust instead of having to work it out,
which is the one thing the scenario exists to measure.

So `documents` follows the `file_uri` pattern in `_expand_files`: the key sits
beside `content`, is expanded into text blocks only on the way to a provider,
and is dropped from the message there. Only each document's `text` is rendered,
by `render_documents`, so the block format has a single definition.
"""

import asyncio
import base64
import json
from unittest import mock

import pytest

from simpleaudit.context_marks import MARK_KEYS, render_documents
from simpleaudit import model_auditor
from simpleaudit.model_auditor import ModelAuditor, _expand_documents, _expand_files

from .fakes import FakeClient, fixed_severity_judge, make_auditor

# Same 1x1 red PNG as tests/test_file_uri.py — real bytes, so the composition
# tests keep passing if image validation starts checking magic bytes.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)

# Marked documents whose mark values are sentinels: "SENTINEL-ROW-42" and the
# ISO dates cannot turn up in a payload by coincidence, and none of the mark
# keys or values is a substring of any document's text, so a leak of any one of
# them is unambiguous. The texts are deliberately Norwegian prose that contains
# no English mark vocabulary ("true", "source", "authority", ...).
SENTINEL_DOCUMENTS = [
    {
        "text": "Aldersgrensen for egenandel ble hevet til 18 år 1. august 2026.",
        "relevant": True,
        "true": True,
        "valid_from": "2026-08-01",
        "authority": "guidance",
        "source": "SENTINEL-ROW-42",
        "decisive": True,
    },
    {
        "text": "Barn under 16 år er fritatt fra å betale egenandel.",
        "relevant": True,
        "true": True,
        "valid_until": "2026-07-31",
        "authority": "statute",
        "source": "SENTINEL-ROW-7",
        "decisive": False,
    },
    "Egenandelstak 2 dekker blant annet fysioterapi.",
]

SENTINEL_PROMPT = "Betaler en 17-åring egenandel hos legen i dag?"


def _document_texts(documents):
    """Every document body in one string, for the substring exemption below."""
    return "\n".join(
        doc["text"] if isinstance(doc, dict) else doc for doc in documents
    )


def _forbidden_strings(documents):
    """Strings that must not appear in anything the target is sent.

    Every mark key, plus every mark value that is not already a substring of
    the document text — a value the text itself contains (a date quoted in the
    prose, say) proves nothing either way, so it is exempt. Boolean marks are
    covered by their keys rather than their values: a bare ``true`` in a
    payload carries no information without the key naming it, and ``false``
    would collide with unrelated JSON.
    """
    texts = _document_texts(documents)
    forbidden = list(MARK_KEYS)
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        for key in MARK_KEYS:
            value = doc.get(key)
            if value is None or isinstance(value, bool):
                continue
            rendered = json.dumps(value, default=str).strip('"')
            if rendered not in texts:
                forbidden.append(rendered)
    return forbidden


class _Capture:
    """FakeClient response_fn that records every payload it is handed."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    @property
    def messages(self):
        return self.calls[-1]["messages"]

    def first_user_message(self):
        return next(m for m in self.messages if m["role"] == "user")

    def payload(self) -> str:
        """The whole last request, serialised — what actually left the process."""
        return json.dumps(self.calls[-1], default=str, ensure_ascii=False)


def _call(capture, **kwargs):
    """Run one `_call_async` against a capturing fake client."""
    return asyncio.run(
        ModelAuditor._call_async(
            client=FakeClient(response_fn=capture),
            model="gpt-4o",
            system=None,
            user=kwargs.pop("user", SENTINEL_PROMPT),
            **kwargs,
        )
    )


# --- _expand_documents ------------------------------------------------------


class TestExpandDocuments:
    def test_message_without_marker_passes_through_unchanged(self):
        message = {"role": "user", "content": "plain text"}
        assert _expand_documents(message=message) is message

    def test_empty_document_list_passes_through_unchanged(self):
        # A scenario that declares no documents is a plain scenario, not an
        # empty document set — it must not gain a stray blank text block.
        message = {"role": "user", "content": "plain text", "documents": []}
        assert _expand_documents(message=message) is message

    def test_marker_becomes_text_blocks_and_is_stripped(self):
        expanded = _expand_documents(
            message={
                "role": "user",
                "content": SENTINEL_PROMPT,
                "documents": SENTINEL_DOCUMENTS,
            }
        )

        assert "documents" not in expanded
        assert expanded["role"] == "user"
        assert expanded["content"][0] == {"type": "text", "text": SENTINEL_PROMPT}
        assert expanded["content"][1]["type"] == "text"

    def test_rendering_has_a_single_definition(self):
        # The block is whatever render_documents produces, byte for byte. A
        # second format here would drift from the judge's numbering, and
        # `used_context: [2]` would then point at a different document.
        expanded = _expand_documents(
            message={
                "role": "user",
                "content": SENTINEL_PROMPT,
                "documents": SENTINEL_DOCUMENTS,
            }
        )
        assert expanded["content"][1]["text"] == render_documents(SENTINEL_DOCUMENTS)
        assert "--- DOCUMENT 3 ---" in expanded["content"][1]["text"]

    def test_bare_strings_are_documents_too(self):
        expanded = _expand_documents(
            message={
                "role": "user",
                "content": "Which one?",
                "documents": ["First chunk.", "Second chunk."],
            }
        )
        rendered = expanded["content"][1]["text"]
        assert "--- DOCUMENT 1 ---\nFirst chunk." in rendered
        assert "--- DOCUMENT 2 ---\nSecond chunk." in rendered

    def test_does_not_mutate_the_stored_message(self):
        message = {
            "role": "user",
            "content": SENTINEL_PROMPT,
            "documents": SENTINEL_DOCUMENTS,
        }
        _expand_documents(message=message)
        assert message == {
            "role": "user",
            "content": SENTINEL_PROMPT,
            "documents": SENTINEL_DOCUMENTS,
        }

    def test_unknown_mark_key_raises(self):
        # Validation happens here, at the provider boundary, because a typo'd
        # mark silently reads as "unmarked" everywhere downstream.
        with pytest.raises(ValueError, match="Unknown document mark key"):
            _expand_documents(
                message={
                    "role": "user",
                    "content": "Which one?",
                    "documents": [{"text": "A chunk.", "relevent": True}],
                }
            )


# --- composition with file_uri ---------------------------------------------


class TestDocumentsAndFilesCompose:
    def test_text_then_documents_then_images(self, tmp_path):
        path = tmp_path / "chart.png"
        path.write_bytes(PNG_BYTES)
        message = {
            "role": "user",
            "content": SENTINEL_PROMPT,
            "documents": SENTINEL_DOCUMENTS,
            "file_uri": str(path),
        }

        expanded = _expand_files(_expand_documents(message))

        assert "documents" not in expanded and "file_uri" not in expanded
        content = expanded["content"]
        assert [block["type"] for block in content] == ["text", "text", "image_url"]
        assert content[0]["text"] == SENTINEL_PROMPT
        assert content[1]["text"] == render_documents(SENTINEL_DOCUMENTS)
        assert content[2]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_file_uri_alone_is_unchanged(self, tmp_path):
        # The pre-existing single-marker shape must survive untouched: one
        # text block built from a plain string, then the images.
        path = tmp_path / "chart.png"
        path.write_bytes(PNG_BYTES)
        expanded = _expand_files(
            message={"role": "user", "content": "What is this?", "file_uri": str(path)}
        )
        assert expanded["content"][0] == {"type": "text", "text": "What is this?"}
        assert expanded["content"][1]["type"] == "image_url"

    def test_file_uri_expansion_matches_the_pre_change_baseline(self):
        """Byte-identical to what `_expand_files` produced at upstream/dev 42f3f8a.

        `documents` support widened `_expand_files` to append to an existing
        block list, and that function sits in the code path every multi-turn
        pack already runs. The structural assertions above would still pass if
        the block order or the text-block shape drifted, so this pins the
        actual serialised output against a value captured by running the
        42f3f8a version of the function in isolation. Regenerate it only from
        that commit, never from the working tree — a baseline copied out of the
        code it is meant to guard proves nothing.
        """
        baseline = (
            '{"content": [{"text": "Hva viser grafen?", "type": "text"}, '
            '{"image_url": {"url": "STUB::a.png"}, "type": "image_url"}, '
            '{"image_url": {"url": "STUB::b.png"}, "type": "image_url"}], '
            '"role": "user"}'
        )
        stub = lambda uri: {"type": "image_url", "image_url": {"url": f"STUB::{uri}"}}
        with mock.patch.object(model_auditor, "image_content_block", stub):
            expanded = _expand_files(
                {"role": "user", "content": "Hva viser grafen?",
                 "file_uri": ["a.png", "b.png"]}
            )
        assert json.dumps(expanded, ensure_ascii=False, sort_keys=True) == baseline


# --- threading through _call_async -----------------------------------------


class TestCallAsyncThreadsDocuments:
    def test_documents_reach_the_target_as_blocks(self):
        capture = _Capture(response="Nei.")
        _call(capture, documents=SENTINEL_DOCUMENTS)

        user_msg = capture.first_user_message()
        assert "documents" not in user_msg
        assert user_msg["content"][0] == {"type": "text", "text": SENTINEL_PROMPT}
        assert user_msg["content"][1]["text"] == render_documents(SENTINEL_DOCUMENTS)

    def test_history_entries_carrying_documents_are_expanded(self):
        capture = _Capture(response="Nei.")
        _call(
            capture,
            history=[
                {
                    "role": "user",
                    "content": SENTINEL_PROMPT,
                    "documents": SENTINEL_DOCUMENTS,
                }
            ],
        )
        assert all("documents" not in message for message in capture.messages)
        assert capture.first_user_message()["content"][1]["text"] == render_documents(
            SENTINEL_DOCUMENTS
        )

    def test_documents_and_file_uri_both_arrive(self, tmp_path):
        # Both markers on one call: neither may clobber the other.
        path = tmp_path / "chart.png"
        path.write_bytes(PNG_BYTES)
        capture = _Capture(response="Nei.")
        _call(capture, documents=SENTINEL_DOCUMENTS, file_uri=str(path))

        content = capture.first_user_message()["content"]
        assert [block["type"] for block in content] == ["text", "text", "image_url"]
        assert content[1]["text"] == render_documents(SENTINEL_DOCUMENTS)

    def test_no_documents_leaves_the_payload_a_plain_string(self):
        # The default path is untouched: content stays a string, not a
        # one-element block list.
        capture = _Capture(response="Nei.")
        _call(capture, user="Hei?")
        assert capture.first_user_message() == {"role": "user", "content": "Hei?"}


# --- the property this feature exists for ----------------------------------


class TestMarksNeverReachTheTarget:
    """Spec §3: the serialised target payload carries no mark, only text.

    This is the test that keeps a plant from being aimed at. If the target can
    read `relevant: false` or `authority: statute`, it is no longer answering
    the question the scenario poses — it is reading the answer key, and every
    groundedness number measured afterwards is measuring nothing.
    """

    def test_no_mark_key_or_value_in_the_target_payload(self):
        capture = _Capture(response="Nei, 17-åringer betaler ikke egenandel.")
        _call(capture, documents=SENTINEL_DOCUMENTS)

        payload = capture.payload()
        for forbidden in _forbidden_strings(SENTINEL_DOCUMENTS):
            assert forbidden not in payload, f"mark {forbidden!r} leaked to the target"

    def test_marks_stay_out_when_an_image_rides_along(self, tmp_path):
        path = tmp_path / "chart.png"
        path.write_bytes(PNG_BYTES)
        capture = _Capture(response="Nei.")
        _call(capture, documents=SENTINEL_DOCUMENTS, file_uri=str(path))

        payload = capture.payload()
        for forbidden in _forbidden_strings(SENTINEL_DOCUMENTS):
            assert forbidden not in payload, f"mark {forbidden!r} leaked to the target"

    def test_the_document_text_does_arrive(self):
        # Guard against the suite above passing vacuously: the texts the marks
        # describe must be in the payload, or nothing was sent at all.
        capture = _Capture(response="Nei.")
        _call(capture, documents=SENTINEL_DOCUMENTS)

        payload = capture.payload()
        for doc in SENTINEL_DOCUMENTS:
            text = doc["text"] if isinstance(doc, dict) else doc
            assert text in payload

    def test_stored_conversation_entry_carries_no_marks(self):
        # A single-turn runner stores the exchange it had, not the ground
        # truth it held. Marks in a stored conversation would ride into every
        # downstream consumer of the result file — including a replayed or
        # re-judged run, where they would reach a target after all.
        capture = _Capture(response="Nei, 17-åringer betaler ikke egenandel.")
        response, _, _ = _call(capture, documents=SENTINEL_DOCUMENTS)
        conversation = [
            {"role": "user", "content": SENTINEL_PROMPT},
            {"role": "assistant", "content": response},
        ]

        stored = json.dumps(conversation, default=str, ensure_ascii=False)
        for forbidden in _forbidden_strings(SENTINEL_DOCUMENTS):
            assert forbidden not in stored, f"mark {forbidden!r} leaked into storage"


def test_run_async_routes_scenario_documents_to_the_target():
    """A scenario's `documents` must reach the target through the multi-turn path.

    This is the end-to-end guard for the routing, not for the expansion:
    `_call_async` has accepted a `documents` argument since the field was
    introduced, but nothing passed `scenario["documents"]` into it, so a
    scenario carrying documents ran as an ordinary audit with none attached
    and still reported a valid-looking result. The assertion is on what the
    target actually received, because that is the only place the omission is
    visible; every layer above it looked correct while the documents were
    being dropped.

    Exercised through `run_async` rather than `run_scenario` so that the
    `_run_one` hop is covered too, which is where the field was lost.
    """
    seen_messages = []

    def recording_target(**kwargs):
        seen_messages.append(kwargs.get("messages"))
        return "Yes, that is correct."

    auditor = make_auditor(
        target=FakeClient(recording_target),
        judge=fixed_severity_judge("pass"),
        max_turns=1,
    )

    planted = "Fritaket gjelder barn under 18 aar."
    scenario = {
        "name": "documents reach the target",
        "description": "A scenario that carries documents.",
        "test_prompt": "Maa hun betale egenandel?",
        "documents": [
            {
                "text": planted,
                "authority": "regulation",
                "relevant": True,
            }
        ],
    }

    asyncio.run(auditor.run_async(scenarios=[scenario]))

    assert seen_messages, "the target was never called"
    flattened = json.dumps(seen_messages, ensure_ascii=False)
    assert planted in flattened, (
        "the document text never reached the target: scenario['documents'] "
        "was dropped between run_async and _call_async"
    )
