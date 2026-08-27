#!/usr/bin/env python3
"""Upload simpleaudit source files to Open WebUI knowledge base."""
import os
import time
import requests

BASE = "https://simulachat.sushant.info.np"
KEY = os.environ.get("OWUI_API_KEY", "")
KB_ID = "a43e450c-a197-4d53-95ca-555a6e6a2edb"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (script lives in docs/)

headers = {"Authorization": f"Bearer {KEY}"}

def collect_files():
    files = []
    # Python source
    for dirpath, _, filenames in os.walk(os.path.join(ROOT, "simpleaudit")):
        for f in filenames:
            if f.endswith(".py"):
                files.append(os.path.join(dirpath, f))
    # Top-level docs
    for f in ["README.md", "pyproject.toml", "FAQ.md", "DPG.md"]:
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
    files = collect_files()
    print(f"Uploading {len(files)} files to KB {KB_ID}...")
    file_ids = []
    for i, path in enumerate(files):
        rel = os.path.relpath(path, ROOT)
        try:
            fid = upload_file(path)
            file_ids.append(fid)
            print(f"  [{i+1}/{len(files)}] {rel} -> {fid[:8]}")
        except Exception as e:
            print(f"  [{i+1}/{len(files)}] {rel} FAILED: {e}")

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
