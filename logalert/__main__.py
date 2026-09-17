"""Command-line entry point for logalert."""

import argparse
import logging
import os
import sys

from logalert import __version__, activity
from logalert.config import (
    DEFAULT_CONFIG_PATH,
    RESERVED_SECTION,
    Config,
    ConfigError,
    Watch,
    check_log,
    check_log_target,
    describe,
    example_config,
    is_address,
    load_config,
)
from logalert.globs import expand, is_glob
from logalert.lock import LockBusy, RunLock
from logalert.mail import clean_header, compose_test
from logalert.run import Options, run
from logalert.state import (
    RESET_HINT,
    State,
    StateError,
    check_state_dir,
    load_state,
    lock_path,
)
from logalert.transport import DeliveryError, choose, deliver, resolve_sender

log = logging.getLogger("logalert.main")

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
    parser.add_argument(
        "-c", "--context", type=int, default=0, metavar="N",
        help="lines of context around each match, like grep -C, for sections that set no "
        "context of their own (default: 0)",
    )
    parser.add_argument(
        "--attach", action="store_true",
        help="send every report as an attachment this run, whatever the sections say",
    )
    parser.add_argument(
        "--from-start", action="store_true",
        help="read a file seen for the first time from its beginning instead of its end",
    )
    parser.add_argument(
        "-n", "--dry-run", action="store_true",
        help="read and match, print the messages that would be sent, send nothing, and "
        "leave the state untouched",
    )
    parser.add_argument(
        "-d", "--debug", action="store_true",
        help="write the activity log at DEBUG level to stderr as well",
    )
    parser.add_argument(
        "--state-file", default=None, metavar="PATH",
        help="use this state file for the run instead of the configured one",
    )
    parser.add_argument(
        "--log", default=None, metavar="DEST",
        help="write the activity log to DEST instead of the configured destination: "
        "syslog, stderr, file:/absolute/path or udp:host:port",
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
        help="forget where every section left off in PATH (spelled as in the config, or "
        "as --check-config lists a glob's match), or in every file when PATH is omitted, "
        "and exit; the next run treats those files as first sight",
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
    if args.context < 0:
        parser.error(f"-c: {args.context} is not a number of lines")
    if args.state_file is not None and not os.path.isabs(args.state_file):
        parser.error(f"--state-file: {args.state_file!r} is not an absolute path")
    if args.log is not None:
        try:
            check_log("--log", args.log)
        except ConfigError as exc:
            parser.error(str(exc))
    mode = args.check_config or args.reset_state is not None or args.test_mail is not None
    if mode and (args.dry_run or args.attach or args.from_start or args.context):
        # a mode that ignored -n would reset state or send a test mail under a flag that
        # promised neither
        parser.error("-n/--attach/--from-start/-c apply to the run, not to --check-config, "
                     "--reset-state or --test-mail")
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"logalert: {clean_header(str(exc))}", file=sys.stderr)
        return EXIT_USAGE
    state_file = args.state_file or config.settings.state_file
    if _same_file(state_file, config.path):
        # --reset-state's replace-an-unparseable-file escape hatch would overwrite the config
        print(f"logalert: state_file: {state_file} is the config file itself", file=sys.stderr)
        return EXIT_USAGE
    # the activity log for this call: the configured destination, or --log. A dry run logs
    # to stderr unless --log says otherwise (it writes nothing, and its records belong in
    # front of the operator, not in syslog as if they were a run's); a check never touches
    # the destination -- it reports it
    log_spec = args.log or config.settings.log
    try:  # the loader checked its own paths; --log and --state-file change them
        check_log_target("--log" if args.log else f"[{RESERVED_SECTION}] log", log_spec,
                         state_file=state_file, config_path=config.path)
    except ConfigError as exc:
        print(f"logalert: {clean_header(str(exc))}", file=sys.stderr)
        return EXIT_USAGE
    to_stderr = args.check_config or (args.dry_run and args.log is None)
    with activity.attach(log_spec, debug=args.debug, to_stderr=to_stderr):
        if args.check_config:
            resolved = activity.describe(log_spec) + (" (--log)" if args.log else "")
            print(describe(config, args.sender, state_file, log=resolved, clean=clean_header))
            try:
                choose(config.settings)  # auto with no sendmail is a configuration error
                resolve_sender(config.settings, args.sender)  # a From the run would refuse
            except ConfigError as exc:
                print(f"logalert: {clean_header(str(exc))}", file=sys.stderr)
                return EXIT_USAGE
            return EXIT_OK
        if args.test_mail is not None:
            return test_mail(config, args.test_mail, args.sender)
        if args.reset_state is not None:
            if args.reset_state != RESET_ALL and not os.path.isabs(args.reset_state):
                # a cursor is keyed by the absolute path the config spells: no match ever
                parser.error(f"--reset-state: {args.reset_state!r} is not an absolute path")
            return reset_state(config, args.reset_state, state_file)
        try:
            return run(config, Options(context=args.context, sender=args.sender,
                                       attach=args.attach, from_start=args.from_start,
                                       dry_run=args.dry_run, state_file=args.state_file))
        except KeyboardInterrupt:
            print("logalert: interrupted", file=sys.stderr)
            return 130  # the shell's convention for SIGINT; the lock was released


def _same_file(a: str, b: str) -> bool:
    """Whether two paths name one existing file; a path that cannot be stat-ed is not."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


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


def reset_state(config: Config, target: str, path: str) -> int:
    """``--reset-state``: forget the cursors for one file, or all of them, under the lock;
    ``path`` is the effective state file (``--state-file`` wins over the config)."""
    settings = config.settings
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
        stuck = (" (older than lock_stale -- a stuck run?)"
                 if exc.stale and not exc.holder_gone else "")  # the gone line answers it
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


def _names(watch: Watch, target: str) -> bool:
    """Whether the section's list names ``target`` as a file: as written, or by a glob's
    expansion now (an archive a glob leaves out is not named; nor is the glob itself)."""
    return any(target in expand(entry, include_archives=watch.include_archives).files
               if is_glob(entry) else entry == target for entry in watch.files)


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
        log.warning("%s; replaced it with an empty state (--reset-state)", problem)
        print(f"{problem}; replaced it with an empty state")
        return EXIT_OK
    if target == RESET_ALL:
        count = state.forget()
    else:
        count = state.forget(target)
        if count == 0:
            if is_glob(target) and any(target in watch.files for watch in config.watches):
                print(f"logalert: {target} is a glob; --reset-state takes one of its matches, "
                      f"spelled as --check-config lists them (or no PATH, to forget every "
                      f"file)", file=sys.stderr)
                return EXIT_ATTENTION
            # 1, not 2: whether an entry exists depends on the state (expired, never seen,
            # already reset), not on the command line; the listing says what would match
            known = sorted({clean_header(file) for _, file in state.entries})
            configured = any(_names(watch, target) for watch in config.watches)
            print(f"logalert: no entry for {target}; the state file knows "
                  + (", ".join(known) if known else "no files")
                  + ("" if configured else " -- and no section lists that file"),
                  file=sys.stderr)
            return EXIT_ATTENTION
    state.save()
    what = "every file" if target == RESET_ALL else target
    log.info("forgot %d cursor(s) for %s (--reset-state)", count, what)
    print(f"forgot {count} cursor(s) for {what}; the next run starts at the current end "
          f"(or the beginning, per the section's start = setting)")
    return EXIT_OK

if __name__ == "__main__":
    sys.exit(main())
