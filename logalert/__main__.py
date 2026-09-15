"""Command-line entry point for logalert."""

import argparse
import os
import sys

from logalert import __version__
from logalert.config import (
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
    describe,
    example_config,
    is_address,
    load_config,
)
from logalert.lock import LockBusy, RunLock
from logalert.mail import compose_test
from logalert.state import (
    RESET_HINT,
    State,
    StateError,
    check_state_dir,
    load_state,
    lock_path,
)
from logalert.transport import DeliveryError, choose, deliver, resolve_sender

EXIT_OK = 0
EXIT_ATTENTION = 1  # ran, but something needs a look (a state problem, a stuck run, ...)
EXIT_USAGE = 2  # usage or configuration error; nothing ran (argparse's own code too)

RESET_ALL = "*"  # the --reset-state sentinel for "every file"; never a valid absolute path


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
        "--from", dest="sender", default=None, metavar="ADDR",
        help="the From address for this run, a bare local@domain (default: from = in the "
        "config, else the user logalert runs as at this host)",
    )
    # The "do this and exit" modes exclude one another; a silent precedence would
    # let `--check-config --reset-state` validate and exit 0 without resetting anything.
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--check-config", action="store_true",
        help="validate the configuration, print the effective settings, and exit "
        "(0 = usable, 2 = not); never touches state",
    )
    modes.add_argument(
        "--example-config", action="store_true",
        help="print a complete, commented example configuration and exit",
    )
    modes.add_argument(
        "--reset-state", nargs="?", const=RESET_ALL, default=None, metavar="PATH",
        help="forget where every section left off in PATH (spelled as in the config), or "
        "in every file when PATH is omitted, and exit; the next run treats those files "
        "as first sight",
    )
    modes.add_argument(
        "--test-mail", default=None, metavar="SECTION",
        help="send a one-line test message to SECTION's recipients through the configured "
        "transport, report the transport's answer, and exit (0 accepted, 1 not delivered, "
        "2 configuration)",
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
    if args.sender is not None and not is_address(args.sender):
        parser.error(f"--from: {args.sender!r} is not a bare local@domain address")

    if args.check_config or args.reset_state is not None or args.test_mail is not None:
        try:
            config = load_config(args.config)
        except ConfigError as exc:
            print(f"logalert: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if args.check_config:
            print(describe(config, args.sender))
            try:
                choose(config.settings)  # auto with no sendmail is a configuration error
                resolve_sender(config.settings, args.sender)  # a From the run would refuse
            except ConfigError as exc:
                print(f"logalert: {exc}", file=sys.stderr)
                return EXIT_USAGE
            return EXIT_OK
        if args.test_mail is not None:
            return test_mail(config, args.test_mail, args.sender)
        if args.reset_state != RESET_ALL and not os.path.isabs(args.reset_state):
            # a cursor is keyed by the absolute path the config spells: this can never match
            parser.error(f"--reset-state: {args.reset_state!r} is not an absolute path")
        return reset_state(config, args.reset_state)

    # The run itself arrives with the orchestration issue; until then, be helpful.
    parser.print_help()
    return EXIT_OK


def test_mail(config: Config, section: str, override: str | None) -> int:
    """``--test-mail SECTION``: one real message to the section's recipients, and the
    transport's answer on stdout."""
    watches = {watch.name: watch for watch in config.watches}
    if section not in watches:
        print(f"logalert: no section [{section}] in {config.path}; sections: "
              f"{', '.join(watches) or '(none)'}", file=sys.stderr)
        return EXIT_USAGE
    try:
        transport = choose(config.settings)
        sender, warning = resolve_sender(config.settings, override)
    except ConfigError as exc:
        print(f"logalert: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if warning:
        print(f"logalert: warning: {warning}", file=sys.stderr)
    mail = compose_test(watches[section], sender=sender, settings=config.settings)
    where = (config.settings.sendmail_path if transport == "sendmail"
             else f"{config.settings.smtp_host}:{config.settings.smtp_port}")
    try:
        delivery = deliver(mail, config.settings)
    except DeliveryError as exc:
        print(f"logalert: not delivered via {transport} ({where}): {exc}", file=sys.stderr)
        return EXIT_ATTENTION
    print(f"sent via {transport} ({where}): {delivery.answer}")
    print(f"to: {', '.join(delivery.accepted)}")
    print(f"size: {delivery.size} bytes; Message-ID: {mail.message_id}")
    for recipient, answer in delivery.refused:
        print(f"refused: {recipient} -- {answer}", file=sys.stderr)
    return EXIT_ATTENTION if delivery.refused else EXIT_OK


def reset_state(config: Config, target: str) -> int:
    """``--reset-state``: forget the cursors for one file, or all of them, under the lock."""
    settings = config.settings
    path = settings.state_file
    try:
        os.stat(path)  # not os.path.exists: that reads a permission problem as absence
    except (FileNotFoundError, NotADirectoryError):
        print(f"no state file at {path}; nothing to forget")
        return EXIT_OK
    except OSError as exc:
        print(f"logalert: state file {path}: cannot stat ({exc.strerror}) -- are you the "
              f"user logalert runs as?", file=sys.stderr)
        return EXIT_ATTENTION
    lock = RunLock(lock_path(path), settings.lock_stale)
    try:
        check_state_dir(path)
        lock.acquire()
    except LockBusy as exc:
        stuck = " (older than lock_stale -- a stuck run?)" if exc.stale else ""
        print(f"logalert: {exc}{stuck}; nothing was reset", file=sys.stderr)
        return EXIT_ATTENTION
    except StateError as exc:
        print(f"logalert: {exc}", file=sys.stderr)
        return EXIT_ATTENTION
    except OSError as exc:
        print(f"logalert: cannot take the run lock {lock.path} ({exc.strerror}) -- it must "
              f"belong to the user logalert runs as (a root run leaves it root-owned)",
              file=sys.stderr)
        return EXIT_ATTENTION
    try:
        return _reset(config, path, target)
    except StateError as exc:
        print(f"logalert: {exc}", file=sys.stderr)
        return EXIT_ATTENTION
    finally:
        lock.release()


def _reset(config: Config, path: str, target: str) -> int:
    try:
        state = load_state(path)
    except StateError as exc:
        problem = str(exc).removesuffix(f" -- {RESET_HINT}")
        if target != RESET_ALL:
            print(f"logalert: {problem} -- --reset-state with no PATH replaces the file",
                  file=sys.stderr)
            return EXIT_ATTENTION
        # The escape hatch: a state file nothing can read is replaced, not repaired.
        State(path).save()
        print(f"{problem}; replaced it with an empty state")
        return EXIT_OK
    if target == RESET_ALL:
        count = state.forget()
    else:
        count = state.forget(target)
        if count == 0:
            # 1, not 2: whether an entry exists depends on the state (expired, never seen,
            # already reset), not on the command line; the listing says what would match
            known = sorted({file for _, file in state.entries})
            configured = any(target in watch.files for watch in config.watches)
            print(f"logalert: no entry for {target}; the state file knows "
                  + (", ".join(known) if known else "no files")
                  + ("" if configured else " -- and no section lists that file"),
                  file=sys.stderr)
            return EXIT_ATTENTION
    state.save()
    what = "every file" if target == RESET_ALL else target
    print(f"forgot {count} cursor(s) for {what}; the next run starts at the current end "
          f"(or the beginning, per the section's start = setting)")
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
