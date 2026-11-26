"""
Calibrate a judge against a reference: human labels, an answer key, or a judge
from another model family.

Every other judge check in SimpleAudit compares the judge with itself
(resampling, reframing, perturbations) or with another model (judge swap,
panel). None of them says whether the judge is *right*. This module scores
the verdicts a judge gave on stored transcripts against a reference that is
independent of it, and reports agreement in a form that is honest about how
few labels there are.

Three references are supported, in order of quality:

- **Human labels.** A dict of ``{scenario_name: severity}`` for some or all
  of the transcripts in a results file. A few labels are better than none;
  every number carries its n and an interval, and the report says when the
  count is too low to be more than indicative.
- **An answer key.** For scenarios with a closed question and an accepted answer
  (a ``decision`` block), the key itself says what each reply deserves. No labelling
  is needed, and the set can hold as many wrong answers as right ones. See
  :mod:`simpleaudit.answer_key`, which builds the replies and the reference.
- **A judge from another model family.** The same transcripts re-graded by
  a model whose training lineage differs from the subject judge's (Claude
  versus GPT, Gemini versus Qwen, ...). Two judges from the same family
  share blind spots, so their agreement says little; the call refuses that
  pairing. Cross-family agreement is weak but real evidence, and it costs
  judge tokens only.

The three calls::

    from simpleaudit.calibration import label, calibrate

    labels = label("results/nav_aap.json")            # interactive, saves as it goes
    calibrate("results/nav_aap.json", labels).summary()

    ref = rejudge("results/nav_aap.json", other_client, "gpt-5.2")
    calibrate("results/nav_aap.json", ref, judge_model="claude-haiku-4-5").summary()

Judge and reference are compared on a :class:`Scale`: the five-level
severity ladder by default, or the two- and three-level scales of the
verdict judges (``scale="pass_fail"``, ``scale="pass_medium_fail"``; see
:mod:`simpleaudit.judges.verdict`). On those, a rater is shown the rules the
judge is given, and a severity judge's verdicts are read through the scale,
so the default judge and a verdict judge can be scored on one set of labels.
:func:`export_labelling_sheet` and :func:`import_labelling_sheet` do the
labelling in a spreadsheet instead of the terminal, and ``simpleaudit
calibrate <judge>`` (:mod:`simpleaudit.calibration_cli`) runs the whole check
from the command line.

Nothing here calls a model. The reference judge is produced by
:func:`simpleaudit.reframing.rejudge` or :func:`simpleaudit.reframing.reframing_check`.
"""

from __future__ import annotations

import csv
import io
import json
import random
import re
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from .results import AuditResults, _atomic_json_dump
from .stats import gwet_ac, krippendorff_alpha, wilson_interval
from .utils import SEVERITY_ORDER, UNGRADED, normalize_severity

#: Below this many compared items the report prints "indicative only" and
#: withholds chance-corrected agreement, which is noise at that size.
MIN_ITEMS_FOR_ALPHA = 10

#: Value a rater writes to leave an item out without deleting the row.
UNSURE = "unsure"

#: 1.1: labels files and reports record the scale they are on.
LABELS_FORMAT_VERSION = "1.1"
REPORT_FORMAT_VERSION = "1.1"

_SHORTCUTS = {"p": "pass", "l": "low", "m": "medium", "h": "high", "c": "critical"}


# ---------------------------------------------------------------------------
# Scales
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Scale:
    """The levels a judge and its raters grade on, best first.

    ``severity`` is the five-level ladder every judge's ``result.severity``
    is on. ``pass_fail`` and ``pass_medium_fail`` are the scales of the
    verdict judges (:mod:`simpleaudit.judges.verdict`): their verdicts are
    read from ``judgment["verdict"]``, and a severity judge's verdicts are
    read through ``reads["severity"]``, so the default judge can be scored
    against the same labels.

    Attributes:
        name: the scale's name, also ``verdict_scale`` in a verdict judge's judgment.
        levels: the levels, best first.
        shortcuts: one-letter answers a rater may type.
        severe: reference levels whose miss is reported as severe ...
        missed: ... when the judge gave one of these instead.
        reads: ``{other scale: {its level: level here}}`` for labels and
            verdicts recorded on another scale.
        criteria: the built-in verdict judge's criteria, shown to a rater
            unless other criteria are given. ``None`` for the severity ladder.
        definitions: what each level means, as the judge is told; always
            shown to a rater on this scale. ``None`` for the severity ladder.
    """

    name: str
    levels: Tuple[str, ...]
    shortcuts: Mapping[str, str]
    severe: Tuple[str, ...]
    missed: Tuple[str, ...]
    reads: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    criteria: Optional[str] = None
    definitions: Optional[str] = None

    def rubric(self, criteria: Optional[str] = None) -> Optional[str]:
        """What a rater is shown: *criteria* (default: the scale's own), then the level definitions."""
        criteria = criteria if criteria is not None else self.criteria
        parts = [p.strip() for p in (criteria, self.definitions) if p and p.strip()]
        if not parts:
            return None
        intro = "The judge model is given these rules. Label by the same rules.\n\n" if self.definitions else ""
        return intro + "\n\n".join(parts)

    @property
    def index(self) -> Dict[str, int]:
        return {level: i for i, level in enumerate(self.levels)}

    def read(self, value: str, scale: Optional[str]) -> str:
        """*value*, recorded on *scale*, as a level of this scale; off-scale values unchanged."""
        if scale is None or scale == self.name:
            return value
        if scale not in self.reads:
            raise ValueError(
                f"labels or verdicts on the {scale!r} scale cannot be read on the {self.name!r} scale"
                + (f"; it reads {sorted(self.reads)}" if self.reads else "")
                + "."
            )
        return self.reads[scale].get(value, value)


def _verdict_scales() -> Dict[str, Scale]:
    from .judges.verdict import (
        PASS_FAIL_DEFINITIONS,
        PASS_FAIL_JUDGE,
        PASS_FAIL_LEVELS,
        PASS_MEDIUM_FAIL_AS_PASS_FAIL,
        PASS_MEDIUM_FAIL_DEFINITIONS,
        PASS_MEDIUM_FAIL_JUDGE,
        PASS_MEDIUM_FAIL_LEVELS,
        SEVERITY_AS_PASS_FAIL,
        SEVERITY_AS_PASS_MEDIUM_FAIL,
    )

    return {
        "pass_fail": Scale(
            name="pass_fail",
            levels=PASS_FAIL_LEVELS,
            shortcuts={"p": "pass", "f": "fail"},
            severe=("fail",),
            missed=("pass",),
            reads={"severity": SEVERITY_AS_PASS_FAIL, "pass_medium_fail": PASS_MEDIUM_FAIL_AS_PASS_FAIL},
            criteria=PASS_FAIL_JUDGE["criteria"],
            definitions=PASS_FAIL_DEFINITIONS,
        ),
        "pass_medium_fail": Scale(
            name="pass_medium_fail",
            levels=PASS_MEDIUM_FAIL_LEVELS,
            shortcuts={"p": "pass", "m": "medium", "f": "fail"},
            severe=("fail",),
            missed=("pass",),
            reads={"severity": SEVERITY_AS_PASS_MEDIUM_FAIL},
            criteria=PASS_MEDIUM_FAIL_JUDGE["criteria"],
            definitions=PASS_MEDIUM_FAIL_DEFINITIONS,
        ),
    }


SEVERITY_SCALE = Scale(
    name="severity",
    levels=tuple(SEVERITY_ORDER),
    shortcuts=_SHORTCUTS,
    severe=("high", "critical"),
    missed=("pass", "low"),
)

#: The scales :func:`label` and :func:`calibrate` accept by name.
SCALES: Dict[str, Scale] = {"severity": SEVERITY_SCALE, **_verdict_scales()}


def get_scale(scale: Union[str, Scale]) -> Scale:
    """A :class:`Scale` by name (``severity``, ``pass_fail``, ``pass_medium_fail``), or as given."""
    if isinstance(scale, Scale):
        return scale
    if scale not in SCALES:
        raise ValueError(f"Unknown scale {scale!r}. Available: {', '.join(SCALES)}")
    return SCALES[scale]


# ---------------------------------------------------------------------------
# Model families
# ---------------------------------------------------------------------------

#: (family, regex on the lowercased model id with any provider prefix removed).
#: Order matters only where a name could match twice; the first wins.
_FAMILY_PATTERNS: List[Tuple[str, str]] = [
    ("anthropic", r"claude"),
    ("openai", r"(^|[^a-z])(gpt|chatgpt|o[1-9](-|$)|davinci|text-embedding)"),
    ("google", r"gemini|gemma|palm|bison"),
    ("meta", r"(^|[^a-z])llama"),   # not "ollama"
    ("mistral", r"mistral|mixtral|codestral|ministral|magistral|pixtral"),
    ("qwen", r"qwen|qwq"),
    ("deepseek", r"deepseek"),
    ("microsoft", r"(^|[^a-z])phi[-_ ]?[0-9]"),
    ("cohere", r"(^|[^a-z])command(-|$)|aya"),
    ("xai", r"grok"),
    ("amazon", r"(^|[^a-z])nova(-|$)|titan"),
    ("nvidia", r"nemotron"),
    ("ai21", r"jamba"),
]


def model_family(model: Optional[str]) -> Optional[str]:
    """Training lineage of a model id, or ``None`` when it is not recognised.

    The whole id is searched, so a provider prefix or a size tag
    (``anthropic/claude-...``, ``openrouter/google/gemini-...``,
    ``ollama:qwen3:8b``) does not hide the family. An unrecognised id gets
    ``None``: the caller decides whether to warn or refuse.
    """
    if not model:
        return None
    text = str(model).strip().lower()
    for family, pattern in _FAMILY_PATTERNS:
        if re.search(pattern, text):
            return family
    return None


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def _normalize_label(
    value: Any, scenario: str, source: str, scale: Scale = SEVERITY_SCALE
) -> Optional[str]:
    """One reference rating onto *scale*; ``None`` means "no rating".

    Humans write ``unsure`` or leave a blank to skip an item. A judge writes
    ``ERROR`` or ``ungraded`` when it did not grade one. Both drop out of the
    comparison; anything else off the scale is a typo and raises, because a
    silently dropped label would make the report look better than it is.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", UNSURE, "skip", "error", UNGRADED):
        return None
    text = scale.shortcuts.get(text, text)
    if text in scale.levels:
        return text
    if scale is SEVERITY_SCALE:
        raise ValueError(
            f"{source}: label {value!r} for {scenario!r} is not a severity. "
            f"Use one of {SEVERITY_ORDER} or {UNSURE!r} to leave the item out."
        )
    raise ValueError(
        f"{source}: label {value!r} for {scenario!r} is not one of {', '.join(scale.levels)} "
        f"(the {scale.name} scale). Use {UNSURE!r} to leave the item out."
    )


def _read_labels(path: Union[str, Path]) -> Tuple[Dict[str, Any], Optional[str], Optional[str]]:
    """``(labels, scale, rater)`` from a labels file; ``None`` for what the file does not say."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "labels" in data and isinstance(data["labels"], dict):
        scale, rater = data.get("scale"), data.get("rater")
        if scale is None and data.get("version") == "1.0":
            scale = "severity"   # label() wrote 1.0 files before scales existed
        return (
            dict(data["labels"]),
            scale if isinstance(scale, str) else None,
            rater if isinstance(rater, str) and rater.strip() else None,
        )
    if isinstance(data, dict) and all(isinstance(v, (str, type(None))) for v in data.values()):
        return dict(data), None, None
    raise ValueError(
        f"{path}: expected a {{scenario_name: label}} mapping or a labels file "
        "with a 'labels' key."
    )


def load_labels(path: Union[str, Path]) -> Dict[str, str]:
    """Read a labels file written by :func:`label` (or by hand).

    The file is JSON, either a bare ``{scenario_name: label}`` mapping or
    the object :func:`label` writes, which holds that mapping under
    ``"labels"`` and the scale it is on under ``"scale"``. Returns the
    mapping; ``unsure`` entries are kept so the report can count them.
    """
    return _read_labels(path)[0]


def save_labels(
    labels: Mapping[str, Any],
    path: Union[str, Path],
    *,
    source: Optional[str] = None,
    rater: Optional[str] = None,
    scale: Optional[Union[str, Scale]] = None,
) -> None:
    """Write labels as the JSON object :func:`load_labels` reads back.

    *scale* is recorded so :func:`calibrate` can refuse, or convert, labels
    given on another scale than the one it scores.
    """
    payload: Dict[str, Any] = {
        "version": LABELS_FORMAT_VERSION,
        "source": source,
        "rater": rater,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "labels": dict(labels),
    }
    if scale is not None:
        payload["scale"] = get_scale(scale).name
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    _atomic_json_dump(payload, str(path))


def _render_transcript(result: Any) -> str:
    lines: List[str] = []
    for turn in result.conversation:
        role = str(turn.get("role", "?")).upper()
        content = turn.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        lines.append(f"[{role}]\n{content}\n")
    # A reply can hold a lone surrogate (half an emoji), which a results file
    # keeps but neither a terminal nor a UTF-8 sheet can write; show it as \udXXX.
    return "\n".join(lines).encode("utf-8", "backslashreplace").decode("utf-8")


def _existing_labels(path: Path, scale: Scale) -> Dict[str, str]:
    """Labels already in *path*, refused when they are on another scale than *scale*."""
    labels, recorded, _ = _read_labels(path)
    if recorded is not None and recorded != scale.name:
        raise ValueError(
            f"{path} holds labels on the {recorded!r} scale; labelling on {scale.name!r} would "
            "mix the two. Use another file."
        )
    if recorded is None and scale is not SEVERITY_SCALE:
        # A file that does not say is taken to be on the scale asked for; make
        # sure it is, so severities are not mixed into it. (On the ladder a file
        # is loaded unchecked, as before scales existed.)
        for name, value in labels.items():
            _normalize_label(value, name, str(path), scale)
    return labels


def label(
    results_or_path: Union[AuditResults, str, Path],
    out: Optional[Union[str, Path]] = None,
    *,
    rater: Optional[str] = None,
    relabel: bool = False,
    show_expected: bool = True,
    scale: Union[str, Scale] = "severity",
    criteria: Optional[str] = None,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[[str], None] = print,
) -> Dict[str, str]:
    """Label stored transcripts one at a time in the terminal.

    Prints the scenario, what the target was expected to do, and the full
    transcript, then asks for a label on *scale*. The judge's own verdict is
    never shown, so the label is blind. Each answer is written to *out* at
    once, so stopping after six items and picking up tomorrow loses nothing.

    On the severity ladder (the default) the answers are ``pass``, ``low``,
    ``medium``, ``high``, ``critical`` or the first letter. On ``pass_fail``
    they are ``pass`` and ``fail`` (``p``, ``f``), on ``pass_medium_fail``
    ``pass``, ``medium`` and ``fail`` (``p``, ``m``, ``f``); the rules the
    judge is given are printed first, and ``?`` prints them again. On every
    scale ``unsure`` leaves the item out, an empty line skips it for now,
    and ``q`` (or the end of input, Ctrl-D) stops.

    Args:
        results_or_path: a results file or object. With an object, *out*
            is required.
        out: where to save; defaults to ``<results stem>.labels.json`` next
            to the results file (``<results stem>.<scale>.labels.json`` off
            the severity ladder).
        rater: a name recorded in the file, for when several people label.
        relabel: ask again for items the file already holds.
        show_expected: print the scenario's ``expected_behavior`` list.
        scale: ``"severity"``, ``"pass_fail"``, ``"pass_medium_fail"`` or a
            :class:`Scale`. The file records it; a file on another scale is
            refused rather than mixed.
        criteria: the judge's criteria to show the rater; defaults to the
            scale's own (the built-in verdict judge's). Pass the criteria of a
            judge built with ``customize_judge`` when labelling for that judge.
            The scale's level definitions are shown after them either way.
        input_fn, print_fn: injectable for tests and for other front ends.

    Returns:
        The labels now in the file, ``{scenario_name: label}``.
    """
    scale = get_scale(scale)
    rubric = scale.rubric(criteria)
    if isinstance(results_or_path, AuditResults):
        if out is None:
            raise ValueError("label() needs out= when given an AuditResults object.")
        results = results_or_path
        source = None
    else:
        source = str(results_or_path)
        results = AuditResults.load(source)
        if out is None:
            p = Path(source)
            suffix = ".labels.json" if scale is SEVERITY_SCALE else f".{scale.name}.labels.json"
            out = p.with_name(p.stem + suffix)
    out = Path(out)
    labels: Dict[str, str] = _existing_labels(out, scale) if out.exists() else {}

    total = len(results.results)
    print_fn(f"Labelling {total} transcripts -> {out}")
    if rubric:
        print_fn(rubric.rstrip() + "\n")
    answers = " / ".join(scale.levels) + " (or " + " ".join(scale.shortcuts) + ")"
    print_fn(f"Answer {answers}, 'unsure' to leave out, "
             + ("'?' to show the rules again, " if rubric else "")
             + "Enter to skip, q to stop.\n")
    for i, result in enumerate(results.results, 1):
        name = result.scenario_name
        if name in labels and not relabel:
            continue
        print_fn("=" * 72)
        print_fn(f"[{i}/{total}] {name}")
        if result.scenario_description:
            print_fn(f"Scenario: {result.scenario_description}")
        if show_expected and result.expected_behavior:
            print_fn("Expected:")
            for item in result.expected_behavior:
                print_fn(f"  - {item}")
        print_fn("-" * 72)
        print_fn(_render_transcript(result))
        prompt = "severity> " if scale is SEVERITY_SCALE else "verdict> "
        while True:
            try:
                answer = input_fn(prompt).strip().lower()
            except EOFError:
                # Ctrl-D, or piped answers that ran out: stop, as q does.
                print_fn("")
                answer = "q"
            if answer == "q":
                print_fn(f"Stopped. {len(labels)} labels in {out}")
                return labels
            if answer == "":
                break
            if answer == "?" and rubric:
                print_fn(rubric.rstrip() + "\n")
                continue
            try:
                value = _normalize_label(answer, name, "label()", scale)
            except ValueError as exc:
                print_fn(str(exc))
                continue
            labels[name] = UNSURE if value is None else value
            save_labels(labels, out, source=source, rater=rater, scale=scale)
            break
    print_fn(f"Done. {len(labels)} labels in {out}")
    return labels


# ---------------------------------------------------------------------------
# Labelling sheets: the same labelling in a spreadsheet
# ---------------------------------------------------------------------------

#: Columns of a labelling sheet. Only scenario_name and label are read back.
#: In the sheet the label column is headed with the scale's levels, e.g.
#: ``label (pass | fail)``, which tells the rater the answers and lets
#: :func:`import_labelling_sheet` refuse a sheet made for another scale.
SHEET_COLUMNS = ("scenario_name", "label", "scenario", "expected_behavior", "transcript")

#: Excel holds at most this many characters in a cell. Only the transcript
#: can be that long.
_EXCEL_CELL_LIMIT = 32767


def _label_header(scale: Scale) -> str:
    return f"label ({' | '.join(scale.levels)})"


def _sheet_instructions(scale: Scale, rubric: Optional[str]) -> str:
    answers = ", ".join(scale.levels)
    lines = [
        f"Labelling sheet: one row per transcript, on the {scale.name} scale.",
        "",
        f"Write one of {answers} in the label column (or the first letter), "
        f"{UNSURE!r} to leave a transcript out, or nothing to skip it for now.",
        "Do not edit scenario_name or the header row: they are how a label finds its transcript.",
        "The other columns are for reading. The judge's own verdict is not in the sheet.",
        "Save the file as CSV (UTF-8 if you are asked) and send it back.",
    ]
    if rubric:
        lines += ["", "=" * 72, "", rubric.rstrip()]
    return "\n".join(lines) + "\n"


def export_labelling_sheet(
    results_or_path: Union[AuditResults, str, Path],
    path: Union[str, Path],
    *,
    scale: Union[str, Scale] = "severity",
    criteria: Optional[str] = None,
    show_expected: bool = True,
    delimiter: str = ",",
) -> Path:
    """Write the transcripts of a run as a CSV sheet for a rater to fill in.

    The spreadsheet route to what :func:`label` does in the terminal, for
    raters who will not use one. One row per transcript with an empty label
    column, headed with the answers allowed (``label (pass | fail)``); the
    judge's verdict is not in the sheet, so the label is blind. The file is
    UTF-8 with a byte order mark, which Excel needs to show non-ASCII text
    such as æ, ø and å. Next to it, ``<sheet stem>.instructions.txt`` holds
    the answers and the rules the judge is given: send both.

    Read the filled-in sheet back with :func:`import_labelling_sheet`.

    Args:
        results_or_path: a results file or object.
        path: the CSV file to write.
        scale: the scale the rater labels on.
        criteria: the judge's criteria for the instructions, as for
            :func:`label`; the scale's level definitions follow them.
        show_expected: include the scenario's ``expected_behavior``.
        delimiter: the field separator. Excel set to a language that writes
            decimal commas (Norwegian, German, ...) splits columns on ``;``
            when it opens a CSV file directly; use ``";"`` for those raters,
            or have them open the file with Data > From Text/CSV.

    Returns:
        The path of the CSV file.

    Raises:
        ValueError: when two transcripts share a scenario name, since their
            labels could not be told apart.
    """
    scale = get_scale(scale)
    results = results_or_path if isinstance(results_or_path, AuditResults) else AuditResults.load(str(results_or_path))
    names = [r.scenario_name for r in results.results]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(
            f"duplicate scenario names {dupes}: labels are keyed by scenario name, so "
            "export one run per sheet."
        )
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    long_rows = []
    # backslashreplace, as results files are written: a run that saves and
    # loads must also export.
    with open(path, "w", encoding="utf-8-sig", errors="backslashreplace", newline="") as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow([_label_header(scale) if c == "label" else c for c in SHEET_COLUMNS])
        for result in results.results:
            expected = ""
            if show_expected and result.expected_behavior:
                expected = "\n".join(f"{i}. {item}" for i, item in enumerate(result.expected_behavior, 1))
            transcript = _render_transcript(result)
            if len(transcript) > _EXCEL_CELL_LIMIT:
                long_rows.append(result.scenario_name)
            writer.writerow([result.scenario_name, "", result.scenario_description or "", expected, transcript])
    path.with_name(path.stem + ".instructions.txt").write_text(
        _sheet_instructions(scale, scale.rubric(criteria)), encoding="utf-8", errors="backslashreplace"
    )
    if long_rows:
        # Not tested: what Excel writes back when it saves such a sheet.
        warnings.warn(
            f"{len(long_rows)} transcript(s) are longer than the {_EXCEL_CELL_LIMIT} characters an "
            f"Excel cell holds, so Excel will not show them in full: {long_rows}. Give raters "
            "who use Excel these transcripts another way, such as the terminal labelling.",
            stacklevel=2,
        )
    return path


def _decode_sheet(data: bytes) -> str:
    """Text of a sheet as a spreadsheet program saved it.

    UTF-16 when it starts with a UTF-16 byte order mark (Excel's "Unicode
    text"), else UTF-8 with or without a BOM, else Windows-1252.
    """
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def import_labelling_sheet(
    path: Union[str, Path],
    *,
    scale: Union[str, Scale] = "severity",
) -> Dict[str, str]:
    """Read a sheet written by :func:`export_labelling_sheet` after a rater filled it in.

    Accepts the sheet as spreadsheet programs save it back: comma-,
    semicolon- or tab-separated, in UTF-8, UTF-16 or Windows-1252, with the
    columns in any order. Only ``scenario_name`` and the label column are
    read. A sheet whose label column names another scale's levels is
    refused; a hand-made sheet headed just ``label`` is read on *scale*. A
    blank label is an item not labelled yet and is left out; ``unsure`` is
    kept so the report can count it. Every label that cannot be read is
    reported at once, with its row, so the rater can fix the sheet in one go.

    Returns:
        ``{scenario_name: label}``, to save with ``save_labels(..., scale=scale)``.
    """
    scale = get_scale(scale)
    text = _decode_sheet(Path(path).read_bytes())
    # The header row holds no separators of its own, so the one it uses most is the one.
    first_line = text.splitlines()[0] if text else ""
    delimiter = max(",;\t", key=first_line.count)
    # A transcript can be longer than the csv module's default field limit
    # (131,072 characters); allow more for this read only.
    old_limit = csv.field_size_limit()
    csv.field_size_limit(max(old_limit, 2**31 - 1))
    try:
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
    except csv.Error as exc:
        raise ValueError(f"{path}: not a readable CSV sheet ({exc})") from None
    finally:
        csv.field_size_limit(old_limit)
    header = [h.strip().lower() for h in (rows[0] if rows else [])]
    label_col = next((i for i, h in enumerate(header) if h == "label" or h.startswith("label (")), None)
    missing = [c for c, col in (("scenario_name", "scenario_name" in header), ("label", label_col is not None)) if not col]
    if missing:
        raise ValueError(f"{path}: no {' or '.join(missing)} column; was the header row changed?")
    if header[label_col] != "label":
        levels = tuple(level.strip() for level in header[label_col][len("label ("):].rstrip(")").split("|"))
        if levels != scale.levels:
            made_for = next((name for name, sc in SCALES.items() if sc.levels == levels), None)
            raise ValueError(
                f"{path} is a sheet for {f'the {made_for}' if made_for else 'another'} scale ({', '.join(levels)}), "
                f"not {scale.name}; import it with the judge it was made for."
            )
    name_col = header.index("scenario_name")
    labels: Dict[str, str] = {}
    problems: List[str] = []
    for row_number, row in enumerate(rows[1:], start=2):
        name = row[name_col].strip() if len(row) > name_col else ""
        value = row[label_col].strip() if len(row) > label_col else ""
        if not value:
            continue
        if not name:
            problems.append(f"row {row_number}: label {value!r} has no scenario_name")
            continue
        try:
            level = _normalize_label(value, name, f"row {row_number}", scale)
        except ValueError as exc:
            problems.append(str(exc))
            continue
        level = UNSURE if level is None else level
        if name in labels and labels[name] != level:
            problems.append(f"row {row_number}: {name!r} is labelled twice, {labels[name]!r} and {level!r}")
            continue
        labels[name] = level
    if problems:
        raise ValueError(f"{path}: {len(problems)} label(s) to fix:\n  " + "\n  ".join(problems))
    return labels


# ---------------------------------------------------------------------------
# References and subjects
# ---------------------------------------------------------------------------

@dataclass
class Rater:
    """One source of reference verdicts: a person, an answer key or a cross-family judge.

    ``scale`` is the scale ``ratings`` are on: the severity ladder for the
    answer key of :mod:`simpleaudit.answer_key`. :func:`calibrate` converts
    a rater's ratings to the scale it scores when that scale can read them.
    """

    name: str
    kind: str                                  # "human" | "answer_key" | "model"
    ratings: Dict[str, Optional[str]]          # scenario -> level on ``scale``, or None
    model: Optional[str] = None
    family: Optional[str] = None
    scale: str = "severity"

    @property
    def n_rated(self) -> int:
        return sum(1 for v in self.ratings.values() if v is not None)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "model": self.model,
                "family": self.family, "n_rated": self.n_rated}


def _is_results_file(path: Path) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return isinstance(data, dict) and isinstance(data.get("results"), list)


def _verdict_of(result: Any, scale: Scale, source: str) -> str:
    """One stored result's verdict as a level of *scale*.

    On the severity ladder that is the result's severity, as it always was.
    On a verdict scale it is ``judgment["verdict"]`` when a verdict judge
    graded the result, read through ``scale.reads`` when that judge used
    another scale; a severity judge's severity is read through
    ``scale.reads["severity"]``. ``ERROR`` and ``ungraded`` stay as they are.
    """
    severity = normalize_severity(result.severity)
    if scale is SEVERITY_SCALE or severity in ("ERROR", UNGRADED):
        return severity
    judgment = result.judgment if isinstance(result.judgment, dict) else {}
    graded_on = judgment.get("verdict_scale")
    try:
        if graded_on is not None:
            verdict = str(judgment.get("verdict") or "").strip().lower()
            return scale.read(verdict, graded_on)
        return scale.read(severity, "severity")
    except ValueError as exc:
        raise ValueError(f"{source}: {result.scenario_name!r}: {exc}") from None


def _verdicts_of(results: AuditResults, source: str, scale: Scale = SEVERITY_SCALE) -> Dict[str, str]:
    """``{scenario_name: verdict on scale}`` for a results object.

    Off-scale values (``ERROR``, ``ungraded``, a custom vocabulary) are kept
    as they are so coverage can count them. Duplicate names (a repeated run
    saved flat) warn, and the last one wins, as elsewhere in the library.
    """
    out: Dict[str, str] = {}
    dupes = set()
    for r in results.results:
        if r.scenario_name in out:
            dupes.add(r.scenario_name)
        out[r.scenario_name] = _verdict_of(r, scale, source)
    if dupes:
        warnings.warn(
            f"{source}: duplicate scenario names {sorted(dupes)}; the last verdict for "
            "each is used. Labels are keyed by scenario name, so label one run per file.",
            stacklevel=3,
        )
    return out


def _labels_on(
    mapping: Mapping[str, Any], recorded: Optional[str], scale: Scale, source: str
) -> Dict[str, Optional[str]]:
    """Labels recorded on *recorded* (``None``: unknown, taken as *scale*) as levels of *scale*."""
    ratings: Dict[str, Optional[str]] = {}
    on_recorded = scale if recorded is None or recorded == scale.name else get_scale(recorded)
    for name, value in mapping.items():
        level = _normalize_label(value, name, source, on_recorded)
        ratings[name] = None if level is None else scale.read(level, on_recorded.name)
    return ratings


def _rater_from(
    ref: Any, index: int, *, reference_model: Optional[str], scale: Scale = SEVERITY_SCALE
) -> Rater:
    """Turn one reference argument into a :class:`Rater` on *scale*."""
    if isinstance(ref, Rater):
        if ref.scale == scale.name:
            return ref
        return Rater(
            name=ref.name, kind=ref.kind, model=ref.model, family=ref.family, scale=scale.name,
            ratings={k: None if v is None else scale.read(v, ref.scale) for k, v in ref.ratings.items()},
        )
    if isinstance(ref, AuditResults):
        model = reference_model or ref.meta.get("judge_model")
        verdicts = _verdicts_of(ref, f"reference {index}", scale)
        return Rater(
            name=f"model:{model or 'unknown'}",
            kind="model",
            ratings={k: (v if v in scale.levels else None) for k, v in verdicts.items()},
            model=model,
            family=model_family(model),
            scale=scale.name,
        )
    if isinstance(ref, (str, Path)):
        path = Path(ref)
        if _is_results_file(path):
            return _rater_from(AuditResults.load(str(path)), index, reference_model=reference_model, scale=scale)
        mapping, recorded, _ = _read_labels(path)
        return Rater(
            name=path.stem,
            kind="human",
            ratings=_labels_on(mapping, recorded, scale, str(path)),
            scale=scale.name,
        )
    if isinstance(ref, Mapping):
        return Rater(
            name=f"rater_{index + 1}",
            kind="human",
            ratings=_labels_on(ref, None, scale, "labels"),
            scale=scale.name,
        )
    raise TypeError(
        f"reference must be a labels dict, a labels file, a Rater, a results file or an "
        f"AuditResults, got {type(ref).__name__}"
    )


def _consensus(values: Sequence[str], scale: Scale = SEVERITY_SCALE) -> str:
    """Median on the scale; an even split resolves to the stricter middle.

    Same conservative tie-break as :func:`simpleaudit.aggregate_severities`:
    when two raters say ``low`` and ``medium``, the item is ``medium``; when
    two say ``pass`` and ``fail``, it is ``fail``.
    """
    index = scale.index
    ranks = sorted(index[v] for v in values)
    mid = len(ranks) // 2
    return scale.levels[ranks[mid]]


@dataclass
class Reference:
    """The consensus the judge is scored against, built from one or more raters."""

    raters: List[Rater]
    consensus: Dict[str, str]                  # items with at least one rating
    split: Dict[str, bool]                     # item -> raters disagreed
    n_unsure: int                              # entries present but unrated by every rater
    scale: Scale = SEVERITY_SCALE

    @property
    def kind(self) -> str:
        kinds = {r.kind for r in self.raters}
        return kinds.pop() if len(kinds) == 1 else "mixed"

    def _rater_units(self) -> List[List[Optional[str]]]:
        units = [[r.ratings.get(name) for r in self.raters] for name in self.consensus]
        return [u for u in units if sum(v is not None for v in u) >= 2]

    def human_ceiling(self) -> Optional[float]:
        """Ordinal alpha among the raters; ``None`` with one rater or too few items."""
        if len(self.raters) < 2:
            return None
        units = self._rater_units()
        if len(units) < MIN_ITEMS_FOR_ALPHA:
            return None
        return krippendorff_alpha(units, "ordinal", list(self.scale.levels))

    def human_ceiling_ac2(self) -> Optional[float]:
        """Gwet's AC2 (linear) among the raters, the companion to :meth:`human_ceiling`."""
        if len(self.raters) < 2:
            return None
        units = self._rater_units()
        if len(units) < MIN_ITEMS_FOR_ALPHA:
            return None
        return gwet_ac(units, "linear", list(self.scale.levels))


def _build_reference(
    refs: Sequence[Any], reference_model: Optional[str], scale: Scale = SEVERITY_SCALE
) -> Reference:
    raters = [_rater_from(ref, i, reference_model=reference_model, scale=scale) for i, ref in enumerate(refs)]
    names: List[str] = []
    for r in raters:
        for name in r.ratings:
            if name not in names:
                names.append(name)
    consensus: Dict[str, str] = {}
    split: Dict[str, bool] = {}
    n_unsure = 0
    for name in names:
        values = [r.ratings[name] for r in raters if r.ratings.get(name) is not None]
        if not values:
            n_unsure += 1
            continue
        consensus[name] = _consensus(values, scale)
        split[name] = len(set(values)) > 1
    return Reference(raters=raters, consensus=consensus, split=split, n_unsure=n_unsure, scale=scale)


# ---------------------------------------------------------------------------
# Agreement
# ---------------------------------------------------------------------------

def _bootstrap(
    stat: Callable[[List[Any]], Optional[float]],
    items: List[Any],
    n_resamples: int = 1000,
    seed: int = 0,
) -> Optional[Tuple[float, float]]:
    """Percentile bootstrap over items; ``None`` when the statistic is undefined."""
    if len(items) < 2:
        return None
    rng = random.Random(seed)
    draws: List[float] = []
    for _ in range(n_resamples):
        sample = [items[rng.randrange(len(items))] for _ in items]
        value = stat(sample)
        if value is not None:
            draws.append(value)
    if len(draws) < n_resamples // 2:
        return None
    draws.sort()
    lo = draws[int(0.025 * (len(draws) - 1))]
    hi = draws[int(0.975 * (len(draws) - 1))]
    return lo, hi


@dataclass
class Agreement:
    """How one judge's verdicts compare with the reference consensus.

    Counts are kept raw; intervals are computed on request so the dataclass
    stays a plain record. ``n_compared`` is the denominator for everything
    except the pass/fail rates, which use the human-fail and human-pass
    subsets.

    Two chance-corrected figures are given because they fail differently.
    ``alpha`` (ordinal Krippendorff) corrects for chance from the observed
    category frequencies, so when nearly every reference item is ``pass`` it
    reads low even at 97% exact agreement. ``ac2`` (Gwet, linear weights)
    does not have that problem and is the one ``leaderboard()`` ranks by;
    ``reference_distribution`` shows the skew that explains a gap between
    the two.
    """

    label: str
    judge_model: Optional[str]
    fail_at: str
    n_reference: int            # items with a consensus
    n_compared: int             # ... that the judge put on the ladder
    n_error: int                # judge said ERROR
    n_ungraded: int             # judge said ungraded / off-ladder
    n_unjudged: int             # reference item not in the judge's output
    exact: int
    within_one: int
    n_stricter: int
    n_lenient: int
    bias_mean: Optional[float]
    bias_interval: Optional[Tuple[float, float]]
    alpha: Optional[float]                      # ordinal Krippendorff's alpha
    alpha_interval: Optional[Tuple[float, float]]
    ac2: Optional[float]                        # Gwet's AC2, linear weights
    ac2_interval: Optional[Tuple[float, float]]
    reference_distribution: Dict[str, int]      # reference level -> count, compared items
    sensitivity: Tuple[int, int]        # judge fail | reference fail
    specificity: Tuple[int, int]        # judge pass | reference pass
    severe_misses: Tuple[int, int]      # judge in scale.missed | reference in scale.severe
    confusion: Dict[str, Dict[str, int]]
    disagreements: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def indicative_only(self) -> bool:
        return self.n_compared < MIN_ITEMS_FOR_ALPHA

    def rate(self, k: int, n: int) -> Dict[str, Any]:
        lo, hi = wilson_interval(k, n)
        return {"k": k, "n": n, "value": (k / n) if n else None, "interval": [lo, hi]}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "label": self.label,
            "judge_model": self.judge_model,
            "fail_at": self.fail_at,
            "indicative_only": self.indicative_only,
            "coverage": {
                "reference": self.n_reference, "compared": self.n_compared,
                "error": self.n_error, "ungraded": self.n_ungraded, "unjudged": self.n_unjudged,
            },
            "exact": self.rate(self.exact, self.n_compared),
            "within_one": self.rate(self.within_one, self.n_compared),
            "bias": {
                "mean": self.bias_mean, "interval": list(self.bias_interval) if self.bias_interval else None,
                "stricter": self.n_stricter, "lenient": self.n_lenient,
            },
            "alpha": {"value": self.alpha, "interval": list(self.alpha_interval) if self.alpha_interval else None},
            "ac2": {"value": self.ac2, "interval": list(self.ac2_interval) if self.ac2_interval else None},
            "reference_distribution": self.reference_distribution,
            "sensitivity": self.rate(*self.sensitivity),
            "specificity": self.rate(*self.specificity),
            "severe_misses": self.rate(*self.severe_misses),
            "confusion": self.confusion,
            "disagreements": self.disagreements,
        }


def _agreement(
    label: str,
    judge_model: Optional[str],
    verdicts: Mapping[str, str],
    consensus: Mapping[str, str],
    fail_at: str,
    scale: Scale = SEVERITY_SCALE,
) -> Agreement:
    index = scale.index
    levels = list(scale.levels)
    fail_rank = index[fail_at]
    n_error = n_ungraded = n_unjudged = 0
    pairs: List[Tuple[str, str, str]] = []     # (scenario, reference, judge)
    confusion: Dict[str, Dict[str, int]] = {s: {c: 0 for c in levels + ["ERROR"]} for s in levels}
    for name, ref in consensus.items():
        if name not in verdicts:
            n_unjudged += 1
            continue
        v = verdicts[name]
        if v == "ERROR":
            n_error += 1
            confusion[ref]["ERROR"] += 1
        elif v not in index:
            n_ungraded += 1
        else:
            pairs.append((name, ref, v))
            confusion[ref][v] += 1

    gaps = [index[v] - index[ref] for _, ref, v in pairs]
    n = len(pairs)
    ref_fail = [(ref, v) for _, ref, v in pairs if index[ref] >= fail_rank]
    ref_pass = [(ref, v) for _, ref, v in pairs if index[ref] < fail_rank]
    ref_severe = [(ref, v) for _, ref, v in pairs if ref in scale.severe]

    def alpha_of(sample: List[Tuple[str, str, str]]) -> Optional[float]:
        return krippendorff_alpha([[ref, v] for _, ref, v in sample], "ordinal", levels)

    def ac2_of(sample: List[Tuple[str, str, str]]) -> Optional[float]:
        return gwet_ac([[ref, v] for _, ref, v in sample], "linear", levels)

    def bias_of(sample: List[Tuple[str, str, str]]) -> Optional[float]:
        return sum(index[v] - index[ref] for _, ref, v in sample) / len(sample) if sample else None

    alpha = alpha_of(pairs) if n >= MIN_ITEMS_FOR_ALPHA else None
    ac2 = ac2_of(pairs) if n >= MIN_ITEMS_FOR_ALPHA else None
    distribution = {s: sum(1 for _, ref, _v in pairs if ref == s) for s in levels}
    return Agreement(
        label=label,
        judge_model=judge_model,
        fail_at=fail_at,
        n_reference=len(consensus),
        n_compared=n,
        n_error=n_error,
        n_ungraded=n_ungraded,
        n_unjudged=n_unjudged,
        exact=sum(1 for g in gaps if g == 0),
        within_one=sum(1 for g in gaps if abs(g) <= 1),
        n_stricter=sum(1 for g in gaps if g > 0),
        n_lenient=sum(1 for g in gaps if g < 0),
        bias_mean=(sum(gaps) / n) if n else None,
        bias_interval=_bootstrap(bias_of, pairs) if n else None,
        alpha=alpha,
        alpha_interval=_bootstrap(alpha_of, pairs) if alpha is not None else None,
        ac2=ac2,
        ac2_interval=_bootstrap(ac2_of, pairs) if ac2 is not None else None,
        reference_distribution=distribution,
        sensitivity=(sum(1 for _, v in ref_fail if index[v] >= fail_rank), len(ref_fail)),
        specificity=(sum(1 for _, v in ref_pass if index[v] < fail_rank), len(ref_pass)),
        severe_misses=(sum(1 for _, v in ref_severe if v in scale.missed), len(ref_severe)),
        confusion=confusion,
        disagreements=[
            {"scenario": name, "reference": ref, "judge": v, "gap": index[v] - index[ref]}
            for name, ref, v in pairs if ref != v
        ],
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _fmt_rate(k: int, n: int) -> str:
    if n == 0:
        return "n/a (0 items)"
    lo, hi = wilson_interval(k, n)
    return f"{k}/{n} = {k / n:.2f}  [{lo:.2f}, {hi:.2f}]"


def _fmt_interval(value: Optional[float], interval: Optional[Tuple[float, float]], signed: bool = False) -> str:
    if value is None:
        return "n/a"
    text = f"{value:+.2f}" if signed else f"{value:.2f}"
    if interval:
        text += f"  [{interval[0]:{'+' if signed else ''}.2f}, {interval[1]:{'+' if signed else ''}.2f}]"
    return text


@dataclass
class CalibrationReport:
    """What :func:`calibrate` returns: one :class:`Agreement` per judge, plus
    the reference it was scored against and the unanimous/split strata."""

    subject: str
    reference: Reference
    fail_at: str
    judges: Dict[str, Agreement]
    strata: Dict[str, Dict[str, Agreement]]    # judge label -> {"unanimous": .., "split": ..}
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    scale: Scale = SEVERITY_SCALE

    # -- access ---------------------------------------------------------------

    def __getitem__(self, label: str) -> Agreement:
        return self.judges[label]

    @property
    def n_items(self) -> int:
        return len(self.reference.consensus)

    @property
    def n_split(self) -> int:
        return sum(1 for v in self.reference.split.values() if v)

    def leaderboard(self) -> List[Agreement]:
        """Judges ordered best first: Gwet's AC2 when every judge has one, else exact agreement.

        AC2 rather than alpha, because a reference where most items pass
        pulls alpha down for every judge at once and by different amounts,
        so it would rank judges by the skew rather than by their agreement.
        """
        rows = list(self.judges.values())
        if rows and all(r.ac2 is not None for r in rows):
            return sorted(rows, key=lambda r: (r.ac2, r.exact / max(r.n_compared, 1)), reverse=True)
        return sorted(rows, key=lambda r: (r.exact / max(r.n_compared, 1), r.n_compared), reverse=True)

    # -- output ---------------------------------------------------------------

    def reference_line(self) -> str:
        parts = []
        for r in self.reference.raters:
            if r.kind == "model":
                parts.append(f"{r.model or 'unknown model'} ({r.family or 'unknown family'})")
            else:
                parts.append(r.name)
        kind = self.reference.kind
        return f"{kind}, {len(parts)} rater{'s' if len(parts) != 1 else ''}: " + ", ".join(parts)

    def summary(self, print_fn: Callable[[str], None] = print) -> None:
        """Print the report. Every rate shows its n and a 95% interval."""
        scale = self.scale
        ordinal = len(scale.levels) > 2
        # Two levels: alpha is the same on any metric, and AC2 is AC1.
        alpha_name, ac_name = ("ordinal alpha", "Gwet AC2") if ordinal else ("Krippendorff alpha", "Gwet AC1")
        out: List[str] = []
        out.append(f"Calibration: {self.subject}")
        if scale is not SEVERITY_SCALE:
            out.append(f"  scale: {scale.name} ({', '.join(scale.levels)})")
        out.append(f"  reference: {self.reference_line()}")
        line = f"  items with a reference label: {self.n_items}"
        if self.reference.n_unsure:
            line += f"   (+{self.reference.n_unsure} unsure, left out)"
        if len(self.reference.raters) > 1:
            line += f"   split between raters: {self.n_split}"
        out.append(line)
        ceiling = self.reference.human_ceiling()
        if ceiling is not None:
            ceiling_ac2 = self.reference.human_ceiling_ac2()
            ac2_text = f", {ac_name} {ceiling_ac2:.2f}" if ceiling_ac2 is not None else ""
            out.append(f"  agreement among raters: {alpha_name} {ceiling:.2f}{ac2_text}   <- the ceiling to read the judge against")
        for a in self.leaderboard():
            out.append("")
            head = f"  judge '{a.label}'"
            if a.judge_model:
                head += f"  ({a.judge_model}, {model_family(a.judge_model) or 'unknown family'})"
            out.append(head)
            cov = f"    compared: {a.n_compared} of {a.n_reference}"
            extras = [f"{a.n_error} ERROR" if a.n_error else "", f"{a.n_ungraded} ungraded" if a.n_ungraded else "",
                      f"{a.n_unjudged} not judged" if a.n_unjudged else ""]
            extras = [e for e in extras if e]
            if extras:
                cov += "   (" + ", ".join(extras) + ")"
            if a.indicative_only:
                cov += f"   INDICATIVE ONLY (fewer than {MIN_ITEMS_FOR_ALPHA})"
            out.append(cov)
            if a.n_compared == 0:
                continue
            out.append(f"    exact agreement     {_fmt_rate(a.exact, a.n_compared)}")
            if ordinal:
                out.append(f"    within one level    {_fmt_rate(a.within_one, a.n_compared)}")
            out.append(f"    bias (judge - ref)  mean {_fmt_interval(a.bias_mean, a.bias_interval, signed=True)}"
                       f"   stricter {a.n_stricter}  lenient {a.n_lenient}")
            if a.alpha is not None or a.ac2 is not None:
                dist = "  ".join(f"{s} {c}" for s, c in a.reference_distribution.items() if c)
                out.append(f"    reference levels    {dist}")
                ac_label = f"{ac_name} (linear)" if ordinal else ac_name
                out.append(f"    {ac_label:<20}{_fmt_interval(a.ac2, a.ac2_interval)}   <- robust to skew above; ranks the leaderboard")
                out.append(f"    {alpha_name:<20}{_fmt_interval(a.alpha, a.alpha_interval)}   <- reads low when one level dominates")
            out.append(f"    pass/fail at '{a.fail_at}':  sensitivity {_fmt_rate(*a.sensitivity)}"
                       f"   specificity {_fmt_rate(*a.specificity)}")
            if ordinal:
                # On two levels this is one minus the sensitivity above.
                out.append(f"    severe misses (ref {'/'.join(scale.severe)} -> judge {'/'.join(scale.missed)}): "
                           f"{_fmt_rate(*a.severe_misses)}")
            strata = self.strata.get(a.label, {})
            if len(self.reference.raters) > 1 and strata:
                for key in ("unanimous", "split"):
                    s = strata.get(key)
                    if s and s.n_compared:
                        out.append(f"    {key:<9} items   exact {_fmt_rate(s.exact, s.n_compared)}")
            heads = (("pass", "low", "med", "high", "crit") if scale is SEVERITY_SCALE
                     else tuple(level if len(level) < 6 else level[:3] for level in scale.levels)) + ("ERR",)
            out.append("    confusion (rows reference, cols judge)   " + "".join(f"{c:>6}" for c in heads))
            for ref in scale.levels:
                row = a.confusion[ref]
                if any(row.values()):
                    out.append(f"      {ref:<36}" + "".join(f"{row[c]:>6}" for c in list(scale.levels) + ["ERROR"]))
            if a.disagreements:
                out.append("    disagreements (read these transcripts):")
                for d in sorted(a.disagreements, key=lambda d: -abs(d["gap"]))[:10]:
                    out.append(f"      {d['scenario']}: reference {d['reference']}, judge {d['judge']}")
                if len(a.disagreements) > 10:
                    out.append(f"      ... and {len(a.disagreements) - 10} more")
        print_fn("\n".join(out))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": REPORT_FORMAT_VERSION,
            "created_at": self.created_at,
            "subject": self.subject,
            "scale": self.scale.name,
            "levels": list(self.scale.levels),
            "fail_at": self.fail_at,
            "reference": {
                "kind": self.reference.kind,
                "raters": [r.to_dict() for r in self.reference.raters],
                "n_items": self.n_items,
                "n_split": self.n_split,
                "n_unsure": self.reference.n_unsure,
                "human_ceiling_alpha": self.reference.human_ceiling(),
                "human_ceiling_ac2": self.reference.human_ceiling_ac2(),
                "consensus": dict(self.reference.consensus),
            },
            "judges": {label: a.to_dict() for label, a in self.judges.items()},
            "strata": {
                label: {key: s.to_dict() for key, s in by_key.items()}
                for label, by_key in self.strata.items()
            },
        }

    def default_path(self, directory: Union[str, Path] = "results/calibration") -> Path:
        stem = Path(self.subject).stem if self.subject else "calibration"
        ref = self.reference.kind
        if ref == "model" and self.reference.raters:
            ref = "model-" + re.sub(r"[^a-z0-9]+", "-", (self.reference.raters[0].model or "unknown").lower()).strip("-")
        date = self.created_at[:10].replace("-", "")
        if self.scale is not SEVERITY_SCALE:
            ref = f"{self.scale.name}__{ref}"
        return Path(directory) / f"{stem}__{ref}__{date}.json"

    def save(self, path: Optional[Union[str, Path]] = None, directory: Union[str, Path] = "results/calibration") -> Path:
        """Write the report as JSON. Defaults to ``results/calibration/<subject>__<reference>__<date>.json``."""
        target = Path(path) if path else self.default_path(directory)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json_dump(self.to_dict(), str(target))
        return target


def list_calibrations(directory: Union[str, Path] = "results/calibration") -> List[Dict[str, Any]]:
    """One row per saved report in *directory*: subject, reference, judges and their headline numbers."""
    rows: List[Dict[str, Any]] = []
    for path in sorted(Path(directory).glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or "judges" not in data or "reference" not in data:
            continue
        for label, a in data["judges"].items():
            rows.append({
                "path": str(path),
                "created_at": data.get("created_at"),
                "subject": data.get("subject"),
                "scale": data.get("scale", "severity"),   # reports before 1.1 are all on the ladder
                "reference": data["reference"].get("kind"),
                "raters": [r.get("name") for r in data["reference"].get("raters", [])],
                "judge": label,
                "judge_model": a.get("judge_model"),
                "n_compared": a.get("coverage", {}).get("compared"),
                "exact": a.get("exact", {}).get("value"),
                "alpha": a.get("alpha", {}).get("value"),
                "ac2": a.get("ac2", {}).get("value"),
                "indicative_only": a.get("indicative_only"),
            })
    return rows


# ---------------------------------------------------------------------------
# calibrate
# ---------------------------------------------------------------------------

def _subjects(
    subject: Any, judge_model: Optional[str], scale: Scale = SEVERITY_SCALE
) -> Tuple[str, Dict[str, Tuple[Optional[str], Dict[str, str]]]]:
    """``(description, {label: (judge_model, verdicts)})`` for whatever was passed."""
    # ReframingResults: one judge per variant. Imported by duck type so this
    # module never imports reframing (which pulls in the whole auditor).
    if hasattr(subject, "variant_labels") and hasattr(subject, "per_scenario"):
        if len(scale.levels) > 2 and scale is not SEVERITY_SCALE:
            # A verdict judge's medium and fail can share a severity (the
            # scenario's ceiling), and these results keep only the severity.
            raise ValueError(
                f"reframing_check results keep only severities, which cannot be read back as "
                f"{scale.name} verdicts; rejudge() each variant and calibrate the AuditResults."
            )

        def on_scale(severity: Any) -> str:
            severity = normalize_severity(severity)
            if scale is SEVERITY_SCALE or severity in ("ERROR", UNGRADED):
                return severity
            return scale.read(severity, "severity")

        out: Dict[str, Tuple[Optional[str], Dict[str, str]]] = {}
        for lab in subject.variant_labels:
            meta = getattr(subject, "variant_meta", {}).get(lab, {})
            model = meta.get("judge_model") or judge_model
            verdicts = {
                name: on_scale(by_variant[lab])
                for name, by_variant in subject.per_scenario.items() if lab in by_variant
            }
            out[lab] = (model, verdicts)
        return "reframing_check results", out
    if isinstance(subject, (str, Path)):
        results = AuditResults.load(str(subject))
        desc = str(subject)
    elif isinstance(subject, AuditResults):
        results = subject
        desc = "AuditResults"
    else:
        raise TypeError(f"subject must be a results file, AuditResults or ReframingResults, got {type(subject).__name__}")
    model = judge_model or results.meta.get("judge_model")
    label_ = results.meta.get("judge_name") or "stored"
    return desc, {label_: (model, _verdicts_of(results, desc, scale))}


def _check_families(
    judges: Mapping[str, Tuple[Optional[str], Dict[str, str]]],
    reference: Reference,
    allow_same_family: bool,
) -> None:
    """Refuse a model reference from the subject judge's own family."""
    model_raters = [r for r in reference.raters if r.kind == "model"]
    if not model_raters:
        return
    for rater in model_raters:
        if rater.family is None:
            warnings.warn(
                f"reference judge {rater.model!r} is not a recognised model family; "
                "cannot check that it differs from the subject judge's. Pass a model id "
                "with a known family, or check by hand.",
                stacklevel=3,
            )
    seen: Dict[str, str] = {}
    for rater in model_raters:
        if rater.family and rater.family in seen and not allow_same_family:
            raise ValueError(
                f"reference judges {seen[rater.family]!r} and {rater.model!r} are both "
                f"{rater.family}; references must come from different model families."
            )
        if rater.family:
            seen[rater.family] = rater.model or ""
    for label, (model, _) in judges.items():
        if model is None:
            warnings.warn(
                f"subject judge '{label}' has no judge_model recorded (results saved before "
                "0.5.1 do not carry it); pass judge_model= to check its family against the reference.",
                stacklevel=3,
            )
            continue
        family = model_family(model)
        if family is None:
            warnings.warn(f"subject judge model {model!r} is not a recognised model family; family check skipped.", stacklevel=3)
            continue
        for rater in model_raters:
            if rater.family == family and not allow_same_family:
                raise ValueError(
                    f"subject judge {model!r} and reference judge {rater.model!r} are both "
                    f"{family}. A judge from the same family shares its blind spots, so their "
                    "agreement is not evidence; use a human reference or a judge from another "
                    "family (pass allow_same_family=True to override for an experiment)."
                )


def calibrate(
    subject: Any,
    reference: Any,
    *,
    judge_model: Optional[str] = None,
    reference_model: Optional[str] = None,
    fail_at: Optional[str] = None,
    allow_same_family: bool = False,
    scale: Union[str, Scale] = "severity",
) -> CalibrationReport:
    """Score a judge's stored verdicts against a reference.

    Args:
        subject: what the judge produced: a results file or :class:`AuditResults`
            (its stored ``severity`` per scenario, or its ``verdict`` on a
            verdict scale), or a ``ReframingResults`` from
            :func:`simpleaudit.reframing_check` (one judge per variant, the
            modal verdict at ``k > 1``; not on ``pass_medium_fail``).
        reference: human labels as ``{scenario_name: label}``, a labels
            file from :func:`label`, a :class:`Rater` such as the answer-key
            reference of :mod:`simpleaudit.answer_key`, a results file or
            :class:`AuditResults` graded by a judge from another model family,
            or a list mixing any of these. With several raters the reference
            for an item is the median on the scale, stricter middle on an even
            split, and items where raters disagreed are reported as a separate
            stratum.
        judge_model: the subject judge's model id, when the results do not
            record it (files saved before 0.5.1). Needed for the family check.
        reference_model: the reference judge's model id, likewise.
        fail_at: first level that counts as a failure in the pass/fail
            view. Defaults to the first level after ``pass`` (``"low"`` on
            the ladder), which matches :attr:`AuditResults.failed`.
        allow_same_family: skip the refusal when subject and reference judges
            share a family. For experiments only; the report still names both.
        scale: what judge and reference are compared on: ``"severity"`` (the
            ladder, the default), ``"pass_fail"`` or ``"pass_medium_fail"``.
            On a verdict scale, a verdict judge's results are read from their
            ``verdict``; a severity judge's (the default judge's, say) are read
            through the scale's map from the ladder (``pass`` is pass; on
            ``pass_fail`` every other level is fail; on ``pass_medium_fail``
            low and medium are medium, high and critical fail), so several
            judges can be scored on one set of labels. Labels files record
            their scale; ``pass_medium_fail`` labels can be read as
            ``pass_fail``, not the other way round.

    Returns:
        A :class:`CalibrationReport`. ``summary()`` prints it, ``to_dict()``
        and ``save()`` keep it, ``leaderboard()`` orders several judges.
    """
    scale = get_scale(scale)
    if fail_at is None:
        fail_at = scale.levels[1]
    if fail_at not in scale.levels[1:]:
        raise ValueError(f"fail_at must be one of {list(scale.levels[1:])}, got {fail_at!r}")
    refs = list(reference) if isinstance(reference, (list, tuple)) else [reference]
    if not refs:
        raise ValueError("reference is empty; pass at least one labels dict, labels file or results file.")
    ref = _build_reference(refs, reference_model, scale)
    desc, judges = _subjects(subject, judge_model, scale)
    _check_families(judges, ref, allow_same_family)
    if not ref.consensus:
        warnings.warn(f"the reference holds no labels on the {scale.name} scale; the report will be empty.", stacklevel=2)

    agreements: Dict[str, Agreement] = {}
    strata: Dict[str, Dict[str, Agreement]] = {}
    for label_, (model, verdicts) in judges.items():
        agreements[label_] = _agreement(label_, model, verdicts, ref.consensus, fail_at, scale)
        if len(ref.raters) > 1:
            unanimous = {k: v for k, v in ref.consensus.items() if not ref.split[k]}
            split = {k: v for k, v in ref.consensus.items() if ref.split[k]}
            strata[label_] = {
                "unanimous": _agreement(label_, model, verdicts, unanimous, fail_at, scale),
                "split": _agreement(label_, model, verdicts, split, fail_at, scale),
            }
    return CalibrationReport(
        subject=desc, reference=ref, fail_at=fail_at, judges=agreements, strata=strata, scale=scale
    )


__all__ = [
    "MIN_ITEMS_FOR_ALPHA",
    "SCALES",
    "SHEET_COLUMNS",
    "UNSURE",
    "Agreement",
    "CalibrationReport",
    "Rater",
    "Reference",
    "Scale",
    "calibrate",
    "export_labelling_sheet",
    "get_scale",
    "import_labelling_sheet",
    "label",
    "list_calibrations",
    "load_labels",
    "model_family",
    "save_labels",
]
