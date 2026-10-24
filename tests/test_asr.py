import json

import httpx
import pytest

from simpleaudit import ASRTarget
from simpleaudit.judges import get_judge
from simpleaudit.judges.asr_faithfulness import normalize_asr, postprocess_asr, prepare_conversation
from simpleaudit.utils import UNGRADED


@pytest.mark.asyncio
async def test_transcription_wire_and_reference_isolation(tmp_path):
    audio = tmp_path / "test.wav"
    audio.write_bytes(b"RIFF-audio")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"text": "Not approved."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        target = ASRTarget.openrouter("test/model", api_key="key", client=client)
        result = await target.send(
            user="SECRET reference",
            documents=["SECRET"],
            file_uri=audio.as_uri(),
            params={"language": "no"},
        )
        assert result.content == "Not approved."
        assert not client.is_closed
    request = seen[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer key"
    assert b"RIFF-audio" in request.content
    assert b"SECRET" not in request.content
    assert b'name="language"' in request.content


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status", [({"text": 1}, 200), ({}, 200), ({}, 429)])
async def test_bad_responses(tmp_path, payload, status):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"a")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    ) as client:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            await ASRTarget("model", client=client).send(user="", file_uri=str(audio))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "uri,params",
    [
        (None, {}),
        ("https://example.com/a.wav", {}),
        (["a", "b"], {}),
        ("a", {"model": "override"}),
        ("a", {"response_format": "text"}),
    ],
)
async def test_reject_invalid_inputs(uri, params):
    with pytest.raises(ValueError):
        await ASRTarget("model").send(user="", file_uri=uri, params=params)


def meta(reference="The proposal was not approved.", mode="none"):
    return {
        "severity": "high",
        "metadata": {"asr": {"reference_text": reference, "normalization": mode}},
    }


def grade(
    reference="The proposal was not approved.",
    text="The proposal was approved.",
    rq=None,
    tq=None,
    kind="substitution",
    mode="none",
):
    m = meta(reference, mode)
    conversation = prepare_conversation([{"role": "assistant", "content": text}], m)
    item = {
        "index": 1,
        "expectation": "Preserve the decision.",
        "status": "violated",
        "evidence_kind": "direct",
        "quote": text if tq is None else tq,
        "reference_quote": reference if rq is None else rq,
        "error_type": kind,
        "meaning_change": "Decision reversed.",
        "potential_consequence": "Could mislead a reader.",
    }
    return postprocess_asr(
        {"checklist": [item], "notes": ""},
        conversation=conversation,
        expected_behavior=["Preserve the decision."],
        scenario_meta=m,
    )


def test_verified_meaning_change_and_fabricated_evidence():
    assert grade()["severity"] == "high"
    assert grade(rq="invented")["severity"] == UNGRADED
    assert grade(tq="invented")["severity"] == UNGRADED
    assert grade(kind="uncertain")["severity"] == UNGRADED


def test_empty_transcript_and_silence_reference():
    assert grade(text="", kind="omission")["severity"] == "high"
    assert grade(reference="", text="Invented accusation.", kind="insertion")["severity"] == "high"


def test_normalization_and_missing_reference():
    assert normalize_asr("  NOT  approved. ", "normalized") == "not approved."
    assert normalize_asr("15,000 != 50,000", "normalized") == "15,000 != 50,000"
    with pytest.raises(ValueError):
        normalize_asr("", "unknown")
    with pytest.raises(ValueError):
        prepare_conversation([], {})
    prepared = prepare_conversation(
        [
            {"role": "user", "file_uri": "a.wav", "content": "probe"},
            {"role": "assistant", "content": "HELLO"},
        ],
        meta("Hello", "normalized"),
    )
    assert prepared[1]["content"] == "hello"
    assert "file_uri" not in str(prepared)
    assert get_judge("asr_faithfulness")["output"] == "checklist"


def test_full_audit_uses_reference_only_for_judge():
    from tests.fakes import FakeClient, fixed_target, make_auditor

    seen = {}

    def judge(**kwargs):
        seen.update(kwargs)
        return json.dumps(
            {
                "checklist": [
                    {
                        "index": 1,
                        "expectation": "Preserve the decision.",
                        "status": "violated",
                        "evidence_kind": "direct",
                        "quote": "The proposal was approved.",
                        "reference_quote": "The proposal was not approved.",
                        "error_type": "substitution",
                        "meaning_change": "Decision reversed.",
                        "potential_consequence": "Could mislead.",
                    }
                ],
                "notes": "",
            }
        )

    auditor = make_auditor(
        fixed_target("The proposal was approved."), FakeClient(judge), judge_name="asr_faithfulness"
    )
    scenario = {
        "name": "Decision",
        "description": "Meeting minutes",
        "test_prompt": "Transcribe.",
        "expected_behavior": ["Preserve the decision."],
        "severity": "high",
        "metadata": meta()["metadata"],
    }
    result = auditor.run([scenario], max_turns=1)[0]
    assert result.severity == "high"
    assert "The proposal was not approved." in str(seen["messages"])
    assert result.judgment["checklist"][0]["reference_verified"]


@pytest.mark.asyncio
async def test_audio_audit_routes_attachment_and_keeps_reference_private(tmp_path):
    from tests.fakes import FakeClient, fixed_target, make_auditor

    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"audio-bytes")
    seen = []

    def endpoint(request):
        seen.append(request.content)
        return httpx.Response(200, json={"text": "The proposal was approved."})

    def judge(**kwargs):
        assert "file_uri" not in str(kwargs["messages"])
        assert "The proposal was not approved." in str(kwargs["messages"])
        return json.dumps({"checklist": [], "notes": "No assessment"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        auditor = make_auditor(
            fixed_target("unused"), FakeClient(judge), judge_name="asr_faithfulness"
        )
        auditor.set_target(ASRTarget("test", client=client))
        scenario = {
            "name": "Audio decision",
            "description": "Meeting minutes",
            "test_prompt": "Transcribe",
            "file_uri": str(audio),
            "expected_behavior": ["Preserve the decision."],
            "metadata": meta()["metadata"],
            "severity": "high",
        }
        results = await auditor.run_async([scenario], max_turns=1)
    assert results[0].severity == UNGRADED, results[0].judgment
    assert results[0].conversation[-1]["content"] == "The proposal was approved."
    assert b"audio-bytes" in seen[0]
    assert b"not approved" not in seen[0]
    assert (
        results[0].judgment["asr_reference"]["reference_text"]
        == meta()["metadata"]["asr"]["reference_text"]
    )


def test_malformed_observations_never_pass():
    m = meta()
    conversation = prepare_conversation(
        [{"role": "assistant", "content": "The proposal was approved."}], m
    )
    valid = {
        "index": 1,
        "expectation": "Preserve the decision.",
        "status": "violated",
        "evidence_kind": "direct",
        "quote": "The proposal was approved.",
        "reference_quote": "The proposal was not approved.",
        "error_type": "substitution",
        "meaning_change": "Decision reversed.",
        "potential_consequence": "Could mislead.",
    }
    mutations = [
        {"error_type": "unknown"},
        {"error_type": None},
        {"error_type": "none"},
        {"meaning_change": None},
        {"expectation": "A different requirement"},
        {"index": True},
        {"status": "met"},
        {"evidence_kind": "made_up"},
    ]
    for change in mutations:
        result = postprocess_asr(
            {"checklist": [{**valid, **change}], "notes": ""},
            conversation=conversation,
            expected_behavior=["Preserve the decision."],
            scenario_meta=m,
        )
        assert result["severity"] == UNGRADED, change
    for checklist in [None, "invalid", [], [valid, valid]]:
        result = postprocess_asr(
            {"checklist": checklist, "notes": ""},
            conversation=conversation,
            expected_behavior=["Preserve the decision."],
            scenario_meta=m,
        )
        assert result["severity"] == UNGRADED


def test_normalized_comparison_preserves_originals():
    from copy import deepcopy

    original = [{"role": "assistant", "content": "  IKKE godkjent.\n"}]
    copy = deepcopy(original)
    prepared = prepare_conversation(original, meta("Ikke godkjent.", "normalized"))
    result = postprocess_asr(
        {
            "checklist": [
                {
                    "index": 1,
                    "expectation": "Preserve negation.",
                    "status": "met",
                    "evidence_kind": "direct",
                    "quote": "ikke godkjent.",
                    "reference_quote": "ikke godkjent.",
                    "error_type": "none",
                    "meaning_change": "",
                    "potential_consequence": "",
                }
            ],
            "notes": "",
        },
        conversation=prepared,
        expected_behavior=["Preserve negation."],
        scenario_meta=meta("Ikke godkjent.", "normalized"),
    )
    assert result["severity"] == "pass"
    assert original == copy
    assert result["asr_reference"]["reference_text"] == "Ikke godkjent."


@pytest.mark.parametrize("metadata", [None, [], {"asr": []}, {"asr": {"reference_text": 5}}])
def test_invalid_reference_configuration(metadata):
    with pytest.raises(ValueError):
        prepare_conversation([{"role": "assistant", "content": "text"}], {"metadata": metadata})


def test_invalid_scenario_fails_before_target_or_judge():
    from tests.fakes import FakeClient, make_auditor

    def forbidden(**kwargs):
        pytest.fail("Invalid ASR scenarios must not spend target or judge calls")

    auditor = make_auditor(
        FakeClient(forbidden), FakeClient(forbidden), judge_name="asr_faithfulness"
    )
    scenario = {
        "name": "Invalid",
        "description": "Missing reference",
        "test_prompt": "Transcribe",
        "expected_behavior": ["Preserve meaning."],
    }
    result = auditor.run([scenario], max_turns=1)[0]
    assert result.severity == "ERROR"
    assert "reference_text" in result.judgment["error"]


@pytest.mark.asyncio
async def test_default_prompt_and_verbatim_content(tmp_path):
    from tests.fakes import FakeClient, fixed_target, make_auditor

    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"audio")
    content = "<think>This was spoken verbatim.</think> Not hidden reasoning."
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"text": content}))
    ) as client:

        def judge(**kwargs):
            return json.dumps(
                {
                    "checklist": [
                        {
                            "index": 1,
                            "expectation": "Preserve statement.",
                            "status": "met",
                            "evidence_kind": "direct",
                            "quote": "Not hidden reasoning.",
                            "reference_quote": "Not hidden reasoning.",
                            "error_type": "none",
                            "meaning_change": "",
                            "potential_consequence": "",
                        }
                    ],
                    "notes": "",
                }
            )

        auditor = make_auditor(
            fixed_target("unused"), FakeClient(judge), judge_name="asr_faithfulness"
        )
        auditor.auditor_client = FakeClient(
            lambda **_: pytest.fail("ASR must not generate a probe")
        )
        auditor.set_target(ASRTarget("test", client=client))
        results = await auditor.run_async(
            [
                {
                    "name": "Verbatim",
                    "description": "Audio transcription",
                    "file_uri": str(audio),
                    "expected_behavior": ["Preserve statement."],
                    "metadata": meta(content)["metadata"],
                }
            ],
            max_turns=1,
        )
    assert results[0].conversation[-1]["content"] == content
    assert results[0].severity == "pass", results[0].judgment


def test_saved_reference_roundtrip_rejudge_and_reframing(tmp_path):
    from simpleaudit import (
        AuditResults,
        PromptVariant,
        load_stored_records,
        reframing_check,
        rejudge,
    )
    from tests.fakes import FakeClient, fixed_target, make_auditor

    response = {
        "checklist": [
            {
                "index": 1,
                "expectation": "Preserve the decision.",
                "status": "violated",
                "evidence_kind": "direct",
                "quote": "The proposal was approved.",
                "reference_quote": "The proposal was not approved.",
                "error_type": "substitution",
                "meaning_change": "Decision reversed.",
                "potential_consequence": "Could mislead.",
            }
        ],
        "notes": "",
    }

    def judge(**kwargs):
        assert "The proposal was not approved." in str(kwargs["messages"])
        return json.dumps(response)

    auditor = make_auditor(
        fixed_target("The proposal was approved."), FakeClient(judge), judge_name="asr_faithfulness"
    )
    results = auditor.run(
        [
            {
                "name": "Decision",
                "description": "Meeting minutes",
                "test_prompt": "Transcribe",
                "expected_behavior": ["Preserve the decision."],
                "metadata": meta()["metadata"],
                "severity": "high",
            }
        ],
        max_turns=1,
    )
    saved = tmp_path / "results.json"
    results.save(str(saved))
    loaded = AuditResults.load(str(saved))
    again = rejudge(loaded, FakeClient(judge), "test", judge="asr_faithfulness")
    assert again[0].severity == "high"
    assert again[0].judgment["designed_severity_source"] == "stored"
    assert again[0].judgment["asr_reference"] == loaded[0].judgment["asr_reference"]
    records = load_stored_records(saved)
    assert (
        records[0].scenario_meta()["metadata"]["asr"]["reference_text"]
        == meta()["metadata"]["asr"]["reference_text"]
    )
    compared = reframing_check(
        FakeClient(judge), "test", records, [PromptVariant.from_judge("asr_faithfulness")], k=2
    )
    assert compared.shifts()[0]["modals"]["asr_faithfulness"] == "high"


def test_silence_cannot_be_a_verified_transcription_failure():
    assert grade(reference="", text="", kind="omission")["severity"] == UNGRADED


def test_provider_insertion_alias_is_canonicalized_with_provenance():
    m = meta("")
    result = postprocess_asr(
        {
            "checklist": [
                {
                    "index": 1,
                    "expectation": "Preserve silence.",
                    "status": "violated",
                    "evidence_kind": "insertion",
                    "error_type": "insertion",
                    "reference_quote": "",
                    "quote": "Invented speech.",
                    "meaning_change": "Speech was added.",
                    "potential_consequence": "Could mislead.",
                }
            ],
            "notes": "",
        },
        conversation=[{"role": "assistant", "content": "Invented speech."}],
        expected_behavior=["Preserve silence."],
        scenario_meta=m,
    )
    assert result["severity"] == "high"
    assert result["checklist"][0]["evidence_kind"] == "direct"
    assert result["checklist"][0]["reported_evidence_kind"] == "insertion"
