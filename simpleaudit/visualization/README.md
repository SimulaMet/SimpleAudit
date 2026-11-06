# SimpleAudit Visualization (moved)

The web visualizer that used to live here (`simpleaudit serve`,
`simpleaudit export-html`, `SIMPLEAUDIT_VISUALIZER_SECRET`) has moved to
[SimpleAudit Studio](https://github.com/SimulaMet/SimpleAuditStudio). Those
commands no longer exist in this package.

To browse a folder of SimpleAudit JSON results:

```bash
uvx simpleaudit-studio --visualize-only --results_dir ./my_audit_results
```

For a full Studio instance, run `uvx simpleaudit-studio`.
