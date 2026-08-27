#!/usr/bin/env python3
"""
AutoDocs — generic two-layer documentation generator.

This script is project-agnostic. All project-specific values (package name,
site metadata, nav sections, module descriptions, LLM prompts) are read from
an ``autodocs.yml`` config file that lives next to this script.

Layer 1 (deterministic, no LLM):
  - Griffe scans the package source tree and renders a complete,
    always-accurate API reference (classes, functions, signatures,
    docstrings) into site_src/reference/*.md
  - mkdocs.yml, nav, and the index page are generated from the config

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
  SOURCE_ROOT     Path to the source repo (default: parent of docs/)
  OWUI_API_KEY    Open WebUI API key (required for LLM layer)
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
# Configuration loading
# ---------------------------------------------------------------------------

DOCS = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DOCS, "autodocs.yml")


def _load_config():
    """Load autodocs.yml. Uses PyYAML if available, else a minimal parser."""
    try:
        import yaml
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f)
    except ImportError:
        return _minimal_yaml_parse(CONFIG_FILE)


def _minimal_yaml_parse(path):
    """Very small YAML subset parser (no external deps).

    Handles: scalars, nested dicts, lists of scalars, lists of dicts,
    block scalars (| and >). Sufficient for autodocs.yml.
    """
    with open(path) as f:
        lines = f.readlines()

    def parse_block(lines, indent=0):
        result = {}
        i = 0
        while i < len(lines):
            raw = lines[i]
            if not raw.strip() or raw.strip().startswith("#"):
                i += 1
                continue
            cur_indent = len(raw) - len(raw.lstrip())
            if cur_indent < indent:
                break
            stripped = raw.strip()
            if stripped.startswith("- "):
                if not isinstance(result, list):
                    result = []
                item_val = stripped[2:].strip()
                if ":" in item_val and not item_val.startswith('"'):
                    d = {}
                    key, _, val = item_val.partition(":")
                    d[key.strip()] = val.strip().strip('"')
                    i += 1
                    while i < len(lines):
                        cont = lines[i]
                        if not cont.strip() or cont.strip().startswith("#"):
                            i += 1
                            continue
                        cont_indent = len(cont) - len(cont.lstrip())
                        if cont_indent <= cur_indent:
                            break
                        cs = cont.strip()
                        if cs.startswith("- "):
                            key2 = "slugs"
                            if key2 not in d:
                                d[key2] = []
                            d[key2].append(cs[2:].strip().strip('"'))
                            i += 1
                        elif ":" in cs:
                            k2, _, v2 = cs.partition(":")
                            v2 = v2.strip()
                            if v2.startswith("["):
                                items = v2.strip("[]").split(",")
                                d[k2.strip()] = [x.strip().strip('"') for x in items if x.strip()]
                            else:
                                d[k2.strip()] = v2.strip('"')
                            i += 1
                        else:
                            i += 1
                    result.append(d)
                else:
                    result.append(item_val.strip('"'))
                    i += 1
                continue
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip()
                if val == "" or val == "|" or val == ">":
                    block_lines = []
                    i += 1
                    while i < len(lines):
                        bl = lines[i]
                        if not bl.strip():
                            block_lines.append("")
                            i += 1
                            continue
                        bl_indent = len(bl) - len(bl.lstrip())
                        if bl_indent <= cur_indent:
                            break
                        block_lines.append(bl.strip())
                        i += 1
                    if val in ("|", ">"):
                        joiner = "\n" if val == "|" else " "
                        result[key] = joiner.join(block_lines).strip()
                    else:
                        sub = []
                        for bl in block_lines:
                            if bl.startswith("- "):
                                sub.append(bl[2:].strip().strip('"'))
                        if sub and all(not ":" in s for s in sub):
                            result[key] = sub
                        else:
                            nested = {}
                            for bl in block_lines:
                                if ":" in bl:
                                    nk, _, nv = bl.partition(":")
                                    nv = nv.strip().strip('"')
                                    if nv.startswith("["):
                                        items = nv.strip("[]").split(",")
                                        nested[nk.strip()] = [x.strip().strip('"') for x in items if x.strip()]
                                    else:
                                        nested[nk.strip()] = nv
                            result[key] = nested
                elif val.startswith("["):
                    items = val.strip("[]").split(",")
                    result[key] = [x.strip().strip('"') for x in items if x.strip()]
                else:
                    result[key] = val.strip('"')
                i += 1
                continue
            i += 1
        return result

    return parse_block(lines)


CFG = _load_config()

# --- Derived paths ---
ROOT = os.environ.get(
    "SOURCE_ROOT",
    CFG.get("root", os.path.dirname(DOCS)),
)
PACKAGE_DIR = CFG.get("package_dir", "")
PKG_PATH = os.path.join(ROOT, PACKAGE_DIR)
SITE_SRC = os.path.join(DOCS, "site_src")
OUT = os.path.join(SITE_SRC, "guides")
REF = os.path.join(SITE_SRC, "reference")
PLAN_FILE = os.path.join(SITE_SRC, ".plan.json")

# --- LLM config ---
LLM_CFG = CFG.get("llm", {})
BASE = LLM_CFG.get("base_url", "")
KEY = os.environ.get("OWUI_API_KEY", "")
KB_ID = LLM_CFG.get("kb_id", "")
MODEL = LLM_CFG.get("model", "default")

# --- Project identity ---
PROJECT_NAME = CFG.get("project_name", "Project")

# --- Site identity (from the `site:` block in autodocs.yml) ---
SITE_CFG = CFG.get("site", {})
SITE_NAME = SITE_CFG.get("name", PROJECT_NAME)
SITE_DESCRIPTION = SITE_CFG.get("description", "")
SITE_URL = SITE_CFG.get("url", "")
REPO_URL = SITE_CFG.get("repo_url", "")
REPO_NAME = SITE_CFG.get("repo_name", "")
EDIT_URI = SITE_CFG.get("edit_uri", "")
SITE_AUTHOR = SITE_CFG.get("author", "")
COPYRIGHT = SITE_CFG.get("copyright", "")

# --- Build directories (from the `build:` block in autodocs.yml) ---
BUILD_CFG = CFG.get("build", {})
DOCS_DIR = BUILD_CFG.get("docs_dir", "site_src")
SITE_DIR = BUILD_CFG.get("site_dir", "site")
USE_DIRECTORY_URLS = BUILD_CFG.get("use_directory_urls", True)

# --- Module descriptions ---
MODULE_DESCRIPTIONS = CFG.get("module_descriptions", {})

# --- Nav sections ---
NARRATIVE_SECTIONS = [
    (s["title"], s["slugs"]) for s in CFG.get("narrative_sections", [])
]
REFERENCE_SECTIONS = [
    (s["title"], s["modules"]) for s in CFG.get("reference_sections", [])
]

# --- Index page config ---
INDEX_CFG = CFG.get("index", {})

# --- Theme ---
THEME_CFG = CFG.get("theme", {})
THEME_PRIMARY = THEME_CFG.get("primary", "indigo")
THEME_ACCENT = THEME_CFG.get("accent", "indigo")
THEME_FEATURES = THEME_CFG.get("features", [
    "navigation.sections", "navigation.top", "navigation.footer",
    "content.code.copy", "search.highlight", "search.suggest", "toc.follow",
])

# --- Plugins & markdown extensions (from autodocs.yml) ---
PLUGINS_CFG = CFG.get("plugins", ["search"])
MARKDOWN_EXTENSIONS_CFG = CFG.get("markdown_extensions", [])

# --- KB upload config ---
KB_CFG = CFG.get("kb", {})


# ---------------------------------------------------------------------------
# File utilities
# ---------------------------------------------------------------------------

def read_file(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def get_file_tree():
    """Build a file tree of the package."""
    lines = []
    for dirpath, dirnames, filenames in os.walk(PKG_PATH):
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
    for dirpath, _, filenames in os.walk(PKG_PATH):
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
    for dirpath, _, filenames in os.walk(PKG_PATH):
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
# LLM client
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
        payload["knowledge_id"] = KB_ID

    for attempt in range(retries):
        try:
            r = requests.post(f"{BASE}/api/v1/chat/completions",
                              headers=headers, json=payload, timeout=120)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            if attempt < retries - 1:
                wait = 2 ** attempt
                print(f"  retry {attempt + 1}/{retries} in {wait}s ({e})")
                time.sleep(wait)
            else:
                raise


def strip_code_fences(text):
    """Remove markdown code fences if the LLM wrapped its JSON in them."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


# ---------------------------------------------------------------------------
# Layer 1: deterministic API reference via Griffe
# ---------------------------------------------------------------------------

def _griffe_docstring(obj):
    """Best-effort docstring text for a Griffe object (never raises)."""
    try:
        doc = obj.docstring
    except Exception:  # noqa: BLE001
        return ""
    if doc is None:
        return ""
    text = getattr(doc, "value", doc)
    if text is None:
        return ""
    return str(text).strip()


def _griffe_signature(obj):
    """Best-effort signature string (never raises)."""
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
    """Resolve a Griffe Alias to its target object, or None on failure."""
    import griffe
    if not isinstance(obj, griffe.Alias):
        return obj
    try:
        return obj.target
    except Exception:  # noqa: BLE001
        return None


def _main_guard_line(mod):
    """Return the 1-indexed line of the ``if __name__ == "__main__":`` guard."""
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
    except Exception:  # noqa: BLE001
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
        lines.append(f"{header} `{name}`")
        if doc_lines:
            lines.append("")
            lines.extend(doc_lines)
            lines.append("")
        try:
            val = obj.value
        except Exception:  # noqa: BLE001
            val = None
        vtype = type(val).__name__ if val is not None else ""
        rendered_rich = False
        if "list" in vtype.lower() and val is not None:
            elements = getattr(val, "elements", None)
            if elements and len(elements) > 0:
                el0 = elements[0]
                k0 = getattr(el0, "keys", None)
                if k0 is not None:
                    k0_stripped = [_strip_token(k) for k in k0]
                    if "name" in k0_stripped and "description" in k0_stripped:
                        table = _render_name_desc_list_md(val)
                        if table:
                            lines.append("")
                            lines.extend(table)
                            lines.append("")
                            rendered_rich = True
        if not rendered_rich and "dict" in vtype.lower() and val is not None:
            config_md = _render_config_dict_md(val)
            if config_md:
                lines.append("")
                lines.extend(config_md)
                lines.append("")
                rendered_rich = True
        if not rendered_rich and not doc_lines:
            size_note = _describe_constant_value(obj, module_members)
            if size_note:
                lines.append("")
                lines.append(f"_{size_note}_")
                lines.append("")
    return lines


def _format_docstring(doc):
    """Format a docstring for markdown, handling numpydoc sections."""
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
            param = stripped[2:].strip()
            line = line.replace(stripped, f"*{param}*", 1)
        out.append(line)
    return out


def _strip_token(tok):
    """Strip quotes from a Griffe source token."""
    if isinstance(tok, str):
        s = tok.strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            return s[1:-1]
        return s
    return str(tok)


def _describe_constant_value(obj, module_members=None):
    """Return a short human description of a constant's value, or ''."""
    try:
        val = obj.value
    except Exception:  # noqa: BLE001
        return ""
    if val is None:
        return ""
    vtype = type(val).__name__
    if hasattr(val, "as_posix"):
        return f"Path: `{val.as_posix()}`"
    if isinstance(val, str):
        if len(val) <= 80:
            return f"Value: `{val}`"
        return f"String ({len(val)} chars)"
    if isinstance(val, (int, float, bool)):
        return f"Value: `{val}`"
    if isinstance(val, list):
        if len(val) <= 8:
            items = ", ".join(repr(v) for v in val)
            return f"List: [{items}]"
        return f"List of {len(val)} items"
    if isinstance(val, dict):
        if "description" in val:
            return f"Config: {val['description']}"
        return f"Dict with {len(val)} keys"
    return ""


def _render_name_desc_list_md(val, depth=4):
    """Render a list-of-dicts with name/description as a markdown table."""
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
    return ["| Name | Description |", "| --- | --- |"] + rows


def _render_config_dict_md(val, depth=4):
    """Render a config dict with description/prompt fields as markdown."""
    keys = getattr(val, "keys", None)
    values = getattr(val, "values", None)
    if keys is None or values is None:
        return []
    d = {}
    for i in range(len(keys)):
        k = _strip_token(keys[i])
        v = values[i]
        if isinstance(v, str):
            d[k] = _strip_token(v)
        else:
            d[k] = v
    if "description" not in d:
        return []
    lines = []
    if d.get("name"):
        lines.append(f"**Name:** {d['name']}")
        lines.append("")
    if d.get("description"):
        lines.append(d["description"])
        lines.append("")
    for prompt_key in ("judge_prompt", "prompt", "system_prompt"):
        jp = d.get(prompt_key, "")
        if jp and isinstance(jp, str):
            jp_text = jp.replace("\\n", "\n")
            criteria = []
            in_criteria = False
            for line in jp_text.splitlines():
                stripped = line.strip()
                if "EVALUATION CRITERIA" in stripped.upper() or "CRITERIA" in stripped.upper():
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
            break
    src = d.get("source")
    if src is not None:
        if isinstance(src, str):
            lines.append(f"**Source:** {src}")
            lines.append("")
        else:
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
    """Render a small Griffe ExprList's string elements inline, or ''."""
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
    """Extract a human description from a Griffe ExprDict, or ''."""
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
    """Extract the list of public names from a module's ``__all__`` attribute."""
    try:
        raw = all_attr.value
    except Exception:  # noqa: BLE001
        return []
    if raw is None:
        return []
    names = []
    for item in raw:
        val = getattr(item, "value", item)
        if not isinstance(val, str):
            continue
        s = val.strip()
        if s in ("[", "]", ","):
            continue
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            s = s[1:-1]
        if s and s.isidentifier():
            names.append(s)
    return names


def _render_module_via_ast(module_name, py_path):
    """Render a module page using Python's ast module (fallback)."""
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
    if mod_doc:
        doc_body = mod_doc.strip()
        if not (desc and doc_body.splitlines()[0].strip() == desc):
            lines += [doc_body, ""]

    try:
        all_members = dict(mod.members)
    except Exception:  # noqa: BLE001
        all_members = {}

    main_line = _main_guard_line(mod)

    members = {}
    for name, obj in all_members.items():
        if name.startswith("_"):
            continue
        if main_line is not None:
            ln = _member_lineno(obj)
            if ln is not None and ln >= main_line:
                continue
        if _is_public_member(obj):
            members[name] = obj
        else:
            resolved = _resolve_alias(obj)
            if resolved is not None and _is_public_member(resolved):
                members[name] = resolved

    all_attr = all_members.get("__all__")
    if all_attr is not None:
        ordered = _extract_all_names(all_attr)
        if ordered:
            members = {n: members[n] for n in ordered if n in members}

    if not members:
        lines += ["_No public members found._", ""]
        return "\n".join(lines) + "\n"

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

    return "\n".join(lines) + "\n"


def build_api_reference():
    """Generate reference/*.md for every module. Returns list of (module, slug)."""
    os.makedirs(REF, exist_ok=True)
    modules = []
    for dirpath, _, filenames in os.walk(PKG_PATH):
        for f in filenames:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), ROOT)
            mod = rel[:-3].replace(os.sep, ".")
            if mod.endswith(".__init__"):
                mod = mod[:-9]
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
    readme_path = os.path.join(ROOT, "README.md")
    readme = read_file(readme_path)[:3000] if os.path.exists(readme_path) else ""

    prompt = f"""You are a technical documentation architect. Given this Python project's file tree and README, plan a developer documentation structure.

FILE TREE:
{tree}

README (excerpt):
{readme}

Return a JSON array of documentation pages. Each entry: {{"title": "Page Title", "slug": "kebab-case-slug", "description": "One-line description", "source_files": ["relative/path.py", ...]}}

Rules:
- 8-12 narrative pages total (the API reference is generated separately — do NOT include an api-reference page)
- Must include: Getting Started, Core Architecture, CLI Usage (if applicable), and pages covering the major subsystems
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
        prompt = f"""You are a senior technical writer maintaining developer documentation for the {PROJECT_NAME} Python library.

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
        prompt = f"""You are a senior technical writer generating developer documentation for the {PROJECT_NAME} Python library.

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

def _section_modules(section_name, reference_modules):
    """Return the modules belonging to a reference section (no duplicates)."""
    for title, prefixes in REFERENCE_SECTIONS:
        if title == section_name:
            return [m for m in reference_modules
                    if any(m == p or m.startswith(p + ".") for p in prefixes)]
    return []


def build_index_md(pages, reference_modules):
    """Deterministic index page (no LLM)."""
    page_map = {p["slug"]: p for p in pages}
    assigned = set()
    tagline = INDEX_CFG.get("tagline", SITE_DESCRIPTION)
    quick = INDEX_CFG.get("quick_start", {})

    lines = [
        f"# {PROJECT_NAME}",
        "",
        tagline.strip(),
        "",
        "## Quick Start",
        "",
    ]
    if quick.get("install"):
        lines += ["```bash", quick["install"], "```", ""]
    if quick.get("example"):
        lines += ["```python", quick["example"].rstrip(), "```", ""]
    if quick.get("cli"):
        lines += ["```bash", quick["cli"].rstrip(), "```", ""]

    lines += ["## Guides", ""]
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


def build_llms_txt(pages, reference_modules):
    """Generate llms.txt — a markdown index for AI agents (https://llmstxt.org)."""
    page_map = {p["slug"]: p for p in pages}
    base = SITE_URL.rstrip("/") if SITE_URL else ""

    lines = [
        f"# {SITE_NAME}",
        "",
        SITE_DESCRIPTION,
        "",
        f"> {REPO_URL}",
        "",
        "## Guides",
        "",
    ]
    for section_name, slugs in NARRATIVE_SECTIONS:
        section_pages = [page_map[s] for s in slugs if s in page_map]
        if not section_pages:
            continue
        for p in section_pages:
            desc = p.get("description", "")
            url = f"{base}/guides/{p['slug']}/" if base else f"guides/{p['slug']}/"
            lines.append(f"- [{p['title']}]({url}): {desc}" if desc else f"- [{p['title']}]({url})")
    lines.append("")

    lines.append("## API Reference")
    lines.append("")
    for section_name, _ in REFERENCE_SECTIONS:
        mods = _section_modules(section_name, reference_modules)
        if not mods:
            continue
        for m in mods:
            slug = m.replace(".", "_")
            desc = MODULE_DESCRIPTIONS.get(m, "")
            url = f"{base}/reference/{slug}/" if base else f"reference/{slug}/"
            lines.append(f"- [{m}]({url}): {desc}" if desc else f"- [{m}]({url})")
    lines.append("")

    content = "\n".join(lines)
    with open(os.path.join(SITE_SRC, "llms.txt"), "w") as f:
        f.write(content)
    print(f"  llms.txt ({len(lines)} lines)")


def build_llms_full_txt(pages, reference_modules):
    """Generate llms-full.txt — all docs in one flat file for single-shot ingestion."""
    page_map = {p["slug"]: p for p in pages}
    parts = [
        f"# {SITE_NAME} — Full Documentation",
        "",
        SITE_DESCRIPTION,
        "",
        f"Source: {REPO_URL}",
        "",
        "=" * 60,
        "",
    ]

    # Guides
    for section_name, slugs in NARRATIVE_SECTIONS:
        section_pages = [page_map[s] for s in slugs if s in page_map]
        if not section_pages:
            continue
        for p in section_pages:
            path = os.path.join(OUT, f"{p['slug']}.md")
            if os.path.exists(path):
                parts.append(f"{'=' * 60}")
                parts.append(f"## {p['title']}")
                parts.append("")
                parts.append(read_file(path))
                parts.append("")

    # Unassigned guides
    assigned = {s for _, slugs in NARRATIVE_SECTIONS for s in slugs}
    for p in pages:
        if p["slug"] not in assigned:
            path = os.path.join(OUT, f"{p['slug']}.md")
            if os.path.exists(path):
                parts.append(f"{'=' * 60}")
                parts.append(f"## {p['title']}")
                parts.append("")
                parts.append(read_file(path))
                parts.append("")

    # API reference
    for section_name, _ in REFERENCE_SECTIONS:
        mods = _section_modules(section_name, reference_modules)
        if not mods:
            continue
        for m in mods:
            slug = m.replace(".", "_")
            path = os.path.join(REF, f"{slug}.md")
            if os.path.exists(path):
                parts.append(f"{'=' * 60}")
                parts.append(f"## {m}")
                parts.append("")
                parts.append(read_file(path))
                parts.append("")

    content = "\n".join(parts)
    with open(os.path.join(SITE_SRC, "llms-full.txt"), "w") as f:
        f.write(content)
    size_kb = len(content) // 1024
    print(f"  llms-full.txt ({size_kb} KB)")


def cross_link_guides():
    """Append a 'See Also' section to each guide page with links to related pages."""
    guides_dir = OUT
    if not os.path.isdir(guides_dir):
        return

    pages_info = {}
    for fname in sorted(os.listdir(guides_dir)):
        if not fname.endswith(".md"):
            continue
        slug = fname[:-3]
        path = os.path.join(guides_dir, fname)
        with open(path) as f:
            content = f.read()
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

    stop = {"the", "and", "for", "with", "from", "that", "this", "using",
            "use", "via", "all", "any", "not", "can", "may", "will", "are",
            "was", "were", "has", "have", "had", "its", "their", "your",
            "our", "how", "what", "when", "where", "which", "who", "why",
            "python", "function", "functions", "class", "classes", "method",
            "methods", "example", "examples", "usage", "guide", "guides",
            "page", "pages", "section", "sections", "module", "modules",
            "reference", "api", "overview", "details", "note", "notes",
            "tip", "tips", "best", "practices", "troubleshooting",
            "configuration", "config", "implementation", "architecture",
            "core", "basic", "advanced", "getting", "started",
            "installation", "setup", "environment", "variables",
            "command", "commands", "line", "interface", "output",
            "input", "data", "file", "files", "directory", "path", "paths",
            "error", "errors", "handling", "resilience", "privacy"}

    slug_section = {}
    for section_name, slugs in NARRATIVE_SECTIONS:
        for s in slugs:
            slug_section[s] = section_name

    def related_to(slug, max_results=4):
        info = pages_info[slug]
        my_words = set(re.findall(r"[a-z]{3,}",
                     " ".join([info["title"]] + info["headings"]).lower()))
        my_words -= stop
        my_section = slug_section.get(slug)
        scores = {}
        for other_slug, other_info in pages_info.items():
            if other_slug == slug:
                continue
            score = 0
            if my_section and slug_section.get(other_slug) == my_section:
                score += 2
            other_words = set(re.findall(r"[a-z]{3,}",
                          " ".join([other_info["title"]] + other_info["headings"]).lower()))
            other_words -= stop
            score += len(my_words & other_words)
            if score > 0:
                scores[other_slug] = score
        ranked = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
        return [s for s, _ in ranked[:max_results]]

    for slug, info in pages_info.items():
        with open(info["path"]) as f:
            content = f.read()

        content = re.sub(
            r"\n### See Also\n[\s\S]*?(?=\n## |\Z)",
            "",
            content,
        ).rstrip() + "\n"

        related = related_to(slug)
        if not related:
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
    nav = ["index.md", "llms.txt"]

    nav.append(["Guides", []])
    guides_nav = nav[-1][1]
    for section_name, slugs in NARRATIVE_SECTIONS:
        section_pages = [page_map[s] for s in slugs if s in page_map]
        if not section_pages:
            continue
        if len(section_pages) == 1:
            # Single page — flatten to a direct link instead of a nested group
            guides_nav.append(f"guides/{section_pages[0]['slug']}.md")
        else:
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
        if len(mods) == 1:
            ref_nav.append(f"reference/{mods[0].replace('.', '_')}.md")
        else:
            ref_nav.append([section_name, [f"reference/{m.replace('.', '_')}.md" for m in mods]])

    return nav


def _yaml_dump_block(obj, indent=0):
    """Serialize a Python object to a YAML block (indented).

    Uses PyYAML if available, otherwise a simple recursive serializer.
    """
    try:
        import yaml
        dumped = yaml.dump(obj, default_flow_style=False, sort_keys=False).rstrip()
        # Indent each line
        pad = "  " * indent
        lines = [pad + line if line.strip() else line for line in dumped.split("\n")]
        return "\n".join(lines)
    except ImportError:
        return _minimal_yaml_dump(obj, indent)


def _minimal_yaml_dump(obj, indent=0):
    """Fallback YAML serializer (no external deps)."""
    pad = "  " * indent
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)):
                lines.append(f"{pad}{k}:")
                lines.append(_minimal_yaml_dump(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {v}")
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(_minimal_yaml_dump(item, indent + 1))
            else:
                lines.append(f"{pad}- {item}")
    else:
        lines.append(f"{pad}{obj}")
    return "\n".join(lines)


def build_mkdocs_yml(nav):
    """Write mkdocs.yml entirely from autodocs.yml config."""
    nav_yaml = _nav_to_yaml(nav)

    # Build theme block
    theme_block = {
        "name": THEME_CFG.get("name", "material"),
        "palette": [
            {
                "scheme": "default",
                "primary": THEME_PRIMARY,
                "accent": THEME_ACCENT,
                "toggle": {"icon": "material/brightness-7", "name": "Switch to dark mode"},
            },
            {
                "scheme": "slate",
                "primary": THEME_PRIMARY,
                "accent": THEME_ACCENT,
                "toggle": {"icon": "material/brightness-4", "name": "Switch to light mode"},
            },
        ],
        "features": THEME_FEATURES,
    }

    # Build plugins block (inject paths into mkdocstrings if present)
    plugins = []
    for p in PLUGINS_CFG:
        if isinstance(p, dict) and "mkdocstrings" in p:
            mk = dict(p["mkdocstrings"])
            handlers = mk.get("handlers", {})
            py = handlers.get("python", {})
            opts = py.get("options", {})
            opts["paths"] = [ROOT]
            py["options"] = opts
            handlers["python"] = py
            mk["handlers"] = handlers
            plugins.append({"mkdocstrings": mk})
        else:
            plugins.append(p)

    # Optional site fields
    extra_site_lines = []
    if EDIT_URI:
        extra_site_lines.append(f"edit_uri: {EDIT_URI}")
    if SITE_AUTHOR:
        extra_site_lines.append(f"site_author: {SITE_AUTHOR}")
    if COPYRIGHT:
        extra_site_lines.append(f"copyright: {COPYRIGHT}")
    extra_site_block = "\n".join(extra_site_lines) + "\n" if extra_site_lines else ""

    content = f'''# MkDocs configuration for {SITE_NAME}
# Generated by docs/generate_docs.py — DO NOT EDIT by hand.
# All values come from docs/autodocs.yml. Edit that file to change anything.

site_name: {SITE_NAME}
site_description: {SITE_DESCRIPTION}
site_url: {SITE_URL}
repo_url: {REPO_URL}
repo_name: {REPO_NAME}
{extra_site_block}
docs_dir: {DOCS_DIR}
site_dir: {SITE_DIR}
use_directory_urls: {str(USE_DIRECTORY_URLS).lower()}

theme:
{_yaml_dump_block(theme_block, indent=1)}

plugins:
{_yaml_dump_block(plugins, indent=1)}

markdown_extensions:
{_yaml_dump_block(MARKDOWN_EXTENSIONS_CFG, indent=1)}

nav:
{nav_yaml}
'''
    # Convert plain mermaid format string to the !!python/name: YAML tag
    content = content.replace(
        "format: pymdownx.superfences.fence_code_format",
        "format: !!python/name:pymdownx.superfences.fence_code_format",
    )
    with open(os.path.join(DOCS, "mkdocs.yml"), "w") as f:
        f.write(content)


def _nav_to_yaml(nav, indent=0):
    """Serialize the nav list to YAML (no external deps)."""
    pad = "  " * (indent + 1)
    lines = []
    for item in nav:
        if isinstance(item, str):
            lines.append(f"{pad}- {item}")
        elif isinstance(item, dict):
            path, title = next(iter(item.items()))
            lines.append(f"{pad}- {path}: {title}")
        elif len(item) == 2 and isinstance(item[1], str):
            lines.append(f"{pad}- {item[0]}: {item[1]}")
        else:
            title, children = item
            lines.append(f"{pad}- {title}:")
            lines.append(_nav_to_yaml(children, indent + 1))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="AutoDocs — generic documentation generator")
    parser.add_argument("--force", action="store_true",
                        help="Full re-plan + regenerate everything")
    parser.add_argument("--no-llm", action="store_true",
                        help="Deterministic layer only (API reference + site assembly), skip LLM pages")
    parser.add_argument("--build", action="store_true",
                        help="Run `mkdocs build` after generating")
    parser.add_argument("--no-build", action="store_true",
                        help="Skip `mkdocs build` even if --build is set")
    args = parser.parse_args()

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

    print("=== AutoDocs Generator ===")
    print(f"Project: {PROJECT_NAME}")
    print(f"Package: {PACKAGE_DIR} ({PKG_PATH})")
    print(f"Site source: {SITE_SRC}")
    if not args.no_llm:
        print(f"LLM backend: {BASE} (KB: {KB_ID})")
    print()

    print("[1/6] Loading source files...")
    source_files = get_source_files()
    trackable_files = get_all_trackable_files()
    new_hashes = compute_file_hashes(trackable_files)
    print(f"  Loaded {len(source_files)} Python files, tracking {len(trackable_files)} files")
    print()

    print("[2/6] Building API reference (Griffe)...")
    reference_modules = [m for m, _ in build_api_reference()]
    print(f"  {len(reference_modules)} modules documented")
    print()

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
            time.sleep(1)

        stale = cleanup_stale_pages(plan_slugs)
        if stale:
            print(f"  Removed {len(stale)} stale pages: {', '.join(stale)}")

        store_plan(pages, new_hashes)

    print()
    print("[5/6] Cross-linking guide pages...")
    cross_link_guides()

    print()
    print("[6/6] Assembling MkDocs site...")
    index_md = build_index_md(pages, reference_modules)
    with open(os.path.join(SITE_SRC, "index.md"), "w") as f:
        f.write(index_md)
    print("  index.md")

    nav = build_nav(pages, reference_modules)
    build_mkdocs_yml(nav)
    print("  mkdocs.yml")

    build_llms_txt(pages, reference_modules)
    build_llms_full_txt(pages, reference_modules)

    img_src = os.path.join(PKG_PATH, "images")
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

    if os.path.isdir(ci_cache):
        os.makedirs(os.path.join(ci_cache, "guides"), exist_ok=True)
        if os.path.exists(PLAN_FILE):
            shutil.copy2(PLAN_FILE, os.path.join(ci_cache, ".plan.json"))
        for f in os.listdir(OUT):
            if f.endswith(".md"):
                shutil.copy2(os.path.join(OUT, f), os.path.join(ci_cache, "guides", f))

    if args.build and not args.no_build:
        print("\n[build] Running mkdocs build...")
        subprocess.run(
            [sys.executable, "-m", "mkdocs", "build"],
            cwd=DOCS, check=True,
        )
        print("  Site built to site/ ✓")


if __name__ == "__main__":
    main()
