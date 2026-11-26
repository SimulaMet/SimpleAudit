"""
CLI interface for SimpleAudit.

``simpleaudit calibrate <judge> ...`` checks a verdict judge against people's
labels (see :mod:`simpleaudit.calibration_cli`).

The web visualizer (``serve`` / ``export-html``) has moved to
SimpleAudit Studio. To browse results, run Studio instead:

    uvx simpleaudit-studio --visualize-only --results_dir ./my_audit_results

or, for a full Studio instance, ``uvx simpleaudit-studio``.
"""
import sys


def main(argv=None):
    """Main entry point for simpleaudit CLI.

    ``--version`` and ``--help`` succeed (exit 0) so scripts and health checks
    can probe the install; anything else prints the Studio pointer and exits 1,
    because the command the user tried no longer exists.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] in ("--version", "-V"):
        from simpleaudit import __version__

        print(f"simpleaudit {__version__}")
        sys.exit(0)
    if args and args[0] == "calibrate":
        from simpleaudit.calibration_cli import main as calibrate_main

        sys.exit(calibrate_main(args[1:]))
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
        "See: https://github.com/SimulaMet/SimpleAuditStudio\n"
        "\n"
        "To check a pass/fail or pass/medium/fail judge against people's labels:\n"
        "\n"
        "    simpleaudit calibrate --help"
    )
    sys.exit(0 if args and args[0] in ("--help", "-h") else 1)


if __name__ == "__main__":
    main()
