"""
CLI interface for SimpleAudit.

The web visualizer (``serve`` / ``export-html``) has moved to
SimpleAudit Studio. To browse results, run Studio instead:

    uvx simpleaudit-studio --visualize-only --results_dir ./my_audit_results

or, for a full Studio instance, ``uvx simpleaudit-studio``.
"""
import sys


def main():
    """Main entry point for simpleaudit CLI."""
    print(
        "The `simpleaudit` CLI no longer includes the web visualizer.\n"
        "\n"
        "To visualize audit results, use SimpleAudit Studio:\n"
        "\n"
        "    uvx simpleaudit-studio --visualize-only --results_dir ./my_audit_results\n"
        "\n"
        "This starts a local web server (no audit worker) that browses a folder\n"
        "of SimpleAudit JSON results, and can export a standalone HTML file.\n"
        "\n"
        "See: https://github.com/SimulaMet/SimpleAuditStudio"
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
