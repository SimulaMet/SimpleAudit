"""File-driven examples reject bad inputs before making billable requests."""

import json

import pytest

from examples import audit_asr, audit_decision_documents


def test_asr_rejects_invalid_rubric_before_inference(tmp_path, monkeypatch):
    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"audio")
    reference = tmp_path / "reference.txt"
    reference.write_text("Reference speech.")
    expectations = tmp_path / "expectations.json"
    expectations.write_text(json.dumps([""]))
    monkeypatch.setattr(audit_asr, "Auditor", lambda **_: pytest.fail("Must not call a model"))
    with pytest.raises(SystemExit) as exc:
        audit_asr.main(
            [
                "--audio",
                str(audio),
                "--reference",
                str(reference),
                "--expectations",
                str(expectations),
                "--asr-model",
                "whisper-1",
                "--judge-model",
                "gpt-4o",
                "--output",
                str(tmp_path / "results.json"),
            ]
        )
    assert exc.value.code == 2
    assert not (tmp_path / "results.json").exists()


@pytest.mark.parametrize("accepted", [None, [], ["absent"]])
def test_decision_requires_valid_answer_key_before_inference(tmp_path, monkeypatch, accepted):
    scenario = {
        "name": "Case",
        "description": "Read a document.",
        "documents": ["Not approved."],
        "decision": {"instructions": "Approved?", "criteria": {"yes": "Yes", "no": "No"}},
    }
    if accepted is not None:
        scenario["decision"]["accepted"] = accepted
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps([scenario]))
    monkeypatch.setattr(
        audit_decision_documents, "Auditor", lambda **_: pytest.fail("Must not call a model")
    )
    with pytest.raises(SystemExit) as exc:
        audit_decision_documents.main(
            [
                "--scenarios",
                str(path),
                "--model",
                "clef",
                "--output",
                str(tmp_path / "results.json"),
            ]
        )
    assert exc.value.code == 2
    assert not (tmp_path / "results.json").exists()


def test_decision_example_saves_execution_error_and_returns_failure(tmp_path, monkeypatch):
    from simpleaudit import AuditResult, AuditResults

    scenario = {
        "name": "Case",
        "description": "Read a document.",
        "documents": ["Not approved."],
        "decision": {
            "instructions": "Approved?",
            "criteria": {"yes": "Yes", "no": "No"},
            "accepted": ["no"],
        },
    }
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps([scenario]))
    failed = AuditResult(
        "Case",
        "Read a document.",
        [],
        "ERROR",
        [],
        [],
        "Endpoint failed",
        [],
        judgment={"severity": "ERROR"},
    )

    class FailingAuditor:
        def __init__(self, **kwargs):
            pass

        def run(self, scenarios):
            return AuditResults([failed])

    monkeypatch.setattr(audit_decision_documents, "Auditor", FailingAuditor)
    output = tmp_path / "nested" / "results.json"
    code = audit_decision_documents.main(
        [
            "--scenarios",
            str(path),
            "--model",
            "clef",
            "--output",
            str(output),
        ]
    )
    assert code == 1
    assert json.loads(output.read_text())["results"][0]["severity"] == "ERROR"
