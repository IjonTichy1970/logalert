"""Command-line entry point for logalert."""

import argparse
import sys

from logalert import __version__
from logalert.config import DEFAULT_CONFIG_PATH, ConfigError, describe, example_config, load_config

EXIT_OK = 0
EXIT_ATTENTION = 1  # ran, but something needs a look (owned by the run loop; reserved here)
EXIT_USAGE = 2  # usage or configuration error; nothing ran (argparse's own code too)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="logalert",
        description="Generalized log file watcher: search log files for patterns and email "
        "the matching entries.",
        epilog="Print a complete commented example config with --example-config; validate "
        "yours with --check-config. Exit codes: 0 ran, 1 something needs attention, "
        "2 usage or configuration error.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-f", "--config", default=DEFAULT_CONFIG_PATH, metavar="PATH",
        help=f"configuration file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--check-config", action="store_true",
        help="validate the configuration, print the effective settings, and exit "
        "(0 = usable, 2 = not); never touches state",
    )
    parser.add_argument(
        "--example-config", action="store_true",
        help="print a complete, commented example configuration and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Output may carry text from the config; never let a console encoding crash the reader.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")

    if args.example_config:
        sys.stdout.write(example_config())
        return EXIT_OK

    if args.check_config:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"logalert: {exc}", file=sys.stderr)
            return EXIT_USAGE
        print(describe(config))
        return EXIT_OK

    # The run itself arrives with the orchestration issue; until then, be helpful.
    parser.print_help()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
