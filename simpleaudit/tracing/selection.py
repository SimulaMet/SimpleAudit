"""
Span selection — choose which spans to show the judge.

The judge should see *evidence-relevant* spans (retrieved documents, tool
calls, guardrail decisions, agent reasoning), not every span. This module
implements a configurable selection policy:

    1. **Kind filter** — keep spans of interesting kinds (RETRIEVER, TOOL,
       GUARDRAIL, EVALUATOR, AGENT, LLM) and drop noise (CHAIN, EMBEDDING by
       default).
    2. **Token budget** — cap the total serialized size so the judge prompt
       stays within context limits.
    3. **Elision marker** — when spans are dropped for budget, record what was
       elided so the judge (and the finding) knows evidence is partial.
    4. **Provenance** — each selected span carries its trace_id / span_id so a
       Finding can reference it via EvidenceRef.

The policy is pure: it takes spans and returns (selected, elided, budget_used).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

# Kinds the judge finds useful as evidence, in priority order.
DEFAULT_EVIDENCE_KINDS = ("RETRIEVER", "TOOL", "GUARDRAIL", "EVALUATOR", "AGENT", "LLM")

# Kinds that are usually structural noise for judging.
DEFAULT_NOISE_KINDS = ("CHAIN", "EMBEDDING", "PROMPT")


@dataclass
class SelectionResult:
    selected: List[Dict[str, Any]] = field(default_factory=list)
    elided: List[Dict[str, Any]] = field(default_factory=list)
    budget_used: int = 0
    budget: Optional[int] = None

    @property
    def elided_count(self) -> int:
        return len(self.elided)


def _span_size(span: Dict[str, Any]) -> int:
    """Approximate serialized size of a span in characters."""
    try:
        return len(json.dumps(span.get("attributes") or {}, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(span.get("attributes") or ""))


def _span_provenance(span: Dict[str, Any]) -> Dict[str, str]:
    return {
        "trace_id": span.get("trace_id") or "",
        "span_id": span.get("span_id") or "",
        "kind": span.get("kind") or "",
        "name": span.get("name") or "",
    }


def select_spans(
    spans: Sequence[Dict[str, Any]],
    *,
    evidence_kinds: Sequence[str] = DEFAULT_EVIDENCE_KINDS,
    noise_kinds: Sequence[str] = DEFAULT_NOISE_KINDS,
    token_budget: Optional[int] = None,
) -> SelectionResult:
    """Select evidence-relevant spans within an optional size budget.

    Parameters
    ----------
    spans:
        Normalized spans (see :func:`simpleaudit.tracing.store.normalize_span`).
    evidence_kinds:
        Kinds to prefer. Spans of these kinds are kept (subject to budget).
    noise_kinds:
        Kinds to drop unless nothing else is selected.
    token_budget:
        Max total serialized size (chars) of selected spans. ``None`` = no cap.
    """
    ev = {k.upper() for k in evidence_kinds}
    noise = {k.upper() for k in noise_kinds}

    evidence = [s for s in spans if (s.get("kind") or "").upper() in ev]
    noise_spans = [s for s in spans if (s.get("kind") or "").upper() in noise]
    other = [s for s in spans if (s.get("kind") or "").upper() not in ev and (s.get("kind") or "").upper() not in noise]

    # Priority: evidence kinds first, then other, then noise (only if nothing else).
    candidates = list(evidence) + list(other)
    if not candidates:
        candidates = list(noise_spans)

    result = SelectionResult(budget=token_budget)
    used = 0
    for span in candidates:
        size = _span_size(span)
        if token_budget is not None and used + size > token_budget and result.selected:
            result.elided.append(_span_provenance(span))
            continue
        selected = dict(span)
        selected["provenance"] = _span_provenance(span)
        result.selected.append(selected)
        used += size
        if token_budget is not None and used >= token_budget:
            # Stop adding; mark the rest as elided.
            for rest in candidates[candidates.index(span) + 1:]:
                result.elided.append(_span_provenance(rest))
            break

    result.budget_used = used
    return result


def evidence_spans_for_turn(
    correlation: Any,
    store: Any,
    turn_id: str,
    *,
    evidence_kinds: Sequence[str] = DEFAULT_EVIDENCE_KINDS,
    noise_kinds: Sequence[str] = DEFAULT_NOISE_KINDS,
    token_budget: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Build judge-ready ``evidence_spans`` for one audit turn.

    Pulls the turn's spans from ``store`` via ``correlation``, selects the
    evidence-relevant ones, and returns them (with provenance) ready to pass
    to ``run_async(..., evidence_spans=...)``.

    This is the glue between trace ingestion (OTLP → SpanStore) and
    judge-over-spans: the engine records ``turn_id → trace_id`` during the
    run, spans arrive in the store, and this selects what the judge sees.
    """
    spans = correlation.spans_for_turn(turn_id, store)
    if not spans:
        return []
    result = select_spans(
        spans,
        evidence_kinds=evidence_kinds,
        noise_kinds=noise_kinds,
        token_budget=token_budget,
    )
    return result.selected


def summarize_for_judge(result: SelectionResult, *, max_chars_per_span: int = 2000) -> str:
    """Render selected spans into a compact text block for the judge prompt."""
    if not result.selected:
        note = f"\n(elided {result.elided_count} span(s))" if result.elided else ""
        return f"(no evidence spans){note}"
    blocks = []
    for span in result.selected:
        prov = span.get("provenance", {})
        header = f"[{prov.get('kind', '?')} {prov.get('name', '?')}] trace={prov.get('trace_id', '?')[:8]} span={prov.get('span_id', '?')[:8]}"
        attrs = span.get("attributes") or {}
        body = json.dumps(attrs, ensure_ascii=False, default=str)
        if len(body) > max_chars_per_span:
            body = body[:max_chars_per_span] + "…(truncated)"
        blocks.append(f"{header}\n{body}")
    text = "\n\n".join(blocks)
    if result.elided:
        text += f"\n\n(elided {result.elided_count} additional span(s): " + ", ".join(
            f"{e.get('kind', '?')}:{e.get('span_id', '?')[:8]}" for e in result.elided[:10]
        ) + (", …" if result.elided_count > 10 else "") + ")"
    return text
