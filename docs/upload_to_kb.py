#!/usr/bin/env python3
"""Upload project source files to an Open WebUI knowledge base.

Generic — reads all project-specific values from autodocs.yml (same config
file used by generate_docs.py). No hardcoded URLs, KB IDs, or package names.

Environment:
  OWUI_API_KEY    Open WebUI API key (required)
  SOURCE_ROOT     Path to the source repo (default: parent of docs/)
"""
import os
import time

import requests

DOCS = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(DOCS, "autodocs.yml")


def _load_config():
    try:
        import yaml
        with open(CONFIG_FILE) as f:
            return yaml.safe_load(f)
    except ImportError:
        # Reuse the minimal parser from generate_docs
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "generate_docs", os.path.join(DOCS, "generate_docs.py"))
        mod = importlib.util.module_from_spec(spec)
        # Don't execute the module — just grab the parser function
        import re
        with open(os.path.join(DOCS, "generate_docs.py")) as f:
            src = f.read()
        # Extract the _minimal_yaml_parse function
        match = re.search(
            r"(def _minimal_yaml_parse\(path\):.*?)(?=\nCFG = )",
            src, re.DOTALL)
        if match:
            ns = {}
            exec(match.group(1), ns)
            return ns["_minimal_yaml_parse"](CONFIG_FILE)
        raise RuntimeError("Cannot parse autodocs.yml")


CFG = _load_config()

ROOT = os.environ.get(
    "SOURCE_ROOT",
    CFG.get("root", os.path.dirname(DOCS)),
)
PACKAGE_DIR = CFG.get("package_dir", "")
PKG_PATH = os.path.join(ROOT, PACKAGE_DIR)

LLM_CFG = CFG.get("llm", {})
BASE = LLM_CFG.get("base_url", "")
KEY = os.environ.get("OWUI_API_KEY", "")
KB_ID = LLM_CFG.get("kb_id", "")

KB_CFG = CFG.get("kb", {})
EXTRA_FILES = KB_CFG.get("extra_files", [])

headers = {"Authorization": f"Bearer {KEY}"}


def collect_files():
    files = []
    # Python source in the package
    for dirpath, _, filenames in os.walk(PKG_PATH):
        for f in filenames:
            if f.endswith(".py"):
                files.append(os.path.join(dirpath, f))
    # Extra top-level docs
    for f in EXTRA_FILES:
        p = os.path.join(ROOT, f)
        if os.path.exists(p):
            files.append(p)
    return files


def upload_file(path):
    rel = os.path.relpath(path, ROOT)
    with open(path, "rb") as f:
        r = requests.post(
            f"{BASE}/api/v1/files/",
            headers=headers,
            files={"file": (rel, f)},
            data={"metadata": f'{{"knowledge_id":"{KB_ID}"}}'},
        )
    r.raise_for_status()
    return r.json()["id"]


def wait_for_file(file_id, timeout=120):
    start = time.time()
    while time.time() - start < timeout:
        r = requests.get(f"{BASE}/api/v1/files/{file_id}/process/status", headers=headers)
        r.raise_for_status()
        status = r.json()["status"]
        if status == "completed":
            return True
        if status == "failed":
            return False
        time.sleep(2)
    return False


def main():
    if not KEY:
        raise RuntimeError(
            "OWUI_API_KEY is not set. Export it before running:\n"
            "  export OWUI_API_KEY='your-key-here'"
        )
    files = collect_files()
    print(f"Uploading {len(files)} files to KB {KB_ID}...")
    file_ids = []
    for i, path in enumerate(files):
        rel = os.path.relpath(path, ROOT)
        try:
            fid = upload_file(path)
            file_ids.append(fid)
            print(f"  [{i + 1}/{len(files)}] {rel} -> {fid[:8]}")
        except Exception as e:
            print(f"  [{i + 1}/{len(files)}] {rel} FAILED: {e}")

    print(f"\nUploaded {len(file_ids)}/{len(files)} files. Waiting for processing...")
    ok = 0
    for fid in file_ids:
        if wait_for_file(fid):
            ok += 1
        else:
            print(f"  {fid[:8]} did not complete")
    print(f"Processing complete: {ok}/{len(file_ids)} files indexed.")


if __name__ == "__main__":
    main()
