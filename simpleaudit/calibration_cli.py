"""
``simpleaudit calibrate``: check a verdict judge against people's labels.

Each judge with a verdict scale (``pass_fail``, ``pass_medium_fail``; see
:mod:`simpleaudit.judges.verdict`) has its own command. The steps, for a
stored run ``run.json``::

    simpleaudit calibrate pass_fail label  run.json --rater anna
    simpleaudit calibrate pass_fail export run.json --rater ola       # a CSV sheet instead
    simpleaudit calibrate pass_fail import run.json run.pass_fail.ola.csv --rater ola
    simpleaudit calibrate pass_fail report run.json --judge-model gpt-4.1-mini --provider openai

Labels are blind: neither the terminal nor the sheet shows the judge's
verdict, and both show the rules the judge is given. Each rater's labels go
to ``run.pass_fail.<rater>.labels.json`` next to the run, so several people
can label the same run and the report shows how well they agree with each
other, the ceiling to read the judge against.

``report`` scores the judge against every rater's labels. When the run was
graded by another judge, ``--judge-model`` grades its stored transcripts
again with this one (judge tokens only; the target is not called) and saves
the result next to the run, so a second report costs nothing. The run's own
judge is then scored on the same labels too, for comparison.

Everything here is a thin layer over :func:`simpleaudit.calibration.label`,
:func:`~simpleaudit.calibration.export_labelling_sheet`,
:func:`~simpleaudit.calibration.import_labelling_sheet`,
:func:`~simpleaudit.calibration.calibrate` and
:func:`simpleaudit.reframing.rejudge`, which do the same from Python.
"""

from __future__ import annotations

import argparse
import glob
import re
import sys
import warnings
from collections.abc import Sequence
from pathlib import Path

from .calibration import (
    _read_labels,
    calibrate,
    export_labelling_sheet,
    get_scale,
    import_labelling_sheet,
    label,
    load_labels,
    save_labels,
)
from .judges import JUDGE_CONFIGS
from .results import AuditResults


def calibratable_judges() -> dict[str, str]:
    """``{judge name: scale}`` for the registered judges this command can calibrate."""
    return {
        name: config["verdict_scale"]
        for name, config in JUDGE_CONFIGS.items()
        if config.get("verdict_scale")
    }


def _slug(text: str) -> str:
    """*text* as part of a file name: letters, digits, ``.``, ``_`` and ``-``."""
    return re.sub(r"[^\w.-]+", "_", text.strip(), flags=re.UNICODE).strip("._")


def _rater_slug(rater: str) -> str:
    # Lower case, so "Ola" and "ola" get the same file on every file system
    # (macOS and Windows would give them the same file anyway).
    return _slug(rater).lower()


def labels_path(results: Path, scale: str, rater: str) -> Path:
    """Where one rater's labels for *results* on *scale* are kept."""
    return results.with_name(f"{results.stem}.{scale}.{_rater_slug(rater)}.labels.json")


def sheet_path(results: Path, scale: str, rater: str) -> Path:
    """Default labelling sheet for one rater."""
    return results.with_name(f"{results.stem}.{scale}.{_rater_slug(rater)}.csv")


def regraded_path(results: Path, judge: str, judge_model: str) -> Path:
    """Where the run graded again by *judge* on *judge_model* is kept."""
    return results.with_name(f"{results.stem}.{judge}.{_slug(judge_model.replace('/', '-'))}.json")


def _rater(value: str) -> str:
    if not _slug(value):
        raise argparse.ArgumentTypeError("the rater name needs at least one letter or digit")
    return value.strip()


def _parser() -> argparse.ArgumentParser:
    judges = calibratable_judges()
    parser = argparse.ArgumentParser(
        prog="simpleaudit calibrate",
        description=(
            "Check a verdict judge against people's labels on the same scale. "
            "Label a stored run (in the terminal, or in a CSV sheet), then report "
            "how often the judge agrees with the raters."
        ),
        epilog="Example: simpleaudit calibrate pass_fail label run.json --rater anna",
    )
    parser.add_argument("judge", choices=sorted(judges), help="the judge to calibrate")
    steps = parser.add_subparsers(dest="step", required=True, metavar="STEP")

    p = steps.add_parser("label", help="label the run's transcripts in the terminal")
    p.add_argument("results", type=Path, help="a results file saved by AuditResults.save()")
    p.add_argument("--rater", required=True, type=_rater, help="your name; each rater has a labels file")
    p.add_argument("--relabel", action="store_true", help="ask again for transcripts you labelled")
    p.add_argument("--no-expected", action="store_true", help="do not show the scenario's expected behaviour")

    p = steps.add_parser("export", help="write a CSV labelling sheet for a rater")
    p.add_argument("results", type=Path, help="a results file saved by AuditResults.save()")
    p.add_argument("--rater", required=True, type=_rater, help="who will fill in the sheet")
    p.add_argument("--out", type=Path, help="the CSV file (default: next to the run)")
    p.add_argument("--delimiter", default=",", choices=[",", ";"],
                   help="';' for Excel set to a language with decimal commas (default: ',')")
    p.add_argument("--no-expected", action="store_true", help="leave out the scenario's expected behaviour")

    p = steps.add_parser("import", help="read a filled-in sheet back as the rater's labels")
    p.add_argument("results", type=Path, help="a results file saved by AuditResults.save()")
    p.add_argument("sheet", type=Path, help="the CSV sheet the rater filled in")
    p.add_argument("--rater", required=True, type=_rater, help="who filled in the sheet")

    p = steps.add_parser("report", help="score the judge against every rater's labels")
    p.add_argument("results", type=Path, help="a results file saved by AuditResults.save()")
    p.add_argument("--labels", type=Path, action="append", default=[],
                   help="a labels file to use besides the raters' files next to the run (repeatable)")
    p.add_argument("--judge-model", help="grade the run with this judge model first, if it was not graded by the judge")
    p.add_argument("--provider", default="openai", help="the judge model's any-llm provider (default: openai)")
    p.add_argument("--base-url", help="the judge model's API base URL")
    p.add_argument("--concurrency", type=int, default=4, help="judge calls at once (default: 4)")
    p.add_argument("--pack", help="the scenario pack the run came from, for the scenarios' severities "
                                  "(default: the pack the run records)")
    p.add_argument("--regrade", action="store_true",
                   help="grade the run again even when grades from an earlier report are kept")
    p.add_argument("--save-dir", type=Path, default=Path("results/calibration"),
                   help="where reports are saved (default: results/calibration)")
    return parser


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _owner_conflict(path: Path, rater: str) -> str | None:
    """A message when *path* already holds the labels of a rater with another name."""
    if not path.exists():
        return None
    owner = _read_labels(path)[2]
    if owner is None or owner == rater:
        return None
    return (
        f"{path} holds the labels of rater {owner!r}. Use another --rater name, or exactly "
        f"{owner!r} to add to theirs."
    )


def _cmd_label(args: argparse.Namespace, scale: str) -> int:
    out = labels_path(args.results, scale, args.rater)
    conflict = _owner_conflict(out, args.rater)
    if conflict:
        return _fail(conflict)
    label(str(args.results), out, rater=args.rater, relabel=args.relabel,
          show_expected=not args.no_expected, scale=scale)
    return 0


def _cmd_export(args: argparse.Namespace, scale: str) -> int:
    out = args.out or sheet_path(args.results, scale, args.rater)
    path = export_labelling_sheet(str(args.results), out, scale=scale, delimiter=args.delimiter,
                                  show_expected=not args.no_expected)
    instructions = path.with_name(path.stem + ".instructions.txt")
    print(f"Wrote {path}")
    print(f"  and {instructions}")
    print(f"Send both to {args.rater}. When the sheet comes back:")
    print(f"  simpleaudit calibrate {args.judge} import {args.results} {path} --rater {args.rater!r}")
    return 0


def _cmd_import(args: argparse.Namespace, scale: str) -> int:
    labels = import_labelling_sheet(args.sheet, scale=scale)
    known = {r.scenario_name for r in AuditResults.load(str(args.results)).results}
    unknown = sorted(set(labels) - known)
    if unknown:
        return _fail(
            f"{len(unknown)} scenario name(s) in {args.sheet} are not in {args.results}, so the "
            f"sheet is for another run or the names were edited: {unknown[:5]}"
        )
    out = labels_path(args.results, scale, args.rater)
    conflict = _owner_conflict(out, args.rater)
    if conflict:
        return _fail(conflict)
    existing = load_labels(out) if out.exists() else {}
    changed = sum(1 for k, v in labels.items() if k in existing and existing[k] != v)
    added = sum(1 for k in labels if k not in existing)
    merged = {**existing, **labels}
    save_labels(merged, out, source=str(args.results), rater=args.rater, scale=scale)
    print(f"Read {len(labels)} labels from {args.sheet} ({added} new, {changed} changed).")
    print(f"{out} now holds {len(merged)} of {len(known)} transcripts.")
    return 0


def _pack_severities(pack: str | None) -> dict[str, str] | None:
    """``{scenario: designed severity}`` for *pack*, or None when it cannot be loaded."""
    if not pack:
        return None
    from .checklist import severity_by_name
    from .scenarios import get_scenarios

    try:
        return severity_by_name(get_scenarios(pack))
    except (ValueError, KeyError, OSError) as exc:
        print(f"note: could not load pack {pack!r} for the scenarios' severities ({exc}); "
              "a fail is stored as high.")
        return None


def _subject(args: argparse.Namespace, results: AuditResults) -> tuple[str | None, str | None]:
    """``(results file graded by the judge, judge model)``; ``(None, message)`` when there is none."""
    graded_by = results.meta.get("judge_name")
    stored_model = results.meta.get("judge_model")
    if graded_by == args.judge and args.judge_model in (None, stored_model):
        return str(args.results), stored_model
    if args.judge_model is None:
        graded = f"judge {graded_by!r}" if graded_by else "no judge name (the default judge or a custom prompt)"
        return None, (
            f"{args.results} was not graded by {args.judge}: it records {graded}. "
            f"Pass --judge-model (and --provider) to grade its transcripts with {args.judge}; "
            "that costs judge tokens only, the target is not called."
        )
    cached = regraded_path(args.results, args.judge, args.judge_model)
    if cached.exists() and not args.regrade:
        if cached.stat().st_mtime >= args.results.stat().st_mtime:
            print(f"Using the {args.judge} grades in {cached} (--regrade to grade again).")
            return str(cached), args.judge_model
        print(f"{args.results} is newer than the grades in {cached}; grading again.")
    from .reframing import make_judge_client, rejudge

    print(f"Grading {len(results.results)} transcripts with {args.judge} on {args.judge_model} ...")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        regraded = rejudge(
            results,
            make_judge_client(args.provider, base_url=args.base_url),
            args.judge_model,
            judge=args.judge,
            max_concurrency=args.concurrency,
            max_retries=2,
            scenario_severities=_pack_severities(args.pack or results.meta.get("scenario_pack")),
        )
    for warning in caught:
        if "default designed severity" in str(warning.message):
            # rejudge() names the Python argument; here it is --pack.
            print("note: the run does not record its scenarios' severities, so a fail is stored as "
                  "high. Pass --pack to use the pack's. The report compares verdicts and is not affected.")
        else:
            warnings.warn_explicit(warning.message, warning.category, warning.filename, warning.lineno)
    # rejudge() names the judge only when the run had metadata to carry over.
    regraded.meta.setdefault("judge_name", args.judge)
    regraded.meta.setdefault("judge_model", args.judge_model)
    # A call that failed (a wrong key, a provider down) says nothing about the
    # judge. Grades with such failures are not kept, so the next report tries
    # again instead of reusing them.
    failed = [r for r in regraded.results if isinstance(r.judgment, dict) and r.judgment.get("error")]
    if len(failed) == len(regraded.results):
        return None, f"every judge call failed; the first: {failed[0].issues_found[0] if failed[0].issues_found else 'no detail'}"
    if failed:
        print(f"note: {len(failed)} of {len(regraded.results)} judge calls failed and count as ERROR. "
              "These grades are not kept; the next report grades again.")
        # Saved apart from the cache, so the report can name a file.
        failed_path = cached.with_name(cached.stem + ".incomplete.json")
        regraded.save(str(failed_path))
        return str(failed_path), args.judge_model
    regraded.save(str(cached))
    return str(cached), args.judge_model


def _label_files(args: argparse.Namespace, scale: str) -> list[Path]:
    pattern = glob.escape(str(args.results.with_name(args.results.stem))) + f".{scale}.*.labels.json"
    files: list[Path] = []
    seen: set[Path] = set()
    # One file given twice (found here and passed with --labels, or by two
    # spellings of its path) must not count as two raters who always agree.
    for path in [Path(p) for p in sorted(glob.glob(pattern))] + list(args.labels):
        key = path.resolve()
        if key not in seen:
            seen.add(key)
            files.append(path)
    return files


def _cmd_report(args: argparse.Namespace, scale: str) -> int:
    files = _label_files(args, scale)
    if not files:
        return _fail(
            f"no labels for {args.results} on the {scale} scale yet. Label first:\n"
            f"  simpleaudit calibrate {args.judge} label {args.results} --rater NAME"
        )
    print("Labels: " + ", ".join(str(f) for f in files))
    results = AuditResults.load(str(args.results))
    subject, model_or_message = _subject(args, results)
    if subject is None:
        return _fail(model_or_message or "")
    print()
    report = calibrate(subject, files, scale=scale, judge_model=model_or_message)
    report.summary()
    print(f"\nsaved {report.save(directory=args.save_dir)}")
    if subject != str(args.results):
        own = results.meta.get("judge_name")
        print(f"\nFor comparison, the run's own judge{f' ({own})' if own else ''} on the same labels, "
              f"read on the {scale} scale:\n")
        try:
            other = calibrate(str(args.results), files, scale=scale, judge_model=results.meta.get("judge_model"))
        except ValueError as exc:
            print(f"  not comparable: {exc}")
        else:
            other.summary()
            print(f"\nsaved {other.save(directory=args.save_dir)}")
    return 0


_STEPS = {"label": _cmd_label, "export": _cmd_export, "import": _cmd_import, "report": _cmd_report}


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``simpleaudit calibrate``; returns the exit status."""
    args = _parser().parse_args(argv)
    scale = get_scale(calibratable_judges()[args.judge]).name
    if not args.results.exists():
        return _fail(f"{args.results} does not exist")
    try:
        return _STEPS[args.step](args, scale)
    except (ValueError, OSError) as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        print("\nStopped. Labels given so far are saved.")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
