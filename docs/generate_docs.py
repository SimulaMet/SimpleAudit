#!/usr/bin/env python3
"""
SimpleAudit documentation generator — two-layer pipeline.

This script lives in the simpleaudit-docs repo. It reads the simpleaudit
package source from SIMPLEAUDIT_ROOT (default: sibling directory) and
writes the MkDocs source tree into ./site_src/.

Layer 1 (deterministic, no LLM):
  - Griffe scans the simpleaudit/ source tree and renders a complete,
    always-accurate API reference (classes, functions, signatures,
    docstrings) into site_src/reference/*.md
  - mkdocs.yml, nav, and the index page are generated from the plan

Layer 2 (LLM, Open WebUI backend):
  - Narrative pages (getting started, architecture, guides, examples)
    are generated/updated by the LLM with source code + KB context
  - Incremental: pages are only regenerated when their source files change

Output: site_src/  (MkDocs source tree)
Build:  mkdocs build  ->  site/  (static site, deployed to GitHub Pages)

Usage:
  python3 generate_docs.py              # incremental generate
  python3 generate_docs.py --force      # full re-plan + regenerate
  python3 generate_docs.py --no-llm     # deterministic layer only (offline)
  python3 generate_docs.py --build      # also run `mkdocs build`

Environment:
  SIMPLEAUDIT_ROOT  Path to the simpleaudit repo checkout (default: ../)
  OWUI_BASE         Open WebUI base URL
  OWUI_API_KEY      Open WebUI API key
  OWUI_KB_ID        Open WebUI knowledge base ID
  OWUI_MODEL        Model name
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE = os.environ.get("OWUI_BASE", "https://simulachat.sushant.info.np")
KEY = os.environ.get("OWUI_API_KEY", "")
KB_ID = os.environ.get("OWUI_KB_ID", "a43e450c-a197-4d53-95ca-555a6e6a2edb")
MODEL = os.environ.get("OWUI_MODEL", "default")

# This repo is the docs repo (simpleaudit-docs). The simpleaudit package
# source lives in a sibling checkout (set SIMPLEAUDIT_ROOT, or place the
# simpleaudit repo next to this one).
ROOT = os.environ.get(
    "SIMPLEAUDIT_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
DOCS = os.path.dirname(os.path.abspath(__file__))                   # repo root
SITE_SRC = os.path.join(DOCS, "site_src")
OUT = os.path.join(SITE_SRC, "guides")          # LLM narrative pages
REF = os.path.join(SITE_SRC, "reference")      # Griffe API reference
PLAN_FILE = os.path.join(SITE_SRC, ".plan.json")

# ---------------------------------------------------------------------------
# LLM backend (Open WebUI)
# ---------------------------------------------------------------------------


def chat(messages, use_kb=True, max_tokens=4096, temperature=0.3, retries=3):
    """Call Open WebUI chat completions with simple retry/backoff."""
    if not KEY:
        raise RuntimeError(
            "OWUI_API_KEY is not set. Export it before running the LLM layer:\n"
            "  export OWUI_API_KEY='your-key-here'"
        )
    headers = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if use_kb:
        payload["files"] = [{"type": "collection", "id": KB_ID}]
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.post(f"{BASE}/api/chat/completions", headers=headers,
                              json=payload, timeout=300)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        except Exception as e:  # noqa: BLE001
            last_err = e
            wait = 5 * (attempt + 1)
            print(f"    (retry {attempt + 1}/{retries} after error: {e}; waiting {wait}s)")
            time.sleep(wait)
    raise RuntimeError(f"LLM call failed after {retries} attempts: {last_err}")


def strip_code_fences(text):
    """Remove markdown code fences the LLM sometimes wraps around output."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()
    return text


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def read_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def get_file_tree():
    """Build a file tree of the simpleaudit package."""
    lines = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, "simpleaudit")):
        dirnames.sort()
        rel = os.path.relpath(dirpath, ROOT)
        depth = rel.count(os.sep)
        indent = "  " * depth
        lines.append(f"{indent}{os.path.basename(dirpath)}/")
        for f in sorted(filenames):
            if f.endswith(".py"):
                lines.append(f"{indent}  {f}")
    return "\n".join(lines)


def get_source_files():
    """Map of relative path -> content for all .py files in the package."""
    files = {}
    for dirpath, _, filenames in os.walk(os.path.join(ROOT, "simpleaudit")):
        for f in filenames:
            if f.endswith(".py"):
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, ROOT)
                files[rel] = read_file(full)
    return files


def get_all_trackable_files():
    """Map of relative path -> content for ALL files tracked for change detection."""
    exts = (".py", ".md", ".toml", ".txt", ".json", ".yaml", ".yml")
    files = {}
    for dirpath, _, filenames in os.walk(os.path.join(ROOT, "simpleaudit")):
        for f in filenames:
            if f.endswith(exts):
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, ROOT)
                files[rel] = read_file(full)
    for f in ("README.md", "pyproject.toml", "FAQ.md"):
        full = os.path.join(ROOT, f)
        if os.path.exists(full):
            files[f] = read_file(full)
    return files


def compute_file_hashes(source_files):
    """Per-file SHA-256 hashes: {relpath: hash}."""
    return {path: hashlib.sha256(content.encode()).hexdigest()
            for path, content in source_files.items()}


# ---------------------------------------------------------------------------
# Plan persistence / incremental logic
# ---------------------------------------------------------------------------


def load_plan():
    if os.path.exists(PLAN_FILE):
        try:
            with open(PLAN_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
    return None


def store_plan(pages, file_hashes):
    plan = {"pages": pages, "file_hashes": file_hashes}
    with open(PLAN_FILE, "w") as f:
        json.dump(plan, f, indent=2)


def page_needs_regeneration(page, old_hashes, new_hashes):
    """True if any source file for this page changed (or is new)."""
    for f in page.get("source_files", []):
        old = old_hashes.get(f)
        new = new_hashes.get(f)
        if old is None or new is None or old != new:
            return True
    return False


def cleanup_stale_pages(plan_slugs):
    """Remove .md files in guides/ not in the current plan."""
    stale = []
    for fname in os.listdir(OUT):
        if not fname.endswith(".md"):
            continue
        slug = fname[:-3]
        if slug not in plan_slugs:
            os.remove(os.path.join(OUT, fname))
            stale.append(fname)
    return stale


# ---------------------------------------------------------------------------
# Layer 1: deterministic API reference via Griffe
# ---------------------------------------------------------------------------

MODULE_DESCRIPTIONS = {
    "simpleaudit": "Top-level package: the public API of SimpleAudit.",
    "simpleaudit.model_auditor": "The core audit engine: drives target model, auditor, and judge across scenarios.",
    "simpleaudit.results": "Result containers: per-scenario results and aggregated audit results with summaries.",
    "simpleaudit.experiment": "Batch experiments: run audits across multiple models and compare them.",
    "simpleaudit.repeated_results": "Stability analysis: repeated runs, fragility thresholds, and model stability reports.",
    "simpleaudit.cross_judge": "Cross-judge experiments: score the same outputs with different judges and compare.",
    "simpleaudit.reframing": "Reframing checks: rephrase a prompt and verify the model's answer stays consistent.",
    "simpleaudit.judge_the_judge": "Judge validation: 'wiggle' the judge's inputs to measure judge robustness.",
    "simpleaudit.cli": "Command-line interface (`simpleaudit` entry point).",
    "simpleaudit.utils": "Shared helpers: LLM client wrappers, JSON parsing, and misc utilities.",
    "simpleaudit.judges": "Built-in judge configurations: safety, harm, factuality, helpfulness, abstention, and more.",
    "simpleaudit.judges.abstention": "Abstention judge: detects when the model should have refused/abstained.",
    "simpleaudit.judges.binary_abstention": "Binary abstention judge: yes/no abstention classification.",
    "simpleaudit.judges.factuality": "Factuality judge: scores factual accuracy of model outputs.",
    "simpleaudit.judges.harm": "Harm judge: scores harmfulness of model outputs.",
    "simpleaudit.judges.helpfulness": "Helpfulness judge: scores how helpful the model was.",
    "simpleaudit.judges.safety": "Safety judge: scores safety of model outputs.",
    "simpleaudit.judges.judge_conviction": "Judge conviction: meta-judge that extracts the candidate judge's current verdict for cross-checking.",
    "simpleaudit.judges.helsedir_sexhealth_no": "Helsedir sex-health (NO) judge: domain-specific Norwegian health judge.",
    "simpleaudit.judges.helsedir_sexhealth_no_rag": "Helsedir sex-health (NO) RAG judge: grounded variant with retrieval context.",
    "simpleaudit.scenarios": "Scenario packs: curated test suites (health, safety, government, benchmarks).",
    "simpleaudit.scenarios.health": "Health domain scenarios: medical Q&A and advice tests.",
    "simpleaudit.scenarios.safety": "Safety scenarios: refusal and harmful-request tests.",
    "simpleaudit.scenarios.bullshitbench_v1_v2": "BullshitBench v1/v2 scenarios: measuring non-informative answers.",
    "simpleaudit.scenarios.bullshitbench_health": "BullshitBench health variant: non-informative medical answers.",
    "simpleaudit.scenarios.hei_refusal": "HEI refusal scenarios: Norwegian youth-advice Q&A (16 refusal + 31 guidance scenarios).",
    "simpleaudit.scenarios.helfo": "Helfo scenarios: Norwegian health insurance authority tests.",
    "simpleaudit.scenarios.helpmed": "HelpMed scenarios: medical help-seeking tests.",
    "simpleaudit.scenarios.lanekassen": "Lanekassen scenarios: Norwegian pension institution tests.",
    "simpleaudit.scenarios.nav_aap": "NAV AAP scenarios: Norwegian disability benefit tests.",
    "simpleaudit.scenarios.skatteetaten": "Skatteetaten scenarios: Norwegian tax authority tests.",
    "simpleaudit.scenarios.ung": "UNG scenarios: youth health service tests.",
    "simpleaudit.scenarios.rag": "RAG scenarios: retrieval-augmented generation tests.",
    "simpleaudit.scenarios.system_prompt": "System-prompt scenarios: tests of system prompt adherence.",
    "simpleaudit.scenarios.vision_integrity": "Vision integrity scenarios: image-based integrity tests.",
    "simpleaudit.scenarios.judge_the_judge": "Judge-the-judge scenarios: probes used to validate judges.",
    "simpleaudit.visualization.server": "FastAPI visualization server: browse audit results in a browser.",
}


def _griffe_docstring(obj):
    """Best-effort docstring text for a Griffe object (never raises)."""
    try:
        doc = obj.docstring
    except Exception:  # noqa: BLE001  (alias resolution can fail)
        return ""
    if doc is None:
        return ""
    # griffe 1.x: Docstring object with .value; older: plain str
    text = getattr(doc, "value", doc)
    if text is None:
        return ""
    return str(text).strip()


def _griffe_signature(obj):
    """Best-effort signature string (never raises).

    griffe 1.x: ``signature`` is a method returning str.
    """
    try:
        sig = obj.signature
        if callable(sig):
            sig = sig()
        return str(sig)
    except Exception:  # noqa: BLE001
        return ""


def _is_public_member(obj):
    """True for real classes/functions/constants (not re-exported imports)."""
    import griffe
    return isinstance(obj, (griffe.Class, griffe.Function, griffe.Attribute))


def _resolve_alias(obj):
    """Resolve a Griffe Alias to its target object, or None on failure.

    Re-exported names (e.g. ``from .model_auditor import ModelAuditor``)
    are Alias objects. Resolving gives the real Class/Function/Attribute so
    the package index page can document the public API.
    """
    import griffe
    if not isinstance(obj, griffe.Alias):
        return obj
    try:
        return obj.target
    except Exception:  # noqa: BLE001  (unresolvable, e.g. stdlib imports)
        return None


def _main_guard_line(mod):
    """Return the 1-indexed line of the ``if __name__ == "__main__":`` guard.

    Members defined at or after this line are script-local (e.g. ``sev =
    Counter(...)`` in a demo block) and must not be documented as API.
    Returns None when the module has no ``__main__`` block.
    """
    try:
        src = mod.source
    except Exception:  # noqa: BLE001
        return None
    if not src:
        return None
    for i, line in enumerate(src.splitlines(), 1):
        if "__name__" in line and "__main__" in line:
            return i
    return None


def _member_lineno(obj):
    """Best-effort 1-indexed source line for a member (None on failure)."""
    try:
        return obj.lineno
    except Exception:  # noqa: BLE001  (alias resolution can fail)
        return None


def _render_member_md(name, obj, depth=3, module_members=None):
    """Render one class/function/attribute as markdown."""
    import griffe
    Class, Function = griffe.Class, griffe.Function

    lines = []
    header = "#" * depth
    doc = _griffe_docstring(obj)
    doc_lines = [l for l in doc.splitlines() if l.strip()] if doc else []

    if isinstance(obj, Class):
        lines.append(f"{header} `{name}`")
        if doc_lines:
            lines.append("")
            lines.extend(_format_docstring(doc))
            lines.append("")
        # init signature
        init = None
        try:
            for attr in obj.attributes.values():
                if attr.name == "__init__":
                    init = attr
                    break
        except Exception:  # noqa: BLE001
            pass
        if init is not None:
            sig = _griffe_signature(init)
            if sig:
                lines.append(f"**Signature:** `{sig}`")
                lines.append("")
        # methods
        try:
            members = list(obj.members.values())
        except Exception:  # noqa: BLE001
            members = []
        methods = [a for a in members
                   if isinstance(a, Function) and not a.name.startswith("_")]
        if methods:
            lines.append("**Methods:**")
            lines.append("")
            for m in methods:
                mdoc = _griffe_docstring(m)
                first = mdoc.splitlines()[0].strip() if mdoc else ""
                msig = _griffe_signature(m)
                label = msig if msig else f"{m.name}()"
                try:
                    if m.source and m.source.lstrip().startswith("async def"):
                        label = f"async {label}"
                except Exception:  # noqa: BLE001
                    pass
                lines.append(f"- `{label}` — {first}" if first else f"- `{label}`")
            lines.append("")
        # attributes
        try:
            attrs = [a for a in obj.attributes.values() if not a.name.startswith("_")]
        except Exception:  # noqa: BLE001
            attrs = []
        if attrs:
            lines.append("**Attributes:**")
            lines.append("")
            for a in attrs:
                adoc = _griffe_docstring(a)
                first = adoc.splitlines()[0].strip() if adoc else ""
                lines.append(f"- `{a.name}` — {first}" if first else f"- `{a.name}`")
            lines.append("")
    elif isinstance(obj, Function):
        sig = _griffe_signature(obj)
        # Mark async functions (griffe 1.x has no is_async flag; the
        # signature omits the `async` keyword, so detect it from source).
        is_async = False
        try:
            if obj.source and obj.source.lstrip().startswith("async def"):
                is_async = True
        except Exception:  # noqa: BLE001
            pass
        label = sig if sig else f"{name}()"
        if is_async:
            label = f"async {label}"
        lines.append(f"{header} `{label}`")
        if doc_lines:
            lines.append("")
            lines.extend(_format_docstring(doc))
            lines.append("")
    else:
        # module-level constant / variable
        lines.append(f"{header} `{name}`")
        if doc_lines:
            lines.append("")
            lines.extend(doc_lines)
            lines.append("")
        # Try rich rendering for structured data constants
        try:
            val = obj.value
        except Exception:  # noqa: BLE001
            val = None
        vtype = type(val).__name__ if val is not None else ""
        rendered_rich = False
        # Scenario list: list of dicts with name/description
        if "list" in vtype.lower() and val is not None:
            elements = getattr(val, "elements", None)
            if elements and len(elements) > 0:
                # Check if first element is a dict with name/description
                el0 = elements[0]
                k0 = getattr(el0, "keys", None)
                if k0 is not None:
                    k0_stripped = [_strip_token(k) for k in k0]
                    if "name" in k0_stripped and "description" in k0_stripped:
                        table = _render_scenario_list_md(val)
                        if table:
                            lines.append("")
                            lines.extend(table)
                            lines.append("")
                            rendered_rich = True
        # Judge config: dict with description/judge_prompt
        if not rendered_rich and "dict" in vtype.lower() and val is not None:
            judge_md = _render_judge_config_md(val)
            if judge_md:
                lines.append("")
                lines.extend(judge_md)
                lines.append("")
                rendered_rich = True
        if not rendered_rich and not doc_lines:
            # No docstring and no rich render — add a size/value annotation
            size_note = _describe_constant_value(obj, module_members)
            if size_note:
                lines.append("")
                lines.append(f"_{size_note}_")
                lines.append("")
    return lines


def _format_docstring(doc):
    """Format a docstring for markdown, handling numpydoc sections.

    - De-indents the block.
    - Renders ``**kwargs**`` parameter lines as ``*kwargs*`` (avoids a stray
      bold line).
    - Inserts a blank line before known section headers (Args, Returns,
      Examples, etc.) so they don't glue to the preceding text.
    """
    import textwrap
    if not doc:
        return []
    text = textwrap.dedent(doc)
    lines = text.splitlines()
    section_headers = {
        "args", "arguments", "parameters", "returns", "yields", "raises",
        "examples", "example", "attributes", "note", "notes", "see also",
        "references", "todo", "warning", "warnings",
    }
    out = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # numpydoc section header: "Examples:" / "Args:" etc.
        # (also matches the underlined form "Examples\n--------")
        if stripped.endswith(":") and stripped[:-1].strip().lower() in section_headers:
            if out and out[-1].strip():
                out.append("")
            out.append(stripped)
            continue
        if stripped.lower() in section_headers and i + 1 < len(lines) \
                and set(lines[i + 1].strip()) <= {"-"} and lines[i + 1].strip():
            if out and out[-1].strip():
                out.append("")
            out.append(stripped + ":")
            continue
        # "**kwargs**" style parameter line -> italic (with or without colon)
        if stripped.startswith("**") and stripped.endswith("**"):
            inner = stripped[2:-2].strip()
            if ":" in inner:
                colon = inner.find(":")
                param = inner[:colon].strip()
                rest = inner[colon + 1:].strip()
                line = line.replace(stripped, f"*{param}*: {rest}", 1)
            else:
                line = line.replace(stripped, f"*{inner}*", 1)
        elif stripped.startswith("**") and not stripped.endswith("**"):
            # numpydoc param name like "**experiment_kwargs" (no closing **)
            param = stripped[2:].strip()
            line = line.replace(stripped, f"*{param}*", 1)
        out.append(line)
    return out


def _describe_constant_value(obj, module_members=None):
    """Return a short human description of a constant's value, or "".

    - Small lists (<=8 items): render the actual values inline.
    - Large lists: report the element count (e.g. "List of 1000 items").
    - ``A + B`` list concatenation: sum the two referenced lists' sizes.
    - Dicts: report the key count, or the ``description`` field for
      judge-config dicts.
    - ``pathlib.Path``: report the path string.
    Returns "" when the value shape can't be determined.
    """
    try:
        val = obj.value
    except Exception:  # noqa: BLE001
        return ""
    if val is None:
        return ""
    vtype = type(val).__name__

    # pathlib.Path (or any object with a .as_posix / str form)
    if vtype == "Path" or "Path" in vtype:
        try:
            return f"Path: `{val.as_posix() if hasattr(val, 'as_posix') else str(val)}`"
        except Exception:  # noqa: BLE001
            return ""

    # A + B concatenation of two list constants
    if vtype == "ExprBinOp":
        try:
            left = getattr(val, "left", None)
            right = getattr(val, "right", None)
            op = getattr(val, "operator", None) or getattr(val, "op", None)
            op_name = type(op).__name__ if op is not None else ""
            is_add = (op == "+" or op_name == "Add")
            if is_add and left is not None and right is not None:
                ln = getattr(left, "name", None)
                rn = getattr(right, "name", None)
                if ln and rn and module_members is not None:
                    lsize = _list_size(module_members.get(ln))
                    rsize = _list_size(module_members.get(rn))
                    if lsize is not None and rsize is not None:
                        return (f"List of {lsize + rsize} items "
                                f"({ln} + {rn}).")
        except Exception:  # noqa: BLE001
            pass
        return ""

    # Griffe ExprDict: uses .keys/.values (no .elements)
    if "dict" in vtype.lower():
        keys = getattr(val, "keys", None)
        if keys is not None:
            try:
                n = len(keys)
            except Exception:  # noqa: BLE001
                n = 0
            desc = _dict_description(val)
            if desc:
                return desc
            return f"Dict with {n} keys."
        return ""

    # Griffe ExprList (and other collections) carry an .elements collection
    elements = getattr(val, "elements", None)
    if elements is not None:
        try:
            n = len(elements)
        except Exception:  # noqa: BLE001
            return ""
        kind = vtype.lower()
        if "list" in kind:
            if n <= 8:
                vals = _list_inline_values(val)
                if vals:
                    return f"Values: `{vals}`."
            return f"List of {n} items."
        return f"Collection of {n} items."
    # Plain Python list/dict (already-resolved value)
    if isinstance(val, list):
        if len(val) <= 8:
            vals = ", ".join(repr(v) for v in val)
            return f"Values: `{vals}`."
        return f"List of {len(val)} items."
    if isinstance(val, dict):
        return f"Dict with {len(val)} keys."
    return ""


def _strip_token(s):
    """Strip surrounding quotes from a raw source token string."""
    if not isinstance(s, str):
        return ""
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1]
    return s


def _render_scenario_list_md(val, depth=4):
    """Render a list-of-dicts (scenario pack) as a markdown table.

    Returns a list of markdown lines, or [] if the structure doesn't
    match the expected {name, description} shape.
    """
    elements = getattr(val, "elements", None)
    if elements is None:
        return []
    rows = []
    for el in elements:
        keys = getattr(el, "keys", None)
        values = getattr(el, "values", None)
        if keys is None or values is None:
            continue
        d = {}
        for i in range(len(keys)):
            k = _strip_token(keys[i])
            v = _strip_token(values[i])
            d[k] = v
        name = d.get("name", "")
        desc = d.get("description", "")
        if name:
            rows.append(f"| {name} | {desc} |")
    if not rows:
        return []
    return ["| Scenario | Description |", "| --- | --- |"] + rows


def _render_judge_config_md(val, depth=4):
    """Render a judge-config dict with its key fields as markdown.

    Returns a list of markdown lines, or [] if not a judge config.
    """
    keys = getattr(val, "keys", None)
    values = getattr(val, "values", None)
    if keys is None or values is None:
        return []
    d = {}
    for i in range(len(keys)):
        k = _strip_token(keys[i])
        v = values[i]
        # Values can be raw string tokens OR nested ExprDict objects
        if isinstance(v, str):
            d[k] = _strip_token(v)
        else:
            d[k] = v  # keep the object for nested handling
    if "description" not in d and "judge_prompt" not in d:
        return []
    lines = []
    if d.get("name"):
        lines.append(f"**Name:** {d['name']}")
        lines.append("")
    if d.get("description"):
        lines.append(d["description"])
        lines.append("")
    # Extract evaluation criteria from judge_prompt (numbered list).
    # The prompt is a raw source token with literal \n sequences.
    jp = d.get("judge_prompt", "")
    if jp and isinstance(jp, str):
        # Convert literal \n to actual newlines for parsing
        jp_text = jp.replace("\\n", "\n")
        criteria = []
        in_criteria = False
        for line in jp_text.splitlines():
            stripped = line.strip()
            if "EVALUATION CRITERIA" in stripped.upper():
                in_criteria = True
                continue
            if in_criteria:
                if stripped and (stripped[0].isdigit() or stripped.startswith("-")):
                    criteria.append(stripped)
                elif stripped and criteria:
                    break
                elif not stripped and criteria:
                    break
        if criteria:
            lines.append("**Evaluation criteria:**")
            lines.append("")
            for c in criteria:
                lines.append(f"- {c}")
            lines.append("")
    # Source field may be a nested ExprDict (e.g. {paper, url, authors})
    src = d.get("source")
    if src is not None:
        if isinstance(src, str):
            lines.append(f"**Source:** {src}")
            lines.append("")
        else:
            # Nested dict — render as key: value pairs
            src_keys = getattr(src, "keys", None)
            src_vals = getattr(src, "values", None)
            if src_keys is not None and src_vals is not None:
                lines.append("**Source:**")
                lines.append("")
                for i in range(len(src_keys)):
                    sk = _strip_token(src_keys[i])
                    sv = src_vals[i]
                    if isinstance(sv, str):
                        sv = _strip_token(sv)
                    lines.append(f"- {sk}: {sv}")
                lines.append("")
    return lines


def _list_size(obj):
    """Element count of a list-valued member, or None."""
    if obj is None:
        return None
    try:
        val = obj.value
    except Exception:  # noqa: BLE001
        return None
    elements = getattr(val, "elements", None)
    if elements is not None:
        try:
            return len(elements)
        except Exception:  # noqa: BLE001
            return None
    if isinstance(val, list):
        return len(val)
    return None


def _list_inline_values(val):
    """Render a small Griffe ExprList's string elements inline, or ""."""
    elements = getattr(val, "elements", None)
    if elements is None:
        return ""
    parts = []
    for el in elements:
        v = getattr(el, "value", el)
        if isinstance(v, str):
            s = v.strip()
            if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
                s = s[1:-1]
            parts.append(s)
    return ", ".join(parts) if parts else ""


def _dict_description(val):
    """Extract a human description from a Griffe ExprDict, or "".

    Judge-config dicts carry a ``description`` key. The keys/values are
    raw source-token strings (e.g. ``"'description'"``), so we strip the
    surrounding quotes. Returns the description string if present.
    """
    keys = getattr(val, "keys", None)
    values = getattr(val, "values", None)
    if keys is None or values is None:
        return ""
    try:
        n = len(keys)
    except Exception:  # noqa: BLE001
        return ""
    for i in range(n):
        k = keys[i]
        if not isinstance(k, str):
            k = getattr(k, "value", k)
        if not isinstance(k, str):
            continue
        if k.strip().strip("'\"") == "description":
            v = values[i]
            if not isinstance(v, str):
                v = getattr(v, "value", v)
            if isinstance(v, str):
                s = v.strip()
                if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
                    s = s[1:-1]
                return s
    return ""


def _extract_all_names(all_attr):
    """Extract the list of public names from a module's ``__all__`` attribute.

    Handles three shapes:
    - a plain list of strings (already-resolved value)
    - a Griffe ``ExprList`` of AST nodes (string literals carry text in ``.value``)
    - a Griffe ``ExprList`` of raw source tokens (strings like ``"'name'"``,
      plus ``'['`` / ``']'`` / ``', '`` separators)

    Returns an empty list if it can't be determined.
    """
    try:
        raw = all_attr.value
    except Exception:  # noqa: BLE001
        return []
    if raw is None:
        return []
    names = []
    for item in raw:
        # AST node (e.g. ast.Constant) -> its .value is the string
        val = getattr(item, "value", item)
        if not isinstance(val, str):
            continue
        s = val.strip()
        # Skip bracket / comma separator tokens from raw source
        if s in ("[", "]", ","):
            continue
        # Strip surrounding quotes from a string-literal token
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            s = s[1:-1]
        if s and s.isidentifier():
            names.append(s)
    return names


def _render_module_via_ast(module_name, py_path):
    """Render a module page using Python's ast module (fallback).

    Used when griffe.load fails (e.g. packages without __init__.py).
    Extracts top-level classes, functions, and constants with docstrings.
    """
    import ast

    with open(py_path) as f:
        source = f.read()

    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return (f"## {module_name}\n\n"
                f"> Could not parse this module: `{e}`\n")

    lines = [f"## {module_name}", ""]
    desc = MODULE_DESCRIPTIONS.get(module_name)
    mod_doc = ast.get_docstring(tree)
    if desc:
        lines += [desc, ""]
    if mod_doc:
        doc_body = mod_doc.strip()
        if not (desc and doc_body.splitlines()[0].strip() == desc):
            lines += [doc_body, ""]

    classes = []
    functions = []
    constants = []

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if not node.name.startswith("_"):
                doc = ast.get_docstring(node) or ""
                classes.append((node.name, doc))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                doc = ast.get_docstring(node) or ""
                # Build a simple signature
                args = []
                for a in node.args.args:
                    args.append(a.arg)
                if node.args.vararg:
                    args.append("*" + node.args.vararg.arg)
                for a in node.args.kwonlyargs:
                    args.append(a.arg)
                if node.args.kwarg:
                    args.append("**" + node.args.kwarg.arg)
                sig = f"({', '.join(args)})"
                prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
                functions.append((node.name, f"{prefix}def {node.name}{sig}", doc))
        elif isinstance(node, ast.Assign):
            # Top-level constant assignment
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    constants.append(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                constants.append(node.target.id)

    if classes:
        lines += ["### Classes", ""]
        for name, doc in classes:
            lines += [f"#### {name}", ""]
            if doc:
                lines += [doc.strip(), ""]
            else:
                lines += ["_No docstring._", ""]

    if functions:
        lines += ["### Functions", ""]
        for name, sig, doc in functions:
            lines += [f"#### {name}", ""]
            lines += [f"```python", sig, "```", ""]
            if doc:
                lines += [doc.strip(), ""]
            else:
                lines += ["_No docstring._", ""]

    if constants:
        lines += ["### Constants", ""]
        for name in constants:
            lines += [f"- `{name}`", ""]

    if not (classes or functions or constants):
        lines += ["_No public members found._", ""]

    return "\n".join(lines) + "\n"


def render_module_reference(module_name):
    """Render one module's public API as a markdown page (deterministic)."""
    import griffe
    Class, Function = griffe.Class, griffe.Function

    try:
        mod = griffe.load(module_name, search_paths=[ROOT])
    except Exception:  # noqa: BLE001
        # Fallback: use the ast module to extract top-level definitions.
        # This handles packages without __init__.py that griffe.load
        # can't resolve as a module path.
        py_path = os.path.join(ROOT, *module_name.split(".")) + ".py"
        if os.path.isfile(py_path):
            return _render_module_via_ast(module_name, py_path)
        else:
            return (f"## {module_name}\n\n"
                    f"> Could not locate source file for this module.\n")

    lines = [f"## {module_name}", ""]
    desc = MODULE_DESCRIPTIONS.get(module_name)
    mod_doc = _griffe_docstring(mod)
    if desc:
        lines += [desc, ""]
    # Surface the full module docstring (not just the first line) so rich
    # scenario-pack / data-module descriptions aren't lost.
    if mod_doc:
        doc_body = mod_doc.strip()
        # Skip if the docstring just repeats the one-line description
        if not (desc and doc_body.splitlines()[0].strip() == desc):
            lines += [doc_body, ""]

    try:
        all_members = dict(mod.members)
    except Exception:  # noqa: BLE001
        all_members = {}

    # Detect the __main__ guard so script-local names (e.g. `sev =
    # Counter(...)`) are excluded from the documented API.
    main_line = _main_guard_line(mod)

    members = {}
    for name, obj in all_members.items():
        if name.startswith("_"):
            continue
        # Skip members defined inside the `if __name__ == "__main__":` block
        if main_line is not None:
            ln = _member_lineno(obj)
            if ln is not None and ln >= main_line:
                continue
        if _is_public_member(obj):
            members[name] = obj
        else:
            # Re-exported import (Alias) — resolve to the real object so the
            # package index page documents the public API.
            resolved = _resolve_alias(obj)
            if resolved is not None and _is_public_member(resolved):
                members[name] = resolved

    # __all__ ordering if present
    all_attr = all_members.get("__all__")
    if all_attr is not None:
        ordered = _extract_all_names(all_attr)
        if ordered:
            members = {n: members[n] for n in ordered if n in members}

    if not members:
        lines += ["_No public members found._", ""]
        return "\n".join(lines) + "\n"

    # Group: classes, functions, constants
    classes = {n: o for n, o in members.items() if isinstance(o, Class)}
    functions = {n: o for n, o in members.items() if isinstance(o, Function)}
    constants = {n: o for n, o in members.items()
                 if not isinstance(o, (Class, Function))}

    if classes:
        lines += ["### Classes", ""]
        for n, o in classes.items():
            lines += _render_member_md(n, o, depth=4)
    if functions:
        lines += ["### Functions", ""]
        for n, o in functions.items():
            lines += _render_member_md(n, o, depth=4)
    if constants:
        lines += ["### Constants", ""]
        for n, o in constants.items():
            lines += _render_member_md(n, o, depth=4, module_members=all_members)

    # Special-case: render a scenario-pack catalog table when the module
    # exposes a SCENARIO_PACKS dict (the scenarios index).
    if module_name == "simpleaudit.scenarios":
        table = _scenario_packs_table(all_members)
        if table:
            lines += ["### Scenario Packs", ""]
            lines += table

    return "\n".join(lines) + "\n"


def _scenario_packs_table(all_members):
    """Render a markdown table of scenario packs from SCENARIO_PACKS.

    Returns a list of markdown lines, or [] if SCENARIO_PACKS is absent.
    """
    packs = all_members.get("SCENARIO_PACKS")
    if packs is None:
        return []
    try:
        val = packs.value
    except Exception:  # noqa: BLE001
        return []
    keys = getattr(val, "keys", None)
    values = getattr(val, "values", None)
    if keys is None or values is None:
        return []
    try:
        n = len(keys)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for i in range(n):
        k = getattr(keys[i], "value", keys[i])
        if isinstance(k, str):
            k = k.strip().strip("'\"")
        v = getattr(values[i], "value", values[i])
        # value is a list of scenario dicts; count them
        size = ""
        elements = getattr(v, "elements", None)
        if elements is not None:
            try:
                size = str(len(elements))
            except Exception:  # noqa: BLE001
                size = ""
        elif isinstance(v, list):
            size = str(len(v))
        elif isinstance(v, str):
            # raw source token like "[s1, s2, ...]" — count top-level commas
            stripped = v.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                inner = stripped[1:-1].strip()
                if inner:
                    size = str(inner.count(",") + 1)
        else:
            # ExprName reference (e.g. SAFETY_SCENARIOS) — resolve from module
            ref_name = getattr(v, "name", None)
            if ref_name and ref_name in all_members:
                ref_obj = all_members[ref_name]
                try:
                    ref_val = ref_obj.value
                    ref_elements = getattr(ref_val, "elements", None)
                    if ref_elements is not None:
                        size = str(len(ref_elements))
                    elif isinstance(ref_val, list):
                        size = str(len(ref_val))
                except Exception:  # noqa: BLE001
                    pass
        rows.append(f"| `{k}` | {size} |")
    if not rows:
        return []
    return ["| Pack | Scenarios |", "| --- | --- |"] + rows


def build_api_reference():
    """Generate reference/*.md for every module. Returns list of (module, slug)."""
    os.makedirs(REF, exist_ok=True)
    modules = []
    for dirpath, _, filenames in os.walk(os.path.join(ROOT, "simpleaudit")):
        for f in filenames:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), ROOT)
            mod = rel[:-3].replace(os.sep, ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
            if mod.endswith(".images.make_images"):
                continue  # asset generator, not API
            modules.append(mod)
    modules.sort()

    rendered = []
    for mod in modules:
        print(f"    reference: {mod}")
        content = render_module_reference(mod)
        slug = mod.replace(".", "_")
        with open(os.path.join(REF, f"{slug}.md"), "w") as f:
            f.write(content)
        rendered.append((mod, slug))

    # Remove stale reference pages
    current = {slug for _, slug in rendered}
    for fname in os.listdir(REF):
        if fname.endswith(".md") and fname[:-3] not in current:
            os.remove(os.path.join(REF, fname))
    return rendered


# ---------------------------------------------------------------------------
# Layer 2: LLM narrative pages
# ---------------------------------------------------------------------------


def determine_structure():
    """Ask LLM to plan the narrative documentation structure."""
    tree = get_file_tree()
    readme = read_file(os.path.join(ROOT, "README.md"))[:3000]

    prompt = f"""You are a technical documentation architect. Given this Python project's file tree and README, plan a developer documentation structure.

FILE TREE:
{tree}

README (excerpt):
{readme}

Return a JSON array of documentation pages. Each entry: {{"title": "Page Title", "slug": "kebab-case-slug", "description": "One-line description", "source_files": ["relative/path.py", ...]}}

Rules:
- 8-12 narrative pages total (the API reference is generated separately — do NOT include an api-reference page)
- Must include: Getting Started, Core Architecture, CLI Usage, and pages covering the major subsystems (judges, scenarios, results/analysis, visualization, advanced workflows like cross-judge / repeated runs / reframing / judge validation)
- source_files lists which files each page should cover
- Be specific and practical for developers who need to USE this library
- Return ONLY the JSON array, no markdown fences"""

    result = chat([{"role": "user", "content": prompt}], use_kb=False,
                  max_tokens=2048, temperature=0.1)
    pages = json.loads(strip_code_fences(result))
    return pages


def generate_page(page, source_files):
    """Generate or update a single narrative documentation page."""
    title = page["title"]
    desc = page.get("description", "")
    files = page.get("source_files", [])

    code_context = ""
    for f in files:
        if f in source_files:
            content = source_files[f]
            if len(content) > 12000:
                content = content[:12000] + "\n# ... (truncated)"
            code_context += f"\n### {f}\n```\n{content}\n```\n"

    if len(code_context) > 60000:
        code_context = code_context[:60000] + "\n# ... (context truncated)"

    existing_path = os.path.join(OUT, f"{page['slug']}.md")
    old_content = ""
    if os.path.exists(existing_path):
        with open(existing_path, "r") as f:
            old_content = f.read()

    if old_content:
        prompt = f"""You are a senior technical writer maintaining developer documentation for the simpleaudit Python library.

A documentation page already exists. The source code it documents has changed.
Your job is to UPDATE the existing page to reflect the current source code.

EXISTING PAGE (keep as much of this as still accurate):
<existing_page>
{old_content}
</existing_page>

CURRENT SOURCE CODE:
{code_context}

INSTRUCTIONS:
- Compare the existing page against the current source code.
- ONLY change sections that are now inaccurate, outdated, or incomplete.
- Preserve the existing structure, tone, headings, and examples that are still correct.
- Add documentation for any new public classes, functions, or parameters.
- Remove or update documentation for anything that no longer exists.
- Do NOT reword or restructure sections that are still accurate.
- Do NOT add new sections unless the source code introduces genuinely new functionality.
- Keep the same Markdown formatting style (## headers, code blocks, tables).
- Start with a ## {title} header. Do NOT include a # title header.

Return ONLY the complete updated markdown page, no preamble."""
    else:
        prompt = f"""You are a senior technical writer generating developer documentation for the simpleaudit Python library.

Write a complete documentation page titled "{title}".
Description: {desc}

SOURCE CODE CONTEXT:
{code_context}

REQUIREMENTS:
- Write in clear, professional technical English
- Include a brief overview of what this module/subsystem does
- Document all public classes, functions, and their parameters
- Include code examples showing how to use the API
- Use proper Markdown with headers (##, ###), code blocks, and tables where appropriate
- Reference specific class names, function names, and file paths
- If the source shows configuration options, document them
- Keep it practical: a developer should be able to use the library after reading this page
- Start with a ## {title} header
- Do NOT include a title (# header) - just start with ##
- Length: 800-2000 words depending on complexity

Return ONLY the markdown content, no preamble."""

    content = chat(
        [{"role": "user", "content": prompt}],
        use_kb=True,
        max_tokens=8192,
        temperature=0.3 if old_content else 0.7,
    )
    return content


# ---------------------------------------------------------------------------
# MkDocs site assembly (index, nav, mkdocs.yml)
# ---------------------------------------------------------------------------

# Narrative page sections for the index + nav (slug -> section)
NARRATIVE_SECTIONS = [
    ("Start Here", ["getting-started", "core-architecture", "cli-usage"]),
    ("Judges", ["judges", "judge-validation", "cross-judge-validation"]),
    ("Scenarios", ["scenarios"]),
    ("Results & Tooling", ["results-analysis", "visualization"]),
    ("Advanced", ["reframing-advanced-workflows"]),
]

# Reference nav grouping: (section title, [module prefixes])
REFERENCE_SECTIONS = [
    ("Core", ["simpleaudit", "simpleaudit.model_auditor", "simpleaudit.results",
              "simpleaudit.experiment", "simpleaudit.repeated_results",
              "simpleaudit.cross_judge", "simpleaudit.reframing",
              "simpleaudit.judge_the_judge", "simpleaudit.cli", "simpleaudit.utils"]),
    ("Judges", ["simpleaudit.judges"]),
    ("Scenarios", ["simpleaudit.scenarios"]),
    ("Visualization", ["simpleaudit.visualization"]),
]

# Modules that belong to "Core" even though they live under a sub-package
# prefix. Used to prevent the Core section from swallowing Judges/Scenarios
# submodules (the bare "simpleaudit" prefix would match everything).
_CORE_EXACT = {
    "simpleaudit", "simpleaudit.model_auditor", "simpleaudit.results",
    "simpleaudit.experiment", "simpleaudit.repeated_results",
    "simpleaudit.cross_judge", "simpleaudit.reframing",
    "simpleaudit.judge_the_judge", "simpleaudit.cli", "simpleaudit.utils",
}


def _section_modules(section_name, reference_modules):
    """Return the modules belonging to a reference section (no duplicates)."""
    if section_name == "Core":
        return [m for m in reference_modules if m in _CORE_EXACT]
    prefixes = dict(REFERENCE_SECTIONS)[section_name]
    return [m for m in reference_modules
            if any(m == p or m.startswith(p + ".") for p in prefixes)]


def build_index_md(pages, reference_modules):
    """Deterministic index page (no LLM — the LLM version kept drifting)."""
    page_map = {p["slug"]: p for p in pages}
    assigned = set()
    lines = [
        "# SimpleAudit",
        "",
        "Lightweight AI safety auditing: LLM judges score model outputs for safety,",
        "harm, factuality, and helpfulness across curated scenario packs. Results are",
        "aggregated, compared across models and judges, and visualized in the browser.",
        "",
        "## Quick Start",
        "",
        "```bash",
        "pip install simpleaudit",
        "```",
        "",
        "```python",
        "from simpleaudit import ModelAuditor, get_scenarios",
        "",
        "auditor = ModelAuditor(",
        '    model="gpt-4o-mini",',
        '    provider="openai",',
        '    judge_model="gpt-4o",',
        '    judge_provider="openai",',
        ")",
        "results = auditor.run(get_scenarios(\"safety\"))",
        "results.summary()",
        "```",
        "",
        "```bash",
        "# Browse results in the browser",
        "simpleaudit serve --results_dir ./results",
        "```",
        "",
        "## Guides",
        "",
    ]
    for section_name, slugs in NARRATIVE_SECTIONS:
        section_pages = [page_map[s] for s in slugs if s in page_map]
        if not section_pages:
            continue
        lines.append(f"### {section_name}")
        lines.append("")
        for p in section_pages:
            lines.append(f"- [{p['title']}](guides/{p['slug']}.md) — {p.get('description', '')}")
            assigned.add(p["slug"])
        lines.append("")

    unassigned = [p for p in pages if p["slug"] not in assigned]
    if unassigned:
        lines.append("### More")
        lines.append("")
        for p in unassigned:
            lines.append(f"- [{p['title']}](guides/{p['slug']}.md) — {p.get('description', '')}")
        lines.append("")

    lines += ["## API Reference", ""]
    for section_name, _ in REFERENCE_SECTIONS:
        mods = _section_modules(section_name, reference_modules)
        if not mods:
            continue
        lines.append(f"### {section_name}")
        lines.append("")
        for m in mods:
            slug = m.replace(".", "_")
            desc = MODULE_DESCRIPTIONS.get(m, "")
            lines.append(f"- [`{m}`](reference/{slug}.md) — {desc}")
        lines.append("")

    return "\n".join(lines)


def cross_link_guides():
    """Append a 'See Also' section to each guide page with links to related pages.

    Runs after all guide pages are written. Idempotent: strips any existing
    'See Also' section before re-adding, so repeated runs don't duplicate.
    """
    guides_dir = OUT
    if not os.path.isdir(guides_dir):
        return

    # Collect all guide pages: slug -> (title, headings)
    pages_info = {}
    for fname in sorted(os.listdir(guides_dir)):
        if not fname.endswith(".md"):
            continue
        slug = fname[:-3]
        path = os.path.join(guides_dir, fname)
        with open(path) as f:
            content = f.read()
        # Extract title from first ## heading (preserves casing like "CLI")
        title = slug.replace("-", " ").title()
        for line in content.splitlines():
            if line.startswith("## "):
                title = line[3:].strip()
                break
        headings = []
        for line in content.splitlines():
            if line.startswith("## ") or line.startswith("### "):
                h = line.lstrip("#").strip()
                if h.lower() != "see also":
                    headings.append(h)
        pages_info[slug] = {"title": title, "headings": headings, "path": path}

    if len(pages_info) < 2:
        return

    # Build a keyword index: word -> set of slugs that mention it
    import re as _re
    word_to_slugs = {}
    for slug, info in pages_info.items():
        text = " ".join([info["title"]] + info["headings"]).lower()
        words = set(_re.findall(r"[a-z]{3,}", text))
        for w in words:
            word_to_slugs.setdefault(w, set()).add(slug)

    # Common words that don't help distinguish pages
    stop = {"the", "and", "for", "with", "from", "that", "this", "using",
            "use", "via", "all", "any", "not", "can", "may", "will", "are",
            "was", "were", "has", "have", "had", "its", "their", "your",
            "our", "how", "what", "when", "where", "which", "who", "why",
            "simpleaudit", "audit", "audits", "model", "models", "python",
            "function", "functions", "class", "classes", "method", "methods",
            "example", "examples", "usage", "guide", "guides", "page", "pages",
            "section", "sections", "module", "modules", "reference", "api",
            "overview", "details", "note", "notes", "tip", "tips", "best",
            "practices", "troubleshooting", "configuration", "config",
            "implementation", "architecture", "core", "basic", "advanced",
            "getting", "started", "installation", "setup", "environment",
            "variables", "command", "commands", "line", "interface", "output",
            "input", "data", "file", "files", "directory", "path", "paths",
            "error", "errors", "handling", "resilience", "privacy", "handling"}

    # Build section membership: slug -> section name
    slug_section = {}
    for section_name, slugs in NARRATIVE_SECTIONS:
        for s in slugs:
            slug_section[s] = section_name

    def related_to(slug, max_results=4):
        """Find pages most related to `slug`.

        Scoring: +2 for same nav section, +1 per shared heading keyword.
        """
        info = pages_info[slug]
        my_words = set(_re.findall(r"[a-z]{3,}",
                     " ".join([info["title"]] + info["headings"]).lower()))
        my_words -= stop
        my_section = slug_section.get(slug)
        scores = {}
        for other_slug, other_info in pages_info.items():
            if other_slug == slug:
                continue
            score = 0
            # Same nav section gets a boost
            if my_section and slug_section.get(other_slug) == my_section:
                score += 2
            # Shared heading keywords
            other_words = set(_re.findall(r"[a-z]{3,}",
                          " ".join([other_info["title"]] + other_info["headings"]).lower()))
            other_words -= stop
            score += len(my_words & other_words)
            if score > 0:
                scores[other_slug] = score
        # Sort by score desc, then alphabetically
        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        return [s for s, _ in ranked[:max_results]]

    # Rewrite each guide: strip old See Also, append new one
    for slug, info in pages_info.items():
        with open(info["path"]) as f:
            content = f.read()

        # Strip existing "### See Also" section (to end of file or next ##)
        content = _re.sub(
            r"\n### See Also\n[\s\S]*?(?=\n## |\Z)",
            "",
            content,
        ).rstrip() + "\n"

        related = related_to(slug)
        if not related:
            # Fallback: link to all other pages (better than no links)
            related = [s for s in pages_info if s != slug][:4]

        lines = ["", "### See Also", ""]
        for r_slug in related:
            r_info = pages_info[r_slug]
            lines.append(f"*   [{r_info['title']}]({r_slug}.md)")
        content += "\n".join(lines) + "\n"

        with open(info["path"], "w") as f:
            f.write(content)

    print(f"  Cross-linked {len(pages_info)} guide pages")


def build_nav(pages, reference_modules):
    """Build the mkdocs nav structure."""
    page_map = {p["slug"]: p for p in pages}
    # Plain "index.md" (no title) — the titled form trips mkdocs strict mode
    nav = ["index.md"]

    nav.append(["Guides", []])
    guides_nav = nav[-1][1]
    for section_name, slugs in NARRATIVE_SECTIONS:
        section_pages = [page_map[s] for s in slugs if s in page_map]
        if not section_pages:
            continue
        guides_nav.append([section_name, [f"guides/{p['slug']}.md" for p in section_pages]])
    unassigned = [p for p in pages
                  if p["slug"] not in {s for _, slugs in NARRATIVE_SECTIONS for s in slugs}]
    if unassigned:
        guides_nav.append(["More", [f"guides/{p['slug']}.md" for p in unassigned]])

    nav.append(["API Reference", []])
    ref_nav = nav[-1][1]
    for section_name, _ in REFERENCE_SECTIONS:
        mods = _section_modules(section_name, reference_modules)
        if not mods:
            continue
        ref_nav.append([section_name, [f"reference/{m.replace('.', '_')}.md" for m in mods]])

    return nav


def build_mkdocs_yml(nav):
    """Write mkdocs.yml (in docs/) with Material theme + mkdocstrings.

    Layout: docs/mkdocs.yml  |  docs_dir = docs/site_src  |  site_dir = docs/site
    """
    nav_yaml = _nav_to_yaml(nav)
    content = f'''# MkDocs configuration for SimpleAudit
# Generated by docs/generate_docs.py — safe to edit theme options,
# but the `nav` section is regenerated on each run.

site_name: SimpleAudit
site_description: Lightweight AI Safety Auditing Framework
site_url: https://sushantgautam.github.io/simpleaudit-docs/
repo_url: https://github.com/SushantGautam/simpleaudit
repo_name: SushantGautam/simpleaudit

docs_dir: site_src
site_dir: site

theme:
  name: material
  palette:
    - scheme: default
      primary: indigo
      accent: indigo
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - scheme: slate
      primary: indigo
      accent: indigo
      toggle:
        icon: material/brightness-4
        name: Switch to light mode
  features:
    - navigation.sections
    - navigation.top
    - navigation.footer
    - content.code.copy
    - search.highlight
    - search.suggest
    - toc.follow

plugins:
  - search
  - mkdocstrings:
      handlers:
        python:
          options:
            paths: ["{ROOT}"]
            docstring_style: numpy
            show_source: true
            merge_init_into_class: true

markdown_extensions:
  - admonition
  - attr_list
  - md_in_html
  - tables
  - toc:
      permalink: true
  - pymdownx.highlight:
      anchor_linenums: true
  - pymdownx.inlinehilite
  - pymdownx.snippets
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format

nav:
{nav_yaml}
'''
    # Config lives in the docs repo root (parent of docs_dir=site_src,
    # sibling of site_dir=site)
    with open(os.path.join(DOCS, "mkdocs.yml"), "w") as f:
        f.write(content)


def _nav_to_yaml(nav, indent=0):
    """Serialize the nav list to YAML (no external deps).

    Nav items can be:
      - "path.md"                      -> plain page
      - ["path.md", "Title"]           -> page with custom title
      - ["Section", [children]]         -> section
    """
    pad = "  " * (indent + 1)
    lines = []
    for item in nav:
        if isinstance(item, str):
            lines.append(f"{pad}- {item}")
        elif isinstance(item, dict):
            path, title = next(iter(item.items()))
            lines.append(f"{pad}- {path}: {title}")
        elif len(item) == 2 and isinstance(item[1], str):
            # [path, title]
            lines.append(f"{pad}- {item[0]}: {item[1]}")
        else:
            # [title, [children]]
            title, children = item
            lines.append(f"{pad}- {title}:")
            lines.append(_nav_to_yaml(children, indent + 1))
    return "\n".join(lines)



# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="SimpleAudit documentation generator")
    parser.add_argument("--force", action="store_true",
                        help="Full re-plan + regenerate everything")
    parser.add_argument("--no-llm", action="store_true",
                        help="Deterministic layer only (API reference + site assembly), skip LLM pages")
    parser.add_argument("--build", action="store_true",
                        help="Run `mkdocs build` after generating")
    parser.add_argument("--no-build", action="store_true",
                        help="Skip `mkdocs build` even if --build is set (CI builds separately)")
    args = parser.parse_args()

    # CI: restore the incremental cache (guides + plan) from the actions cache
    ci_cache = os.path.join(DOCS, ".docs_cache")
    if os.path.isdir(ci_cache):
        if os.path.isfile(os.path.join(ci_cache, ".plan.json")):
            shutil.copy2(os.path.join(ci_cache, ".plan.json"), PLAN_FILE)
        ci_guides = os.path.join(ci_cache, "guides")
        if os.path.isdir(ci_guides):
            for f in os.listdir(ci_guides):
                if f.endswith(".md"):
                    shutil.copy2(os.path.join(ci_guides, f), os.path.join(OUT, f))
        print("Restored docs cache from CI")

    os.makedirs(OUT, exist_ok=True)
    os.makedirs(REF, exist_ok=True)

    print("=== SimpleAudit Doc Generator ===")
    print(f"Site source: {SITE_SRC}")
    if not args.no_llm:
        print(f"LLM backend: {BASE} (KB: {KB_ID})")
    print()

    # Step 1: load sources + hashes
    print("[1/6] Loading source files...")
    source_files = get_source_files()
    trackable_files = get_all_trackable_files()
    new_hashes = compute_file_hashes(trackable_files)
    print(f"  Loaded {len(source_files)} Python files, tracking {len(trackable_files)} files")
    print()

    # Step 2: deterministic API reference (always regenerated — it's cheap)
    print("[2/6] Building API reference (Griffe)...")
    reference_modules = [m for m, _ in build_api_reference()]
    print(f"  {len(reference_modules)} modules documented")
    print()

    # Step 3: narrative pages (LLM)
    old_plan = None if args.force else load_plan()
    old_hashes = old_plan.get("file_hashes", {}) if old_plan else {}

    if args.no_llm:
        print("[3/6] Narrative pages — SKIPPED (--no-llm)")
        pages = old_plan.get("pages", []) if old_plan else []
        if not pages:
            print("  No stored plan found; index will list guides as they appear.")
    else:
        if old_plan and not args.force:
            structure_valid = set(old_hashes.keys()) == set(new_hashes.keys())
            if structure_valid:
                pages = old_plan.get("pages", [])
                print(f"[3/6] Reusing stored plan ({len(pages)} narrative pages)")
            else:
                added = set(new_hashes.keys()) - set(old_hashes.keys())
                removed = set(old_hashes.keys()) - set(new_hashes.keys())
                print("[3/6] Source file set changed — re-planning structure...")
                if added:
                    print(f"  Added: {', '.join(sorted(added))}")
                if removed:
                    print(f"  Removed: {', '.join(sorted(removed))}")
                pages = determine_structure()
                print(f"  Planned {len(pages)} pages:")
                for p in pages:
                    print(f"    - {p['title']} ({p['slug']})")
        else:
            label = "--force: re-planning" if args.force else "First run — planning"
            print(f"[3/6] {label} documentation structure...")
            pages = determine_structure()
            print(f"  Planned {len(pages)} pages:")
            for p in pages:
                print(f"    - {p['title']} ({p['slug']})")
            old_hashes = {}

        print()
        print("[4/6] Generating narrative pages...")
        plan_slugs = {p["slug"] for p in pages}
        any_generated = False

        for i, page in enumerate(pages):
            slug = page["slug"]
            title = page["title"]
            out_path = os.path.join(OUT, f"{slug}.md")

            if (not args.force
                    and os.path.exists(out_path)
                    and os.path.getsize(out_path) > 200
                    and not page_needs_regeneration(page, old_hashes, new_hashes)):
                print(f"  [{i + 1}/{len(pages)}] {title} — SKIP (source unchanged)")
                continue

            if args.force:
                reason = "forced"
            elif not old_plan:
                reason = "first run"
            else:
                changed = [f for f in page.get("source_files", [])
                           if old_hashes.get(f) != new_hashes.get(f)]
                reason = f"changed: {', '.join(changed)}" if changed else "new page"

            print(f"  [{i + 1}/{len(pages)}] {title} ({reason})...", end=" ", flush=True)
            try:
                content = generate_page(page, source_files)
                with open(out_path, "w") as f:
                    f.write(content)
                print(f"OK ({len(content)} chars)")
                any_generated = True
            except Exception as e:  # noqa: BLE001
                print(f"FAILED: {e}")
            time.sleep(1)  # rate limit

        stale = cleanup_stale_pages(plan_slugs)
        if stale:
            print(f"  Removed {len(stale)} stale pages: {', '.join(stale)}")

        store_plan(pages, new_hashes)

    # Step 5: cross-link guide pages
    print()
    print("[5/6] Cross-linking guide pages...")
    cross_link_guides()

    # Step 6: assemble MkDocs site
    print()
    print("[6/6] Assembling MkDocs site...")
    index_md = build_index_md(pages, reference_modules)
    with open(os.path.join(SITE_SRC, "index.md"), "w") as f:
        f.write(index_md)
    print("  index.md")

    nav = build_nav(pages, reference_modules)
    build_mkdocs_yml(nav)
    print("  mkdocs.yml")

    # Copy scenario images into the site so guide pages can reference them
    img_src = os.path.join(ROOT, "simpleaudit", "scenarios", "images")
    img_dst = os.path.join(SITE_SRC, "images")
    if os.path.isdir(img_src):
        os.makedirs(img_dst, exist_ok=True)
        for f in os.listdir(img_src):
            if f.endswith(".png"):
                shutil.copy2(os.path.join(img_src, f), os.path.join(img_dst, f))
        print(f"  images/ ({len(os.listdir(img_dst))} files)")

    n_guides = len([f for f in os.listdir(OUT) if f.endswith(".md")])
    n_ref = len([f for f in os.listdir(REF) if f.endswith(".md")])
    print(f"\nDone. {n_guides} guide pages + {n_ref} reference pages in {SITE_SRC}")
    print("Build the site with:  cd docs && mkdocs build")

    # CI: persist the incremental cache for the next run
    if os.path.isdir(ci_cache):
        os.makedirs(os.path.join(ci_cache, "guides"), exist_ok=True)
        if os.path.exists(PLAN_FILE):
            shutil.copy2(PLAN_FILE, os.path.join(ci_cache, ".plan.json"))
        for f in os.listdir(OUT):
            if f.endswith(".md"):
                shutil.copy2(os.path.join(OUT, f), os.path.join(ci_cache, "guides", f))

    if args.build and not args.no_build:
        print("\n[build] Running mkdocs build...")
        # Use the current interpreter's mkdocs module (avoids PATH issues)
        subprocess.run(
            [sys.executable, "-m", "mkdocs", "build"],
            cwd=DOCS, check=True,
        )
        print("  Site built to site/ ✓")


if __name__ == "__main__":
    main()
