#!/usr/bin/env python3
"""
SimpleAudit documentation generator — two-layer pipeline.

Layer 1 (deterministic, no LLM):
  - Griffe scans the simpleaudit/ source tree and renders a complete,
    always-accurate API reference (classes, functions, signatures,
    docstrings) into docs/site_src/reference/*.md
  - mkdocs.yml, nav, and the index page are generated from the plan

Layer 2 (LLM, Open WebUI backend):
  - Narrative pages (getting started, architecture, guides, examples)
    are generated/updated by the LLM with source code + KB context
  - Incremental: pages are only regenerated when their source files change

Output: docs/site_src/  (MkDocs source tree)
Build:  mkdocs build  ->  docs/site/  (static site, deployable to GitHub Pages)

Usage:
  python3 docs/generate_docs.py              # incremental generate
  python3 docs/generate_docs.py --force      # full re-plan + regenerate
  python3 docs/generate_docs.py --no-llm     # deterministic layer only (offline)
  python3 docs/generate_docs.py --build      # also run `mkdocs build`
  python3 docs/generate_docs.py --push-wiki  # legacy: sync to GitHub wiki
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
DOCS = os.path.dirname(os.path.abspath(__file__))                   # docs/
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
    "simpleaudit.judges.judge_conviction": "Judge conviction: measures how strongly a judge commits to its verdict.",
    "simpleaudit.judges.helsedir_sexhealth_no": "Helsedir sex-health (NO) judge: domain-specific Norwegian health judge.",
    "simpleaudit.judges.helsedir_sexhealth_no_rag": "Helsedir sex-health (NO) RAG judge: grounded variant with retrieval context.",
    "simpleaudit.scenarios": "Scenario packs: curated test suites (health, safety, government, benchmarks).",
    "simpleaudit.scenarios.health": "Health domain scenarios: medical Q&A and advice tests.",
    "simpleaudit.scenarios.safety": "Safety scenarios: refusal and harmful-request tests.",
    "simpleaudit.scenarios.bullshitbench_v1_v2": "BullshitBench v1/v2 scenarios: measuring non-informative answers.",
    "simpleaudit.scenarios.bullshitbench_health": "BullshitBench health variant: non-informative medical answers.",
    "simpleaudit.scenarios.hei_refusal": "HEI refusal scenarios: higher-education institutional refusal tests.",
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


def _render_member_md(name, obj, depth=3):
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
            lines.extend(doc_lines)
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
        lines.append(f"{header} `{sig if sig else name + '()'}`")
        if doc_lines:
            lines.append("")
            lines.extend(doc_lines)
            lines.append("")
    else:
        # module-level constant / variable
        lines.append(f"{header} `{name}`")
        if doc_lines:
            lines.append("")
            lines.extend(doc_lines)
            lines.append("")
    return lines


def render_module_reference(module_name):
    """Render one module's public API as a markdown page (deterministic)."""
    import griffe
    Class, Function = griffe.Class, griffe.Function

    try:
        mod = griffe.load(module_name, search_paths=[ROOT])
    except Exception as e:  # noqa: BLE001
        return (f"## {module_name}\n\n"
                f"> Could not parse this module: `{e}`\n")

    lines = [f"## {module_name}", ""]
    desc = MODULE_DESCRIPTIONS.get(module_name)
    mod_doc = _griffe_docstring(mod)
    if desc:
        lines += [desc, ""]
    elif mod_doc:
        lines += [mod_doc.splitlines()[0].strip(), ""]

    # Public members only (skip re-exported imports like `argparse`)
    members = {}
    try:
        all_members = dict(mod.members)
    except Exception:  # noqa: BLE001
        all_members = {}
    for name, obj in all_members.items():
        if name.startswith("_"):
            continue
        if not _is_public_member(obj):
            continue
        members[name] = obj

    # __all__ ordering if present
    all_attr = all_members.get("__all__")
    if all_attr is not None:
        try:
            ordered = list(all_attr.value)
            members = {n: members[n] for n in ordered if n in members}
        except Exception:  # noqa: BLE001
            pass

    if not members:
        lines += ["_No public members found._", ""]
        return "\n".join(lines)

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
            lines += _render_member_md(n, o, depth=4)

    return "\n".join(lines)


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
    ("Judges", ["judges-framework", "safety-harm-judges", "judge-validation", "custom-judges"]),
    ("Scenarios", ["scenarios-overview", "creating-scenarios", "health-domain-scenarios",
                   "health-medical-scenarios", "safety-refusal-scenarios",
                   "government-institutional-scenarios", "bullshitbench-safety-scenarios",
                   "benchmarks-rag-scenarios", "benchmarks-comparative-tests", "advanced-scenarios"]),
    ("Results & Tooling", ["results-analysis", "visualization-server", "visualization"]),
    ("Advanced", ["cross-judge", "repeated-runs", "reframing", "judge-the-judge"]),
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
    for section_name, prefixes in REFERENCE_SECTIONS:
        mods = [m for m in reference_modules
               if any(m == p or m.startswith(p + ".") for p in prefixes)]
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
    for section_name, prefixes in REFERENCE_SECTIONS:
        mods = [m for m in reference_modules
                if any(m == p or m.startswith(p + ".") for p in prefixes)]
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
site_url: https://sushantgautam.github.io/simpleaudit/
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
    # Config lives in docs/ (parent of docs_dir=site_src, sibling of site_dir=site)
    with open(os.path.join(ROOT, "docs", "mkdocs.yml"), "w") as f:
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
# Legacy: GitHub wiki push
# ---------------------------------------------------------------------------


def slug_to_wiki(slug):
    """Convert a file slug to GitHub wiki page name (Title-Case with hyphens)."""
    parts = slug.replace("INDEX", "Home").split("-")
    known_acronyms = {"api": "API", "cli": "CLI", "llm": "LLM", "rag": "RAG"}
    known_camel = {"bullshitbench": "BullshitBench"}
    converted = []
    for part in parts:
        if part.lower() in known_acronyms:
            converted.append(known_acronyms[part.lower()])
        elif part in known_camel:
            converted.append(known_camel[part])
        else:
            converted.append(part.capitalize())
    return "-".join(converted)


def convert_wiki_links(content, all_slugs):
    """Convert relative .md links to wiki-style page references."""
    for slug in all_slugs:
        wiki_name = slug_to_wiki(slug)
        content = re.sub(
            rf"\]\(\./?{re.escape(slug)}\.md\)",
            f"]({wiki_name})",
            content,
        )
    return content


def push_to_wiki():
    """Clone/pull the GitHub wiki, sync docs, convert links, commit, push."""
    wiki_dir = "/tmp/simpleaudit-wiki"
    wiki_url = "https://github.com/SushantGautam/simpleaudit.wiki.git"

    print("\n[wiki] Pushing to GitHub wiki...")

    if os.path.exists(wiki_dir):
        subprocess.run(["git", "-C", wiki_dir, "pull", "--rebase", "origin", "master"],
                       check=True, capture_output=True)
    else:
        subprocess.run(["git", "clone", wiki_url, wiki_dir],
                       check=True, capture_output=True)

    all_slugs = [f[:-3] for f in os.listdir(OUT) if f.endswith(".md")]

    for fname in sorted(os.listdir(OUT)):
        if not fname.endswith(".md"):
            continue
        src = os.path.join(OUT, fname)
        wiki_fname = "Home.md" if fname == "INDEX.md" else fname
        dst = os.path.join(wiki_dir, wiki_fname)
        with open(src, "r") as f:
            content = f.read()
        content = convert_wiki_links(content, all_slugs)
        with open(dst, "w") as f:
            f.write(content)
        print(f"  {fname} -> {wiki_fname}")

    # Also copy the index
    index_src = os.path.join(SITE_SRC, "index.md")
    if os.path.exists(index_src):
        with open(index_src) as f:
            content = f.read()
        content = convert_wiki_links(content, all_slugs)
        with open(os.path.join(wiki_dir, "Home.md"), "w") as f:
            f.write(content)

    subprocess.run(["git", "-C", wiki_dir, "add", "-A"], check=True)
    result = subprocess.run(["git", "-C", wiki_dir, "diff", "--cached", "--quiet"],
                            capture_output=True)
    if result.returncode != 0:
        subprocess.run(["git", "-C", wiki_dir, "commit", "-m",
                        "Sync docs from generate_docs.py"], check=True, capture_output=True)
        subprocess.run(["git", "-C", wiki_dir, "push", "origin", "master"],
                       check=True, capture_output=True)
        print("  Pushed to wiki ✓")
    else:
        print("  No changes to push")


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
    parser.add_argument("--push-wiki", action="store_true",
                        help="Legacy: sync narrative pages to the GitHub wiki")
    args = parser.parse_args()

    # CI: restore the incremental cache (guides + plan) from the actions cache
    ci_cache = os.path.join(ROOT, ".docs_cache")
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
    print("[1/5] Loading source files...")
    source_files = get_source_files()
    trackable_files = get_all_trackable_files()
    new_hashes = compute_file_hashes(trackable_files)
    print(f"  Loaded {len(source_files)} Python files, tracking {len(trackable_files)} files")
    print()

    # Step 2: deterministic API reference (always regenerated — it's cheap)
    print("[2/5] Building API reference (Griffe)...")
    reference_modules = [m for m, _ in build_api_reference()]
    print(f"  {len(reference_modules)} modules documented")
    print()

    # Step 3: narrative pages (LLM)
    old_plan = None if args.force else load_plan()
    old_hashes = old_plan.get("file_hashes", {}) if old_plan else {}

    if args.no_llm:
        print("[3/5] Narrative pages — SKIPPED (--no-llm)")
        pages = old_plan.get("pages", []) if old_plan else []
        if not pages:
            print("  No stored plan found; index will list guides as they appear.")
    else:
        if old_plan and not args.force:
            structure_valid = set(old_hashes.keys()) == set(new_hashes.keys())
            if structure_valid:
                pages = old_plan.get("pages", [])
                print(f"[3/5] Reusing stored plan ({len(pages)} narrative pages)")
            else:
                added = set(new_hashes.keys()) - set(old_hashes.keys())
                removed = set(old_hashes.keys()) - set(new_hashes.keys())
                print("[3/5] Source file set changed — re-planning structure...")
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
            print(f"[3/5] {label} documentation structure...")
            pages = determine_structure()
            print(f"  Planned {len(pages)} pages:")
            for p in pages:
                print(f"    - {p['title']} ({p['slug']})")
            old_hashes = {}

        print()
        print("[4/5] Generating narrative pages...")
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

    # Step 5: assemble MkDocs site
    print()
    print("[5/5] Assembling MkDocs site...")
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
            cwd=os.path.join(ROOT, "docs"), check=True,
        )
        print("  Site built to docs/site/ ✓")

    if args.push_wiki:
        push_to_wiki()


if __name__ == "__main__":
    main()
