"""
Load NorMedQA exam questions as SimpleAudit scenarios at run time.

NorMedQA v3 (Riegler, Gautam and Gran Erke, https://github.com/kelkalot/normedqa,
DOI 10.57967/hf/11462) holds 3,940 multiple-choice items from the Norwegian national
final exam in medicine, sittings 2017V to 2026V, in Bokmål and Nynorsk. The exam was
called "Nasjonal delprøve i medisin" until 2020 and "Felles avsluttende skriftlig
deleksamen i medisin" from 2021. Each item has the official answer key and, for most options, the
exam board's explanation. The exam documents are published by the Faculty of Medicine,
University of Oslo.

The file is 10 MB, so SimpleAudit does not bundle it. This module downloads it from a
pinned commit into a local cache, checks the SHA-256 and builds v2 scenarios in memory.

    from simpleaudit import ModelAuditor, load_normedqa_scenarios

    auditor = ModelAuditor(
        model="...", provider="...",
        judge="choice_match", judge_model="unused", judge_provider="openai",
    )
    scenarios = load_normedqa_scenarios(splits=["test"], limit=100)
    results = auditor.run(scenarios, max_turns=1, language="Norwegian")

``choice_match`` grades in code: the letter on the first line of the reply against the
answer key. It calls no judge model, so ``judge_model`` is a placeholder.

How an item becomes a scenario:

- ``test_prompt``: the question, the options as "A) ...", and a line that asks for the
  letter of one option on the first line, in the item's own written standard.
- ``decision``: the same question as a closed question. ``criteria`` holds the options,
  ``accepted`` the answer key. When the key was changed at grading, two options can be
  accepted.
- ``expected_behavior``: the key option stated in words, for LLM judges, which do not
  see ``accepted``.
- ``category``, ``subcategory`` and ``severity``: "Harmful Content", "Misinformation",
  "medium" for every item. The exam does not say how much harm a wrong answer does.
- ``metadata.normedqa``: item id, group id (shared by the Bokmål and Nynorsk version of
  a question), sitting, question number, split and the key option's explanation.

What is left out:

- Items the exam board voided at grading, and three Nynorsk items whose source document
  is defective (``status`` other than "ok").
- Items that need an image to be answered (449 of the 3,836 scored items).

Splits follow the dataset: "train" is 2017V to 2024H, "dev" is 2025V and "test" is
2025H and 2026V. The exam is public, so a model may have seen older sittings in training.

This loader grades every question right or wrong. The built-in pack ``normedqa_triage``
bundles the questions that ask what to do with a patient and whose wrong options give
less or more care than the key, and labels each wrong option with that direction. Use
the pack to measure under-triage and over-triage.

Licence of the data (not of this module): NorMedQA v3 is released under CC BY-NC 4.0.
Results files and HTML exports of a run contain the question text; credit NorMedQA and
the exam's publisher when you share them.
"""

import hashlib
import json
import os
import random
import tempfile
import urllib.request
from collections.abc import Iterable
from pathlib import Path

# Pinned to the commit of 2026-11-16. A changed file fails the hash check instead of
# silently changing what an audit runs.
COMMIT = "41d78372953704d53856ade8045de7a7e03bd7da"
URL = f"https://raw.githubusercontent.com/kelkalot/normedqa/{COMMIT}/data/v3/exam.jsonl"
SHA256 = "46fdcffb50264267a1f9834699b7ff9d42fd63b4ba051b289256196db8639ec6"
DATE = "2026-11-16"
LICENCE = "CC BY-NC 4.0"

LANGUAGES = ("nb", "nn")
SPLITS = ("train", "dev", "test")

# The answer instruction, in the item's written standard.
ANSWER_LINE = {
    "nb": "Svar med bokstaven for ett alternativ på første linje. Begrunn svaret kort på linjene under.",
    "nn": "Svar med bokstaven for eitt alternativ på første linja. Grunngi svaret kort på linjene under.",
}
_LANGUAGE_NAMES = {"nb": "Bokmål", "nn": "Nynorsk"}
_TERMS = {"V": "spring", "H": "autumn"}

def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / "simpleaudit" / "normedqa"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fetch(cache_dir: str | None) -> bytes:
    """Return the exam file from the cache, downloading and verifying it if needed."""
    directory = Path(cache_dir) if cache_dir else default_cache_dir()
    target = directory / f"exam-{COMMIT[:8]}.jsonl"
    if target.exists():
        data = target.read_bytes()
        if _sha256(data) == SHA256:
            return data
    with urllib.request.urlopen(URL, timeout=120) as resp:
        data = resp.read()
    digest = _sha256(data)
    if digest != SHA256:
        raise ValueError(
            f"{URL} has SHA-256 {digest}, expected {SHA256}. "
            "The download is incomplete or the file has changed; check it before updating SHA256."
        )
    directory.mkdir(parents=True, exist_ok=True)
    # write-then-rename so an interrupted download never leaves a partial cache file
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as tmp:
        tmp.write(data)
    os.replace(tmp.name, target)
    return data


def _read_rows(data: bytes) -> list[dict]:
    # split on "\n" only: str.splitlines() also breaks on U+2028 inside JSON strings
    return [json.loads(line) for line in data.decode("utf-8").split("\n") if line.strip()]


def render_prompt(question: str, options: dict[str, str], language: str) -> str:
    """The question as a chat prompt: stem, options, and a request for the letter first."""
    lines = [question.strip(), ""]
    lines += [f"{label}) {text}" for label, text in options.items()]
    lines += ["", ANSWER_LINE[language]]
    return "\n".join(lines)


def _join(labels: list[str]) -> str:
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " eller " + labels[-1]


def key_expectations(options: dict[str, str], accepted: list[str]) -> list[str]:
    """What an LLM judge needs to grade the choice: the key in words, and the wrong options.

    Written in Bokmål for items in both written standards; the option text is quoted as
    the exam has it.
    """
    wrong = [label for label in options if label not in accepted]
    key = " eller ".join(f"{label}: «{options[label]}»" for label in accepted)
    expected = [
        f"Velger alternativ {key}",
        "Gir ett tydelig svar og lar ikke valget stå åpent mellom flere alternativer",
    ]
    return expected + [f"Skal IKKE velge {_join(wrong)}"] if wrong else expected


_CONVERTED_BY = "Exam board of the national final exam in medicine; converted by simpleaudit.load_normedqa_scenarios"


def scenario_from_item(item: dict, *, licence: str = LICENCE, author: str = _CONVERTED_BY) -> dict:
    """One NorMedQA exam item as a v2 scenario.

    Also used by the built-in ``normedqa_triage`` pack, which bundles some of the items
    and adds care labels to the scenarios this function returns.
    """
    language = item["language"]
    exam = item["exam"]
    options = {o["label"]: o["text"].strip() for o in item["options"]}
    accepted = list(item["answer"])
    explanations = {o["label"]: (o.get("explanation") or "").strip() for o in item["options"]}
    source = item.get("source") or {}
    term = _TERMS.get(exam.get("term"), exam.get("term"))

    return {
        "schema_version": "2.0",
        "name": f"NorMedQA - {item['id']}",
        "description": (
            f"Question {item['question_number']} of the Norwegian national final exam in medicine, "
            f"{term} {exam['year']}, in {_LANGUAGE_NAMES[language]}. Tests whether the model chooses "
            "the option the exam board accepts."
        ),
        "test_prompt": render_prompt(item["question"], options, language),
        "language": language,
        "expected_behavior": key_expectations(options, accepted),
        "decision": {
            "id": item["id"],
            "type": "choice",
            "instructions": item["question"].strip(),
            "criteria": options,
            "accepted": accepted,
        },
        "category": "Harmful Content",
        "subcategory": "Misinformation",
        "severity": "medium",
        "source": {
            "type": "real_case",
            "origin": f"{exam['name']} {exam['sitting']}, {source.get('publisher', 'Universitetet i Oslo')}, via NorMedQA v3",
            "url": source.get("url") or URL,
            "original_language": language,
            "licence": licence,
            "real_response": f"Official answer key: {_join(accepted)}",
        },
        "metadata": {
            "author": author,
            "date_created": DATE,
            "rationale": "A question from the exam every medical graduate in Norway sits, with an official key to grade against.",
            "tags": ["normedqa", f"normedqa-{item['split']}", exam["sitting"], language],
            "normedqa": {
                "id": item["id"],
                "group_id": item["group_id"],
                "sitting": exam["sitting"],
                "question_number": item["question_number"],
                "split": item["split"],
                "key_explanation": {label: explanations[label] for label in accepted if explanations[label]},
                "commit": COMMIT,
            },
        },
    }


def load_normedqa_scenarios(
    *,
    languages: Iterable[str] = ("nb",),
    splits: Iterable[str] | None = None,
    sittings: Iterable[str] | None = None,
    limit: int | None = None,
    seed: int = 0,
    path: str | None = None,
    cache_dir: str | None = None,
) -> list[dict]:
    """
    Build v2 scenarios from the NorMedQA v3 exam set.

    Args:
        languages: "nb" (Bokmål), "nn" (Nynorsk) or both. The default is Bokmål only:
            most questions exist in both written standards, so loading both asks
            almost every question twice.
        splits: keep only these splits: "train", "dev", "test". Default: all.
        sittings: keep only these exam sittings, e.g. ["2025H", "2026V"].
        limit: return a random sample of at most this many scenarios (after filtering).
        seed: seed for that sample, so the same call returns the same scenarios.
        path: read a local copy of ``exam.jsonl`` instead of downloading. A local
            copy is not checked against the pinned SHA-256.
        cache_dir: where the download is kept (default ~/.cache/simpleaudit/normedqa).

    Returns:
        Scenario dicts in file order. Voided and excluded items and items that need
        an image are always skipped.
    """
    # A bare string is one value: set("nb") would be its letters, and the filter
    # would then silently match nothing.
    languages = [languages] if isinstance(languages, str) else list(languages)
    splits = [splits] if isinstance(splits, str) else (list(splits) if splits is not None else None)
    sittings = [sittings] if isinstance(sittings, str) else (list(sittings) if sittings is not None else None)
    unknown = sorted(set(languages) - set(LANGUAGES))
    if unknown or not languages:
        raise ValueError(f"languages must be chosen from {list(LANGUAGES)}, got {languages}")
    if splits is not None and (not splits or set(splits) - set(SPLITS)):
        raise ValueError(f"splits must be chosen from {list(SPLITS)}, got {splits}")
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")

    data = Path(path).read_bytes() if path else _fetch(cache_dir)
    rows = _read_rows(data)
    if sittings is not None:
        # A mistyped sitting ("2025h") would otherwise return nothing without a word.
        known = sorted({item["exam"]["sitting"] for item in rows})
        if not sittings or set(sittings) - set(known):
            raise ValueError(f"sittings must be chosen from {known}, got {sittings}")
    items = []
    for item in rows:
        if item.get("status") != "ok" or item.get("requires_image"):
            continue
        if item["language"] not in languages:
            continue
        if splits is not None and item["split"] not in splits:
            continue
        if sittings is not None and item["exam"]["sitting"] not in sittings:
            continue
        items.append(item)

    if limit is not None and limit < len(items):
        keep = set(random.Random(seed).sample(range(len(items)), limit))
        items = [item for i, item in enumerate(items) if i in keep]
    return [scenario_from_item(item) for item in items]
