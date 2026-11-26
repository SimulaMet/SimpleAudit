"""
Calibration on the verdict scales (pass_fail, pass_medium_fail) and the
labelling sheets.

No model is called. Verdict judges' results are built with the judges' own
post-processors, so the stored judgments look exactly as an audit writes
them. The severity ladder's behaviour is pinned in tests/test_calibration.py,
which this change leaves untouched.
"""

import csv
import io
import json

import pytest

from simpleaudit.calibration import (
    SCALES,
    SHEET_COLUMNS,
    Rater,
    calibrate,
    export_labelling_sheet,
    get_scale,
    import_labelling_sheet,
    label,
    list_calibrations,
    load_labels,
    save_labels,
)
from simpleaudit.judges import get_judge
from simpleaudit.reframing import PromptVariant, StoredRecord, reframing_check
from simpleaudit.results import AuditResult, AuditResults

from .fakes import cycling_severity_judge


def _result(name, severity, judgment=None, conversation=None):
    return AuditResult(
        scenario_name=name,
        scenario_description=f"Scenario {name}",
        conversation=conversation or [
            {"role": "user", "content": f"Question {name}?"},
            {"role": "assistant", "content": f"Answer {name}."},
        ],
        severity=severity,
        issues_found=[], positive_behaviors=[], summary="", recommendations=[],
        expected_behavior=["Be right"],
        judgment=judgment,
    )


def _verdict_run(judge, verdicts, ceilings=None, meta=None):
    """A run graded by a verdict judge: ``{name: verdict}``, optional ``{name: designed severity}``."""
    post = get_judge(judge)["postprocess"]
    results = []
    for name, verdict in verdicts.items():
        judgment = post({"verdict": verdict, "issues_found": [], "positive_behaviors": [], "summary": "",
                         "recommendations": []}, scenario_meta={"severity": (ceilings or {}).get(name)})
        results.append(_result(name, judgment["severity"], judgment))
    return AuditResults(results, meta=meta or {"judge_name": judge})


def _severity_run(severities, meta=None):
    return AuditResults([_result(n, s) for n, s in severities.items()], meta=meta or {})


NAMES = [f"s{i:02d}" for i in range(12)]
PF = {n: ("fail" if i % 3 == 0 else "pass") for i, n in enumerate(NAMES)}
PMF = {n: ("pass", "medium", "fail")[i % 3] for i, n in enumerate(NAMES)}


# ---------------------------------------------------------------------------
# scales
# ---------------------------------------------------------------------------

def test_scales_by_name():
    assert set(SCALES) == {"severity", "pass_fail", "pass_medium_fail"}
    assert get_scale("pass_fail").levels == ("pass", "fail")
    assert get_scale(get_scale("pass_medium_fail")).levels == ("pass", "medium", "fail")
    with pytest.raises(ValueError, match="Unknown scale"):
        get_scale("likert")


def test_verdict_scale_rubric_is_what_the_judge_is_given():
    for name in ("pass_fail", "pass_medium_fail"):
        config = get_judge(name)
        rubric = SCALES[name].rubric()
        assert config["criteria"].strip() in rubric
        definitions = config["format_prompt"].split("\n\nList the issues")[0]
        assert definitions in rubric
        own = SCALES[name].rubric("Judge tax answers.")       # a customised judge's criteria
        assert "Judge tax answers." in own and definitions in own and "EVALUATION CRITERIA" not in own
    assert SCALES["severity"].rubric() is None


# ---------------------------------------------------------------------------
# calibrate on a verdict scale
# ---------------------------------------------------------------------------

def test_pass_fail_judge_against_pass_fail_labels():
    """Ceilings vary per scenario, but the verdict, not the severity, is compared."""
    ceilings = {n: ("low", "medium", "critical")[i % 3] for i, n in enumerate(NAMES)}
    report = calibrate(_verdict_run("pass_fail", PF, ceilings), dict(PF), scale="pass_fail")
    a = report["pass_fail"]
    assert report.scale.name == "pass_fail" and a.fail_at == "fail"
    assert a.n_compared == 12 and a.exact == 12
    assert a.sensitivity == (4, 4) and a.specificity == (8, 8)
    assert set(a.confusion) == {"pass", "fail"}
    assert a.reference_distribution == {"pass": 8, "fail": 4}
    assert a.ac2 == pytest.approx(1.0) and a.alpha == pytest.approx(1.0)


def test_pass_medium_fail_reads_the_verdict_not_the_capped_severity():
    """On a low-ceiling scenario medium and fail are both rated low; the verdict tells them apart."""
    ceilings = dict.fromkeys(NAMES, "low")
    run = _verdict_run("pass_medium_fail", PMF, ceilings)
    assert {r.severity for r in run.results} == {"pass", "low"}
    a = calibrate(run, dict(PMF), scale="pass_medium_fail")["pass_medium_fail"]
    assert a.exact == 12 and a.fail_at == "medium"


def test_disagreements_and_bias_on_pass_medium_fail():
    human = dict(PMF)
    judge = dict(PMF)
    judge["s00"] = "fail"     # human pass
    judge["s02"] = "pass"     # human fail: a severe miss
    a = calibrate(_verdict_run("pass_medium_fail", judge), human, scale="pass_medium_fail")["pass_medium_fail"]
    assert a.exact == 10 and a.n_stricter == 1 and a.n_lenient == 1
    assert a.severe_misses == (1, 4)
    assert {d["scenario"] for d in a.disagreements} == {"s00", "s02"}
    assert a.confusion["fail"]["pass"] == 1


def test_a_severity_judge_is_read_through_the_scale():
    """The default judge on the same labels: pass is pass, any other level is fail."""
    severities = {"a": "pass", "b": "low", "c": "medium", "d": "high", "e": "critical"}
    human = {"a": "pass", "b": "fail", "c": "fail", "d": "fail", "e": "pass"}
    a = calibrate(_severity_run(severities), human, scale="pass_fail")["stored"]
    assert a.exact == 4 and a.n_stricter == 1
    on_three = {"a": "pass", "b": "medium", "c": "medium", "d": "fail", "e": "fail"}
    a = calibrate(_severity_run(severities), on_three, scale="pass_medium_fail")["stored"]
    assert a.exact == 5


def test_a_pass_medium_fail_judge_reads_as_pass_fail_but_not_the_reverse():
    a = calibrate(_verdict_run("pass_medium_fail", PMF), {n: ("pass" if v == "pass" else "fail") for n, v in PMF.items()},
                  scale="pass_fail")["pass_medium_fail"]
    assert a.exact == 12
    with pytest.raises(ValueError, match="'pass_fail' scale cannot be read on the 'pass_medium_fail' scale"):
        calibrate(_verdict_run("pass_fail", PF), dict(PMF), scale="pass_medium_fail")


def test_errors_and_off_scale_verdicts_count_in_coverage():
    run = _verdict_run("pass_fail", PF)
    run.results[0] = _result("s00", "ERROR", {"severity": "ERROR", "parse_error": True})
    run.results[1] = _result("s01", "ungraded", {})
    a = calibrate(run, dict(PF), scale="pass_fail")["pass_fail"]
    assert a.n_error == 1 and a.n_ungraded == 1 and a.n_compared == 10


def test_typo_and_wrong_scale_labels_raise():
    with pytest.raises(ValueError, match="not one of pass, fail"):
        calibrate(_verdict_run("pass_fail", PF), {"s00": "medium"}, scale="pass_fail")
    with pytest.raises(ValueError, match="not one of pass, medium, fail"):
        calibrate(_verdict_run("pass_medium_fail", PMF), {"s00": "high"}, scale="pass_medium_fail")


def test_shortcuts_and_unsure():
    labels = {"s00": "F", "s01": " p ", "s02": "unsure"}
    report = calibrate(_verdict_run("pass_fail", PF), labels, scale="pass_fail")
    assert report["pass_fail"].exact == 2 and report.reference.n_unsure == 1


def test_fail_at_is_checked_against_the_scale():
    with pytest.raises(ValueError, match=r"\['fail'\]"):
        calibrate(_verdict_run("pass_fail", PF), dict(PF), scale="pass_fail", fail_at="low")
    a = calibrate(_verdict_run("pass_medium_fail", PMF), dict(PMF), scale="pass_medium_fail", fail_at="fail")
    assert a["pass_medium_fail"].specificity == (8, 8)


def test_two_raters_split_to_the_stricter_level():
    rater_a = dict(PF)
    rater_b = {**PF, "s01": "fail"}
    report = calibrate(_verdict_run("pass_fail", PF), [rater_a, rater_b], scale="pass_fail")
    assert report.reference.consensus["s01"] == "fail"
    assert report.n_split == 1
    assert report.reference.human_ceiling() is not None
    assert report.strata["pass_fail"]["split"].n_compared == 1


def test_answer_key_rater_on_the_ladder_is_read_through_the_scale():
    key = Rater(name="answer key", kind="answer_key", ratings={"s00": "high", "s01": "pass", "s02": None})
    report = calibrate(_verdict_run("pass_fail", PF), key, scale="pass_fail")
    assert report.reference.consensus == {"s00": "fail", "s01": "pass"}
    assert key.ratings["s00"] == "high"          # the caller's rater is not changed


def test_cross_family_judge_reference_on_a_verdict_scale():
    subject = _verdict_run("pass_fail", PF, meta={"judge_name": "pass_fail", "judge_model": "claude-haiku-4-5"})
    reference = _verdict_run("pass_fail", PF, meta={"judge_name": "pass_fail", "judge_model": "gpt-5.2"})
    report = calibrate(subject, reference, scale="pass_fail")
    assert report.reference.kind == "model" and report["pass_fail"].exact == 12


def test_reframing_results_on_pass_fail_only():
    records = [StoredRecord(scenario_name=n, scenario_description=n,
                            conversation=[{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}],
                            expected_behavior=["Be right"]) for n in NAMES]
    variants = [PromptVariant("v", "Grade.", judge_model="gpt-5.2"),
                PromptVariant("w", "Grade again.", judge_model="gemini-2.5-pro")]
    results = reframing_check(cycling_severity_judge(["pass", "high"]), "gpt-5.2", records, variants)
    report = calibrate(results, dict.fromkeys(NAMES, "pass"), scale="pass_fail")
    assert report["v"].n_compared == 12
    assert report["v"].exact + report["w"].exact == 12          # 24 calls, every second one passes
    with pytest.raises(ValueError, match="keep only severities"):
        calibrate(results, dict.fromkeys(NAMES, "pass"), scale="pass_medium_fail")


# ---------------------------------------------------------------------------
# report output and persistence
# ---------------------------------------------------------------------------

def _summary(report):
    lines = []
    report.summary(print_fn=lines.append)
    return "\n".join(lines)


def test_two_level_summary_leaves_out_what_two_levels_cannot_show():
    text = _summary(calibrate(_verdict_run("pass_fail", PF), dict(PF), scale="pass_fail"))
    assert "scale: pass_fail (pass, fail)" in text
    assert "within one level" not in text and "severe misses" not in text
    assert "Gwet AC1" in text and "Krippendorff alpha" in text
    assert "pass  fail   ERR" in text


def test_three_level_summary():
    text = _summary(calibrate(_verdict_run("pass_medium_fail", PMF), dict(PMF), scale="pass_medium_fail"))
    assert "within one level" in text
    assert "severe misses (ref fail -> judge pass)" in text
    assert "Gwet AC2 (linear)" in text and "ordinal alpha" in text
    assert "pass   med  fail   ERR" in text


def test_severity_summary_is_unchanged_by_the_scale_line():
    text = _summary(calibrate(_severity_run({"a": "pass"}), {"a": "pass"}))
    assert "scale:" not in text


def test_report_records_its_scale(tmp_path):
    report = calibrate(_verdict_run("pass_fail", PF), dict(PF), scale="pass_fail")
    data = report.to_dict()
    json.dumps(data)
    assert data["scale"] == "pass_fail" and data["levels"] == ["pass", "fail"]
    path = report.save(directory=tmp_path)
    assert "__pass_fail__human__" in path.name
    calibrate(_severity_run({"a": "pass"}), {"a": "pass"}).save(directory=tmp_path)
    scales = sorted(row["scale"] for row in list_calibrations(tmp_path))
    assert scales == ["pass_fail", "severity"]


# ---------------------------------------------------------------------------
# labels files and label()
# ---------------------------------------------------------------------------

def test_labels_file_records_scale_and_rater(tmp_path):
    path = tmp_path / "anna.json"
    save_labels({"s00": "fail"}, path, rater="Anna", scale="pass_fail")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["scale"] == "pass_fail" and data["version"] == "1.1"
    assert load_labels(path) == {"s00": "fail"}
    report = calibrate(_verdict_run("pass_fail", PF), path, scale="pass_fail")
    assert report.reference.raters[0].name == "anna"      # the file name, as on the ladder


def test_three_level_labels_file_is_read_on_two_levels(tmp_path):
    path = tmp_path / "pmf.json"
    save_labels(dict(PMF), path, scale="pass_medium_fail")
    a = calibrate(_verdict_run("pass_fail", PF), path, scale="pass_fail")["pass_fail"]
    assert a.n_compared == 12
    assert a.exact == sum(1 for n in NAMES if (PMF[n] == "pass") == (PF[n] == "pass"))
    two = tmp_path / "pf.json"
    save_labels(dict(PF), two, scale="pass_fail")
    with pytest.raises(ValueError, match="cannot be read"):
        calibrate(_verdict_run("pass_medium_fail", PMF), two, scale="pass_medium_fail")


def test_label_on_pass_fail_shows_the_rules_and_never_the_verdict(tmp_path):
    run = _verdict_run("pass_fail", {"a": "fail", "b": "pass"})
    src = tmp_path / "run.json"
    run.save(str(src))
    printed = []
    answers = iter(["medium", "?", "f", "p"])
    labels = label(str(src), input_fn=lambda prompt: (printed.append(prompt), next(answers))[1],
                   print_fn=printed.append, scale="pass_fail")
    assert labels == {"a": "fail", "b": "pass"}
    text = "\n".join(printed)
    assert text.count("VERDICT (pass or fail)") == 2            # at the start and after '?'
    assert "not one of pass, fail" in text
    assert "verdict> " in printed
    assert "verdict_scale" not in text and "designed_severity" not in text
    out = tmp_path / "run.pass_fail.labels.json"
    assert json.loads(out.read_text(encoding="utf-8"))["scale"] == "pass_fail"


def test_label_refuses_a_file_on_another_scale(tmp_path):
    run = _severity_run({"a": "pass"})
    out = tmp_path / "labels.json"
    save_labels({"a": "high"}, out, scale="severity")
    with pytest.raises(ValueError, match="mix the two"):
        label(run, out, scale="pass_fail", input_fn=lambda _: "q", print_fn=lambda _: None)


# ---------------------------------------------------------------------------
# labelling sheets
# ---------------------------------------------------------------------------

def _rows(path, delimiter=","):
    return list(csv.reader(io.StringIO(path.read_bytes().decode("utf-8-sig")), delimiter=delimiter))


def test_sheet_round_trip(tmp_path):
    run = _verdict_run("pass_fail", {"Skatt – æøå": "fail", "b": "pass", "c": "pass"})
    sheet = export_labelling_sheet(run, tmp_path / "sheet.csv", scale="pass_fail")
    assert sheet.read_bytes().startswith(b"\xef\xbb\xbf")                 # BOM for Excel
    rows = _rows(sheet)
    assert rows[0] == ["scenario_name", "label (pass | fail)", *SHEET_COLUMNS[2:]]
    assert rows[1][0] == "Skatt – æøå" and rows[1][1] == ""
    assert "1. Be right" in rows[1][3] and "[ASSISTANT]" in rows[1][4]
    assert "fail" not in "".join(rows[1][:2]) and "verdict" not in sheet.read_text(encoding="utf-8-sig")
    instructions = (tmp_path / "sheet.instructions.txt").read_text(encoding="utf-8")
    assert "VERDICT (pass or fail)" in instructions and "pass, fail" in instructions
    rows[1][1], rows[2][1], rows[3][1] = "F", "unsure", ""
    with open(sheet, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    assert import_labelling_sheet(sheet, scale="pass_fail") == {"Skatt – æøå": "fail", "b": "unsure"}


def test_sheet_as_norwegian_excel_saves_it(tmp_path):
    """Semicolons, Windows-1252, columns moved: still read."""
    text = "label;scenario_name;transcript\r\nfail;Skatt – æøå;x\r\np;b;\"line 1\r\nline 2\"\r\n"
    path = tmp_path / "excel.csv"
    path.write_bytes(text.encode("cp1252"))
    assert import_labelling_sheet(path, scale="pass_fail") == {"Skatt – æøå": "fail", "b": "pass"}


def test_sheet_with_semicolons_is_exported_and_read(tmp_path):
    run = _verdict_run("pass_medium_fail", {"a": "medium"})
    sheet = export_labelling_sheet(run, tmp_path / "s.csv", scale="pass_medium_fail", delimiter=";")
    rows = _rows(sheet, ";")
    rows[1][1] = "m"
    with open(sheet, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=";").writerows(rows)
    assert import_labelling_sheet(sheet, scale="pass_medium_fail") == {"a": "medium"}


def test_sheet_reports_every_bad_label_with_its_row(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("scenario_name,label\na,pass\nb,maybe\nc,high\nb,fail\nd,fail\nd,pass\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        import_labelling_sheet(path, scale="pass_fail")
    message = str(exc.value)
    assert "3 label(s) to fix" in message
    assert "row 3" in message and "row 4" in message and "row 7" in message and "labelled twice" in message


def test_sheet_without_the_columns_raises(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("name,verdict\na,pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no scenario_name or label column"):
        import_labelling_sheet(path, scale="pass_fail")


def test_sheet_refuses_duplicate_names_and_warns_on_long_transcripts(tmp_path):
    dupes = AuditResults([_result("a", "pass"), _result("a", "high")])
    with pytest.raises(ValueError, match="duplicate scenario names"):
        export_labelling_sheet(dupes, tmp_path / "d.csv", scale="pass_fail")
    long = AuditResults([_result("a", "pass", conversation=[{"role": "assistant", "content": "x" * 40000}])])
    with pytest.warns(UserWarning, match="Excel cell"):
        export_labelling_sheet(long, tmp_path / "l.csv", scale="pass_fail")


# ---------------------------------------------------------------------------
# found in review
# ---------------------------------------------------------------------------

def test_a_sheet_for_one_scale_is_refused_on_another(tmp_path):
    run = _verdict_run("pass_fail", {"a": "fail"})
    sheet = export_labelling_sheet(run, tmp_path / "pf.csv", scale="pass_fail")
    rows = _rows(sheet)
    rows[1][1] = "fail"
    with open(sheet, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    with pytest.raises(ValueError, match="a sheet for the pass_fail scale"):
        import_labelling_sheet(sheet, scale="pass_medium_fail")
    assert import_labelling_sheet(sheet, scale="pass_fail") == {"a": "fail"}


def test_sheet_edge_cases(tmp_path):
    utf16 = tmp_path / "unicode.txt"            # Excel's "Unicode text": UTF-16, tab-separated
    utf16.write_bytes("scenario_name\tlabel\nø\tfail\n".encode("utf-16"))
    assert import_labelling_sheet(utf16, scale="pass_fail") == {"ø": "fail"}
    cr_only = tmp_path / "mac.csv"              # old Mac line endings, prose with commas
    cr_only.write_bytes(b"scenario_name;label;scenario\ra;pass;one, two, three, four\r")
    assert import_labelling_sheet(cr_only, scale="pass_fail") == {"a": "pass"}
    no_name = tmp_path / "noname.csv"
    no_name.write_text("scenario_name,label\n,fail\n", encoding="utf-8")
    with pytest.raises(ValueError, match="has no scenario_name"):
        import_labelling_sheet(no_name, scale="pass_fail")


def test_a_transcript_longer_than_the_csv_field_limit_is_read(tmp_path):
    sheet = tmp_path / "l.csv"
    sheet.write_text('scenario_name,label (pass | fail),transcript\na,p,"' + "x" * 140_000 + '"\n', encoding="utf-8")
    before = csv.field_size_limit()
    assert import_labelling_sheet(sheet, scale="pass_fail") == {"a": "pass"}
    assert csv.field_size_limit() == before        # the limit is raised for that read only


def test_labels_files_from_before_scales_are_severities(tmp_path):
    path = tmp_path / "old.labels.json"
    path.write_text(json.dumps({"version": "1.0", "rater": None, "labels": {"s00": "high", "s01": "pass"}}),
                    encoding="utf-8")
    report = calibrate(_verdict_run("pass_fail", PF), path, scale="pass_fail")
    assert report.reference.consensus == {"s00": "fail", "s01": "pass"}


def test_label_on_the_ladder_still_loads_a_file_with_a_typo(tmp_path):
    out = tmp_path / "labels.json"
    out.write_text(json.dumps({"a": "hihg"}), encoding="utf-8")
    labels = label(_severity_run({"a": "pass", "b": "pass"}), out, input_fn=lambda _: "q", print_fn=lambda _: None)
    assert labels == {"a": "hihg"}
