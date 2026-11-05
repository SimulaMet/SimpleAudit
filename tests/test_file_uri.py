"""Tests for the scenario `file_uri` field.

An image attached via `file_uri` reaches every model that has to reason about
the exchange — target, judge, and probe generator — as OpenAI-style content
blocks, tagged `[file N]` in the transcript so each one can be traced to the
turn that carried it. The stored conversation and saved results stay plain
text; expansion happens only on the way to a provider.
"""

import asyncio
import base64
import gzip
import json
import mimetypes
import pathlib

import pytest

from simpleaudit import utils
from simpleaudit.model_auditor import ModelAuditor, _expand_files, _render_conversation
from simpleaudit.utils import (
    image_content_block,
    image_data_uri,
    image_media_type,
    sniff_image_media_type,
)

from .fakes import FakeClient, make_auditor

# A real, minimal 1x1 red PNG — not just a header glued to ASCII, so the
# tests keep passing if image validation starts checking magic bytes.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)

# Content sniffing reads only the leading signature, so the other formats need
# no more than a valid header.
JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 16
GIF_BYTES = b"GIF89a\x01\x00\x01\x00" + b"\x00" * 16
WEBP_BYTES = b"RIFF\x1a\x00\x00\x00WEBPVP8 " + b"\x00" * 16

JUDGE_JSON = json.dumps({
    "severity": "pass",
    "issues_found": [],
    "positive_behaviors": ["Described the image"],
    "summary": "Fine.",
    "recommendations": [],
})


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


def _image_scenario(*, file_uri, name="Image read"):
    return {
        "name": name,
        "description": "Model should describe the image",
        "test_prompt": "What is in this image?",
        "file_uri": file_uri,
    }


def _run(*, scenario, target, judge, auditor=None, max_turns=1):
    return _run_batch(
        scenarios=[scenario],
        target=target,
        judge=judge,
        auditor=auditor,
        max_turns=max_turns,
    )


def _run_batch(*, scenarios, target, judge, auditor=None, max_turns=1):
    ma = make_auditor(
        target=FakeClient(response_fn=target),
        judge=FakeClient(response_fn=judge),
        auditor=FakeClient(response_fn=auditor) if auditor else None,
        max_turns=max_turns,
    )
    return asyncio.run(ma.run_async(scenarios=scenarios, max_turns=max_turns))


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
        path.write_bytes(JPEG_BYTES)
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
        assert image_media_type(file_uri=file_uri) == expected

    def test_rejects_uri_without_a_usable_extension(self):
        with pytest.raises(ValueError, match="Cannot determine an image type"):
            image_media_type(file_uri="/tmp/screenshot")

    def test_rejects_a_non_image_file(self):
        with pytest.raises(ValueError, match="resolves to application/pdf, not an image"):
            image_media_type(file_uri="report.pdf")

    @pytest.mark.parametrize(
        "file_uri", ["logo.svg", "scan.tiff", "old.bmp", "favicon.ico", "photo.heic", "photo.avif"]
    )
    def test_rejects_image_types_the_vision_apis_do_not_accept(self, file_uri):
        if mimetypes.guess_type(file_uri)[0] is None:
            pytest.skip(f"this platform's mimetypes has no entry for {file_uri}")
        with pytest.raises(ValueError, match="vision APIs do not accept"):
            image_media_type(file_uri=file_uri)


class TestContentSniffing:
    @pytest.mark.parametrize(
        "data,expected",
        [
            (PNG_BYTES, "image/png"),
            (JPEG_BYTES, "image/jpeg"),
            (GIF_BYTES, "image/gif"),
            (b"GIF87a" + b"\x00" * 8, "image/gif"),
            (WEBP_BYTES, "image/webp"),
        ],
    )
    def test_identifies_allowed_types(self, data, expected):
        assert sniff_image_media_type(data) == expected

    @pytest.mark.parametrize(
        "data",
        [
            b"",
            b"<svg xmlns='http://www.w3.org/2000/svg'/>",
            # mtime=0 keeps the bytes, and so the test ids, identical across
            # xdist workers.
            gzip.compress(PNG_BYTES, mtime=0),
            b"II*\x00" + b"\x00" * 8,
            b"BM" + b"\x00" * 12,
            b"RIFF\x1a\x00\x00\x00WAVEfmt ",
            b"%PDF-1.7",
        ],
        ids=["empty", "svg", "gzipped-png", "tiff", "bmp", "riff-wave", "pdf"],
    )
    def test_returns_none_for_anything_else(self, data):
        assert sniff_image_media_type(data) is None

    def test_label_comes_from_content_not_extension(self, tmp_path):
        path = tmp_path / "renamed.png"
        path.write_bytes(JPEG_BYTES)
        url = image_content_block(file_uri=str(path))["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")

    def test_accepts_a_valid_image_without_an_extension(self, tmp_path):
        path = tmp_path / "screenshot"
        path.write_bytes(PNG_BYTES)
        url = image_content_block(file_uri=str(path))["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")

    def test_rejects_gzip_named_as_png(self, tmp_path):
        path = tmp_path / "pic.png.gz"
        path.write_bytes(gzip.compress(PNG_BYTES, mtime=0))
        with pytest.raises(ValueError, match="does not contain a supported image"):
            image_content_block(file_uri=str(path))

    def test_rejects_svg_renamed_to_png(self, tmp_path):
        path = tmp_path / "logo.png"
        path.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'/>")
        with pytest.raises(ValueError, match="does not contain a supported image"):
            image_content_block(file_uri=str(path))


class TestSizeGuard:
    def test_default_limit_keeps_the_payload_within_five_mib(self):
        limit = utils._max_image_file_bytes()
        assert len(base64.b64encode(b"\x00" * limit)) <= utils.MAX_IMAGE_PAYLOAD_BYTES
        assert len(base64.b64encode(b"\x00" * (limit + 1))) > utils.MAX_IMAGE_PAYLOAD_BYTES

    def test_image_at_the_limit_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils, "MAX_IMAGE_PAYLOAD_BYTES", 400)
        limit = utils._max_image_file_bytes()
        path = tmp_path / "at_limit.png"
        path.write_bytes(PNG_BYTES + b"\x00" * (limit - len(PNG_BYTES)))
        url = image_content_block(file_uri=str(path))["image_url"]["url"]
        assert len(url.split(",", 1)[1]) <= 400

    def test_image_over_the_limit_is_rejected_before_upload(self, tmp_path, monkeypatch):
        monkeypatch.setattr(utils, "MAX_IMAGE_PAYLOAD_BYTES", 400)
        limit = utils._max_image_file_bytes()
        path = tmp_path / "too_big.png"
        path.write_bytes(PNG_BYTES + b"\x00" * (limit + 1 - len(PNG_BYTES)))
        with pytest.raises(ValueError, match="per-image limit"):
            image_content_block(file_uri=str(path))


class TestEncodingIsCached:
    def test_repeated_uris_are_read_once(self, png_path):
        image_data_uri.cache_clear()
        for _ in range(3):
            image_content_block(file_uri=png_path)
        info = image_data_uri.cache_info()
        assert (info.misses, info.hits) == (1, 2)

    def test_cache_clear_resets_counters(self, png_path):
        image_data_uri.cache_clear()
        image_content_block(file_uri=png_path)
        assert image_data_uri.cache_info().misses == 1
        image_data_uri.cache_clear()
        assert image_data_uri.cache_info().misses == 0
        assert image_data_uri.cache_info().hits == 0

    def test_a_new_run_re_reads_a_changed_file(self, png_path):
        image_data_uri.cache_clear()
        scenario = _image_scenario(file_uri=png_path)
        first = _Capture(response="A bar chart.")
        _run(scenario=scenario, target=first, judge=_Capture(response=JUDGE_JSON))

        # Same URI, new bytes — the notebook loop of regenerating a figure and
        # re-auditing must not replay the old encoding.
        regenerated = PNG_BYTES + b"regenerated"
        pathlib.Path(png_path).write_bytes(regenerated)
        second = _Capture(response="A line chart.")
        _run(scenario=scenario, target=second, judge=_Capture(response=JUDGE_JSON))

        def encoded(capture):
            return capture.first_user_message()["content"][1]["image_url"]["url"]

        assert encoded(first) != encoded(second)
        assert base64.b64decode(encoded(second).split(",", 1)[1]) == regenerated

    def test_distinct_uris_are_cached_separately(self, png_path, tmp_path):
        image_data_uri.cache_clear()
        other = tmp_path / "second.png"
        other.write_bytes(PNG_BYTES + b"different-bytes")
        first = image_content_block(file_uri=png_path)
        second = image_content_block(file_uri=str(other))
        assert first["image_url"]["url"] != second["image_url"]["url"]
        assert image_data_uri.cache_info().misses == 2


class TestFsspecRoundTrip:
    def test_memory_uri_round_trips_through_fsspec(self):
        # Exercise fsspec beyond local paths: write to an in-memory filesystem,
        # then encode from the memory:// URI.
        import fsspec

        fs = fsspec.filesystem("memory")
        fs.pipe("dir/chart.png", PNG_BYTES)
        block = image_content_block(file_uri="memory://dir/chart.png")
        assert block["type"] == "image_url"
        url = block["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == PNG_BYTES


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

    def test_list_of_uris_produces_one_block_each(self, png_path, tmp_path):
        # Two distinct files, so reordering or misattribution is detectable.
        other = tmp_path / "second.png"
        other.write_bytes(PNG_BYTES + b"-second")
        expanded = _expand_files(
            message={"role": "user", "content": "Compare", "file_uri": [png_path, str(other)]}
        )
        content = expanded["content"]
        assert len(content) == 3
        assert [block["type"] for block in content] == ["text", "image_url", "image_url"]
        assert base64.b64decode(content[1]["image_url"]["url"].split(",", 1)[1]) == PNG_BYTES
        assert base64.b64decode(content[2]["image_url"]["url"].split(",", 1)[1]) == PNG_BYTES + b"-second"


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
        image_data_uri.cache_clear()
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
        assert image_data_uri.cache_info().misses == 1

    def test_bad_file_uri_fails_the_scenario_not_the_run(self, png_path, tmp_path):
        # A multi-scenario batch: the broken scenario must come back as an
        # ERROR result while the healthy sibling still runs to completion —
        # one bad file must not abort the whole run.
        not_an_image = tmp_path / "notes.png"
        not_an_image.write_bytes(b"plain text, not an image")
        target = _Capture(response="A bar chart.")
        judge = _Capture(response=JUDGE_JSON)
        results = _run_batch(
            scenarios=[
                _image_scenario(file_uri=str(not_an_image), name="Broken image"),
                _image_scenario(file_uri=png_path, name="Fine image"),
            ],
            target=target,
            judge=judge,
        )

        by_name = {r.scenario_name: r for r in results.results}
        assert set(by_name) == {"Broken image", "Fine image"}
        assert by_name["Broken image"].severity == "ERROR"
        assert "does not contain a supported image" in " ".join(by_name["Broken image"].issues_found)
        # The sibling ran normally and kept its file_uri in the stored conversation.
        assert by_name["Fine image"].severity != "ERROR"
        assert by_name["Fine image"].conversation[0]["file_uri"] == png_path

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

