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
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from simpleaudit.utils import is_secret_name, redact_url_secrets

# Kinds the judge finds useful as evidence, in priority order.
DEFAULT_EVIDENCE_KINDS = ("RETRIEVER", "TOOL", "GUARDRAIL", "EVALUATOR", "AGENT", "LLM")

# Kinds that are usually structural noise for judging.
DEFAULT_NOISE_KINDS = ("CHAIN", "EMBEDDING", "PROMPT")

#: Default size cap (serialized chars) for evidence rendered into a judge
#: prompt. An agent trace can hold hundreds of spans; uncapped, the evidence
#: block alone can overflow the judge's context and turn the scenario into
#: an ERROR. Beyond the cap spans are elided (and listed as elided).
DEFAULT_JUDGE_EVIDENCE_BUDGET = 20_000

#: Attribute keys whose values are never sent to the judge model: captured
#: auth headers, cookies, API keys and similar secrets.
# A bearer/basic credential: after "Authorization:" any value, otherwise
# only a token-shaped one (16+ chars with a digit or "="), so prose such as
# "Basic information" or "Bearer bonds" is left alone.
def _redact_header_text(match: "re.Match[str]", name_check: Any = None) -> str:
    """Redact the value of a ``name: value`` pair in span text when it is a
    credential: any value under a header-shaped name (``X-Api-Key``,
    ``Authorization``, ``Cookie``), but under a bare word (``password: not
    logged``, ``Key: the second document``) only a credential-shaped value —
    prose the judge reads must keep its meaning."""
    lead, name, sep, value = match.groups()
    if not (name_check or is_secret_name)(name) or "[REDACTED]" in value or value.lower() in ("bearer", "basic"):
        return match.group(0)
    # Authorization / Cookie are decided by _redact_credential_headers alone,
    # which knows whether the text is a header or prose.
    if name.lower() in _CREDENTIAL_HEADERS:
        return match.group(0)
    header_like = "-" in name
    credential_like = len(value) >= 8 and any(c.isdigit() or c in "=_" for c in value)
    if header_like or credential_like:
        return f"{lead}{name}{sep}[REDACTED]"
    return match.group(0)


_CREDENTIAL_HEADERS = ("authorization", "proxy-authorization", "cookie", "set-cookie")
# "Authorization: token …", "'Cookie': 'a=b; c=d'", "{\\"Authorization\\": …}",
# "Authorization=Digest …": the name, an optional (possibly escaped) closing
# quote, ":" or "=", and an optional opening quote. Whether a credential
# follows is decided in _redact_credential_headers.
_CREDENTIAL_HEADER_START = re.compile(
    # Not inside a word, except after an escaped line break ("\\r\\nCookie:").
    r"(?i)(?:(?<![\w-])|(?<=\\[nrt]))((?:proxy-)?authorization|set-cookie|cookie)"
    r"((?:\\*['\"])?[ \t]*[:=][ \t]*)((?:\\*['\"])?)"
)
_AUTH_SCHEME = re.compile(
    r"(?i)(?:bearer|basic|digest|token|negotiate|ntlm|apikey|api-key|key|hoba|mutual|vapid"
    r"|aws|aws4-hmac-sha256|scram-sha-\d+|oauth|sharedkey|sso-key|ssws|splunk|googlelogin)"
    r"(?=[ \t])"
)
# Bounded, and ":" excluded, so "Cookie:Cookie:…" cannot make each match scan
# to the end of the text (quadratic time on a stuck target's output).
_COOKIE_PAIR = re.compile(r"[ \t]*([^\s=;,:'\"\\]{1,256})=([^\s;,'\"\\]{0,4096})")
_TOKENS = re.compile(r"[ \t]*([^\s'\"\\]{1,4096})(?:[ \t]+([^\s'\"\\]{1,4096}))?")
_CURL_HEADER_FLAG = re.compile(r"(?:^|\s)(?:-H|--header|-b|--cookie)\s+$")


_LINE_BREAK = re.compile(r"\r\n|\n|\r|\\r\\n|\\n|\\r")
# A Cookie / Set-Cookie value made only of name=value pairs ("sid=abc; x=y"),
# or one carrying Set-Cookie attributes: not a recipe line ("oatmeal=2 cups").
_COOKIE_VALUE = r'(?:"[^"\r\n]*"|[^\s;,"]*)'
_COOKIE_PAIRS_ONLY = re.compile(
    rf"[ \t]*[^\s=;,]+={_COOKIE_VALUE}(?:;[ \t]*[^\s=;,]+={_COOKIE_VALUE})*;?[ \t]*$"
)
_COOKIE_ATTRIBUTE = re.compile(r"(?i);[ \t]*(?:path|expires|max-age|domain|httponly|secure|samesite)\b")


def _credential_shaped(token: Optional[str]) -> bool:
    """A token that reads as a secret, not a word: 8+ characters with a digit
    or one of ``=_+/``. "coverage", "required" and "process" are not."""
    return bool(token) and len(token) >= 8 and any(c.isdigit() or c in "=_+/" for c in token)


def _credential_value_end(text: str, i: int, quote: str) -> int:
    """Where a header value starting at *i* ends.

    Quoted (``'Cookie': 'a=b; c=d'``, or ``\\"…\\"`` inside serialized JSON):
    at the matching unescaped quote. Unquoted: at the end of the line, a
    quote, or a ``\\n`` escape, except that a quoted parameter
    (``response="…"``, ``username=\\"bob\\"``) is part of the value — Digest
    credentials are full of them.
    """
    n = len(text)
    j = i
    if len(quote) > 1:  # an escaped quote (\\", \\\\\\"): JSON inside JSON
        k = text.find(quote, i)
        return n if k == -1 else k
    if quote:
        while j < n and text[j] not in (quote, "\n", "\r"):
            j += 2 if text[j] == "\\" else 1
        return min(j, n)
    while j < n and text[j] not in "\r\n'":
        if text[j] == '"' or text[j:j + 2] == '\\"':
            closer = text[j] if text[j] == '"' else '\\"'
            if j > i and text[j - 1] == "=":
                k = text.find(closer, j + len(closer))
                if k == -1 or "\n" in text[j:k]:
                    return n if k == -1 else j
                j = k + len(closer)
                continue
            break
        if text[j] == "\\":
            break  # an escape such as \\n inside unparsed JSON: the line ends
        j += 1
    return j


def _header_position(text: str, m: "re.Match[str]") -> str:
    """How the header name sits: "structured" (quoted — a dict, JSON, a curl
    argument), "line" (it starts its line, as in a logged request), or "prose"."""
    if m.group(3) or m.group(2)[:1] in ("'", '"', "\\"):
        return "structured"
    before = text[max(0, m.start() - 64):m.start()]
    if before[-1:] in ("'", '"') or _CURL_HEADER_FLAG.search(before):
        return "structured"
    # A line break may be real or written as an escape: a raw request logged
    # as text ("send: b'GET / HTTP/1.1\\r\\nAuthorization: ...") has only the
    # escaped form.
    parts = _LINE_BREAK.split(before)
    if not parts[-1].replace("\\t", "").strip() and (len(parts) > 1 or m.start() <= 64):
        return "line"
    return "prose"


def _redact_credential_headers(text: str) -> str:
    """Hide the whole value of an Authorization / Cookie header written as text.

    How sure the text is a header decides what counts as its value:

    - **structured** (``{'Authorization': '…'}``, ``-H "Cookie: …"``): any
      value, whatever its scheme (``AWS …``, ``SSWS …``, ``Key …``).
    - **line** (the name starts a line): an authorization value led by a
      known scheme, a single token, or a scheme-like word followed by a
      credential-shaped token; a cookie value starting with ``name=value``.
    - **prose** ("The prior authorization: Basic coverage requires …"): only
      a known scheme followed by a credential-shaped token (or Digest-style
      ``param=`` pairs), and a cookie only when its first value is
      credential-shaped. Prose the judge reads keeps its meaning.
    """
    out: list[str] = []
    pos = 0
    for m in _CREDENTIAL_HEADER_START.finditer(text):
        if m.start() < pos:
            continue
        start = m.end()
        if start >= len(text) or text.startswith("[REDACTED]", start) or text[start] in "\r\n":
            continue
        where = _header_position(text, m)
        if "authorization" in m.group(1).lower():
            scheme = _AUTH_SCHEME.match(text, start)
            tokens = _TOKENS.match(text, start)
            first, second = (tokens.group(1), tokens.group(2)) if tokens else (None, None)
            if where == "structured":
                ok = True
            elif where == "line":
                # A scheme counts when a credential follows it, or when the
                # value is just "<scheme> <token>": "Authorization: Basic plan
                # members must submit form A." at the start of a policy line
                # is prose.
                value = text[start:_credential_value_end(text, start, m.group(3))]
                ok = (
                    (second is None and _credential_shaped(first))
                    or _credential_shaped(second)
                    or bool(scheme) and (
                        len(value.split()) <= 2
                        or bool(second and re.match(r"\w+=", second))
                    )
                )
            else:
                ok = bool(scheme) and (
                    _credential_shaped(second) or bool(second and re.match(r"\w+=", second))
                    and first.lower() == "digest"
                )
        else:
            pair = _COOKIE_PAIR.match(text, start)
            if where == "structured":
                ok = True
            elif where == "line":
                value = text[start:_credential_value_end(text, start, m.group(3))]
                ok = bool(pair) and (
                    _credential_shaped(pair.group(2))
                    or bool(_COOKIE_PAIRS_ONLY.match(value))
                    or bool(_COOKIE_ATTRIBUTE.search(value))
                )
            else:
                ok = bool(pair) and _credential_shaped(pair.group(2))
        if not ok:
            continue
        end = _credential_value_end(text, start, m.group(3))
        if end <= start:
            continue
        out.append(text[pos:start])
        out.append("[REDACTED]")
        pos = end
    if not out:
        return text
    out.append(text[pos:])
    return "".join(out)


_HEADER_TEXT = re.compile(r"(^|[\s'\"{,;(])([A-Za-z][\w-]*)(\s*[:=]\s*)([^\s'\",;)}]+)")
# A bare bearer/basic credential outside any header ("… with Bearer eyJ…"):
# only a token-shaped one, so "Basic coverage" or "Bearer bonds" stays.
_BEARER = re.compile(
    r"(?i)\b((?:bearer|basic)\s+)(?=[A-Za-z0-9._~+/=-]{16,})(?=\S*[0-9=])[A-Za-z0-9._~+/=-]+"
)


_HEADER_KEY = re.compile(r"(?i)(?:^|[._])headers?(?:\.|$)")


def _is_header_key(key: Any) -> bool:
    # "headers", "http.request.header.x-functions-key", "request_headers": a
    # header value is a credential often enough under a name SECRET_NAME
    # cannot know (x-functions-key, x-goog-user-project-key...), so every
    # value under such a key is hidden. Not "Header 1" (a markdown splitter's
    # section title) or "markdown.header_level": those are document evidence.
    return bool(_HEADER_KEY.search(str(key)))


def _redact_all(value: Any, _depth: int = 0) -> Any:
    """*value* with every leaf hidden; header names (dict keys) are kept."""
    if _depth > 50:
        return "[REDACTED: nested too deeply]"
    if isinstance(value, dict):
        return {k: _redact_all(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_all(v, _depth + 1) for v in value]
    if isinstance(value, str) and value.strip()[:1] in ("{", "["):
        try:
            parsed = json.loads(value.strip())
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return json.dumps(_redact_all(parsed, _depth + 1), ensure_ascii=False)
    if isinstance(value, str) and value:
        return "[REDACTED]"
    return value  # numbers, booleans, None: not credentials


#: Key names that only ever hold an infrastructure credential — what an
#: agent's tool call sends to a real service — as opposed to data a scenario
#: is about ("password" in a leaked user record, a lookup "key").
_INFRA_SECRET_NAME = re.compile(
    r"(?i)(?:^|[^a-z])(?:x-)?(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|id[_-]?token"
    r"|auth[_-]?token|bearer[_-]?token|client[_-]?secret|secret[_-]?key|private[_-]?key"
    r"|session[_-]?(?:id|token))(?:$|[^a-z])"
    # Header names only as the whole key (or a header path's last part):
    # "prior_authorization" and "cookie_consent" are evidence, not secrets.
    r"|(?i:^(?:.*\.)?(?:proxy-)?authorization$|^(?:.*\.)?(?:set-)?cookie$)"
)


def redact_tool_value(value: Any) -> Any:
    """Redact an agent's tool-call arguments or results for the judge and the
    saved results.

    Narrower than span redaction on purpose. Tool data is the evidence the
    judge grades: a leak scenario must let it see the planted record
    (``{"password": "hunter2"}``) and an ordinary ``{"key": "user_42"}``
    lookup. Only what is unambiguously an infrastructure credential is hidden:
    keys like ``api_key`` / ``access_token`` / ``Authorization``, header maps,
    credential headers and bearer tokens written as text, and URL secrets.
    """
    return _redacted(value, _name=_INFRA_SECRET_NAME.search, _text_names=False)


def _redact_infra_header_text(match: "re.Match[str]") -> str:
    """``_redact_header_text`` for tool data: only infrastructure-credential
    names (``X-Api-Key: …``, ``OPENAI_API_KEY=…``), never ``password: …``."""
    if not _INFRA_SECRET_NAME.search(match.group(2)):
        return match.group(0)
    # These names only ever hold a credential: any value, not only a
    # credential-shaped one.
    lead, name, sep, value = match.groups()
    if "[REDACTED]" in value or not value:
        return match.group(0)
    return f"{lead}{name}{sep}[REDACTED]"


def _redacted(attrs: Any, _depth: int = 0, *, _name: Any = None, _text_names: bool = True) -> Any:
    """*attrs* with secret-looking keys hidden at every depth.

    OTLP ``kvlistValue`` attributes decode to nested dicts (captured header
    maps, tool parameters), so a top-level check is not enough.
    """
    secret_name = _name or is_secret_name
    kw = {"_name": _name, "_text_names": _text_names}
    if _depth > 50:
        # Pathologically nested input (a model stuck emitting brackets): do
        # not recurse further, and do not let it take the whole run down.
        return "[REDACTED: nested too deeply]"
    if isinstance(attrs, dict):
        return {
            k: "[REDACTED]" if secret_name(k)
            else _redact_all(v, _depth + 1) if _is_header_key(k)
            else _redacted(v, _depth + 1, **kw)
            for k, v in attrs.items()
        }
    if isinstance(attrs, list):
        return [_redacted(v, _depth + 1, **kw) for v in attrs]
    if isinstance(attrs, str):
        stripped = attrs.strip()
        if stripped[:1] in ("{", "["):
            # OpenInference sends inputs and invocation parameters as JSON
            # strings (OTel attributes cannot nest maps): redact inside them.
            try:
                parsed = json.loads(stripped)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return json.dumps(_redacted(parsed, _depth + 1, **kw), ensure_ascii=False)
        # Credential headers written as text: the whole value, to the end of
        # the line or quote (any scheme — "token ghp_…", "Digest …" — and
        # every cookie pair, not just the first word).
        attrs = _redact_credential_headers(attrs)
        attrs = _BEARER.sub(lambda m: m.group(1) + "[REDACTED]", attrs)
        # "X-Api-Key: value" / "cookie=value" written as text (curl lines,
        # logged requests): redact when the name itself names a credential.
        attrs = _HEADER_TEXT.sub(
            _redact_header_text if _text_names else _redact_infra_header_text, attrs
        )
        if "://" in attrs:
            attrs = redact_url_secrets(attrs)  # url.full, http.url, ...
    return attrs


def redact_span(span: Dict[str, Any]) -> Dict[str, Any]:
    """A copy of *span* whose attributes are redacted (for storing or showing)."""
    out = dict(span)
    if isinstance(out.get("attributes"), dict):
        out["attributes"] = _redacted(out["attributes"])
    # HTTP client spans are often named after the request
    # ("GET https://h/x?api_key=..."): the name is redacted like a value.
    if isinstance(out.get("name"), str):
        out["name"] = _redacted(out["name"])
    if isinstance(out.get("provenance"), dict) and isinstance(out["provenance"].get("name"), str):
        out["provenance"] = {**out["provenance"], "name": _redacted(out["provenance"]["name"])}
    return out


def _as_normalized(span: Dict[str, Any]) -> Dict[str, Any]:
    """A span with string ``kind``/``status``, normalizing raw OTLP spans.

    ``parse_otlp_json`` (and providers that return its output) give the OTLP
    integer kind; selection compares strings. Already-normalized spans are
    returned unchanged so their extra keys survive.
    """
    if isinstance(span.get("kind") or "", str) and isinstance(span.get("status") or "", str):
        return span
    from .store import normalize_span

    out = normalize_span(span)
    status = out.get("status")
    if isinstance(status, dict):
        # OTLP status object: code 2 / STATUS_CODE_ERROR is an error.
        code = status.get("code")
        status = "ERROR" if code in (2, "2", "STATUS_CODE_ERROR") else "OK"
    out["status"] = str(status or "")
    return out


@dataclass
class SelectionResult:
    selected: List[Dict[str, Any]] = field(default_factory=list)
    elided: List[Dict[str, Any]] = field(default_factory=list)
    budget_used: int = 0
    budget: Optional[int] = None
    selector_version: str = "v1"  # Track the selection policy version

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
        "name": _redacted(span.get("name") or ""),
    }


def select_spans(
    spans: Sequence[Dict[str, Any]],
    *,
    evidence_kinds: Sequence[str] = DEFAULT_EVIDENCE_KINDS,
    noise_kinds: Sequence[str] = DEFAULT_NOISE_KINDS,
    token_budget: Optional[int] = None,
    prefer_failing_spans: bool = False,
    selector_version: str = "v1",
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
    prefer_failing_spans:
        Prioritize spans with error/failure status over success spans.
    selector_version:
        Version identifier for the selection policy (recorded in provenance).
    """
    ev = {k.upper() for k in evidence_kinds}
    noise = {k.upper() for k in noise_kinds}
    spans = [_as_normalized(s) for s in spans]

    evidence = [s for s in spans if (s.get("kind") or "").upper() in ev]
    noise_spans = [s for s in spans if (s.get("kind") or "").upper() in noise]
    other = [s for s in spans if (s.get("kind") or "").upper() not in ev and (s.get("kind") or "").upper() not in noise]

    # If prefer_failing_spans, partition into failing and non-failing
    if prefer_failing_spans:
        def _is_failing(span: Dict[str, Any]) -> bool:
            status = (span.get("status") or "").lower()
            return status in ("error", "failed", "exception")

        evidence_failing = [s for s in evidence if _is_failing(s)]
        evidence_passing = [s for s in evidence if not _is_failing(s)]
        other_failing = [s for s in other if _is_failing(s)]
        other_passing = [s for s in other if not _is_failing(s)]

        # Priority: failing evidence, passing evidence, failing other, passing other, then noise
        candidates = evidence_failing + evidence_passing + other_failing + other_passing
    else:
        # Priority: evidence kinds first, then other, then noise (only if nothing else).
        candidates = list(evidence) + list(other)

    if not candidates:
        candidates = list(noise_spans)

    result = SelectionResult(budget=token_budget, selector_version=selector_version)
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
    prefer_failing_spans: bool = False,
    selector_version: str = "v1",
) -> List[Dict[str, Any]]:
    """Build judge-ready ``evidence_spans`` for one audit turn.

    Pulls the turn's spans from ``store`` via ``correlation``, selects the
    evidence-relevant ones, and returns them (with provenance) ready to pass
    to ``run_async(..., evidence_spans=...)``.

    This is the glue between trace ingestion (OTLP → SpanStore) and
    judge-over-spans: the engine records ``turn_id → trace_id`` during the
    run, spans arrive in the store, and this selects what the judge sees.

    Note: the engine uses one trace id per *scenario* (every turn carries the
    same traceparent trace id), so a turn's spans are those of the whole
    scenario, not only of that turn.
    """
    spans = correlation.spans_for_turn(turn_id, store)
    if not spans:
        return []
    result = select_spans(
        spans,
        evidence_kinds=evidence_kinds,
        noise_kinds=noise_kinds,
        token_budget=token_budget,
        prefer_failing_spans=prefer_failing_spans,
        selector_version=selector_version,
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
        attrs = _redacted(span.get("attributes") or {})
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


def render_agentic_for_judge(agentic_evaluation: Dict[str, Any]) -> str:
    """Render a structured agentic evaluation into a compact judge-prompt block.

    The agentic evaluation contains deterministic check results and optional
    semantic dimension assessments computed from the observed trajectory.
    This block is inserted into the judge prompt so the judge can weigh
    execution-quality evidence alongside conversation quality.

    Args:
        agentic_evaluation: Dict with keys ``checks`` (list), optionally
            ``semantic_judgement`` (dict with ``dimensions``), and
            ``trace_completeness`` (dict).
    """
    if not agentic_evaluation:
        return ""
    lines: list[str] = ["AGENTIC EVALUATION (observed execution facts):", ""]

    # Deterministic checks
    checks = agentic_evaluation.get("checks") or []
    if checks:
        lines.append("Deterministic checks:")
        for check in checks[:15]:
            status = check.get("status", "?")
            name = check.get("id", check.get("category", "unknown"))
            summary = (check.get("summary") or "")[:120]
            lines.append(f"  - {name}: {status}" + (f" — {summary}" if summary else ""))
        lines.append("")

    # Semantic judgement
    semantic = agentic_evaluation.get("semantic_judgement") or {}
    dims = semantic.get("dimensions") or {}
    if dims:
        lines.append("Semantic assessment:")
        for dim_name, dim_data in list(dims.items())[:8]:
            if isinstance(dim_data, dict):
                status = dim_data.get("status", "?")
                rationale = (dim_data.get("rationale") or "")[:100]
                lines.append(f"  - {dim_name}: {status}" + (f" — {rationale}" if rationale else ""))
        lines.append("")

    # Trace completeness
    tc = agentic_evaluation.get("trace_completeness") or {}
    if tc:
        complete = tc.get("complete", False)
        span_count = tc.get("span_count", 0)
        lines.append(f"Trace: {'complete' if complete else 'INCOMPLETE'} ({span_count} spans observed)")
        if not complete and tc.get("missing_reason"):
            lines.append(f"  Note: {tc['missing_reason'][:100]}")
        lines.append("")

    # Overall pre-assessment
    overall = agentic_evaluation.get("status")
    if overall:
        lines.append(f"Agentic pre-assessment: {overall}")
        reason = agentic_evaluation.get("reason")
        if reason:
            lines.append(f"  Reason: {str(reason)[:150]}")

    return "\n".join(lines)
