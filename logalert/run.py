"""The run: read every configured file, mail what matched, move the state -- and say nothing.

This module is the single home of the run's rules; ``logalert.__main__`` parses the command
line and dispatches here. The rules (decided in issue #12; the seams are #7's state and lock,
#8's ``open_source``, #9's ``scan``, #10's ``compose``, #11's ``deliver``):

  * Order: the transport and the effective From are settled first (a configuration error is
    exit 2 before anything is read -- nothing ran -- and so is a ``state_file`` that is the
    config file itself); then the state directory's writability and the state file's
    ownership (BEFORE the lock: a root run refused for another user's state must not leave a
    root-owned lock behind, which would break the very remedy it names; and BEFORE any mail:
    a run that can send but cannot persist would double-send next time); then the lock, the
    state file (a corrupt one is a hard error naming ``--reset-state``), and expiry of the
    entries unseen for ``state_ttl`` days. ``--dry-run`` takes no lock and needs no writable
    directory: it reads the state if there is one and never writes.
  * Per section, in file order, each glob expanded in its place (``logalert.globs``: sorted,
    regular files only, rotated copies left out unless ``include_archives``; a directory
    that cannot be listed is a failed item, a glob matching nothing is nothing to do), each
    path read once however often the list names it, per file: ``open_source`` -- a file
    that cannot be read is a failed item and its cursor stays where it was, the section
    continues; a file absent this run is nothing to do (its cursor and its ``last_seen``
    stay, so it expires in time); a source is scanned with the section's context (or
    ``-c``) and its ``max_lines`` cap, its cursor taken after the read. A first sight starts
    at the end of the file unless ``--from-start`` or ``start = beginning`` -- or, for a
    path a glob matched, the NEW-FILE RULE (issue #18): some glob that matched it has a
    recorded moment (the start of the last saved run that listed its directories without
    error) and the file's mtime is not older than it -- a new daily file is all new
    content -- and then it is read from 0. The moments are written with the section's
    cursors (``state.record_run``), so a glob added to the list first-sights every match at
    the end, a section whose delivery failed reads the same new file from 0 again next
    run, and a glob whose directory was away or unlistable keeps its moment, so a file
    created during the outage is read whole once the directory is back. A listed path is
    never new, wherever a glob also matches it: its first sight is #7's, unchanged; a
    path is one path however it is spelled (``normcase``, ``normpath``).
  * A file's scan is bounded by ``scan_timeout`` seconds on POSIX (issue #28: ``SIGALRM``
    reaches into a regex that backtracks without bound, measured; a thread cannot, and
    Windows has no interval timer): past it the file is a failed item naming the line and
    the pattern being tried, its cursor stays, the section continues -- one loud exit 1
    instead of a held lock and an hour of quiet turn-aways.
  * The section's outcome: nothing matched -> its cursors move and the state is saved; a
    match -> ONE message composed and delivered; accepted (a partial refusal is accepted --
    the recipient who got it must not get it twice -- with the refused ones as failed items)
    -> cursors move, state saved; refused -> a failed item, and the cursors of the files that
    contributed to the message stay so the re-run re-sends them, while a file that was read
    and matched nothing moves anyway (none of its lines is in the message -- the summary
    still names it -- and context never crosses files) -- a file at its first sight in that
    run would otherwise be first-sighted again next time and lose what was written in
    between. Every file read is ``touch``ed so it never expires while its mail keeps
    failing. The state is saved after EACH section, never once at the end.
  * Exit 0 writes nothing to stdout or stderr -- cron mails every byte, or discards it with a
    note. Every non-zero exit writes exactly ONE line to stderr naming each failed item:
    ``logalert: 2 of 3 sections sent; failed: [router-disk] sendmail exit 75 (EX_TEMPFAIL);
    see the log`` (the count clause only when some section had something to send; ``would
    be sent`` under ``--dry-run``). A fresh lock holder is exit 0 with an INFO line: cron
    overlap is normal; a stale one is exit 1, named. Matches never make the exit non-zero.
    The line is header-clean: a relay's or a file name's control characters cannot forge a
    second one.
  * ``--dry-run`` prints each message that would be sent (headers decoded, the body as a
    reader sees it) to stdout, sends nothing, writes nothing; exit 0 or 1 by the same rule.
    ``--debug`` attaches a DEBUG handler on stderr, breaking cron-quiet on purpose.
  * The activity log (``logalert.activity`` wires its destinations, issue #13; the library
    ``NullHandler`` swallows it otherwise): the start (config path, section count) first of
    all, per file the lines read and matched, per section the delivery (``transport`` logs
    it) or the failure with the transport's answer and the Message-ID, every failed item
    once at ERROR where it is collected, every expired entry, and the exit code at the end
    -- on every exit the run returns (a killed run, Ctrl-C included, leaves no end line).
"""

import logging
import os
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from logalert.config import Config, ConfigError, Watch
from logalert.globs import expand, is_glob
from logalert.lock import LockBusy, RunLock
from logalert.mail import Mail, clean_header, compose
from logalert.match import FileReport, progress, scan
from logalert.rotation import open_source
from logalert.state import (
    Cursor,
    State,
    StateError,
    check_state_dir,
    load_state,
    lock_path,
    parse_timestamp,
)
from logalert.transport import DeliveryError, choose, deliver, resolve_sender

log = logging.getLogger("logalert.run")

EXIT_OK = 0
EXIT_ATTENTION = 1  # ran, but something needs a look
EXIT_USAGE = 2  # usage or configuration error; nothing ran


class ScanTimeout(BaseException):
    """A file's scan ran past ``scan_timeout``; the message names where it was. A
    ``BaseException``, like ``KeyboardInterrupt``: it is raised from a signal handler at an
    arbitrary bytecode boundary, and an ``Exception`` landing inside a log call would be
    swallowed by ``logging.Handler.emit`` (review) -- the bound gone and the activity log
    declared dead. Nothing in the package catches ``BaseException`` except to clean up and
    re-raise."""


@contextmanager
def scan_bound(seconds: int) -> Iterator[None]:
    """``ScanTimeout`` out of whatever the main thread is doing ``seconds`` from now --
    POSIX only, main thread only (a signal handler is installable nowhere else), and
    ``0`` is no bound. The previous handler and timer are restored on the way out."""
    if (seconds <= 0 or sys.platform == "win32"
            or threading.current_thread() is not threading.main_thread()):
        yield
        return
    else:

        def expired(signum: int, frame: object) -> None:
            where = f"at line {progress.line}" if progress.line else "before the first line"
            pattern = progress.pattern  # None while reading: a stall that is not a regex
            what = (f"while trying {'regex' if pattern.regex else 'pattern'} {pattern.text!r}"
                    if pattern is not None else "while reading")
            raise ScanTimeout(f"scanning exceeded scan_timeout ({seconds} s) {where} {what}")

        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        try:
            yield
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


@dataclass(frozen=True)
class Options:
    """What the command line adds to the configuration for one run."""

    context: int = 0  # -c N: the context for sections that set none
    sender: str | None = None  # --from
    attach: bool = False  # --attach
    from_start: bool = False  # --from-start
    dry_run: bool = False  # -n
    state_file: str | None = None  # --state-file


@dataclass
class Outcome:
    """What the run has to say at the end: the count clause and the failed items."""

    due: int = 0  # sections with something to send
    sent: int = 0  # of those, accepted by the transport (or previewed, in a dry run)
    dry_run: bool = False
    failed: list[str] = field(default_factory=list)  # one clause each, header-clean

    def fail(self, item: str) -> None:
        """Record a failed item, and log it once, here, at ERROR."""
        clean = clean_header(item)
        self.failed.append(clean)
        log.error("%s", clean)

    def summary(self) -> str:
        """The one stderr line of a non-zero exit."""
        parts = []
        if self.due:
            noun = "section" if self.due == 1 else "sections"
            verb = "would be sent" if self.dry_run else "sent"
            parts.append(f"{self.sent} of {self.due} {noun} {verb}")
        parts.append("failed: " + "; ".join(self.failed))
        parts.append("see the log")
        return "logalert: " + "; ".join(parts)


def run(config: Config, options: Options) -> int:
    """One run over every section; returns the exit code, and prints the one summary line
    on a non-zero exit. A ``ConfigError`` (the transport, the From) is exit 2 with its own
    line, before anything is read; ``main`` has already refused a state file that is the
    config file itself, for every mode."""
    log.info("start: %s, %d section(s)%s", config.path, len(config.watches),
             " (dry run)" if options.dry_run else "")
    started = datetime.now(UTC)  # the run record's moment: a file created from here on is newer
    state_file = options.state_file or config.settings.state_file
    try:
        choose(config.settings)
        sender, warning = resolve_sender(config.settings, options.sender)
    except ConfigError as exc:
        log.error("%s", exc)
        log.info("end: exit %d", EXIT_USAGE)
        print(f"logalert: {clean_header(str(exc))}", file=sys.stderr)
        return EXIT_USAGE
    if warning:
        log.warning("%s", warning)
    outcome = Outcome(dry_run=options.dry_run)

    lock: RunLock | None = None
    if not options.dry_run:
        try:
            check_state_dir(state_file)  # before the lock: a refused run leaves nothing
        except StateError as exc:
            outcome.fail(f"state directory: {exc}")
            return _finish(outcome)
        lock = RunLock(lock_path(state_file), config.settings.lock_stale)
        try:
            lock.acquire()
        except LockBusy as busy:
            if busy.stale:
                outcome.fail(f"stale lock: {busy.describe()}")
                return _finish(outcome)
            log.info("%s; this run exits quietly", busy.describe())
            return _finish(outcome)
        except OSError as exc:
            outcome.fail(f"state directory: {exc.strerror or exc} ({lock_path(state_file)}) -- "
                         f"the lock file must belong to the user logalert runs as")
            return _finish(outcome)
    try:
        try:
            state = load_state(state_file)
        except StateError as exc:
            outcome.fail(f"state file: {exc}")  # the StateError names --reset-state itself
            return _finish(outcome)
        for section, file in state.expire(config.settings.state_ttl_days):
            log.info("[%s] %s: %s, unseen for %d days", section, file,
                     "would be forgotten" if options.dry_run else "forgotten",
                     config.settings.state_ttl_days)
        for section in state.expire_runs(config.settings.state_ttl_days,
                                         configured=[w.name for w in config.watches]):
            log.debug("[%s] the last-run record %s: not configured, no run saved for %d days",
                      section,
                      "would be forgotten" if options.dry_run else "forgotten",
                      config.settings.state_ttl_days)
        for watch in config.watches:
            _section(watch, config, options, sender, state, outcome, started)
    finally:
        if lock is not None:
            lock.release()
    return _finish(outcome)


def _finish(outcome: Outcome) -> int:
    code = EXIT_ATTENTION if outcome.failed else EXIT_OK
    log.info("end: exit %d", code)
    if outcome.failed:
        print(outcome.summary(), file=sys.stderr)
    return code


def _section(watch: Watch, config: Config, options: Options, sender: str, state: State,
             outcome: Outcome, started: datetime) -> None:
    """One section: read its files, mail once when anything matched, move its state."""
    context = options.context if watch.context is None else watch.context
    reports: list[FileReport] = []
    cursors: dict[str, Cursor] = {}
    paths, seen = _files(watch, outcome)
    for path, globs in paths:
        saved = state.get(watch.name, path)
        from_start = options.from_start or watch.start == "beginning"
        if globs and saved is None and not from_start:
            pattern = _new_file(watch.name, globs, path, state)
            if pattern is not None:
                log.info("[%s] %s: new since the last run (%s); reading from the beginning",
                         watch.name, path, pattern)
                from_start = True
        try:
            source = open_source(watch.name, path, saved, archive_dir=watch.archive_dir,
                                 from_start=from_start)
        except OSError as exc:
            outcome.fail(f"[{watch.name}] {path}: {_reason(exc, path)}")
            continue
        if source is None:
            continue  # absent this run; the cursor and its last_seen stay
        progress.file, progress.line, progress.pattern = path, 0, None
        try:
            with source:
                with scan_bound(config.settings.scan_timeout):
                    report = scan(path, source.lines(), watch, context=context,
                                  before=source.context_before, cap=watch.max_lines)
                # outside the bound (review): an alarm handled on the way out of it must
                # not leave a cursor here for _advance to save past lines never mailed
                cursors[path] = source.cursor()
        except OSError as exc:  # an archive vanishing mid-read, a corrupt stream
            outcome.fail(f"[{watch.name}] {path}: {_reason(exc, path)}")
            continue
        except ScanTimeout as exc:  # the cursor stays: the file is re-read next run
            outcome.fail(f"[{watch.name}] {path}: {exc}")
            continue
        log.info("[%s] %s: %d line(s) read, %d matched", watch.name, path, report.lines,
                 report.matched)
        reports.append(report)

    if not any(report.matched for report in reports):
        _advance(watch, state, cursors, options, outcome, started, seen, delivered=False)
        return
    outcome.due += 1
    mail = compose(watch, reports, sender=sender, settings=config.settings,
                   attach=options.attach)
    if options.dry_run:
        _preview(mail)
        outcome.sent += 1
        return
    try:
        delivery = deliver(mail, config.settings)
    except DeliveryError as exc:
        log.error("[%s] not delivered (%s): %s", watch.name, mail.message_id, exc)
        outcome.fail(f"[{watch.name}] {exc}")
        # a file that contributed nothing to the message may move (its lines are not in the
        # mail, and context never crosses files); the others keep their place and are touched
        quiet = {report.file for report in reports if not report.matched}
        for path, cursor in cursors.items():
            if path in quiet:
                state.set(watch.name, path, cursor)
            else:
                state.touch(watch.name, path)
        _save(state, watch.name, outcome, delivered=False)
        return
    outcome.sent += 1
    for recipient, answer in delivery.refused:
        outcome.fail(f"[{watch.name}] refused: {recipient} -- {answer}")
    _advance(watch, state, cursors, options, outcome, started, seen, delivered=True)


def _files(watch: Watch, outcome: Outcome) -> tuple[list[tuple[str, tuple[str, ...]]],
                                                  set[str]]:
    """The paths to read this run, in the list's order with each glob expanded in its
    place, each once, with every glob that matched it -- none for a path the list names
    itself, wherever it stands: a name the operator wrote is a listed file -- and the set
    of globs looked into (their directories listed, without error), for the record."""
    listed = {_same(entry) for entry in watch.files if not is_glob(entry)}
    paths: dict[str, tuple[str, list[str]]] = {}  # normalised -> (spelling, globs)
    seen: set[str] = set()
    for entry in watch.files:
        if not is_glob(entry):
            paths.setdefault(_same(entry), (entry, []))
            continue
        found = expand(entry, include_archives=watch.include_archives)
        if found.listed and not found.errors:
            seen.add(entry)
        for error in found.errors:
            outcome.fail(f"[{watch.name}] {entry}: {error}")
        if found.archives:
            names = [os.path.basename(p) for p in found.archives]
            log.debug("[%s] %s: left out %d rotated %s: %s", watch.name, entry, len(names),
                      "copy" if len(names) == 1 else "copies", ", ".join(names[:10])
                      + (", ..." if len(names) > 10 else ""))
        for path, what in found.skipped:
            log.debug("[%s] %s: passed over %s (%s)", watch.name, entry, path, what)
        if not found.files and not found.errors:
            log.debug("[%s] %s: matches nothing this run", watch.name, entry)
        for path in found.files:
            key = _same(path)
            if key not in listed:
                paths.setdefault(key, (path, []))[1].append(entry)
            else:
                paths.setdefault(key, (path, []))
    return [(path, tuple(globs)) for path, globs in paths.values()], seen


def _same(path: str) -> str:
    """One key for the spellings of one path, so the list's ``x//y`` and a glob's ``x/y``
    are read once; the state keeps the first spelling."""
    return os.path.normcase(os.path.normpath(path))


def _new_file(section: str, globs: tuple[str, ...], path: str, state: State) -> str | None:
    """The new-file rule: the glob among ``globs`` whose recorded moment the file is not
    older than, or None (the moment is whole seconds, so a file from the same second
    counts as newer: it was not there to be matched then)."""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None  # the open will say why
    for glob in globs:
        at = state.last_run(section, glob)
        if at is not None and mtime >= parse_timestamp(at).timestamp():
            return glob
    return None


def _reason(exc: OSError, path: str) -> str:
    """The OSError's reason without repeating the path our own open_log already names."""
    return exc.strerror or str(exc).removeprefix(f"{path}: ")


def _advance(watch: Watch, state: State, cursors: dict[str, Cursor], options: Options,
             outcome: Outcome, started: datetime, seen: set[str], *,
             delivered: bool) -> None:
    if options.dry_run:
        return
    for path, cursor in cursors.items():
        state.set(watch.name, path, cursor)
    state.record_run(watch.name, watch.files, seen, started)
    _save(state, watch.name, outcome, delivered=delivered)


def _save(state: State, section: str, outcome: Outcome, *, delivered: bool) -> None:
    try:
        state.save()
    except (StateError, OSError) as exc:
        outcome.fail(f"[{section}] state not saved: {exc}")
        if delivered:
            log.error("[%s] the mail went out; unless a later save in this run succeeds, "
                      "the next run re-sends it", section)


def _preview(mail: Mail) -> None:
    sys.stdout.write(mail.preview())
    sys.stdout.write(chr(10) + chr(10))
