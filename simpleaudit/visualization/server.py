"""
FastAPI server for visualizing SimpleAudit results.
"""
import json
import os
import secrets
from pathlib import Path
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


app = FastAPI(title="SimpleAudit Visualizer")

# read secret from environment variable; if blank, authentication is disabled
SECRET = os.getenv("SIMPLEAUDIT_VISUALIZER_SECRET", "")

# contact email that will be shown in the frontend when auth is enabled;
# this mirrors the behaviour of the secret variable.  if not set we fall
# back to the historical default address.
CONTACT_EMAIL = os.getenv("SIMPLEAUDIT_VISUALIZER_EMAIL", "sushant@simula.no")


# Global variable to store results directory
RESULTS_DIR = None


def _looks_like_audit_result(obj: object) -> bool:
    return (
        isinstance(obj, dict)
        and ("scenario_name" in obj or "name" in obj)
        and "severity" in obj
    )


def _experiment_models(data: object) -> List[str]:
    """Model labels in an experiment file that have at least one loadable run.

    A run is loadable when it is a dict holding a non-empty list of
    audit-shaped results. Both the file tree and the JSON endpoint derive
    their notion of "experiment" from this list, so the tree never shows an
    entry (file or model) that the endpoint would refuse to serve.
    """
    if not isinstance(data, dict):
        return []
    runs = data.get("runs")
    if not isinstance(runs, dict):
        return []
    models = []
    for label, run_list in runs.items():
        entries = run_list if isinstance(run_list, list) else [run_list]
        for entry in entries:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("results"), list)
                and entry["results"]
                and all(_looks_like_audit_result(item) for item in entry["results"])
            ):
                models.append(label)
                break
    return models


def is_valid_audit_data(data) -> bool:
    """Check whether parsed JSON has the shape of audit results."""

    # Legacy shape: a list[AuditResult]
    if isinstance(data, list):
        return bool(data) and all(_looks_like_audit_result(item) for item in data)

    # Current shape: {"results": list[AuditResult], ...}
    if isinstance(data, dict) and "results" in data:
        results = data["results"]
        return (
            isinstance(results, list)
            and bool(results)
            and all(_looks_like_audit_result(item) for item in results)
        )

    # Multi-model experiment shape: {"runs": {"model": [{"results": [...]}]}}
    if isinstance(data, dict) and "runs" in data:
        return bool(_experiment_models(data))

    return False


def is_valid_audit_json(file_path: str) -> bool:
    """
    Check if a JSON file contains valid audit results.

    Args:
        file_path: Full path to the JSON file

    Returns:
        True if the file contains valid audit results, False otherwise
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return is_valid_audit_data(data)
    except (json.JSONDecodeError, IOError, Exception):
        return False


def get_file_tree(directory: str, base_path: str = "") -> List[Dict]:
    """
    Recursively get the file tree structure for JSON files.
    
    Args:
        directory: Full path to scan
        base_path: Relative path from the root results directory
    
    Returns:
        List of dicts representing folders and JSON files
    """
    items = []
    
    try:
        entries = sorted(os.listdir(directory))
    except (PermissionError, OSError):
        return items
    
    for entry in entries:
        full_path = os.path.join(directory, entry)
        rel_path = os.path.join(base_path, entry) if base_path else entry
        
        if os.path.isdir(full_path):
            # Get children recursively
            children = get_file_tree(full_path, rel_path)
            # Only include folder if it has JSON files (directly or in subdirs)
            if children:
                items.append({
                    "name": entry,
                    "type": "folder",
                    "path": rel_path,
                    "children": children
                })
        elif os.path.isfile(full_path) and entry.endswith('.json'):
            # Parse once, then classify as a multi-model experiment or a plain results file
            try:
                with open(full_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue
            experiment_models = _experiment_models(data)
            if experiment_models:
                items.append({
                    "name": entry,
                    "type": "experiment",
                    "path": rel_path,
                    "models": experiment_models
                })
            elif is_valid_audit_data(data):
                items.append({
                    "name": entry,
                    "type": "file",
                    "path": rel_path
                })
    
    return items


@app.get("/")
async def root():
    """Serve the main visualization page."""
    html_path = Path(__file__).resolve().parent / "visualizer.html"
    
    if not html_path.exists():
        return HTMLResponse(
            content="<h1>Error: Visualization template not found</h1>",
            status_code=500
        )
    
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()

    return HTMLResponse(content=content)


@app.get("/scenario_viewer.html")
async def scenario_viewer():
    """Serve the standalone scenario viewer page."""
    html_path = Path(__file__).resolve().parent / "scenario_viewer.html"
    
    if not html_path.exists():
        return HTMLResponse(
            content="<h1>Error: Scenario viewer template not found</h1>",
            status_code=500
        )
    
    with open(html_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    return HTMLResponse(content=content)


@app.get("/favicon.png")
async def favicon():
    """Serve the favicon."""
    favicon_path = Path(__file__).resolve().parent / "thumbnail.png"
    
    if not favicon_path.exists():
        raise HTTPException(status_code=404, detail="Favicon not found")
    
    return FileResponse(favicon_path, media_type="image/png")



# --- standalone HTML export -------------------------------------------------

def export_standalone_html(json_path: str, output_path: str) -> str:
    """Create a self-contained HTML file with the audit results inlined.

    The output can be opened directly in a browser (or sent to someone) —
    no server and no JSON upload step required. The visualizer detects the
    inlined data on load and renders it as a custom upload.

    Args:
        json_path: Path to the audit results JSON file.
        output_path: Where to write the standalone HTML file.

    Returns:
        The output path.

    Raises:
        ValueError: If the JSON is not valid audit data.
    """
    json_path = os.path.abspath(json_path)
    output_path = os.path.abspath(output_path)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not is_valid_audit_data(data):
        raise ValueError(f"{json_path} does not look like SimpleAudit results")

    template_path = os.path.join(os.path.dirname(__file__), "visualizer.html")
    with open(template_path, "r", encoding="utf-8") as f:
        html = f.read()

    # Escape so the payload cannot break out of the inline <script> tag.
    payload = json.dumps(data)
    payload = payload.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    name = os.path.splitext(os.path.basename(json_path))[0]
    name_json = json.dumps(name).replace("<", "\\u003c")
    # Detect if this is an experiment (has runs) or a single run
    is_experiment = "runs" in data and isinstance(data["runs"], dict)
    mode_json = json.dumps("experiment" if is_experiment else "single")
    inline = f"<script>window.__inlinedData = {payload}; window.__inlinedName = {name_json}; window.__standaloneMode = {mode_json};</script>\n"

    if "</head>" not in html:
        raise ValueError("visualizer.html has no </head> anchor — cannot inline data")

    html = html.replace("</head>", f"{inline}</head>", 1)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path


# --- authentication helpers ------------------------------------------------
from fastapi import Request, Depends, status

def check_secret(request: Request):
    """Raise HTTP 401 if a secret is configured and the request does not
    provide the correct value in an X-Secret header.  When no secret is
    configured the check is a no-op.
    """
    if not SECRET:
        return
    token = request.headers.get("X-Secret") or ""
    # Constant-time comparison to avoid leaking the secret via timing. Compare
    # UTF-8 bytes: compare_digest raises TypeError on non-ASCII str operands, so
    # a non-ASCII header (or a non-ASCII configured secret) would otherwise turn
    # a clean 401 into a 500.
    if not secrets.compare_digest(token.encode("utf-8"), SECRET.encode("utf-8")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")

@app.get("/api/auth")
async def auth_check(request: Request):
    """Endpoint used by the frontend to verify a key and learn if auth
    is enabled.  When the server has no secret configured it still
    returns 200 but sets ``enabled`` to False.

    The response also includes ``contact_email`` which is read from the
    ``SIMPLEAUDIT_VISUALIZER_EMAIL`` environment variable and defaults
    to the original maintainer address.  The frontend uses this to
    populate the authentication overlay message.
    """
    try:
        check_secret(request)
    except HTTPException as exc:
        # propagate unauthorized status
        raise

    return JSONResponse(content={"ok": True, "enabled": bool(SECRET), "contact_email": CONTACT_EMAIL})

@app.get("/api/files", dependencies=[Depends(check_secret)])
def get_files():
    # Plain def (not async): building the tree opens and json-parses every
    # result file, and FastAPI runs sync endpoints in its threadpool instead
    # of blocking the event loop for all concurrent requests.
    """Get the file tree of JSON files in the results directory."""
    if not RESULTS_DIR:
        raise HTTPException(status_code=500, detail="Results directory not set")
    
    if not os.path.exists(RESULTS_DIR):
        raise HTTPException(status_code=404, detail="Results directory not found")
    
    tree = get_file_tree(RESULTS_DIR)
    
    return JSONResponse(content={"tree": tree})


@app.get("/api/json/{file_path:path}", dependencies=[Depends(check_secret)])
def get_json_file(file_path: str):
    # Plain def for the same reason as get_files: file reads + json.load of
    # arbitrarily large result files must not stall the event loop.
    """Get the contents of a specific JSON file."""
    if not RESULTS_DIR:
        raise HTTPException(status_code=500, detail="Results directory not set")

    root = os.path.realpath(os.path.abspath(RESULTS_DIR))

    try:
        full_path = os.path.realpath(os.path.join(root, file_path))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid path")

    if not full_path.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="Access denied")

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    if os.path.splitext(full_path)[1].lower() != ".json":
        raise HTTPException(status_code=400, detail="Not a JSON file")

    try:
        with open(full_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except OSError:
        raise HTTPException(status_code=500, detail="Error reading file")

    # Serve only files shaped like audit results.
    if not is_valid_audit_data(data):
        raise HTTPException(status_code=403, detail="Not an audit results file")

    return JSONResponse(content=data)


@app.get("/api/image", dependencies=[Depends(check_secret)])
def get_image(uri: str):
    # Reuse the audit's own loader so local paths, http(s), s3 & friends all
    # resolve the same way they did at audit time, and non-images are rejected
    # identically. Returns a data URI (not raw bytes) so the frontend can fetch
    # it through apiFetch (carrying X-Secret) and assign it to an <img> src.
    from ..utils import image_data_uri

    try:
        data_uri = image_data_uri(uri)
    except ValueError:
        raise HTTPException(status_code=415, detail="Not a previewable image")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Image not found")
    except OSError:
        raise HTTPException(status_code=500, detail="Error reading image")

    return JSONResponse(content={"data_uri": data_uri})


def start_server(results_dir: str, host: str = "127.0.0.1", port: int = 8000):
    """
    Start the FastAPI server.
    
    Args:
        results_dir: Directory containing JSON result files
        host: Host to bind to
        port: Port to run on
    """
    global RESULTS_DIR, SECRET, CONTACT_EMAIL
    # make sure we pick up the environment variables in case they were
    # changed after the module was imported (e.g. during testing)
    SECRET = os.getenv("SIMPLEAUDIT_VISUALIZER_SECRET", "")
    CONTACT_EMAIL = os.getenv("SIMPLEAUDIT_VISUALIZER_EMAIL", "sushant@simula.no")

    # Resolve to absolute path from current working directory
    RESULTS_DIR = os.path.abspath(os.path.join(os.getcwd(), results_dir))
    
    if not os.path.exists(RESULTS_DIR):
        print(f"Error: Results directory '{RESULTS_DIR}' does not exist")
        return
    
    if not os.path.isdir(RESULTS_DIR):
        print(f"Error: '{RESULTS_DIR}' is not a directory")
        return
    
    print(f"Starting SimpleAudit Visualizer...")
    print(f"Results directory: {RESULTS_DIR}")
    print(f"Server: http://{host}:{port}")
    print(f"Press Ctrl+C to stop")
    
    uvicorn.run(app, host=host, port=port, log_level="info")
