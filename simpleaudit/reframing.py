"""
Re-grade stored transcripts under paraphrased judge prompts.

The reproducibility leg of an audit resamples: the same scenario is run
``n_repetitions`` times and the spread of the verdicts is reported. Resampling
varies the conversation as well as the grading, so a verdict that moves may be
telling you about the target, the auditor, or the judge.

This module holds the transcript fixed and varies only the wording of the judge
prompt. A verdict that survives resampling but flips between two semantically
equivalent rubrics is measuring the prompt rather than the target — an
apparatus artifact, not a finding.

Nothing here calls the target model. The transcripts are already stored, so a
check costs judge tokens only.

Usage::

    from any_llm import AnyLLM
    from simpleaudit.judges import get_judge
    from simpleaudit.reframing import (
        load_stored_records, reframing_check, PromptVariant,
    )

    base = get_judge("safety")["judge_prompt"]
    results = reframing_check(
        judge_client=AnyLLM.create("anthropic"),
        judge_model="claude-opus-4-7",
        records=load_stored_records("examples/nav_aap/nav_aap_sonnet_4_6.json"),
        variants=[
            PromptVariant("baseline", base),
            PromptVariant("reordered", reordered_rubric_text),
        ],
    )
    for entry in results.shifts():
        if entry["shifted"]:
            print(entry["scenario"], entry["modals"], entry["direction"])
"""

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from simpleaudit.model_auditor import ModelAuditor
from simpleaudit.utils import normalize_severity, severity_direction


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class PromptVariant:
    """One wording of the judge prompt.

    Variants are supplied explicitly rather than generated. A tool built to
    detect prompt-induced verdict movement cannot introduce a model-generated
    prompt of its own: the paraphrases would then be an uncontrolled axis
    inside the instrument measuring that axis.
    """

    label: str
    judge_prompt: str
    response_schema: Optional[Dict[str, Any]] = None


@dataclass
class StoredRecord:
    """A transcript to re-grade, plus the scenario context the judge needs."""

    scenario_name: str
    scenario_description: str
    conversation: List[Dict[str, Any]]
    expected_behavior: Optional[List[str]] = None


def load_stored_records(
    source: Union[str, Path, Dict[str, Any]],
) -> List[StoredRecord]:
    """
    Read transcripts out of a saved audit result.

    Accepts a path to a file written by ``AuditResults.save()`` or the already
    parsed payload. Entries whose ``conversation`` is missing or empty are
    skipped: there is nothing to re-grade, and carrying them through would
    produce verdicts on an empty transcript that look like real ones.

    Parameters
    ----------
    source : str, Path, or dict
        Saved result file, or its parsed contents.

    Returns
    -------
    list of StoredRecord
    """
    if isinstance(source, dict):
        payload = source
    else:
        import json

        with open(source, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

    entries = payload.get("results")
    if not isinstance(entries, list):
        raise ValueError(
            "Saved result has no 'results' list — expected the payload shape "
            "written by AuditResults.save()."
        )

    records = []
    for entry in entries:
        conversation = entry.get("conversation")
        if not conversation:
            continue
        records.append(
            StoredRecord(
                scenario_name=entry.get("scenario_name", ""),
                scenario_description=entry.get("scenario_description", ""),
                conversation=conversation,
                expected_behavior=entry.get("expected_behavior"),
            )
        )
    return records


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class ReframingResults:
    """Verdicts for every (scenario, variant) pair, and the shifts between them.

    Attributes
    ----------
    variant_labels : list of str
        Variant labels in the order they were run.
    per_scenario : dict
        ``{scenario_name: {variant_label: severity}}``.
    judgments : dict
        ``{scenario_name: {variant_label: raw_judgment_dict}}`` — the full
        judge output, kept so a shift can be read back to its reasoning.
    input_tokens, output_tokens : int
        Judge tokens spent. No target tokens are spent on this path.
    """

    variant_labels: List[str]
    per_scenario: Dict[str, Dict[str, str]] = field(default_factory=dict)
    judgments: Dict[str, Dict[str, Dict[str, Any]]] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0

    def shifts(self) -> List[Dict[str, Any]]:
        """Per-scenario verdicts across variants, with shift detection.

        Returns
        -------
        list of dict
            One entry per scenario:

            - ``scenario``: scenario name
            - ``modals``: ``{variant_label: severity}`` — only variants that
              actually produced a verdict appear
            - ``shifted``: True if any two variants disagree
            - ``direction``: (two-variant case only) ``idx_b - idx_a`` in
              ``SEVERITY_ORDER``; positive means the second variant is
              stricter. None when either verdict is off the ladder.

        The shape matches ``CrossJudgeResults.severity_shifts()`` so the same
        reporting code reads both. There the varying axis is the judge model;
        here it is the prompt wording.
        """
        out = []
        for name, by_variant in self.per_scenario.items():
            modals = {label: by_variant[label] for label in self.variant_labels if label in by_variant}
            entry: Dict[str, Any] = {
                "scenario": name,
                "modals": modals,
                "shifted": len(set(modals.values())) > 1,
            }
            if entry["shifted"] and len(modals) == 2 and len(self.variant_labels) == 2:
                first, second = self.variant_labels
                entry["direction"] = severity_direction(modals[first], modals[second])
            out.append(entry)
        return out

    def invariant_rate(self) -> float:
        """Fraction of scenarios whose verdict is identical across all variants.

        Returns 0.0 for an empty result rather than raising, so a caller
        reporting several runs side by side does not need to special-case one
        that produced nothing.
        """
        if not self.per_scenario:
            return 0.0
        stable = sum(1 for entry in self.shifts() if not entry["shifted"])
        return stable / len(self.per_scenario)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "variant_labels": list(self.variant_labels),
            "per_scenario": {k: dict(v) for k, v in self.per_scenario.items()},
            "shifts": self.shifts(),
            "invariant_rate": self.invariant_rate(),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

async def reframing_check_async(
    judge_client: Any,
    judge_model: str,
    records: Sequence[StoredRecord],
    variants: Sequence[PromptVariant],
    json_format: bool = True,
    max_retries: int = 0,
    retry_backoff: float = 0.5,
) -> ReframingResults:
    """
    Re-grade every stored transcript once per prompt variant.

    Parameters
    ----------
    judge_client : Any
        An AnyLLM client for the judge. This is the only client used.
    judge_model : str
        Judge model identifier.
    records : sequence of StoredRecord
        Transcripts to re-grade, e.g. from ``load_stored_records()``.
    variants : sequence of PromptVariant
        At least two wordings of the judge prompt. One variant would produce
        no comparison.
    json_format : bool, default True
        Whether the judge is asked for schema-constrained JSON.
    max_retries, retry_backoff
        Forwarded to the judge call.

    Returns
    -------
    ReframingResults
    """
    if len(variants) < 2:
        raise ValueError("variants must contain at least two entries — one wording yields nothing to compare.")
    labels = [variant.label for variant in variants]
    if len(set(labels)) != len(labels):
        raise ValueError(f"variant labels must be unique, got {labels}.")

    results = ReframingResults(variant_labels=labels)

    for record in records:
        per_variant: Dict[str, str] = {}
        raw: Dict[str, Dict[str, Any]] = {}
        for variant in variants:
            judgment, tokens_in, tokens_out = await ModelAuditor._judge_conversation_async(
                judge_client,
                judge_model,
                record.scenario_description or record.scenario_name,
                record.conversation,
                record.expected_behavior,
                judge_prompt=variant.judge_prompt,
                json_format=json_format,
                response_schema=variant.response_schema,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )
            per_variant[variant.label] = normalize_severity(judgment.get("severity"))
            raw[variant.label] = judgment
            results.input_tokens += tokens_in
            results.output_tokens += tokens_out
        results.per_scenario[record.scenario_name] = per_variant
        results.judgments[record.scenario_name] = raw

    return results


def reframing_check(
    judge_client: Any,
    judge_model: str,
    records: Sequence[StoredRecord],
    variants: Sequence[PromptVariant],
    json_format: bool = True,
    max_retries: int = 0,
    retry_backoff: float = 0.5,
) -> ReframingResults:
    """Synchronous wrapper around :func:`reframing_check_async`.

    Cannot be called from an active event loop; await the async form there.
    Mirrors ``CrossJudgeExperiment.run()``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            reframing_check_async(
                judge_client=judge_client,
                judge_model=judge_model,
                records=records,
                variants=variants,
                json_format=json_format,
                max_retries=max_retries,
                retry_backoff=retry_backoff,
            )
        )
    raise RuntimeError(
        "reframing_check() cannot be called from an active event loop. "
        "Use await reframing_check_async() instead."
    )
