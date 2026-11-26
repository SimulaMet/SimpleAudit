"""
``simpleaudit calibrate`` (simpleaudit/calibration_cli.py).

No model is called: re-grading goes through a fake judge client patched in
for ``make_judge_client``.
"""

import csv
import io
import json

import pytest

from simpleaudit.calibration_cli import calibratable_judges, labels_path, main, regraded_path
from simpleaudit.cli import main as cli_main
from simpleaudit.judges import get_judge
from simpleaudit.results import AuditResult, AuditResults
from tests.fakes import FakeClient

NAMES = [f"Skatt {i}" for i in range(12)]


def _run(tmp_path, *, judge="pass_fail", verdicts=None, name="run.json"):
    """A saved run: graded by *judge* (a verdict judge) or, with judge=None, by the default judge."""
    results = []
    for i, scenario in enumerate(NAMES):
        verdict = (verdicts or {}).get(scenario, "fail" if i % 3 == 0 else "pass")
        if judge:
            judgment = get_judge(judge)["postprocess"](
                {"verdict": verdict, "issues_found": [], "positive_behaviors": [], "summary": "", "recommendations": []},
                scenario_meta={"severity": "high"})
            severity = judgment["severity"]
        else:
            judgment, severity = {"severity": "low" if verdict == "fail" else "pass"}, "low" if verdict == "fail" else "pass"
        results.append(AuditResult(
            scenario_name=scenario, scenario_description="Tax question",
            conversation=[{"role": "user", "content": "q"}, {"role": "assistant", "content": f"answer {i}"}],
            severity=severity, issues_found=[], positive_behaviors=[], summary="", recommendations=[],
            expected_behavior=["Cite the rule"], judgment=judgment,
        ))
    meta = {"judge_model": "claude-haiku-4-5"}
    if judge:
        meta["judge_name"] = judge
    path = tmp_path / name
    AuditResults(results, meta=meta).save(str(path))
    return path


def _fill(sheet, labels):
    text = sheet.read_bytes().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    for row in rows[1:]:
        row[1] = labels.get(row[0], "")
    with open(sheet, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)


def test_only_verdict_judges_can_be_calibrated():
    assert calibratable_judges() == {"pass_fail": "pass_fail", "pass_medium_fail": "pass_medium_fail"}


def test_simpleaudit_calibrate_is_reachable_from_the_main_cli(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main(["calibrate", "--help"])
    assert exc.value.code == 0
    assert "pass_fail" in capsys.readouterr().out
    with pytest.raises(SystemExit) as exc:
        cli_main(["calibrate", "safety", "report", "run.json"])
    assert exc.value.code == 2          # argparse: not a calibratable judge


def test_main_help_mentions_calibrate(capsys):
    with pytest.raises(SystemExit):
        cli_main(["--help"])
    assert "simpleaudit calibrate --help" in capsys.readouterr().out


def test_label_in_the_terminal(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("f\np\nq\n"))
    assert main(["pass_fail", "label", str(run), "--rater", "Anna"]) == 0
    out = labels_path(run, "pass_fail", "Anna")
    assert out.name == "run.pass_fail.anna.labels.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["labels"] == {"Skatt 0": "fail", "Skatt 1": "pass"}
    assert data["rater"] == "Anna" and data["scale"] == "pass_fail"
    assert "VERDICT (pass or fail)" in capsys.readouterr().out


def test_sheet_round_trip_then_report_on_stored_verdicts(tmp_path, capsys):
    run = _run(tmp_path)
    assert main(["pass_fail", "export", str(run), "--rater", "Ola Nordmann"]) == 0
    sheet = tmp_path / "run.pass_fail.ola_nordmann.csv"
    assert sheet.exists() and (tmp_path / "run.pass_fail.ola_nordmann.instructions.txt").exists()
    _fill(sheet, {n: ("fail" if i % 3 == 0 else "pass") for i, n in enumerate(NAMES)})
    assert main(["pass_fail", "import", str(run), str(sheet), "--rater", "Ola Nordmann"]) == 0
    assert "12 new, 0 changed" in capsys.readouterr().out
    save_dir = tmp_path / "reports"
    assert main(["pass_fail", "report", str(run), "--save-dir", str(save_dir)]) == 0
    out = capsys.readouterr().out
    assert "reference: human, 1 rater: run.pass_fail.ola_nordmann.labels" in out
    assert "exact agreement     12/12" in out
    assert "For comparison" not in out          # the run was graded by this judge
    [report] = list(save_dir.glob("*.json"))
    assert json.loads(report.read_text(encoding="utf-8"))["scale"] == "pass_fail"


def test_import_merges_and_rejects_another_runs_sheet(tmp_path, capsys):
    run = _run(tmp_path)
    sheet = tmp_path / "s.csv"
    sheet.write_text("scenario_name,label\nSkatt 0,fail\n", encoding="utf-8")
    assert main(["pass_fail", "import", str(run), str(sheet), "--rater", "anna"]) == 0
    sheet.write_text("scenario_name,label\nSkatt 0,pass\nSkatt 1,pass\n", encoding="utf-8")
    assert main(["pass_fail", "import", str(run), str(sheet), "--rater", "anna"]) == 0
    assert "1 new, 1 changed" in capsys.readouterr().out
    sheet.write_text("scenario_name,label\nNot in the run,pass\n", encoding="utf-8")
    assert main(["pass_fail", "import", str(run), str(sheet), "--rater", "anna"]) == 1
    assert "not in" in capsys.readouterr().err


def test_bad_labels_in_a_sheet_exit_with_the_rows(tmp_path, capsys):
    run = _run(tmp_path)
    sheet = tmp_path / "s.csv"
    sheet.write_text("scenario_name,label\nSkatt 0,medium\n", encoding="utf-8")
    assert main(["pass_fail", "import", str(run), str(sheet), "--rater", "anna"]) == 1
    assert "row 2" in capsys.readouterr().err


def test_report_without_labels_or_without_this_judge(tmp_path, capsys):
    run = _run(tmp_path, judge=None)
    assert main(["pass_fail", "report", str(run)]) == 1
    assert "no labels" in capsys.readouterr().err
    (tmp_path / "run.pass_fail.anna.labels.json").write_text(json.dumps({"Skatt 0": "fail"}), encoding="utf-8")
    assert main(["pass_fail", "report", str(run)]) == 1
    err = capsys.readouterr().err
    assert "was not graded by pass_fail" in err and "--judge-model" in err


def test_report_grades_the_run_once_and_compares_with_its_own_judge(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, judge=None)
    labels = {n: ("fail" if i % 3 == 0 else "pass") for i, n in enumerate(NAMES)}
    (tmp_path / "run.pass_fail.anna.labels.json").write_text(
        json.dumps({"scale": "pass_fail", "rater": "anna", "labels": labels}), encoding="utf-8")
    calls = []

    def judge(**kwargs):
        calls.append(1)
        return json.dumps({"issues_found": [], "positive_behaviors": [], "verdict": "fail",
                           "summary": "", "recommendations": []})

    monkeypatch.setattr("simpleaudit.reframing.make_judge_client", lambda *a, **k: FakeClient(judge))
    argv = ["pass_fail", "report", str(run), "--judge-model", "gpt-4.1-mini", "--save-dir", str(tmp_path / "r")]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert len(calls) == 12
    assert "Pass --pack to use the pack's" in out      # rejudge's warning, in the CLI's words
    cached = regraded_path(run, "pass_fail", "gpt-4.1-mini")
    assert cached.name == "run.pass_fail.gpt-4.1-mini.json" and cached.exists()
    regraded = AuditResults.load(str(cached))
    assert regraded.meta["judge_name"] == "pass_fail" and regraded.meta["judge_model"] == "gpt-4.1-mini"
    assert "judge 'pass_fail'" in out and "exact agreement     4/12" in out      # it failed everything
    assert "For comparison, the run's own judge on the same labels" in out
    assert "exact agreement     12/12" in out                                    # low read as fail
    assert main(argv) == 0
    assert len(calls) == 12                                                      # no second grading
    assert "Using the pass_fail grades in" in capsys.readouterr().out


def test_missing_results_file(tmp_path, capsys):
    assert main(["pass_medium_fail", "report", str(tmp_path / "nope.json")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_rater_name_needs_a_letter(tmp_path):
    run = _run(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["pass_fail", "label", str(run), "--rater", "///"])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# found in review
# ---------------------------------------------------------------------------

def test_another_raters_file_is_not_overwritten(tmp_path, capsys):
    run = _run(tmp_path)
    sheet = tmp_path / "s.csv"
    sheet.write_text("scenario_name,label\nSkatt 0,fail\n", encoding="utf-8")
    assert main(["pass_fail", "import", str(run), str(sheet), "--rater", "Ola N."]) == 0
    assert main(["pass_fail", "import", str(run), str(sheet), "--rater", "ola n"]) == 1
    assert "holds the labels of rater 'Ola N.'" in capsys.readouterr().err
    assert main(["pass_fail", "label", str(run), "--rater", "OLA N"]) == 1


def test_one_labels_file_given_twice_is_one_rater(tmp_path, capsys):
    run = _run(tmp_path)
    labels = tmp_path / "run.pass_fail.anna.labels.json"
    labels.write_text(json.dumps({"scale": "pass_fail", "labels": {"Skatt 0": "fail"}}), encoding="utf-8")
    assert main(["pass_fail", "report", str(run), "--labels", str(labels.resolve()), "--labels", str(labels),
                 "--save-dir", str(tmp_path / "r")]) == 0
    assert "1 rater:" in capsys.readouterr().out


def _labels_for(tmp_path):
    labels = {n: ("fail" if i % 3 == 0 else "pass") for i, n in enumerate(NAMES)}
    (tmp_path / "run.pass_fail.anna.labels.json").write_text(
        json.dumps({"scale": "pass_fail", "labels": labels}), encoding="utf-8")


def test_failed_judge_calls_are_not_kept(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, judge=None)
    _labels_for(tmp_path)

    def broken(**kwargs):
        raise RuntimeError("401 invalid api key")

    monkeypatch.setattr("simpleaudit.reframing.make_judge_client", lambda *a, **k: FakeClient(broken))
    # Every call is retried twice with a short backoff; all at once keeps the test quick.
    argv = ["pass_fail", "report", str(run), "--judge-model", "gpt-4.1-mini", "--concurrency", "12",
            "--save-dir", str(tmp_path / "r")]
    assert main(argv) == 1
    assert "every judge call failed" in capsys.readouterr().err
    assert not regraded_path(run, "pass_fail", "gpt-4.1-mini").exists()

    def flaky(**kwargs):
        if "answer 0\n" in kwargs["messages"][-1]["content"]:
            raise RuntimeError("timeout")
        return json.dumps({"issues_found": [], "positive_behaviors": [], "verdict": "pass",
                           "summary": "", "recommendations": []})

    monkeypatch.setattr("simpleaudit.reframing.make_judge_client", lambda *a, **k: FakeClient(flaky))
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "1 of 12 judge calls failed" in out and "(1 ERROR)" in out
    assert not regraded_path(run, "pass_fail", "gpt-4.1-mini").exists()


def test_kept_grades_are_redone_when_the_run_is_newer_or_on_request(tmp_path, monkeypatch, capsys):
    import os

    run = _run(tmp_path, judge=None)
    _labels_for(tmp_path)
    calls = []

    def judge(**kwargs):
        calls.append(1)
        return json.dumps({"issues_found": [], "positive_behaviors": [], "verdict": "pass",
                           "summary": "", "recommendations": []})

    monkeypatch.setattr("simpleaudit.reframing.make_judge_client", lambda *a, **k: FakeClient(judge))
    argv = ["pass_fail", "report", str(run), "--judge-model", "gpt-4.1-mini", "--save-dir", str(tmp_path / "r")]
    assert main(argv) == 0 and len(calls) == 12
    cached = regraded_path(run, "pass_fail", "gpt-4.1-mini")
    later = cached.stat().st_mtime + 10
    os.utime(run, (later, later))                    # a new run saved over the old one
    assert main(argv) == 0 and len(calls) == 24
    assert "is newer than the grades" in capsys.readouterr().out
    assert main(argv + ["--regrade"]) == 0 and len(calls) == 36


# ---------------------------------------------------------------------------
# found in the PR review
# ---------------------------------------------------------------------------

def _counting_judge(monkeypatch, verdict="pass"):
    calls = []

    def judge(**kwargs):
        calls.append(1)
        return json.dumps({"issues_found": [], "positive_behaviors": [], "verdict": verdict,
                           "summary": "", "recommendations": []})

    monkeypatch.setattr("simpleaudit.reframing.make_judge_client", lambda *a, **k: FakeClient(judge))
    return calls


def test_a_failed_conversation_in_the_run_does_not_stop_grades_being_kept(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, judge=None)
    stored = AuditResults.load(str(run))
    cut = stored.results[0]
    cut.conversation = cut.conversation[:1]          # the user turn is kept, the reply never came
    cut.severity = "ERROR"
    cut.judgment = {"severity": "ERROR", "error": "timeout", "error_phase": "conversation"}
    stored.save(str(run))
    _labels_for(tmp_path)
    calls = _counting_judge(monkeypatch)
    argv = ["pass_fail", "report", str(run), "--judge-model", "m", "--save-dir", str(tmp_path / "r")]
    assert main(argv) == 0 and len(calls) == 11
    out = capsys.readouterr().out
    assert "judge calls failed" not in out
    assert regraded_path(run, "pass_fail", "m").exists()
    assert not list(tmp_path.glob("*.incomplete.json"))
    assert main(argv) == 0 and len(calls) == 11      # the kept grades are used
    assert "Using the pass_fail grades in" in capsys.readouterr().out


def test_kept_grades_are_redone_for_another_provider_base_url_or_pack(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path, judge=None)
    _labels_for(tmp_path)
    calls = _counting_judge(monkeypatch)
    argv = ["pass_fail", "report", str(run), "--judge-model", "m", "--save-dir", str(tmp_path / "r")]
    assert main(argv) == 0 and len(calls) == 12
    kept = AuditResults.load(str(regraded_path(run, "pass_fail", "m"))).meta
    assert kept["calibrate_graded_with"] == {"provider": "openai", "base_url": None, "scenario_pack": None}
    assert kept["judge_provider"] == "openai"
    assert main(argv + ["--provider", "anthropic"]) == 0 and len(calls) == 24
    assert "made with another --provider; grading again" in capsys.readouterr().out
    assert main(argv + ["--provider", "anthropic", "--base-url", "http://localhost:8000/v1"]) == 0
    assert len(calls) == 36
    assert "made with another --base-url; grading again" in capsys.readouterr().out
    pack = ["--provider", "anthropic", "--base-url", "http://localhost:8000/v1", "--pack", "no_such_pack"]
    assert main(argv + pack) == 0 and len(calls) == 48
    assert "made with another --pack; grading again" in capsys.readouterr().out
    assert main(argv + pack) == 0 and len(calls) == 48
    assert "Using the pass_fail grades in" in capsys.readouterr().out


def test_regrade_on_a_run_graded_by_the_judge_says_its_own_grades_are_used(tmp_path, capsys):
    run = _run(tmp_path)
    _labels_for(tmp_path)
    assert main(["pass_fail", "report", str(run), "--regrade", "--save-dir", str(tmp_path / "r")]) == 0
    assert "its own grades are used" in capsys.readouterr().out


def test_export_does_not_replace_a_sheet_without_force(tmp_path, capsys):
    run = _run(tmp_path)
    assert main(["pass_fail", "export", str(run), "--rater", "ola"]) == 0
    sheet = tmp_path / "run.pass_fail.ola.csv"
    _fill(sheet, {"Skatt 0": "fail"})
    assert main(["pass_fail", "export", str(run), "--rater", "ola", "--delimiter", ";"]) == 1
    assert "--force" in capsys.readouterr().err
    assert "Skatt 0,fail" in sheet.read_text(encoding="utf-8-sig")       # the filled-in sheet is kept
    assert main(["pass_fail", "export", str(run), "--rater", "ola", "--force"]) == 0
    assert "Skatt 0,fail" not in sheet.read_text(encoding="utf-8-sig")


def test_half_an_emoji_in_a_reply_exports_and_labels(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path)
    stored = AuditResults.load(str(run))
    stored.results[1].conversation[-1]["content"] = "ok \ud83d"
    stored.save(str(run))
    assert main(["pass_fail", "export", str(run), "--rater", "ola"]) == 0
    assert "ok \\ud83d" in (tmp_path / "run.pass_fail.ola.csv").read_text(encoding="utf-8-sig")
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO("f\np\nq\n"))
    assert main(["pass_fail", "label", str(run), "--rater", "anna"]) == 0
    assert "ok \\ud83d" in capsys.readouterr().out


def test_end_of_input_in_label_stops_like_q(tmp_path, monkeypatch, capsys):
    run = _run(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("f\n"))     # then Ctrl-D
    assert main(["pass_fail", "label", str(run), "--rater", "anna"]) == 0
    assert "Stopped. 1 labels in" in capsys.readouterr().out
    data = json.loads(labels_path(run, "pass_fail", "anna").read_text(encoding="utf-8"))
    assert data["labels"] == {"Skatt 0": "fail"}


def test_report_finds_labels_at_the_python_default_path(tmp_path, capsys):
    run = _run(tmp_path)
    labels = {n: ("fail" if i % 3 == 0 else "pass") for i, n in enumerate(NAMES)}
    (tmp_path / "run.pass_fail.labels.json").write_text(
        json.dumps({"scale": "pass_fail", "labels": labels}), encoding="utf-8")
    assert main(["pass_fail", "report", str(run), "--save-dir", str(tmp_path / "r")]) == 0
    out = capsys.readouterr().out
    assert "Labels: " + str(tmp_path / "run.pass_fail.labels.json") in out
    assert "exact agreement     12/12" in out


def test_import_refuses_a_labels_file_on_another_scale(tmp_path, capsys):
    run = _run(tmp_path)
    labels_path(run, "pass_fail", "anna").write_text(
        json.dumps({"scale": "pass_medium_fail", "labels": {"Skatt 1": "medium"}}), encoding="utf-8")
    sheet = tmp_path / "s.csv"
    sheet.write_text("scenario_name,label\nSkatt 0,fail\n", encoding="utf-8")
    assert main(["pass_fail", "import", str(run), str(sheet), "--rater", "anna"]) == 1
    assert "holds labels on the 'pass_medium_fail' scale" in capsys.readouterr().err
