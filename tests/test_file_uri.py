"""Tests for the scenario `file_uri` field.

An image attached via `file_uri` reaches every model that has to reason about
the exchange — target, judge, and probe generator — as OpenAI-style content
blocks, tagged `[file N]` in the transcript so each one can be traced to the
turn that carried it. The stored conversation and saved results stay plain
text; expansion happens only on the way to a provider.
"""

import asyncio
import base64
import json

import pytest

from simpleaudit.model_auditor import ModelAuditor, _expand_files, _render_conversation
from simpleaudit.utils import _image_data_uri, _image_media_type, image_content_block

from .fakes import FakeClient, make_auditor

PNG_BYTES = b"\x89PNG\r\n\x1a\nnot-a-real-image"

JUDGE_JSON = json.dumps({
    "severity": "pass",
    "issues_found": [],
    "positive_behaviors": ["Described the image"],
    "summary": "Fine.",
    "recommendations": [],
})


@pytest.fixture(autouse=True)
def clear_image_cache():
    """Encoded payloads are cached process-wide; keep tests independent."""
    _image_data_uri.cache_clear()
    yield
    _image_data_uri.cache_clear()


@pytest.fixture
def png_path(tmp_path):
    path = tmp_path / "chart.png"
    path.write_bytes(PNG_BYTES)
    return str(path)


class _Capture:
    """FakeClient response_fn that records every messages payload it sees."""

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs["messages"])
        return self.response

    @property
    def last(self):
        return self.calls[-1]

    def first_user_message(self):
        return next(m for m in self.last if m["role"] == "user")


def _image_scenario(*, file_uri):
    return {
        "name": "Image read",
        "description": "Model should describe the image",
        "test_prompt": "What is in this image?",
        "file_uri": file_uri,
    }


def _run(*, scenario, target, judge, auditor=None, max_turns=1):
    ma = make_auditor(
        target=FakeClient(response_fn=target),
        judge=FakeClient(response_fn=judge),
        auditor=FakeClient(response_fn=auditor) if auditor else None,
        max_turns=max_turns,
    )
    return asyncio.run(ma.run_async(scenarios=[scenario], max_turns=max_turns))


# --- image_content_block ---------------------------------------------------


class TestImageContentBlock:
    def test_builds_data_uri_from_local_path(self, png_path):
        block = image_content_block(file_uri=png_path)
        assert block["type"] == "image_url"
        url = block["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == PNG_BYTES

    def test_jpg_extension_normalized_to_jpeg(self, tmp_path):
        path = tmp_path / "photo.JPG"
        path.write_bytes(b"jpeg-bytes")
        url = image_content_block(file_uri=str(path))["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")

    def test_returns_a_fresh_dict_each_call(self, png_path):
        first = image_content_block(file_uri=png_path)
        second = image_content_block(file_uri=png_path)
        assert first == second
        assert first is not second
        assert first["image_url"] is not second["image_url"]


class TestMediaTypeResolution:
    @pytest.mark.parametrize(
        "file_uri,expected",
        [
            ("chart.png", "image/png"),
            ("photo.JPG", "image/jpeg"),
            ("photo.jpeg", "image/jpeg"),
            ("art.webp", "image/webp"),
            ("loop.gif", "image/gif"),
            ("relative/dir/a.png", "image/png"),
            ("https://host/chart.png?v=2#frag", "image/png"),
            ("s3://bucket/scan.PNG", "image/png"),
        ],
    )
    def test_resolves_media_type(self, file_uri, expected):
        assert _image_media_type(file_uri=file_uri) == expected

    def test_rejects_uri_without_a_usable_extension(self):
        with pytest.raises(ValueError, match="Cannot determine an image type"):
            _image_media_type(file_uri="/tmp/screenshot")

    def test_rejects_a_non_image_file(self):
        with pytest.raises(ValueError, match="resolves to application/pdf, not an image"):
            _image_media_type(file_uri="report.pdf")


class TestEncodingIsCached:
    def test_repeated_uris_are_read_once(self, png_path):
        for _ in range(3):
            image_content_block(file_uri=png_path)
        info = _image_data_uri.cache_info()
        assert (info.misses, info.hits) == (1, 2)

    def test_distinct_uris_are_cached_separately(self, png_path, tmp_path):
        other = tmp_path / "second.png"
        other.write_bytes(b"different-bytes")
        first = image_content_block(file_uri=png_path)
        second = image_content_block(file_uri=str(other))
        assert first["image_url"]["url"] != second["image_url"]["url"]
        assert _image_data_uri.cache_info().misses == 2


# --- _expand_files ----------------------------------------------------------


class TestExpandFiles:
    def test_message_without_marker_passes_through_unchanged(self):
        message = {"role": "user", "content": "plain text"}
        assert _expand_files(message=message) is message

    def test_marker_becomes_content_blocks_and_is_stripped(self, png_path):
        expanded = _expand_files(
            message={"role": "user", "content": "What is this?", "file_uri": png_path}
        )

        assert "file_uri" not in expanded
        assert expanded["role"] == "user"
        assert expanded["content"][0] == {"type": "text", "text": "What is this?"}
        assert expanded["content"][1]["type"] == "image_url"

    def test_does_not_mutate_the_stored_message(self, png_path):
        message = {"role": "user", "content": "What is this?", "file_uri": png_path}
        _expand_files(message=message)
        assert message == {
            "role": "user",
            "content": "What is this?",
            "file_uri": png_path,
        }

    def test_list_of_uris_produces_one_block_each(self, png_path):
        expanded = _expand_files(
            message={"role": "user", "content": "Compare", "file_uri": [png_path, png_path]}
        )
        content = expanded["content"]
        assert len(content) == 3
        assert [block["type"] for block in content] == ["text", "image_url", "image_url"]


class TestRenderConversation:
    def test_numbers_files_continuously_across_turns(self):
        transcript, uris = _render_conversation(
            [
                {"role": "user", "content": "Compare these.", "file_uri": ["a.png", "b.png"]},
                {"role": "assistant", "content": "Done."},
                {"role": "user", "content": "And this?", "file_uri": "c.png"},
            ],
            role_separator="\n",
            turn_separator="\n\n",
        )

        assert uris == ["a.png", "b.png", "c.png"]
        assert transcript == (
            "USER:\n[file 1] [file 2]\nCompare these.\n\n"
            "ASSISTANT:\nDone.\n\n"
            "USER:\n[file 3]\nAnd this?"
        )

    def test_conversation_without_files_is_unmarked(self):
        transcript, uris = _render_conversation(
            [{"role": "user", "content": "Hello?"}, {"role": "assistant", "content": "Hi."}],
            role_separator=" ",
            turn_separator="\n",
        )
        assert uris == []
        assert transcript == "USER: Hello?\nASSISTANT: Hi."


# --- end-to-end through run_async ------------------------------------------


class TestFileUriEndToEnd:
    def test_image_reaches_the_target_model(self, png_path):
        target = _Capture(response="A bar chart.")
        judge = _Capture(response=JUDGE_JSON)
        _run(scenario=_image_scenario(file_uri=png_path), target=target, judge=judge)

        user_msg = target.first_user_message()
        assert "file_uri" not in user_msg
        assert user_msg["content"][0] == {"type": "text", "text": "What is in this image?"}
        assert user_msg["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_judge_sees_the_image_and_a_marker(self, png_path):
        target = _Capture(response="A bar chart.")
        judge = _Capture(response=JUDGE_JSON)
        _run(scenario=_image_scenario(file_uri=png_path), target=target, judge=judge)

        content = next(m for m in judge.last if m["role"] == "user")["content"]
        transcript, image = content[0], content[1]
        # The marker tells the judge which turn the image belongs to; without
        # it the transcript reads like the target hallucinated a description.
        assert "[file 1]\nWhat is in this image?" in transcript["text"]
        assert image["image_url"]["url"].startswith("data:image/png;base64,")

    def test_auditor_sees_the_image_when_generating_a_follow_up(self, png_path):
        target = _Capture(response="A bar chart.")
        judge = _Capture(response=JUDGE_JSON)
        _run(
            scenario=_image_scenario(file_uri=png_path),
            target=target,
            judge=judge,
            max_turns=2,
        )

        # The auditor shares the judge client here, so its probe call is the
        # first one made — the judge only runs after the conversation ends.
        content = next(m for m in judge.calls[0] if m["role"] == "user")["content"]
        assert "[file 1] What is in this image?" in content[0]["text"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_auditor_sees_the_image_when_it_writes_turn_zero(self, png_path):
        # No test_prompt, so the auditor composes the opening probe itself. It
        # must see the image first, or it writes the prompt blind.
        auditor = _Capture(response="Tell me about this.")
        _run(
            scenario={
                "name": "Chart",
                "description": "Model should read a chart",
                "file_uri": png_path,
            },
            target=_Capture(response="A bar chart."),
            judge=_Capture(response=JUDGE_JSON),
            auditor=auditor,
        )

        content = next(m for m in auditor.calls[0] if m["role"] == "user")["content"]
        assert "FILES ATTACHED TO YOUR NEXT MESSAGE: [file 1]" in content[0]["text"]
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_test_prompt_still_bypasses_the_auditor_on_turn_zero(self, png_path):
        auditor = _Capture(response="should never be called")
        _run(
            scenario=_image_scenario(file_uri=png_path),
            target=_Capture(response="A bar chart."),
            judge=_Capture(response=JUDGE_JSON),
            auditor=auditor,
        )
        assert auditor.calls == []

    def test_turn_zero_files_are_not_counted_twice_later(self, png_path):
        auditor = _Capture(response="And what about the axes?")
        _run(
            scenario={
                "name": "Chart",
                "description": "Model should read a chart",
                "file_uri": png_path,
            },
            target=_Capture(response="A bar chart."),
            judge=_Capture(response=JUDGE_JSON),
            auditor=auditor,
            max_turns=2,
        )

        # Turn 1 reads the file from the conversation instead, so it must not be
        # listed as pending as well — one marker, one image block.
        content = next(m for m in auditor.calls[1] if m["role"] == "user")["content"]
        text = content[0]["text"]
        assert text.count("[file 1]") == 1
        assert "[file 2]" not in text
        assert "FILES ATTACHED TO YOUR NEXT MESSAGE" not in text
        assert [block["type"] for block in content] == ["text", "image_url"]

    def test_stored_conversation_stays_plain_text(self, png_path):
        target = _Capture(response="A bar chart.")
        judge = _Capture(response=JUDGE_JSON)
        results = _run(
            scenario=_image_scenario(file_uri=png_path), target=target, judge=judge
        )

        entry = results.results[0].conversation[0]
        assert entry["content"] == "What is in this image?"
        assert entry["file_uri"] == png_path

    def test_image_persists_across_turns(self, png_path):
        target = _Capture(response="A bar chart.")
        judge = _Capture(response=JUDGE_JSON)
        _run(
            scenario=_image_scenario(file_uri=png_path),
            target=target,
            judge=judge,
            max_turns=2,
        )

        # Turn 2 resends the whole history, so the target must still see the
        # image — otherwise the model forgets it mid-conversation.
        assert len(target.calls) == 2
        assert target.first_user_message()["content"][1]["type"] == "image_url"

        # ...but it is read and encoded only once across those turns.
        assert _image_data_uri.cache_info().misses == 1

    def test_bad_file_uri_fails_the_scenario_not_the_run(self):
        target = _Capture(response="A bar chart.")
        judge = _Capture(response=JUDGE_JSON)
        results = _run(
            scenario=_image_scenario(file_uri="/tmp/screenshot"),
            target=target,
            judge=judge,
        )

        result = results.results[0]
        assert result.severity == "ERROR"
        assert "Cannot determine an image type" in " ".join(result.issues_found)

    def test_scenario_without_file_uri_is_unchanged(self):
        target = _Capture(response="Sure.")
        judge = _Capture(response=JUDGE_JSON)
        results = _run(
            scenario={
                "name": "Text only",
                "description": "Plain text scenario",
                "test_prompt": "Hello?",
            },
            target=target,
            judge=judge,
        )

        assert target.first_user_message() == {"role": "user", "content": "Hello?"}
        assert results.results[0].conversation[0] == {"role": "user", "content": "Hello?"}


class TestCallAsyncStripsMarker:
    def test_marker_never_reaches_the_api(self, png_path):
        capture = _Capture(response="ok")
        asyncio.run(
            ModelAuditor._call_async(
                client=FakeClient(response_fn=capture),
                model="gpt-4o",
                system=None,
                user="Hi",
                history=[{"role": "user", "content": "Hi", "file_uri": png_path}],
            )
        )
        assert all("file_uri" not in message for message in capture.last)
