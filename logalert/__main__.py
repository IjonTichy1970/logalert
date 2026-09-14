"""Command-line entry point for logalert."""

import argparse
import sys

from logalert import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logalert",
        description="Generalized log file watcher.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    # No watcher yet -- the CLI surface is defined as the project takes shape.
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
