"""The state file: where each watch left off in each file, between runs.

One JSON file (``state_file``, default /var/lib/logalert/state.json), keyed by (section, the
configured absolute path). A file listed by two sections has two cursors, so a section whose
mail failed keeps its position while the other advances -- at-least-once delivery per section.

Rules (decided in issue #7, pinned by tests/test_state.py):
  * The file is written ATOMICALLY: a temporary file in the SAME directory (``mkstemp``, which
    creates it 0600 -- so the state directory belongs to the user cron runs logalert as),
    ``fsync``, then ``os.replace``. A crash leaves either the old file or the new one.
  * It is written after EACH section's mail is accepted, never once at the end. A run that can
    send but cannot persist would double-send next time, so ``check_state_dir`` runs BEFORE any
    mail goes out.
  * A missing file is the first run. A file that cannot be parsed is a hard error naming the
    file and ``--reset-state``; logalert never silently starts over.
  * An entry unseen for ``state_ttl`` days expires; ``touch`` records a sighting even when the
    section's mail failed, so a file that is present never expires.
  * ``version`` is the schema version. A file from a newer logalert is refused, not guessed at.
    A cursor's ``line`` (the complete lines before ``offset``, so a report can number lines
    as the file does) is optional: a file without it is read, counted once, and updated.
  * A run as root against a state file another user owns is refused up front: ``mkstemp``
    plus ``os.replace`` would hand the file to root, and that user's next cron run could
    not read it. The remedy is named (``sudo -u <owner>``), never applied by chown.
  * ``runs`` (issue #18) records, per section and per glob in its ``files``, the start of
    the last SUCCESSFUL run that looked into that glob: what the run's new-file rule
    compares a first sight against. A glob's moment moves when the section's cursors move
    and the glob's directories were listed without error -- never when its delivery
    failed, and never for a glob whose directory was away or unlistable that run, so a
    file created during the outage is still newer than the moment when the directory is
    back; a glob never looked into has none. ``forget`` drops a section's record with its
    cursors so ``--reset-state``'s promise (the next run starts at the end, or the
    beginning per ``start``) stays true; a record whose section left the configuration
    expires after ``state_ttl`` days, a configured section's never (its next saved run
    refreshes it). A file without ``runs`` is from before #18 and reads as if no section
    had run. The schema version stays 1: an older logalert reads such a file correctly and
    drops the record on save.
"""

import json
import os
import sys
import tempfile
from collections.abc import Collection, Iterable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any

if sys.platform != "win32":
    import pwd

STATE_VERSION = 1
LOCK_FILE_NAME = "lock"

RESET_HINT = "fix it or start over with --reset-state"
_MAX_OFFSET = 2**63 - 1  # seek() refuses more; inode and device ids are unbounded


class StateError(Exception):
    """A state file or directory that cannot be used, with the path in the message."""


@dataclass(frozen=True)
class Cursor:
    """Where one section left off in one file."""

    offset: int  # the byte just after the last COMPLETE line read (uncompressed stream)
    ino: int
    dev: int
    fingerprint: str | None  # sha256 of the first complete line; None until the file has one
    realpath: str
    last_seen: str  # ISO 8601 UTC, seconds; the last run that found the file present
    line: int | None = None  # complete lines before offset; None in a file from before #9


RunRecord = dict[str, str]  # glob, as written in ``files`` -> ISO 8601 UTC seconds, the run START


def timestamp(now: datetime | None = None) -> str:
    """The ``last_seen`` form: ``2026-09-14T23:51:38Z``. ``now`` must be timezone-aware."""
    return _aware(now).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("naive datetime; pass a timezone-aware one")
    return now


def parse_timestamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def lock_path(state_file: str) -> str:
    """The run lock lives next to the state file."""
    return os.path.join(os.path.dirname(state_file), LOCK_FILE_NAME)


class State:
    """The cursors of one state file, in memory; ``save`` writes them back atomically."""

    def __init__(self, path: str, entries: dict[tuple[str, str], Cursor] | None = None,
                 runs: dict[str, RunRecord] | None = None) -> None:
        self.path = path
        self.entries: dict[tuple[str, str], Cursor] = dict(entries or {})
        self.runs: dict[str, RunRecord] = dict(runs or {})
        self.dirty = False

    def get(self, section: str, file: str) -> Cursor | None:
        return self.entries.get((section, file))

    def set(self, section: str, file: str, cursor: Cursor) -> None:
        self.entries[(section, file)] = cursor
        self.dirty = True

    def touch(self, section: str, file: str, now: datetime | None = None) -> None:
        """Record that the file was present this run, without moving the cursor."""
        cursor = self.entries.get((section, file))
        if cursor is not None:
            self.entries[(section, file)] = replace(cursor, last_seen=timestamp(now))
            self.dirty = True

    def record_run(self, section: str, files: Iterable[str], seen: Iterable[str],
                   started: datetime | None = None) -> None:
        """The section ran to a saved state: the globs in ``seen`` (looked into this run)
        get the run's start as their moment, the other globs of ``files`` keep the moment
        they had, and a glob no longer in ``files`` is dropped."""
        previous = self.runs.get(section, {})
        moment = timestamp(started)
        wanted = set(seen)
        self.runs[section] = {
            glob: moment if glob in wanted else previous[glob]
            for glob in files if glob in wanted or glob in previous
        }
        self.dirty = True

    def last_run(self, section: str, glob: str) -> str | None:
        """When the last saved run that looked into ``glob`` started, or None."""
        return self.runs.get(section, {}).get(glob)

    def forget(self, file: str | None = None) -> int:
        """Drop every entry (``file`` None) or every section's entry for one file -- and the
        run record of each section that lost an entry, so its next first sight is a plain
        one (see the module docstring); returns the number of entries dropped."""
        keys = [k for k in self.entries if file is None or k[1] == file]
        for key in keys:
            del self.entries[key]
            self.runs.pop(key[0], None)
        if file is None:
            self.runs.clear()
        if keys or file is None:
            self.dirty = True
        return len(keys)

    def expire(self, ttl_days: int, now: datetime | None = None) -> list[tuple[str, str]]:
        """Drop entries unseen for longer than ``ttl_days``; returns what was dropped."""
        moment = _aware(now)
        dropped: list[tuple[str, str]] = []
        for key, cursor in list(self.entries.items()):
            age = moment - parse_timestamp(cursor.last_seen)
            if age.total_seconds() > ttl_days * 86400:
                del self.entries[key]
                dropped.append(key)
        if dropped:
            self.dirty = True
        return dropped

    def expire_runs(self, ttl_days: int, now: datetime | None = None, *,
                    configured: Collection[str] = ()) -> list[str]:
        """Drop the run records of sections that are not in the configuration and whose
        newest moment is older than ``ttl_days``; returns their names. A configured
        section's record is kept however old: its next saved run refreshes it, and losing
        it would silently turn the section's next new file into a first sight at the end."""
        moment = _aware(now)
        dropped: list[str] = []
        for section, record in list(self.runs.items()):
            if section in configured:
                continue
            newest = max((parse_timestamp(at) for at in record.values()), default=moment)
            if (moment - newest).total_seconds() > ttl_days * 86400 or not record:
                del self.runs[section]
                dropped.append(section)
        if dropped:
            self.dirty = True
        return dropped

    def to_json(self) -> dict[str, Any]:
        entries: dict[str, dict[str, dict[str, Any]]] = {}
        for (section, file), cursor in sorted(self.entries.items()):
            entries.setdefault(section, {})[file] = asdict(cursor)
        runs = {section: dict(sorted(record.items()))
                for section, record in sorted(self.runs.items())}
        return {"version": STATE_VERSION, "entries": entries, "runs": runs}

    def save(self) -> None:
        """Write the file atomically; see the module docstring. Clears ``dirty``."""
        write_atomically(self.path, json.dumps(self.to_json(), indent=2) + "\n")
        self.dirty = False


def load_state(path: str) -> State:
    """Read the state file; a missing file is an empty state (the first run)."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return State(path)
    except PermissionError as exc:
        raise StateError(f"state file {path}: cannot read ({exc.strerror}) -- is it owned "
                         f"by another user? -- {RESET_HINT}") from exc
    except OSError as exc:
        raise StateError(f"state file {path}: cannot read ({exc.strerror}) -- "
                         f"{RESET_HINT}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StateError(f"state file {path}: not valid JSON ({exc}) -- {RESET_HINT}") from exc
    return State(path, _entries(path, data), _runs(path, data))


def _corrupt(path: str, what: str) -> StateError:
    return StateError(f"state file {path}: {what} -- {RESET_HINT}")


def _runs(path: str, data: dict[str, Any]) -> dict[str, RunRecord]:
    """The ``runs`` records; absent in a file from before #18, which is not an error."""
    sections = data.get("runs", {})
    if not isinstance(sections, dict):
        raise _corrupt(path, "'runs' is not an object")
    runs: dict[str, RunRecord] = {}
    for section, moments in sections.items():
        where = f"runs[{section!r}]"
        if not isinstance(moments, dict):
            raise _corrupt(path, f"{where} is not an object")
        for glob, at in moments.items():
            if not isinstance(at, str):
                raise _corrupt(path, f"{where}[{glob!r}] is not a string")
            try:
                parse_timestamp(at)
            except ValueError:
                raise _corrupt(path, f"{where}[{glob!r}] is not a UTC timestamp") from None
        runs[section] = dict(moments)
    return runs


def _entries(path: str, data: Any) -> dict[tuple[str, str], Cursor]:
    def corrupt(what: str) -> StateError:
        return _corrupt(path, what)

    if not isinstance(data, dict):
        raise corrupt("the top level is not an object")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise corrupt("no integer 'version'")
    if version > STATE_VERSION:
        raise corrupt(f"schema version {version} was written by a newer logalert (this one "
                      f"reads {STATE_VERSION})")
    sections = data.get("entries")
    if not isinstance(sections, dict):
        raise corrupt("'entries' is not an object")
    entries: dict[tuple[str, str], Cursor] = {}
    for section, files in sections.items():
        if not isinstance(files, dict):
            raise corrupt(f"entries[{section!r}] is not an object")
        for file, fields in files.items():
            if not isinstance(fields, dict):
                raise corrupt(f"entries[{section!r}][{file!r}] is not an object")
            entries[(section, file)] = _cursor(fields, corrupt, f"[{section!r}][{file!r}]")
    return entries


def _cursor(fields: dict[str, Any], corrupt: Any, where: str) -> Cursor:
    def integer(key: str, high: int | None = None) -> int:
        value = fields.get(key)
        if (not isinstance(value, int) or isinstance(value, bool) or value < 0
                or (high is not None and value > high)):
            raise corrupt(f"entries{where}: {key!r} is not a non-negative integer")
        return value

    def text(key: str, optional: bool = False) -> Any:
        value = fields.get(key)
        if value is None and optional:
            return None
        if not isinstance(value, str):
            raise corrupt(f"entries{where}: {key!r} is not a string")
        return value

    offset, ino, dev = integer("offset", _MAX_OFFSET), integer("ino"), integer("dev")
    fp, realpath = text("fingerprint", optional=True), text("realpath")
    last_seen = text("last_seen")
    line = integer("line", _MAX_OFFSET) if fields.get("line") is not None else None
    try:
        parse_timestamp(last_seen)
    except ValueError:
        raise corrupt(f"entries{where}: 'last_seen' is not a UTC timestamp") from None
    return Cursor(offset=offset, ino=ino, dev=dev, fingerprint=fp, realpath=realpath,
                  last_seen=last_seen, line=line)


def check_state_dir(state_file: str) -> None:
    """Prove the state can be persisted BEFORE anything irreversible happens.

    Creates and removes a temporary file the way ``save`` will; raises StateError with what
    to do. Never creates the directory: a root run would leave it owned by root and lock the
    cron user out, which is exactly the failure this check exists to name -- and a root run
    against another user's state file is refused for the same reason.
    """
    directory = os.path.dirname(state_file) or "."
    if not os.path.isdir(directory):
        raise StateError(f"state directory {directory} does not exist -- create it, owned by "
                         f"the user logalert runs as")
    try:
        fd, temp = tempfile.mkstemp(prefix=".probe.", dir=directory)
    except OSError as exc:
        raise StateError(f"state directory {directory} is not writable ({exc.strerror}) -- "
                         f"nothing was sent, because a run that cannot save its position "
                         f"would send everything again next time") from exc
    os.close(fd)
    os.unlink(temp)
    if os.path.exists(state_file) and not os.path.isfile(state_file):
        raise StateError(f"state file {state_file} is not a regular file")
    if sys.platform == "win32" and os.path.isfile(state_file) and not os.access(
            state_file, os.W_OK):
        # the read-only attribute survives the directory probe and fails os.replace later
        raise StateError(f"state file {state_file} is read-only -- nothing was sent")
    owner = _foreign_owner(state_file)
    if owner is not None:
        if os.path.exists(state_file):
            raise StateError(f"state file {state_file} belongs to {owner}; a run as root "
                             f"would leave it root-owned and unreadable by that user -- "
                             f"run as that user instead: sudo -u {owner} logalert ...")
        raise StateError(f"state directory {directory} belongs to {owner}; a run as root "
                         f"would leave the state and the lock root-owned and unusable by "
                         f"that user -- run as that user instead: sudo -u {owner} "
                         f"logalert ...")


def _foreign_owner(state_file: str) -> str | None:
    """The owner of the state file -- or, before the first run, of its directory -- when we
    are root and it is not root's: a root run into another user's directory would leave
    root-owned state and a root-owned lock there, and follow whatever that user planted."""
    if sys.platform == "win32":
        return None
    else:  # mypy narrows the platform per branch, not past an early return
        if os.geteuid() != 0:
            return None
        try:
            uid = os.stat(state_file).st_uid
        except FileNotFoundError:
            uid = os.stat(os.path.dirname(state_file) or ".").st_uid
        if uid == 0:
            return None
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return f"#{uid}"  # sudo -u accepts a numeric uid spelled this way


def write_atomically(path: str, text: str) -> None:
    """Temp file in the same directory (``os.replace`` across devices is EXDEV), fsync, replace."""
    directory = os.path.dirname(path) or "."
    try:
        fd, temp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=directory)
    except OSError as exc:
        raise StateError(f"state directory {directory} is not writable ({exc.strerror})") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException as exc:
        try:
            os.unlink(temp)
        except OSError:
            pass
        if isinstance(exc, OSError):
            raise StateError(f"state file {path}: cannot write ({exc.strerror})") from exc
        raise
