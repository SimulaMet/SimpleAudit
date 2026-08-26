"""
CLI interface for SimpleAudit tools.
"""
import argparse
import sys


def main():
    """Main entry point for simpleaudit CLI."""
    parser = argparse.ArgumentParser(
        prog="simpleaudit",
        description="SimpleAudit CLI - AI Safety Auditing Tools"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    # Serve command
    serve_parser = subparsers.add_parser(
        "serve",
        help="Start a web server to visualize audit results"
    )
    serve_parser.add_argument(
        "--results_dir",
        type=str,
        default=None,
        help="Directory containing JSON result files to visualize (default: current directory)"
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to run the server on (default: 8000)"
    )
    serve_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind the server to (default: 127.0.0.1)"
    )

    # Export command
    export_parser = subparsers.add_parser(
        "export-html",
        help="Create a standalone HTML file with the results inlined (no server needed)"
    )
    export_parser.add_argument(
        "json_path",
        type=str,
        help="Path to the audit results JSON file"
    )
    export_parser.add_argument(
        "-o", "--output",
        type=str,
        default=None,
        help="Output HTML path (default: <json_path> with .html extension)"
    )

    args = parser.parse_args()
    
    if args.command == "serve":
        try:
            from .visualization.server import start_server
        except ModuleNotFoundError as exc:
            print(f"Error: the visualization server needs optional dependencies ({exc.name} is missing).")
            print("Install them with: pip install 'simpleaudit[visualize]'")
            sys.exit(1)
        
        # Default to current directory if not specified
        results_dir = args.results_dir
        if results_dir is None:
            results_dir = "."
            print("⚠️  Warning: --results_dir not specified, using current directory '.'")
            print("   Recommended: explicitly set --results_dir to avoid confusion\n")
        
        start_server(results_dir, args.host, args.port)
    elif args.command == "export-html":
        try:
            from .visualization.server import export_standalone_html
        except ModuleNotFoundError as exc:
            print(f"Error: the visualizer needs optional dependencies ({exc.name} is missing).")
            print("Install them with: pip install 'simpleaudit[visualize]'")
            sys.exit(1)

        output = args.output
        if output is None:
            output = args.json_path.rsplit(".", 1)[0] + ".html" if "." in args.json_path else args.json_path + ".html"

        try:
            path = export_standalone_html(args.json_path, output)
        except FileNotFoundError:
            print(f"Error: {args.json_path} does not exist")
            sys.exit(1)
        except ValueError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        print(f"Standalone HTML written to {path}")
        print("Open it directly in a browser — no server or JSON upload needed.")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
