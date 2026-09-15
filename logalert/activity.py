"""The activity log: where the run's records go, in what shape, and what happens when the
destination fails. This module is the single home of those rules (decided in issue #13;
the records themselves are emitted by the other modules on the ``logalert.*`` loggers):

  * ``log = syslog`` (the default) | ``stderr`` | ``file:/absolute/path`` | ``udp:host:port``,
    validated by the loader; ``--log DEST`` on the command line wins over the file in every
    mode. ``attach()`` wires the destination for ONE ``main()`` call and unwires it after, so a
    process that calls ``main()`` twice accumulates nothing.
  * ``syslog`` probes ``/dev/log``, ``/var/run/log``, ``/var/run/syslog`` in that order for a
    path that IS a socket (``stat.S_ISSOCK``, never ``exists()``: ``/var/run/log`` is a directory
    on Ubuntu and a socket on FreeBSD) and that accepts a datagram (else a stream) ``connect()``
    -- measured, ``SysLogHandler`` constructs without raising on a missing path or a directory,
    holding a closed socket, and then prints three chained tracebacks per record. The fallback
    line says why each candidate was rejected (missing, not a socket, connection refused: the
    daemon down with its socket still there is the realistic fault). The handler's ``ident``
    is ``logalert[<pid>]: `` so journald records ``SYSLOG_IDENTIFIER=logalert`` and
    ``journalctl -t logalert`` finds the run (without it the journal shows ``_COMM=python`` and
    no identifier); the facility is user. On Windows there is no syslog at all
    (``socket.AF_UNIX`` is absent; the constructor raises ``AttributeError``).
  * ``file:`` is opened at setup, never lazily: measured, ``FileHandler(delay=True)`` raises the
    open failure out of ``logger.info()`` into the caller, which would abort a run at its first
    record. It is opened by us, like the lock: ``O_NOFOLLOW`` (a symlink planted at the path by
    another user would have a root run append into any file of that user's choosing) and a
    regular-file check (a FIFO with no reader blocks ``open(2)`` for good, before the lock, so
    cron runs would pile up); a state file, the run lock or the config file as the destination
    is refused by the loader and by ``--log``. ``udp:`` is built only when configured -- a
    datagram to a port nobody listens on is silent forever, the "logs nowhere" fault -- and an
    unresolvable host raises at construction; ``udp:[2001:db8::1]:514`` is the IPv6 spelling.
  * The fallback is ALWAYS stderr and ALWAYS loud: when the destination cannot be used, the
    first record on stderr is one WARNING naming what was tried, and the run goes on with its
    exit code unchanged (cron mails that line, which is the point). A destination that fails
    later (the syslog socket's server gone, a full disk) is reported ONCE, in the same shape,
    and the rest of the call is logged to stderr -- never the stdlib's traceback per record,
    and never a second report from ``close()`` at teardown (measured: a full disk fails the
    flush again there, and an unguarded close turned an exit 0 into a traceback and exit 1).
  * One line per record, whatever the destination: the message, prefixed ``warning: `` /
    ``error: `` / ``debug: `` for those levels (INFO carries no word; journald and rsyslog
    carry the priority anyway, a file or a cron mail needs the word), header-clean -- a section
    name, a file name, an archive name or a relay's reply may carry ESC or a line boundary;
    rsyslog writes ``#012`` for a newline and a terminal honours a raw CSI. The destination's
    own prefix: syslog the ident (journald adds the time); file the local time with its offset
    and ``logalert[<pid>]: ``; stderr ``logalert: ``.
  * ``--debug`` adds a stderr handler at DEBUG; the configured destination stays at INFO (a
    debug session does not flood syslog). When the destination IS stderr -- configured, by
    fallback, or because the mode says so (``--dry-run`` logs to stderr unless ``--log`` is
    explicit; ``--check-config`` never touches the destination, it reports it) -- there is
    one stderr handler, at DEBUG under ``--debug``.
"""

import contextlib
import errno
import logging
import logging.handlers
import os
import socket
import stat
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from io import TextIOWrapper
from typing import TextIO

from logalert.config import udp_address
from logalert.mail import clean_header

LOGGER = "logalert"
SYSLOG_SOCKETS = ("/dev/log", "/var/run/log", "/var/run/syslog")
IDENT = f"logalert[{os.getpid()}]: "

_LEVEL_WORDS = {logging.DEBUG: "debug: ", logging.INFO: "", logging.WARNING: "warning: ",
                logging.ERROR: "error: ", logging.CRITICAL: "critical: "}


class OneLine(logging.Formatter):
    """A record as one header-clean line: ``<prefix><level word><message>``; ``stamp`` puts
    the local time with its offset before the prefix (the file destination; syslog and the
    console have their own)."""

    def __init__(self, prefix: str, *, stamp: bool = False) -> None:
        super().__init__("%(message)s")
        self.prefix = prefix
        self.stamp = stamp

    def format(self, record: logging.LogRecord) -> str:
        word = _LEVEL_WORDS.get(record.levelno, record.levelname.lower() + ": ")
        line = clean_header(self.prefix + word + super().format(record))
        if self.stamp:
            when = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created))
            return f"{when} {line}"
        return line


class _Stderr(logging.StreamHandler[TextIO]):
    """The console destination, and every fallback; a failure here has nowhere left to go."""

    def __init__(self) -> None:
        super().__init__(sys.stderr)
        self.setFormatter(OneLine("logalert: "))

    def handleError(self, record: logging.LogRecord) -> None:
        return


class _Loud(logging.Handler):
    """A destination handler that fails ONCE, loudly, then forwards to stderr: the stdlib
    prints a traceback per failed record (``logging.raiseExceptions``), which is exactly the
    noise a cron mail must not carry. ``forward`` is the stderr handler the records go to
    after the failure; ``None`` while the destination works, or when a stderr handler is
    attached to the logger already (``--debug``) and receives every record itself."""

    where = ""  # the destination as the warning names it
    dead = False
    forward: logging.Handler | None = None
    debug_on_stderr = False  # a stderr handler is attached beside this one

    def emit(self, record: logging.LogRecord) -> None:
        if self.dead:
            if self.forward is not None:
                self.forward.handle(record)
            return
        super().emit(record)

    def handleError(self, record: logging.LogRecord) -> None:
        if self.dead:
            return
        self.dead = True
        exc = sys.exc_info()[1]
        reason = f"{type(exc).__name__}: {exc}" if exc is not None else "unknown error"
        stderr = _Stderr()
        stderr.setLevel(logging.INFO)
        if not self.debug_on_stderr:
            self.forward = stderr
        notice = logging.getLogger(LOGGER).makeRecord(
            LOGGER, logging.WARNING, __file__, 0,
            "the activity log at %s failed (%s); logging to stderr from here on",
            (self.where, reason), None)
        stderr.handle(notice)
        if self.forward is not None:
            self.forward.handle(record)  # the record that failed; with --debug it arrived


class _Syslog(_Loud, logging.handlers.SysLogHandler):
    pass


class _File(_Loud, logging.FileHandler):
    def _open(self) -> TextIOWrapper:
        """Append to a regular file that is not reached through a symlink; the refusal's text
        is what the fallback line prints."""
        path = self.baseFilename
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            mode = stat.S_IFREG  # created below
        if stat.S_ISLNK(mode):
            raise OSError(errno.ELOOP, "is a symbolic link -- name the real path")
        if stat.S_ISDIR(mode):
            raise IsADirectoryError(errno.EISDIR, "is a directory")
        if not stat.S_ISREG(mode):
            raise OSError(errno.EINVAL, "not a regular file")
        # O_BINARY: the CRT would translate the text layer's newlines a second time
        flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_BINARY", 0))
        fd = os.open(path, flags, 0o644)
        if not stat.S_ISREG(os.fstat(fd).st_mode):  # swapped between the lstat and the open
            os.close(fd)
            raise OSError(errno.EINVAL, "not a regular file")
        return open(fd, "a", encoding=self.encoding, errors=self.errors)


@dataclass(frozen=True)
class SyslogProbe:
    """What ``syslog`` resolves to here: the first usable socket path with the socket type
    that connected, or ``None`` with one reason per candidate."""

    found: tuple[str, int] | None
    rejected: tuple[str, ...]

    def reasons(self) -> str:
        return "; ".join(self.rejected)


def probe_syslog() -> SyslogProbe:
    rejected: list[str] = []
    if sys.platform == "win32":
        return SyslogProbe(None, ("no syslog on this platform",))
    else:  # mypy narrows the platform per branch; AF_UNIX exists only here
        for path in SYSLOG_SOCKETS:
            try:
                if not stat.S_ISSOCK(os.stat(path).st_mode):
                    rejected.append(f"{path}: not a socket")
                    continue
            except FileNotFoundError:
                rejected.append(f"{path}: missing")
                continue
            except OSError as exc:
                rejected.append(f"{path}: {(exc.strerror or str(exc)).lower()}")
                continue
            refused = ""
            for socktype in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
                try:
                    with socket.socket(socket.AF_UNIX, socktype) as probe:
                        probe.connect(path)
                except OSError as exc:
                    refused = refused or (exc.strerror or str(exc)).lower()
                    continue
                return SyslogProbe((path, socktype), tuple(rejected))
            rejected.append(f"{path}: {refused}")
        return SyslogProbe(None, tuple(rejected))


def describe(spec: str) -> str:
    """The destination as ``--check-config`` prints it: what ``syslog`` resolves to here."""
    if spec != "syslog":
        return spec
    if sys.platform == "win32":
        return "syslog -- not available on this platform; the run would log to stderr"
    probe = probe_syslog()
    if probe.found is None:
        return f"syslog -- no usable socket ({probe.reasons()}); the run would log to stderr"
    return f"syslog ({probe.found[0]})"


def _open(spec: str) -> tuple[_Loud | None, str | None]:
    """The handler for a destination, or ``None`` and the one warning the fallback logs."""
    if spec == "syslog":
        if sys.platform == "win32":
            return None, "syslog is not available on this platform; logging to stderr"
        probe = probe_syslog()
        if probe.found is None:
            return None, f"no usable syslog socket ({probe.reasons()}); logging to stderr"
        path, socktype = probe.found
        return _syslog(_Syslog(address=path, socktype=socktype), f"syslog ({path})"), None
    if spec.startswith("file:"):
        path = spec[len("file:"):]
        try:
            handler = _File(path, encoding="utf-8")
        except OSError as exc:
            return None, (f"cannot open the activity log {path} ({exc.strerror or exc}); "
                          f"logging to stderr")
        handler.where = spec
        handler.setFormatter(OneLine(IDENT, stamp=True))
        return handler, None
    # udp:host:port, validated by the loader; built here and nowhere else
    try:
        return _syslog(_Syslog(address=udp_address(spec)), spec), None
    except socket.gaierror as exc:
        return None, f"{spec}: cannot resolve the host ({exc}); logging to stderr"
    except OSError as exc:
        return None, f"{spec}: cannot open ({exc}); logging to stderr"


def _syslog(handler: _Syslog, where: str) -> _Syslog:
    handler.where = where
    handler.ident = IDENT
    handler.setFormatter(OneLine(""))
    return handler


@contextlib.contextmanager
def attach(spec: str, *, debug: bool = False, to_stderr: bool = False) -> Iterator[None]:
    """Wire the activity log for one call: the destination at INFO, a stderr handler at DEBUG
    under ``debug``, stderr instead of the destination under ``to_stderr``; unwired after."""
    logger = logging.getLogger(LOGGER)
    handlers: list[logging.Handler] = []
    notice: str | None = None
    stderr: _Stderr | None = None
    if to_stderr or spec == "stderr":
        stderr = _Stderr()
    else:
        destination, notice = _open(spec)
        if destination is None:
            stderr = _Stderr()
        else:
            destination.setLevel(logging.INFO)
            destination.debug_on_stderr = debug
            handlers.append(destination)
    if debug and stderr is None:
        stderr = _Stderr()
    if stderr is not None:
        stderr.setLevel(logging.DEBUG if debug else logging.INFO)
        handlers.append(stderr)
    for handler in handlers:
        logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    try:
        if notice:
            logger.warning("%s", notice)
        yield
    finally:
        try:
            for handler in handlers:
                logger.removeHandler(handler)
                # a destination that failed at its flush fails again here (the bytes are
                # still in the buffer): it was reported then, and nothing reached it since;
                # a stream closed under a handler is the ValueError
                with contextlib.suppress(OSError, ValueError):
                    handler.close()
        finally:
            logger.setLevel(logging.NOTSET)
