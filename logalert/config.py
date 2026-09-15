"""The configuration file: format, loading, validation.

One INI-style file (default /etc/logalert.conf). A reserved ``[logalert]`` section carries the
global settings; every other section is a WATCH: a subject for the alert email, recipients, the
log files to read, and the patterns to look for. This module is the single place that turns the
text into validated settings -- every other part of logalert consumes the dataclasses below and
never reopens the schema.

Format rules (decided, and pinned by tests/test_config.py):
  * ``configparser`` with interpolation OFF (a ``%`` in a pattern is literal) and strict mode
    (a duplicate section or key is an error). Keys are case-insensitive; section names are
    case-sensitive and case-preserved.
  * A multi-line value (indented continuation lines) is a list, one entry per line. An entry
    that is empty after stripping is an ERROR: an empty pattern would match every line.
  * Patterns are case-SENSITIVE by default. ``ipatterns`` / ``iregex`` opt in to
    case-insensitive matching; ``regex`` / ``iregex`` are Python regular expressions.
  * A pattern line may start with a priority tag -- exactly ``[high] ``, ``[medium] `` or
    ``[low] ``. Any other bracketed prefix is part of the literal. The priority system is OFF
    unless a section sets ``priority`` or a pattern carries a tag.
  * Paths are absolute, on the platform logalert runs on. Glob characters are rejected in
    0.1.0 so a glob is never silently taken for a literal path.
  * Addresses are bare ``local@domain`` in 0.1.0: ASCII, no display name, no leading ``-``
    (every sendmail implementation would read it as an option), no whitespace.
  * A continuation line that begins with ``#`` or ``;`` is dropped by configparser as a
    comment. That is a documented limitation of literal patterns; a regex with an escape is the
    way to match such text.

Errors are ``ConfigError`` with a message of the form ``[section] key: what is wrong`` or, for
the errors configparser itself detects, ``path:line: what is wrong``.
"""

import configparser
import getpass
import math
import os
import re
import socket
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from os import path as ospath
from typing import Literal, cast

if sys.platform != "win32":
    import pwd

RESERVED_SECTION = "logalert"

DEFAULT_CONFIG_PATH = "/etc/logalert.conf"
DEFAULT_STATE_FILE = "/var/lib/logalert/state.json"
DEFAULT_SENDMAIL_PATH = "/usr/sbin/sendmail"
DEFAULT_SMTP_PORT = 25
DEFAULT_MAIL_TIMEOUT = 60.0
DEFAULT_LOCK_STALE = 3600
DEFAULT_STATE_TTL_DAYS = 30
DEFAULT_MAX_LINES = 200

Priority = Literal["high", "medium", "low"]
PRIORITIES: tuple[str, ...] = ("high", "medium", "low")
REPORT_MODES: tuple[str, ...] = ("inline", "attachment")
START_MODES: tuple[str, ...] = ("end", "beginning")
TRANSPORTS: tuple[str, ...] = ("auto", "sendmail", "smtp")

WATCH_KEYS: frozenset[str] = frozenset(
    {
        "subject", "to", "files",
        "patterns", "ipatterns", "regex", "iregex",
        "exclude", "iexclude", "exclude_regex", "iexclude_regex",
        "priority", "report", "context", "max_lines", "start", "archive_dir",
    }
)
PATTERN_KEYS: tuple[str, ...] = ("patterns", "ipatterns", "regex", "iregex")
EXCLUDE_KEYS: tuple[str, ...] = ("exclude", "iexclude", "exclude_regex", "iexclude_regex")
GLOBAL_KEYS: frozenset[str] = frozenset(
    {
        "from", "state_file", "log", "transport", "sendmail_path",
        "smtp_host", "smtp_port", "smtp_starttls", "mail_timeout",
        "lock_stale", "state_ttl", "subject_suffix",
    }
)

_GLOB_CHARS = frozenset("*?[")
_PRIORITY_TAG = re.compile(r"^\[(high|medium|low)\] (.+)$", re.S)
# Bare address: an RFC 5322 dot-atom local part (no quotes, no display name, no angle
# brackets) and a domain of letters, digits, dots and hyphens. Deliberately narrower than
# what mail systems accept: what a cron job hands to sendmail must be unambiguous.
_ADDRESS = re.compile(
    r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$"
)
_BOOL_TRUE = frozenset({"yes", "true", "on", "1"})
_BOOL_FALSE = frozenset({"no", "false", "off", "0"})


class ConfigError(Exception):
    """A configuration file that cannot be used, with the location in the message."""


@dataclass(frozen=True)
class Pattern:
    """One thing to look for (or, for excludes, one thing to drop)."""

    text: str
    regex: bool
    ignore_case: bool
    priority: Priority | None = None
    compiled: re.Pattern[str] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class Watch:
    """One section of the config: what to read, what to look for, whom to tell."""

    name: str
    subject: str
    to: tuple[str, ...]
    files: tuple[str, ...]
    patterns: tuple[Pattern, ...]
    excludes: tuple[Pattern, ...]
    priority: Priority | None  # None: the priority system is off for this section
    report: Literal["inline", "attachment"]
    context: int | None  # None: use the command line's -c
    max_lines: int
    start: Literal["end", "beginning"]
    archive_dir: str | None


@dataclass(frozen=True)
class Settings:
    """The reserved ``[logalert]`` section."""

    from_address: str | None = None
    state_file: str = DEFAULT_STATE_FILE
    log: str = "syslog"
    transport: Literal["auto", "sendmail", "smtp"] = "auto"
    sendmail_path: str = DEFAULT_SENDMAIL_PATH
    smtp_host: str | None = None
    smtp_port: int = DEFAULT_SMTP_PORT
    smtp_starttls: bool = False
    mail_timeout: float = DEFAULT_MAIL_TIMEOUT
    lock_stale: int = DEFAULT_LOCK_STALE
    state_ttl_days: int = DEFAULT_STATE_TTL_DAYS
    subject_suffix: bool = True


@dataclass(frozen=True)
class Config:
    path: str
    settings: Settings
    watches: tuple[Watch, ...]
    warnings: tuple[str, ...]  # unknown keys and the like: reported, never fatal


def example_config() -> str:
    """The complete, commented example shipped inside the package."""
    return resources.files("logalert.data").joinpath("logalert.conf.example").read_text(
        encoding="utf-8"
    )


def load_config(path: str) -> Config:
    """Read and validate the file at ``path``. Raises ConfigError; never touches state."""
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        read = parser.read(path, encoding="utf-8-sig")
    except configparser.Error as exc:
        raise ConfigError(_parser_error(path, exc)) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not valid UTF-8 ({exc.reason} at byte {exc.start})") from exc
    if not read:
        raise ConfigError(f"{path}: config file not found or not readable")
    if parser.defaults():
        # configparser would merge these keys into every section, [logalert] included
        raise ConfigError(f"{path}: [DEFAULT] is not supported; every section other than "
                          f"[{RESERVED_SECTION}] is a watch")

    warnings: list[str] = []
    settings = Settings()
    watches: list[Watch] = []
    for name in parser.sections():
        if name == RESERVED_SECTION:
            settings = _load_settings(parser[name], warnings)
        elif name.lower() == RESERVED_SECTION:
            raise ConfigError(
                f"[{name}]: the name is reserved for the global settings; spell it "
                f"[{RESERVED_SECTION}] or rename the watch"
            )
        else:
            watches.append(_load_watch(name, parser[name], warnings))
    if not watches:
        raise ConfigError(f"{path}: no watch sections (only [{RESERVED_SECTION}] or nothing)")
    return Config(path=path, settings=settings, watches=tuple(watches), warnings=tuple(warnings))


# -- the reserved section --------------------------------------------------------------------


def _load_settings(section: configparser.SectionProxy, warnings: list[str]) -> Settings:
    where = f"[{RESERVED_SECTION}]"
    for key in section:
        if key in WATCH_KEYS:
            raise ConfigError(f"{where} {key}: this is a watch key; [{RESERVED_SECTION}] holds "
                              f"only the global settings")
        if key not in GLOBAL_KEYS:
            warnings.append(f"{where} {key}: unknown key, ignored")

    from_address = _optional(where, section, "from")
    if from_address is not None:
        _check_address(where, "from", from_address)

    transport = _enum(where, section, "transport", TRANSPORTS, "auto")
    smtp_host = _optional(where, section, "smtp_host")
    if transport == "smtp" and smtp_host is None:
        raise ConfigError(f"{where} smtp_host: required when transport = smtp")

    log = _optional(where, section, "log") or "syslog"
    _check_log(where, log)

    return Settings(
        from_address=from_address,
        state_file=_abs_path(where, section, "state_file", DEFAULT_STATE_FILE),
        log=log,
        transport=cast(Literal["auto", "sendmail", "smtp"], transport),
        sendmail_path=_abs_path(where, section, "sendmail_path", DEFAULT_SENDMAIL_PATH),
        smtp_host=smtp_host,
        smtp_port=_int(where, section, "smtp_port", DEFAULT_SMTP_PORT, low=1, high=65535),
        smtp_starttls=_bool(where, section, "smtp_starttls", False),
        mail_timeout=_number(where, section, "mail_timeout", DEFAULT_MAIL_TIMEOUT),
        lock_stale=_int(where, section, "lock_stale", DEFAULT_LOCK_STALE, low=1),
        state_ttl_days=_int(where, section, "state_ttl", DEFAULT_STATE_TTL_DAYS, low=1),
        subject_suffix=_bool(where, section, "subject_suffix", True),
    )


def _check_log(where: str, log: str) -> None:
    if log in ("syslog", "stderr"):
        return
    if log.startswith("file:"):
        target = log[len("file:"):]
        if not target or not ospath.isabs(target):
            raise ConfigError(f"{where} log: file: needs an absolute path, got {target!r}")
        return
    if log.startswith("udp:"):
        host, sep, port = log[len("udp:"):].rpartition(":")
        # isdigit() alone accepts digits int() rejects (superscripts, other scripts)
        digits = port.isascii() and port.isdigit()
        if not sep or not host or not digits or not 1 <= int(port) <= 65535:
            raise ConfigError(f"{where} log: udp: needs host:port, got {log[4:]!r}")
        return
    raise ConfigError(
        f"{where} log: expected syslog, stderr, file:/absolute/path or udp:host:port, got {log!r}"
    )


# -- watch sections --------------------------------------------------------------------------


def _load_watch(name: str, section: configparser.SectionProxy, warnings: list[str]) -> Watch:
    where = f"[{name}]"
    for key in section:
        if key not in WATCH_KEYS:
            if key in GLOBAL_KEYS:
                raise ConfigError(
                    f"{where} {key}: this is a [{RESERVED_SECTION}] setting, not a watch key"
                )
            warnings.append(f"{where} {key}: unknown key, ignored")

    subject = _required(where, section, "subject")

    to = tuple(_list(where, section, "to", required=True, split_commas=True))
    for address in to:
        _check_address(where, "to", address)

    files = tuple(_list(where, section, "files", required=True))
    for file in files:
        _check_path(where, "files", file)

    section_priority = _enum(where, section, "priority", PRIORITIES, None)
    patterns: list[Pattern] = []
    for key in PATTERN_KEYS:
        patterns.extend(_patterns(where, section, key, is_exclude=False))
    if not patterns:
        raise ConfigError(f"{where}: at least one of {', '.join(PATTERN_KEYS)} is required")
    excludes: list[Pattern] = []
    for key in EXCLUDE_KEYS:
        excludes.extend(_patterns(where, section, key, is_exclude=True))

    archive_dir = _optional(where, section, "archive_dir")
    if archive_dir is not None:
        _check_path(where, "archive_dir", archive_dir)

    context = _optional(where, section, "context")
    return Watch(
        name=name,
        subject=subject,
        to=to,
        files=files,
        patterns=tuple(patterns),
        excludes=tuple(excludes),
        priority=cast(Priority | None, section_priority),
        report=cast(
            Literal["inline", "attachment"],
            _enum(where, section, "report", REPORT_MODES, "inline"),
        ),
        context=None if context is None else _int(where, section, "context", 0, low=0),
        max_lines=_int(where, section, "max_lines", DEFAULT_MAX_LINES, low=1),
        start=cast(
            Literal["end", "beginning"], _enum(where, section, "start", START_MODES, "end")
        ),
        archive_dir=archive_dir,
    )


def _patterns(
    where: str, section: configparser.SectionProxy, key: str, *, is_exclude: bool
) -> list[Pattern]:
    is_regex = "regex" in key
    ignore_case = key.startswith("i")
    result: list[Pattern] = []
    for line in _list(where, section, key, required=False):
        priority: Priority | None = None
        text = line
        if not is_exclude:
            tag = _PRIORITY_TAG.match(line)
            if tag:
                priority = cast(Priority, tag.group(1))
                # stripped like every untagged entry: "[high]  x" must not become " x"
                text = tag.group(2).strip()
        compiled: re.Pattern[str] | None = None
        if is_regex:
            try:
                compiled = re.compile(text, re.IGNORECASE if ignore_case else 0)
            # re.compile also raises OverflowError (a{99999999999}) and RecursionError
            except (re.error, OverflowError, RecursionError) as exc:
                raise ConfigError(f"{where} {key}: cannot compile {text!r}: {exc}") from exc
        result.append(
            Pattern(
                text=text, regex=is_regex, ignore_case=ignore_case, priority=priority,
                compiled=compiled,
            )
        )
    return result


# -- value readers ---------------------------------------------------------------------------


def _optional(where: str, section: configparser.SectionProxy, key: str) -> str | None:
    """A scalar value, or None when absent or blank. A continuation line is an error here:
    a path or a host with a newline inside would pass every other check and fail at run time."""
    value = section.get(key)
    if value is None:
        return None
    value = value.strip()
    if "\n" in value:
        raise ConfigError(f"{where} {key}: must be a single line")
    return value or None


def _required(where: str, section: configparser.SectionProxy, key: str) -> str:
    if section.get(key) is None:
        raise ConfigError(f"{where} {key}: required")
    value = _optional(where, section, key)
    if value is None:
        raise ConfigError(f"{where} {key}: is empty")
    return value


def _list(
    where: str, section: configparser.SectionProxy, key: str, *, required: bool,
    split_commas: bool = False,
) -> list[str]:
    """A multi-line value as a list. The value's leading and trailing blank lines are layout;
    an empty entry INSIDE the list is an error (an empty pattern would match every line)."""
    raw = section.get(key)
    if raw is None:
        if required:
            raise ConfigError(f"{where} {key}: required")
        return []
    # split on the newline configparser joins with, not splitlines(): a pattern carrying a
    # form feed, NEL or U+2028 is one line to the parser and must stay one pattern
    lines: Iterable[str] = raw.strip().split("\n")
    if split_commas:
        lines = [part for line in lines for part in line.split(",")]
    entries = [line.strip() for line in lines]
    if not entries or not any(entries):
        raise ConfigError(f"{where} {key}: is empty")
    if any(not entry for entry in entries):
        raise ConfigError(f"{where} {key}: has an empty entry (a blank line or a stray comma)")
    return entries


def _enum(
    where: str, section: configparser.SectionProxy, key: str, allowed: tuple[str, ...],
    default: str | None,
) -> str | None:
    value = _optional(where, section, key)
    if value is None:
        return default
    value = value.lower()
    if value not in allowed:
        raise ConfigError(f"{where} {key}: expected one of {', '.join(allowed)}, got {value!r}")
    return value


def _int(
    where: str, section: configparser.SectionProxy, key: str, default: int, *,
    low: int | None = None, high: int | None = None,
) -> int:
    value = _optional(where, section, key)
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError as exc:
        raise ConfigError(f"{where} {key}: expected an integer, got {value!r}") from exc
    if (low is not None and number < low) or (high is not None and number > high):
        bounds = f">= {low}" if high is None else f"between {low} and {high}"
        raise ConfigError(f"{where} {key}: must be {bounds}, got {number}")
    return number


def _number(where: str, section: configparser.SectionProxy, key: str, default: float) -> float:
    value = _optional(where, section, key)
    if value is None:
        return default
    try:
        number = float(value)
    except ValueError as exc:
        raise ConfigError(f"{where} {key}: expected a number of seconds, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ConfigError(f"{where} {key}: must be a positive number, got {value}")
    return number


def _bool(where: str, section: configparser.SectionProxy, key: str, default: bool) -> bool:
    value = _optional(where, section, key)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    raise ConfigError(f"{where} {key}: expected yes or no, got {value!r}")


def _abs_path(where: str, section: configparser.SectionProxy, key: str, default: str) -> str:
    value = _optional(where, section, key)
    if value is None:
        return default
    _check_path(where, key, value)
    return value


def _check_path(where: str, key: str, value: str) -> None:
    if not ospath.isabs(value):
        raise ConfigError(f"{where} {key}: {value!r} is not an absolute path")
    if any(char in _GLOB_CHARS for char in value):
        raise ConfigError(
            f"{where} {key}: {value!r} contains a glob character; globs are not supported "
            f"in this release, list each file"
        )


def is_address(value: str) -> bool:
    """The bare ``local@domain`` the loader accepts: what ``from``, ``to`` and ``--from``
    (issue #12) must be. ``_check_address`` says which rule a rejected value broke."""
    return value.isascii() and not value.startswith("-") and _ADDRESS.fullmatch(value) is not None


def _check_address(where: str, key: str, value: str) -> None:
    if value.startswith("-"):
        raise ConfigError(f"{where} {key}: {value!r} starts with '-'")
    if not value.isascii():
        raise ConfigError(f"{where} {key}: {value!r} is not ASCII; non-ASCII addresses are not "
                          f"supported in this release")
    if not _ADDRESS.match(value):
        raise ConfigError(
            f"{where} {key}: {value!r} is not a bare local@domain address (no display "
            f"names, no angle brackets, no spaces)"
        )


# -- configparser's own errors ---------------------------------------------------------------


def _parser_error(path: str, exc: configparser.Error) -> str:
    lineno = getattr(exc, "lineno", None)
    if isinstance(exc, configparser.DuplicateSectionError):
        what = f"section [{exc.section}] appears twice"
    elif isinstance(exc, configparser.DuplicateOptionError):
        what = f"key {exc.option!r} appears twice in [{exc.section}]"
    elif isinstance(exc, configparser.MissingSectionHeaderError):
        what = "the file must start with a section header such as [logalert]"
    elif isinstance(exc, configparser.ParsingError):
        # exc.errors is (lineno, line) pairs; the line's spelling differs across 3.12-3.14
        # (repr there, raw text later), so only the numbers are reported
        numbers = [n for n, _ in exc.errors]
        others = f" (also line(s) {', '.join(map(str, numbers[1:]))})" if numbers[1:] else ""
        return (f"{path}:{numbers[0]}: not 'key = value', a [section] header or an indented "
                f"continuation line{others}")
    else:
        what = str(exc)
    return f"{path}:{lineno}: {what}" if lineno else f"{path}: {what}"


# -- the From address ------------------------------------------------------------------------


def default_from() -> str:
    """``<user>@<host>`` for the user logalert runs as: the From address when none is set.

    The user is the real one (``pwd`` on POSIX; ``getpass.getuser()`` follows ``$USER`` and lies
    under ``su``). The host is ``gethostname()`` when it already carries a dot and otherwise
    ``getfqdn()``, which can block for the resolver timeout on a host with broken DNS -- so this
    is computed only when ``from`` is unset, and once per run.
    """
    if sys.platform == "win32":
        user = getpass.getuser()
    else:
        user = pwd.getpwuid(os.geteuid()).pw_name
    host = socket.gethostname()
    if "." not in host:
        host = socket.getfqdn()
    return f"{user}@{host}"


def from_warning(address: str) -> str | None:
    """Why a From address may not travel beyond this host, or None. No lookups."""
    domain = address.rpartition("@")[2].lower()
    if "." not in domain:
        return (f"From address {address!r} has no domain part; set from = in "
                f"[{RESERVED_SECTION}]")
    if domain.endswith((".localdomain", ".local", ".localhost")):
        return (f"From address {address!r} ends in a local-only domain; set from = in "
                f"[{RESERVED_SECTION}]")
    return None


# -- --check-config --------------------------------------------------------------------------


def _sendmail_note(path: str) -> str:
    if not ospath.isfile(path):
        return "NOT FOUND -- install an MTA or set transport = smtp"
    if not os.access(path, os.X_OK):
        return "found but NOT EXECUTABLE -- fix its mode or set transport = smtp"
    return "found, executable"


def describe(config: Config) -> str:
    """The effective settings, one ASCII line each, for ``--check-config``."""
    settings = config.settings
    out: list[str] = [f"config: {config.path}"]
    if settings.transport == "auto":
        out.append(f"transport: auto -> sendmail at {settings.sendmail_path} "
                   f"({_sendmail_note(settings.sendmail_path)})")
    elif settings.transport == "sendmail":
        out.append(f"transport: sendmail at {settings.sendmail_path} "
                   f"({_sendmail_note(settings.sendmail_path)})")
    else:
        tls = "STARTTLS" if settings.smtp_starttls else "plain"
        out.append(f"transport: smtp to {settings.smtp_host}:{settings.smtp_port} ({tls})")
    if settings.from_address:
        out.append(f"from: {settings.from_address}")
        warning = from_warning(settings.from_address)
    else:
        address = default_from()
        out.append(f"from: {address} (default: the user logalert runs as, at this host)")
        warning = from_warning(address)
    if warning:
        out.append(f"warning: {warning}")
    out.append(f"state_file: {settings.state_file}")
    out.append(f"log: {settings.log}")
    out.append(f"mail_timeout: {settings.mail_timeout:g}s; lock_stale: {settings.lock_stale}s; "
               f"state_ttl: {settings.state_ttl_days} days; "
               f"subject_suffix: {'yes' if settings.subject_suffix else 'no'}")
    for watch in config.watches:
        literal = sum(1 for p in watch.patterns if not p.regex)
        regex = len(watch.patterns) - literal
        nocase = sum(1 for p in watch.patterns if p.ignore_case)
        tagged = sum(1 for p in watch.patterns if p.priority)
        out.append(f"[{watch.name}] subject: {watch.subject}")
        out.append(f"[{watch.name}] to: {', '.join(watch.to)}")
        for file in watch.files:
            out.append(f"[{watch.name}] file: {file}")
        out.append(f"[{watch.name}] patterns: {literal} literal, {regex} regex "
                   f"({nocase} case-insensitive, {tagged} with a priority tag); "
                   f"excludes: {len(watch.excludes)}")
        priority = watch.priority or ("off" if not tagged else "off (per-pattern tags only)")
        context = "from -c" if watch.context is None else str(watch.context)
        out.append(f"[{watch.name}] priority: {priority}; report: {watch.report}; "
                   f"context: {context}; max_lines: {watch.max_lines}; start: {watch.start}"
                   + (f"; archive_dir: {watch.archive_dir}" if watch.archive_dir else ""))
    for warning in config.warnings:
        out.append(f"warning: {warning}")
    return "\n".join(out)
