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
    
    args = parser.parse_args()
    
    if args.command == "serve":
        from .visualization.server import start_server
        
        # Default to current directory if not specified
        results_dir = args.results_dir
        if results_dir is None:
            results_dir = "."
            print("⚠️  Warning: --results_dir not specified, using current directory '.'")
            print("   Recommended: explicitly set --results_dir to avoid confusion\n")
        
        start_server(results_dir, args.host, args.port)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
