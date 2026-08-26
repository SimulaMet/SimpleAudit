# SimpleAudit Visualization

Visual tools for reviewing SimpleAudit results with an interactive web interface.
## 🔐 Authentication

To require a secret key for the CLI server, set the `SIMPLEAUDIT_VISUALIZER_SECRET` environment variable:

```bash
export SIMPLEAUDIT_VISUALIZER_SECRET="mysecret"
export SIMPLEAUDIT_VISUALIZER_EMAIL="security@example.com"

simpleaudit serve --results_dir ./my_audit_results
```
 When set, the web UI prompts for the key on first load.
By default the authentication overlay includes a contact email address.  You can override it by setting `SIMPLEAUDIT_VISUALIZER_EMAIL` before starting the server


## 📊 Two Ways to Visualize Results

### Method 1: CLI Server (Recommended for Multiple Files)

Start a local web server to browse and visualize multiple JSON result files from a directory.

**Installation:**
```bash
pip install simpleaudit[visualize]
```

**Usage:**
```bash
# Visualize results from a specific directory
simpleaudit serve --results_dir ./my_audit_results

# Custom port and host
simpleaudit serve --results_dir ./results --port 8080 --host 0.0.0.0
```
 👉 [Check for live demo](https://simulamet-simpleauditvisualization.hf.space)


**Or run directly with uv (no installation needed):**
```bash
# Run directly using uvx
uvx simpleaudit[visualize] serve --results_dir ./my_audit_results

# With custom port and host
uvx simpleaudit[visualize] serve --results_dir ./results --port 8080 --host 0.0.0.0
```

> **Note:** `uvx` requires `uv` to be installed on your system. Install it with:
> ```bash
> pip install uv
> # or see https://docs.astral.sh/uv/getting-started/installation/
> ```

**Features:**
- 📁 Browse nested folder structures
- 🔍 File tree with JSON validation
- 📱 Mobile responsive design
- 🎨 Interactive scenario list and detail views
- 📊 Statistics dashboard with severity breakdown
- 🔄 Hot reload - just refresh to see updated files

**Access:**
Open your browser to `http://127.0.0.1:8000` (or your custom host/port)

---

### Method 2: Standalone Viewer (No Server Required)

Open a single HTML file directly in your browser and upload JSON files.

**How to use:**

1. **Download the file:**
   - Get `scenario_viewer.html` from `simpleaudit/visualization/`
   - Or from: https://github.com/kelkalot/simpleaudit/blob/main/simpleaudit/visualization/scenario_viewer.html

2. **Open in browser:**
   - Double-click `scenario_viewer.html` or
   - Right-click → Open with → Your browser

3. **Upload your JSON:**
   - Drag and drop your audit results JSON file
   - Or click "Select File" to browse
   - Try sample data to see how it works

**Features:**
- ✨ Works completely offline
- 🔒 No data leaves your computer
- 📤 Drag & drop JSON files
- 🎯 Sample data included for demo
- 📱 Mobile friendly
- 🔄 Upload multiple files sequentially

**Use cases:**
- Quick one-off result review
- Sharing results with stakeholders (single HTML file)
- Offline environments
- Privacy-sensitive data

### Method 3: Standalone HTML Export (Share a Single File)

Generate a self-contained HTML file with the results inlined — send it to
someone and they can open it directly in a browser. No server, no JSON
upload step, no network required.

**From the CLI:**
```bash
simpleaudit export-html ./my_audit_results/audit_results.json
# writes audit_results.html next to the JSON (or use -o to choose a path)
```

**From the visualizer UI:**
Open the results in the visualizer (server or upload), then click
**Download HTML** in the header. The downloaded file behaves like a
custom upload — the full scenario list, detail view, and statistics are
all available offline.

**Notes:**
- Works with all supported JSON shapes: array of results, `{results: [...]}`,
  and multi-model experiment files (`{runs: {...}}`).
- The HTML file is self-contained; image attachments referenced by URI are
  still fetched at view time, so they need to be reachable from wherever
  the file is opened.

---

## 📋 Supported JSON Formats

Both visualization methods support these JSON structures:

### Format 1: Array of Results
```json
[
  {
    "scenario_name": "Test Scenario",
    "scenario_description": "Description here",
    "score": 8.5,
    "severity": "medium",
    "history": [
      {"role": "user", "content": "..."},
      {"role": "assistant", "content": "..."}
    ],
    "reasoning": "Judge feedback..."
  }
]
```

### Format 2: Object with Results Array
```json
{
  "timestamp": "2026-02-16T10:00:00Z",
  "summary": {
    "score": 8.5,
    "total_scenarios": 10
  },
  "results": [
    {
      "scenario_name": "...",
      "score": 8.5,
      "severity": "medium",
      ...
    }
  ]
}
```

### Required Fields:
- `scenario_name` or `name`: Scenario title
- `scenario_description` or `description`: Brief description
- `score`: Numeric score (0-10)
- `severity`: One of: `pass`, `low`, `medium`, `high`, `critical`
- `history` or `conversation`: Array of role/content pairs
- `reasoning` or `feedback`: Judge evaluation text

---

## 🎨 Features

### Statistics Dashboard
- **Pass Rate**: Percentage of passing scenarios
- **Average Score**: Overall score across all scenarios
- **Severity Breakdown**: Visual bar chart of severity distribution

### Scenario List
- Color-coded severity indicators
- Search by name or description
- Sort by score or severity
- Quick score preview

### Detail View
- Full conversation history with role badges
- Comprehensive judge feedback
- Severity tags and scores
- Mobile-optimized back navigation

### File Tree (CLI Server only)
- Collapsible folder navigation  
- JSON file validation
- Nested directory support
- Persistent selection state
- Resizable sidebar (desktop)
- Hide/show toggle

---

## 🚀 Examples

### Example 1: Local Audit Results
```bash
# After running your audit
simpleaudit serve --results_dir ./examples/local_audit_example_results
<!-- 👉 [Check for demo](https://simulamet-simpleauditvisualization.hf.space) -->
```

### Example 2: Multiple Audits
```bash
# Browse all audits in a directory
simpleaudit serve --results_dir ./all_audits
<!-- 👉 [Check for demo](https://simulamet-simpleauditvisualization.hf.space) -->
```

### Example 3: Share with Team
```bash
# Start server on network-accessible host
simpleaudit serve --results_dir ./results --host 0.0.0.0 --port 8080
<!-- 👉 [Check for demo](https://simulamet-simpleauditvisualization.hf.space) -->
```

Then share: `http://YOUR_IP:8080`

### Example 4: Offline Review
1. Download `scenario_viewer.html`
2. Open in browser
3. Upload your `audit_results.json`
4. Review scenarios offline

---

## 🛠️ Customization

### Custom Port
If port 8000 is in use:
```bash
simpleaudit serve --results_dir ./results --port 8001
<!-- 👉 [Check for demo](https://simulamet-simpleauditvisualization.hf.space) -->
```

### Allow External Access
To access from other devices on your network:
```bash
simpleaudit serve --results_dir ./results --host 0.0.0.0
<!-- 👉 [Check for demo](https://simulamet-simpleauditvisualization.hf.space) -->
```

---

## 💡 Tips

1. **File Organization**: Keep audit results in dated folders:
   ```
   results/
   ├── 2026-02-01/
   │   ├── model_a.json
   │   └── model_b.json
   └── 2026-02-15/
       └── latest_audit.json
   ```

2. **Quick Checks**: Use standalone viewer for quick reviews of single files

3. **Bulk Analysis**: Use CLI server when comparing multiple audit runs

4. **Mobile Review**: Both methods work on mobile devices - use hamburger menu for navigation

5. **File Validation**: Invalid JSON files won't appear in the file tree (CLI server)

---

## 🐛 Troubleshooting

### Port Already in Use
```bash
# Error: Address already in use
# Solution: Use a different port
simpleaudit serve --results_dir ./results --port 8001
```

### No Files Showing
- Ensure JSON files have `.json` extension
- Check JSON is valid (use a validator)
- Verify at least one file has `results` array

### Standalone Viewer Not Loading
- Make sure you're opening `scenario_viewer.html` (not `visualizer.html`)
- Check browser console for errors
- Try a different browser (Chrome, Firefox, Edge)

### File Upload Not Working
- Ensure you're uploading `.json` files
- Check file size (very large files may be slow)
- Verify JSON structure matches supported formats

---

## 📚 Related Documentation

- [Main README](https://github.com/kelkalot/simpleaudit/blob/main/README.md) - SimpleAudit overview
- [Example Notebooks](https://github.com/kelkalot/simpleaudit/blob/main/examples/) - Usage examples
- [PyPI Package](https://pypi.org/project/simpleaudit/) - Installation

---

## 🤝 Contributing

Found a bug or have a feature request? Open an issue on [GitHub](https://github.com/kelkalot/simpleaudit/issues).

---

**Happy Auditing! 🎯**
