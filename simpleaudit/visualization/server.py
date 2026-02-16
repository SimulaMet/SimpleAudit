"""
FastAPI server for visualizing SimpleAudit results.
"""
import json
import os
from pathlib import Path
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


app = FastAPI(title="SimpleAudit Visualizer")


# Global variable to store results directory
RESULTS_DIR = None


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
        
        # Check if it's an array
        if isinstance(data, list):
            return len(data) > 0
        
        # Check if it's an object with a 'results' key that's an array
        if isinstance(data, dict) and 'results' in data:
            return isinstance(data['results'], list) and len(data['results']) > 0
        
        return False
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
            # Validate JSON file before including it
            if is_valid_audit_json(full_path):
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


@app.get("/api/files")
async def get_files():
    """Get the file tree of JSON files in the results directory."""
    if not RESULTS_DIR:
        raise HTTPException(status_code=500, detail="Results directory not set")
    
    if not os.path.exists(RESULTS_DIR):
        raise HTTPException(status_code=404, detail="Results directory not found")
    
    tree = get_file_tree(RESULTS_DIR)
    
    return JSONResponse(content={"tree": tree})


@app.get("/api/json/{file_path:path}")
async def get_json_file(file_path: str):
    """Get the contents of a specific JSON file."""
    if not RESULTS_DIR:
        raise HTTPException(status_code=500, detail="Results directory not set")
    
    # Security: Ensure the path doesn't escape the results directory
    full_path = os.path.normpath(os.path.join(RESULTS_DIR, file_path))
    
    if not full_path.startswith(os.path.normpath(RESULTS_DIR)):
        raise HTTPException(status_code=403, detail="Access denied")
    
    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail="File not found")
    
    if not full_path.endswith('.json'):
        raise HTTPException(status_code=400, detail="Not a JSON file")
    
    try:
        with open(full_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return JSONResponse(content=data)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error reading file: {str(e)}")


def start_server(results_dir: str, host: str = "127.0.0.1", port: int = 8000):
    """
    Start the FastAPI server.
    
    Args:
        results_dir: Directory containing JSON result files
        host: Host to bind to
        port: Port to run on
    """
    global RESULTS_DIR
    
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
