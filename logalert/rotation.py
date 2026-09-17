"""Rotation catch-up: when a watched file has rotated, read the rotated copies FIRST.

The cursor (``logalert.cursor``) decides that the live file is ROTATED or TRUNCATED; this module
finds where the lines written since the last run went and reads them before the live file, so
nothing written between two runs is missed (owner requirement), however many rotations happened
in between and whatever naming style the rotating tool uses.

The search (decided in issue #8; the logrotate, savelog and gzip facts measured in the sandbox
with logrotate 3.21, the newsyslog and TimedRotatingFileHandler names from their documentation):

  * Candidates are the regular files in ``dirname(realpath(path))``, in the directory the
    cursor was last following (a re-pointed link), and in the section's ``archive_dir``.
    Symbolic links are never candidates: no rotation tool makes archives as links, and a link
    could lead outside the directory. A file is an ARCHIVE OF THIS LOG when its name is the
    log's basename plus a rotation suffix, with an optional ``.gz`` / ``.bz2`` / ``.xz`` /
    ``.zst`` after it:
      - numeric ``.N`` (up to four digits) -- LOWER is NEWER (``.0`` is the latest for savelog
        and newsyslog, ``.1`` for logrotate);
      - dated: logrotate ``dateext`` (``-YYYYMMDD``, ``-YYYYMMDDHH``, ``-YYYYMMDD-<epoch>``,
        ``-YYYY-MM-DD``, ``-YYYYMMDDHHMMSS``, ``-<epoch>``), TimedRotatingFileHandler
        (``.YYYY-MM-DD``, ``.YYYY-MM-DD_HH-MM-SS``), newsyslog ``-t`` (``.YYYYMMDDTHHMMSS``),
        and the hand-rolled ``.YYYYMMDD`` and ``.<epoch>`` -- the stamp orders them;
      - anything else sharing the stem -- mtime orders them, and the log says so.
    When ``<x>`` and ``<x>.<ext>`` both exist in one directory (compression in progress) the
    uncompressed one is kept. Compression ALWAYS creates a new inode (measured), so an inode
    alone never finds a compressed copy.
  * A copy of our file has our first line and is at least as long as our position in it;
    read through the decompressor, that is what the CONTENT check asks. The archives of this
    log written AFTER the last run are ours or newer, and the oldest of them that fits is ours:
    that is tried first. Only then is the saved INODE looked for, among ALL regular files in
    those directories whatever their name (a plain rename, the ``delaycompress`` window,
    ``olddir``) -- with the saved first line, because a same-inode file with a different
    first line, or with none, is a different file (the cursor's own rule). The inode comes
    second because ext4 hands a freed number straight back (measured): after a rotation
    that compressed our file, the NEW live file got our inode, and the next rotation left it
    under ``.1`` with the same banner first line -- a fit by inode and first line that was
    not ours. With no saved first line (the file was empty at the last run) only a file
    NAMED as an archive of this log qualifies by inode: the number alone would accept
    another log's archive. Last, the older archives are tried newest first, and a WARNING
    says the choice was a guess when an older one shares the first line. A truncation
    (``copytruncate``) never looks for the inode: it is the live file's.
  * The CHAIN: after the matched archive's tail, every archive NEWER than it, oldest first and in
    full, then the live file from 0. "Newer" is by the style key when every file involved shares
    a style and by mtime otherwise (announced). A file of unrecognised style joins the chain only
    when the match itself is an unrecognised archive OF THIS LOG: a ``router.log.bak`` with a
    fresh mtime must not be mailed as log lines.
  * An archive that fails part-way through (a ``.gz`` still being written, a bogus file with an
    archive's name) yields what it had, a WARNING, and the chain continues. Nothing matching is
    one WARNING naming the file, the saved identity, the directories searched and the likely
    causes; the live file is then read from 0 and the exit code is untouched.
  * The live file may be ABSENT after a rotation (``nocreate``). With a cursor on record the search
    runs anyway; the cursor then describes the last archive read, so the next run -- when the live
    file is back under a new inode -- resolves that archive by inode or first line and reads
    nothing from it twice. A consumer that stops reading part-way through the chain gets a cursor
    on the archive it stopped in, for the same reason: the next run resumes there.
  * A ROTATION DURING THE RUN (issue #32) is the same rule made literal. A chain member found
    renamed at its open (a different inode under the name the plan saw, or nothing under it:
    compressed away, removed) STOPS the stream -- no later archive, not the live file -- and
    the cursor is the last archive read (at its end, or where its read failed; the last
    one with a first line to find it by), so the next run plans again from there and reads
    the member under its new name; the old answer, skip it and go on to the live file,
    lost its content for good (measured: the lines of a third rotation inside one interval
    were never mailed). The matched archive itself cannot be renamed under the read: its
    verified handle travels (issue #46). One stage earlier, when nothing matched, the plan
    lists the directories again and searches again if the listing changed or a candidate
    was renamed or gone at its open -- once (a rotation inside the listing itself leaves
    every name with its new inode and no other signal); a second rotation inside one run
    reaches the no-copy warning, which names that cause. The stop needs the archive it
    parks on to survive until the next run: retention one rotation deeper than the
    interval needs.
"""

import logging
import os
import re
import stat
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Literal

from logalert.cursor import (
    STREAM_ERRORS,
    BinaryStream,
    Line,
    LineReader,
    LogFile,
    Verdict,
    compressed_suffix,
    count_newlines,
    fingerprint,
    lines_before,
    lines_from_window,
    open_log,
    window_for,
)
from logalert.state import Cursor, parse_timestamp, timestamp

log = logging.getLogger("logalert.rotation")
_READ_ERRORS: tuple[type[Exception], ...] = (OSError, *STREAM_ERRORS)  # what a bad archive raises
_SLACK = 2.0  # seconds: last_seen is whole seconds; mtimes are not
TAIL = 256 * 1024  # bytes before the saved offset kept from the confirming seek (issue #46):
#                    the context hook's window for up to 98 lines of the cap (window_for(98)
#                    = 261 634 <= TAIL - 1), far past any real `context`; a wider one falls
#                    back to a second open

Style = Literal["numeric", "dated", "other"]
DatedKey = tuple[int, int, int, int, int, int, int]  # Y, M, D, HH, MM, SS, epoch

_EXT = re.compile(r"\.(gz|bz2|xz|zst)$", re.I)
_NUMERIC = re.compile(r"^\.(\d{1,4})$")
_EPOCH = re.compile(r"^[.-](\d{9,10})$")
_DATED: tuple[tuple[re.Pattern[str], tuple[int, ...]], ...] = (
    # each pattern names the DatedKey positions its groups fill
    (re.compile(r"^-(\d{4})(\d{2})(\d{2})(\d{2})?(?:-(\d{9,10}))?$"), (0, 1, 2, 3, 6)),
    (re.compile(r"^-(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$"), (0, 1, 2, 3, 4, 5)),
    (re.compile(r"^-(\d{4})-(\d{2})-(\d{2})$"), (0, 1, 2)),
    (re.compile(r"^\.(\d{4})-(\d{2})-(\d{2})(?:_(\d{2})-(\d{2})-(\d{2}))?$"), (0, 1, 2, 3, 4, 5)),
    (re.compile(r"^\.(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})$"), (0, 1, 2, 3, 4, 5)),
    (re.compile(r"^\.(\d{4})(\d{2})(\d{2})$"), (0, 1, 2)),
)


@dataclass(frozen=True)
class Archive:
    """One regular file that may hold this log's rotated content."""

    path: str
    of_log: bool  # named as an archive of this log (any regular file is an inode candidate)
    style: Style
    key: tuple[int, ...]  # larger is newer within a style; () for "other"
    suffix: str  # the rotation suffix without the compression extension; "" when unnamed
    compressed: bool
    ino: int
    dev: int
    size: int
    mtime: float


def classify(name: str, base: str) -> tuple[Style, tuple[int, ...], str, bool] | None:
    """The rotation style of ``name`` as an archive of ``base``, or None when it is not one."""
    if not name.startswith(base) or name == base:
        return None
    rest = name[len(base):]
    compressed = False
    ext = _EXT.search(rest)
    if ext is not None:
        rest, compressed = rest[:ext.start()], True
    if rest and rest[0] not in ".-":
        return None  # router.log2 is another log, not a copy of router.log
    numeric = _NUMERIC.match(rest)
    if numeric is not None:
        return "numeric", (-int(numeric.group(1)),), rest, compressed
    for pattern, positions in _DATED:
        dated = pattern.match(rest)
        if dated is None:
            continue
        fields = [0] * 7
        for position, part in zip(positions, dated.groups(), strict=True):
            fields[position] = int(part) if part else 0
        if fields[6]:
            fields[3:6] = list(_epoch_key(fields[6])[3:6])  # the epoch says the time of day
        if _plausible_date(fields):
            return "dated", tuple(fields), rest, compressed
        break  # ten digits that are not a date: an epoch, tried next
    epoch = _EPOCH.match(rest)
    if epoch is not None:
        return "dated", _epoch_key(int(epoch.group(1))), rest, compressed
    return "other", (), rest, compressed


COPY_SUFFIXES: tuple[str, ...] = (".bak", ".old", ".orig", ".save", "-old", "-bak")


def archive_suffix(name: str) -> str | None:
    """The suffix that makes ``name`` the shape of a copy of SOME log, judged without knowing
    which, or None -- what a glob leaves out under ``include_archives = no`` (issue #18), so
    that ``router.log.1`` is never read as a file of its own, live file present or not:
    a numeric or dated rotation suffix per ``classify`` whose base does not end in a digit
    (``192.0.2.1`` is a host's file, not a copy of ``192.0.2``; ``2026-09-15`` is a daily
    file); a hand-made copy's suffix (``COPY_SUFFIXES``); a bare compression extension
    (``messages.gz`` beside, or instead of, ``messages``) -- each with an optional
    compression extension after it. The ``other`` style needs a base and is not a shape.
    The longest rotation suffix that is one: what ``classify`` would name with the log's
    own base."""
    for index in range(1, len(name)):
        if name[index] not in ".-" or name[index - 1].isdigit():
            continue
        found = classify(name, name[:index])
        if found is not None and found[0] != "other":
            return found[2]
    ext = _EXT.search(name)
    stem = name[:ext.start()] if ext else name
    for suffix in COPY_SUFFIXES:
        if stem.endswith(suffix) and len(stem) > len(suffix):
            return suffix
    if ext and stem:
        return ext.group(0)
    return None


def _plausible_date(fields: list[int]) -> bool:
    """Whether a dated key names a real moment: ``-2026091412`` is a date with an hour,
    ``-1789440820`` (logrotate ``dateformat -%s``) is an epoch that only looks like one."""
    year, month, day, hour, minute, second = fields[:6]
    try:
        datetime(year, month, day, hour, minute, second)
    except ValueError:
        return False
    return year >= 1970


def _epoch_key(epoch: int) -> DatedKey:
    moment = datetime.fromtimestamp(epoch, UTC)
    return (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second,
            epoch)


def newer(a: Archive, b: Archive) -> bool:
    """Whether ``a`` holds more recent content than ``b``."""
    if a.style == b.style and a.style != "other":
        return a.key > b.key
    return a.mtime > b.mtime


def _oldest_first(archives: list[Archive]) -> list[Archive]:
    """Oldest first: by the style key when every archive shares a recognised style, else by
    mtime for the whole list -- a comparison across styles is not transitive."""
    styles = {a.style for a in archives}
    if len(styles) == 1 and "other" not in styles:
        return sorted(archives, key=lambda a: a.key)
    return sorted(archives, key=lambda a: a.mtime)


def _rank(archive: Archive) -> int:
    """How much a name tells us, for choosing between hard links to one file."""
    if not archive.of_log:
        return 0
    return 1 if archive.style == "other" else 2


def scan_directories(directories: list[str], base: str) -> tuple[list[Archive], list[Archive]]:
    """Every regular file in the directories (for the inode stage) and the archives of ``base``
    among them (for the content stage and the chain), compression-in-progress pairs collapsed to
    the uncompressed member. Symbolic links are skipped; hard links to one file keep the
    better-named entry."""
    by_identity: dict[tuple[int, int], Archive] = {}
    for directory in directories:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            log.warning("cannot list %s while looking for rotated copies (%s)",
                        directory, exc.strerror)
            continue
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                # os.stat, not entry.stat(): on Windows the DirEntry version leaves st_ino and
                # st_dev at 0, and the inode stage would then match nothing
                st = os.stat(entry.path)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            found = classify(entry.name, base)
            style, key, suffix, compressed = found if found else (
                "other", (), "", compressed_suffix(entry.name) is not None)
            archive = Archive(
                path=entry.path, of_log=found is not None, style=style, key=key, suffix=suffix,
                compressed=compressed, ino=st.st_ino, dev=st.st_dev, size=st.st_size,
                mtime=st.st_mtime,
            )
            identity = (st.st_ino, st.st_dev)
            if identity not in by_identity or _rank(archive) > _rank(by_identity[identity]):
                by_identity[identity] = archive
    everything = list(by_identity.values())
    twins: dict[tuple[str, str], list[Archive]] = {}
    for archive in everything:
        if archive.of_log:
            twins.setdefault((os.path.dirname(archive.path), archive.suffix), []).append(archive)
    archives: list[Archive] = []
    for pair in twins.values():
        plain = [a for a in pair if not a.compressed]
        if len(plain) == 1 and len(pair) > 1:
            archives += plain  # <x> beside <x>.<ext>: the compression is in progress
        else:
            archives += pair
    return everything, archives


class Renamed:
    """What ``_content_matches`` answers for a candidate that is not the file the scan saw."""


RENAMED = Renamed()


def _content_matches(section: str, path: str, archive: Archive, saved: Cursor, *,
                     by_content: bool) -> "Verified | Renamed | Literal[False] | None":
    """The archive's open handle at the saved offset when its first line and length fit the
    saved cursor (``Verified``); False when it does not fit; None if unreadable.

    ``by_content`` (stage 2) demands the first lines be EQUAL: a file with no complete first
    line identifies nothing. The inode stage only asks that the archive not contradict the
    saved line -- and a file that HAD a first line cannot have lost it, so None contradicts.
    """
    try:
        handle = open_log(archive.path)
    except FileNotFoundError:  # gone from the name the scan saw (issue #32)
        log.info("[%s] %s: %s was renamed or removed between the scan and its open (a "
                 "rotation during the run)", section, path, archive.path)
        return RENAMED
    except _READ_ERRORS as exc:
        log.warning("[%s] %s: rotated copy %s could not be read (%s); skipped",
                    section, path, archive.path, exc)
        return None
    try:
        st = os.fstat(handle.fileno())
        if (st.st_ino, st.st_dev) != (archive.ino, archive.dev):
            log.info("[%s] %s: %s was renamed or removed between the scan and its open (a "
                     "rotation during the run)", section, path, archive.path)
            handle.close()
            return RENAMED
        current = fingerprint(handle)
        if by_content and current != saved.fingerprint:
            handle.close()
            return False
        if saved.fingerprint is not None and current != saved.fingerprint:
            handle.close()
            return False
        if archive.compressed:
            # the confirming seek, by hand: a seek past the end is silent, so the bytes
            # are read instead -- keeping the tail for the context hook and counting the
            # lines, the two later passes issue #46 measured
            reached, tail, newlines = _forward(handle, saved.offset, TAIL)
            if not reached:
                handle.close()
                return False
            return Verified(handle, current, tail, newlines)
        if archive.size < saved.offset:
            handle.close()
            return False
        return Verified(handle, current, _tail_of(handle, saved.offset, TAIL), None)
    except _READ_ERRORS as exc:
        handle.close()
        log.warning("[%s] %s: rotated copy %s could not be read (%s); skipped",
                    section, path, archive.path, exc)
        return None
    except BaseException:
        handle.close()
        raise


def _forward(handle: BinaryStream, offset: int, keep: int) -> tuple[bool, bytes, int]:
    """Read a compressed stream forward from 0 to ``offset``: whether the stream reaches it,
    the last ``keep`` bytes before it, and the newlines before it. One pass, the position
    left at the offset (or at the end of a shorter stream)."""
    handle.seek(0)
    tail = b""
    newlines = 0
    position = 0
    while position < offset:
        chunk = handle.read1(min(65536, offset - position))
        if not chunk:
            return False, b"", newlines
        newlines += chunk.count(b"\n")
        position += len(chunk)
        tail = (tail + chunk)[-keep:] if keep else b""
    return True, tail, newlines


def _tail_of(handle: BinaryStream, offset: int, keep: int) -> bytes:
    """The last ``keep`` bytes before ``offset`` of a plain file (a seek is free there), the
    position left at the offset."""
    start = max(0, offset - keep)
    handle.seek(start)
    parts: list[bytes] = []
    remaining = offset - start
    while remaining > 0:
        chunk = handle.read1(remaining)
        if not chunk:
            break
        parts.append(chunk)
        remaining -= len(chunk)
    handle.seek(offset)
    return b"".join(parts)


@dataclass
class Plan:
    """The archives to read before the live file, oldest first, and how the search went."""

    match: Archive | None = None
    chain: list[Archive] = field(default_factory=list)
    stage: str = ""  # "inode" | "content" | "" (nothing matched)
    searched: list[str] = field(default_factory=list)
    verified: "Verified | None" = None  # the match's open handle, positioned at the offset


@dataclass
class Verified:
    """What the content stage learned about the archive that fits, kept for the read (issue
    #46): the open handle positioned at the saved offset (a descriptor cannot be renamed
    under us, so no second open and no second identity check), its first-line hash, the
    last ``TAIL`` bytes before the offset (the context hook's window, read on the way),
    and -- for a compressed stream -- the complete lines before the offset, counted on the
    way too (a cursor from before #9 has no count)."""

    handle: BinaryStream = field(repr=False)
    fingerprint: str | None
    tail: bytes = field(repr=False)  # 256 KiB: not for a failing assertion's output
    lines_before: int | None

    def close(self) -> None:
        self.handle.close()


Fits = Callable[[Archive], bool]


def plan_catch_up(section: str, path: str, saved: Cursor, verdict: Verdict | None, *,
                  archive_dir: str | None, exclude: tuple[int, int] | None) -> Plan:
    """Find the archive that holds the saved position and the chain of newer ones.

    ``verdict`` is the cursor's decision about the live file, or None when the live file is
    absent. ``exclude`` is the live file's current (ino, dev), never a candidate.
    """
    real = os.path.realpath(path)
    if real != os.path.abspath(path) and saved.realpath and saved.realpath != real:
        log.info("[%s] %s: the link now points at %s (was %s)", section, path, real, saved.realpath)
    directories = [os.path.dirname(real)]
    for extra in (os.path.dirname(saved.realpath) if saved.realpath else None, archive_dir):
        if extra and os.path.realpath(extra) not in (os.path.realpath(d) for d in directories):
            directories.append(extra)
    plan = Plan(searched=directories)
    unreadable: set[str] = set()
    renamed: set[str] = set()
    kept: dict[str, Verified] = {}
    everything: list[Archive] = []
    archives: list[Archive] = []

    def fits(archive: Archive, *, by_content: bool) -> bool:
        answer = _content_matches(section, path, archive, saved, by_content=by_content)
        if answer is None:
            unreadable.add(archive.path)
            return False
        if isinstance(answer, Renamed):
            renamed.add(archive.path)
            return False
        if answer is False:
            return False
        kept[archive.path] = answer
        return True

    def scan() -> set[tuple[str, int, int]]:
        """One listing of the directories; what it saw, by name and identity."""
        nonlocal everything, archives
        everything, archives = scan_directories(directories, os.path.basename(real))
        if exclude is not None:
            everything = [a for a in everything if (a.ino, a.dev) != exclude]
            archives = [a for a in archives if (a.ino, a.dev) != exclude]
        return {(a.path, a.ino, a.dev) for a in everything}

    def stages() -> None:
        """The three stages over the last listing; sets ``plan.match``."""
        since = parse_timestamp(saved.last_seen).timestamp() - _SLACK
        recent = _oldest_first([a for a in archives if a.mtime >= since])
        older = list(reversed(_oldest_first([a for a in archives if a.mtime < since])))
        if saved.fingerprint is not None:
            plan.match = next((a for a in recent if fits(a, by_content=True)), None)
        if plan.match is None and verdict != "truncated":  # a truncated file kept its inode
            for archive in everything:
                if (archive.ino, archive.dev) != (saved.ino, saved.dev):
                    continue
                if saved.fingerprint is None and not archive.of_log:
                    continue  # an inode number alone would accept another log's archive
                if fits(archive, by_content=False):
                    plan.match = archive
                    break
        if plan.match is None and saved.fingerprint is not None:
            plan.match = _newest_older_fit(section, path, older, saved,
                                           lambda a: fits(a, by_content=True))

    try:
        listing = scan()
        stages()
        if plan.match is None:
            # a rotation may have landed between the listing and a candidate's open, or
            # inside the listing itself (issue #32): the names moved on; look again, once
            moved = bool(renamed)
            unreadable.clear()
            renamed.clear()
            if scan() != listing or moved:
                log.info("[%s] %s: the rotated copies moved while the plan was made (a "
                         "rotation during the run); scanning again", section, path)
                stages()
        if plan.match is None:
            for leftover in kept.values():
                leftover.close()
            _nothing_matched(section, path, saved, verdict, directories, renamed=bool(renamed))
            return plan
        plan.stage = "inode" if (plan.match.ino, plan.match.dev) == (saved.ino, saved.dev) \
            else "content"
        match = plan.match
        later = [a for a in archives
                 if a.path != match.path and a.path not in unreadable and newer(a, match)
                 and (a.style != "other" or (match.style == "other" and match.of_log))]
        plan.chain = _oldest_first(later)
        styles = {a.style for a in [match, *plan.chain]}
        if plan.chain and (len(styles) > 1 or "other" in styles):
            log.info("[%s] %s: the archive names mix styles or are not a recognised rotation "
                     "style; ordering by mtime", section, path)
        steps = [f"{os.path.basename(match.path)} from {saved.offset}"]
        steps += [os.path.basename(a.path) for a in plan.chain]
        log.info("[%s] %s: found the saved position by %s; reading %s%s", section, path, plan.stage,
                 ", then ".join(steps), ", then the live file" if verdict is not None else "")
        plan.verified = kept.pop(plan.match.path, None)  # last: kept until here, see except
        for leftover in kept.values():  # a fit that was not chosen (none today)
            leftover.close()
        return plan
    except BaseException:  # a KeyboardInterrupt between a fit and the return (review)
        for leftover in kept.values():
            leftover.close()
        raise


def _newest_older_fit(section: str, path: str, older: list[Archive], saved: Cursor,
                      fits: Fits) -> Archive | None:
    """The last resort: among the archives written BEFORE the last run, newest first, the
    first that fits -- a guess when an older one shares the first line, and said so. Every
    archive tried costs a fingerprint read; a fitting one costs a seek to the saved offset.
    """
    for index, archive in enumerate(older):
        if fits(archive):
            rivals = [a for a in older[index + 1:] if _same_first_line(a, saved)]
            if rivals:
                log.warning("[%s] %s: %d older rotated copies share the saved first line; "
                            "taking the newest that is long enough, %s, which is a guess",
                            section, path, len(rivals) + 1, os.path.basename(archive.path))
            return archive
    return None


def _same_first_line(archive: Archive, saved: Cursor) -> bool:
    """The cheap half of a content match: no seek to the offset."""
    try:
        with open_log(archive.path) as handle:
            return fingerprint(handle) == saved.fingerprint
    except _READ_ERRORS:
        return False


def _nothing_matched(section: str, path: str, saved: Cursor, verdict: Verdict | None,
                     directories: list[str], *, renamed: bool = False) -> None:
    identity = (f"inode {saved.ino}, offset {saved.offset}, first-line hash "
                f"{(saved.fingerprint or 'none')[:12]}")
    causes = ["rotate 0", "an olddir or -a elsewhere (set archive_dir)",
              "unsupported compression", "the archive aged out"]
    if renamed:
        causes.insert(0, "a second rotation during this run (a copy was renamed twice)")
    if saved.fingerprint is None:
        causes.append("the file had no complete first line at the last run")
    where = ", ".join(directories)
    if verdict is None:
        log.info("[%s] %s: absent this run, and no rotated copy holds the saved position (%s) "
                 "in %s", section, path, identity, where)
        return
    log.warning("[%s] %s: no rotated copy holds the saved position (%s) in %s -- likely "
                "causes: %s; reading the live file from the beginning, and lines written "
                "between the last run and the last rotation are lost",
                section, path, identity, where, ", ".join(causes))


class Segment:
    """One archive read from ``start``; ``offset`` is where the read ended."""

    def __init__(self, section: str, path: str, start: int,
                 expected: tuple[int, int] | None, start_line: int | None = 0,
                 verified: Verified | None = None) -> None:
        self.section = section
        self.verified = verified  # the content stage's handle, for the match (issue #46)
        self.path = path
        self.start = start
        self.expected = expected  # the (ino, dev) the plan saw; a rename since stops the stream
        self.start_line = start_line  # None: not counted yet (a cursor from before #9)
        self.line = start_line or 0
        self.offset = start
        self.ino = 0
        self.dev = 0
        self.fingerprint: str | None = None
        self.started = False
        self.finished = False
        self.yielded = False  # at least one line came out of it
        self.renamed = False  # renamed or gone at its open: the stream stops here (issue #32)
        self.handle: BinaryStream | None = verified.handle if verified else None

    def lines(self) -> Iterator[Line]:
        self.started = True
        try:
            try:
                opened = self._open()
            except FileNotFoundError:
                # gone from the name the plan saw (compressed into another name, or
                # removed): the names moved on, as for a rename
                self.renamed = True
                self.finished = True
                return
            with opened as handle:
                self.handle = handle
                st = os.fstat(handle.fileno())
                if (self.verified is None and self.expected is not None
                        and (st.st_ino, st.st_dev) != self.expected):
                    self.renamed = True  # CatchUpSource stops the stream and says so
                    self.finished = True
                    return
                self.ino, self.dev = st.st_ino, st.st_dev
                self.fingerprint = (self.verified.fingerprint if self.verified
                                    else fingerprint(handle))
                if self.start_line is None:
                    counted = self.verified.lines_before if self.verified else None
                    self.start_line = (counted if counted is not None
                                       else count_newlines(handle, self.start))
                    self.line = self.start_line
                reader = LineReader(handle, self.start, start_line=self.start_line,
                                    path=self.path)
                for line in reader:
                    self.offset, self.line = reader.offset, reader.line
                    self.yielded = True
                    yield line
                self.offset, self.line = reader.offset, reader.line
                if reader.nul_bytes:
                    log.info("[%s] %s: skipped %d NUL bytes", self.section, self.path,
                             reader.nul_bytes)
                self.finished = True
        except _READ_ERRORS as exc:
            self.finished = True  # nothing more will come of it this run
            log.warning("[%s] %s: rotated copy could not be read past offset %d (%s); "
                        "continuing with the next file", self.section, self.path,
                        self.offset, exc)
        finally:
            self.handle = None

    def _open(self) -> BinaryStream:
        """The content stage's handle when it verified this archive, else a fresh open."""
        if self.verified is not None:
            return self.verified.handle
        return open_log(self.path)

    def cursor(self, now: datetime | None = None) -> Cursor:
        return Cursor(offset=self.offset, ino=self.ino, dev=self.dev, fingerprint=self.fingerprint,
                      realpath=os.path.realpath(self.path), last_seen=timestamp(now),
                      line=self.line)

    def context_before(self, n: int) -> list[Line]:
        """The lines before ``start`` in this archive, numbered; see ``LogFile``. Answered
        from the tail the content stage kept when it covers the window (issue #46), else
        from a second open."""
        if n <= 0 or self.start == 0:
            return []
        if self.verified is not None:
            tail = self.verified.tail
            window = window_for(n)
            if len(tail) >= self.start or len(tail) - 1 >= window:
                start = max(0, self.start - window)
                buf = tail[len(tail) - (self.start - max(0, start - 1)):]
                return lines_from_window(buf, start, n, self.start_line or 0, self.path)
            log.debug("[%s] %s: the context window (%d lines) is wider than the kept tail; "
                      "a second read", self.section, self.path, n)
        try:
            with open_log(self.path) as handle:
                st = os.fstat(handle.fileno())
                if self.expected is not None and (st.st_ino, st.st_dev) != self.expected:
                    return []  # renamed under us: not this archive any more
                # lines() ran before any hook can be asked (CatchUpSource's yielded gate),
                # so the count is known
                return lines_before(handle, self.start, n, self.start_line or 0, self.path)
        except _READ_ERRORS as exc:
            log.warning("[%s] %s: context before the saved position could not be read (%s)",
                        self.section, self.path, exc)
            return []


class CatchUpSource:
    """A rotated (or absent) live file with its rotated copies read first.

    The same surface as ``LogFile``: ``lines()``, ``cursor()``, ``close()``, a context manager.
    ``cursor()`` after ``lines()`` is exhausted is the live file's; after a consumer stopped
    part-way it is the archive being read at that point, at the last complete line -- the next
    run resolves that archive by inode or first line and continues the chain from there. Before
    anything was read it is the saved cursor, unchanged.
    """

    def __init__(self, section: str, path: str, plan: Plan, live: LogFile | None,
                 saved: Cursor) -> None:
        self.section = section
        self.path = path
        self.plan = plan
        self.live = live
        self.saved = saved
        self.verdict: Verdict | Literal["absent"] = live.verdict if live else "absent"
        self.stopped = False  # a chain member renamed under us: nothing past it this run
        self.realpath = os.path.realpath(path)  # what a parked cursor records (issue #32)
        self.segments: list[Segment] = []
        if plan.match is not None:
            self.segments.append(Segment(section, plan.match.path, saved.offset,
                                         (plan.match.ino, plan.match.dev), saved.line,
                                         verified=plan.verified))
            self.segments += [Segment(section, a.path, 0, (a.ino, a.dev)) for a in plan.chain]

    def context_before(self, n: int) -> list[Line]:
        """The lines before the stream's first line: those before the saved offset in the
        matched archive, when that is where the stream started. Context never crosses
        files, so a chain member or the live file after a rotation (both read from 0) has
        none -- and an archive tail that yielded nothing was not the stream's first file."""
        if self.segments:
            first = self.segments[0]
            return first.context_before(n) if first.yielded else []
        if self.live is not None:
            return self.live.context_before(n)
        return []

    def lines(self) -> Iterator[Line]:
        for segment in self.segments:
            yield from segment.lines()
            if segment.renamed:
                self.stopped = True
                last = self._last_read()
                resume = os.path.basename(last.path) if last else "the saved position"
                log.warning("[%s] %s: %s was renamed or removed under us (a rotation during "
                            "the run); stopping here, the next run resumes after %s (the "
                            "names are from before the rotation)", self.section, self.path,
                            os.path.basename(segment.path), resume)
                return
        if self.live is not None:
            yield from self.live.lines()

    def start_cursor(self, now: datetime | None = None) -> Cursor:
        """The saved cursor, seen now: what a run over the copies would read again from
        (issue #31; the same surface as ``LogFile``, never consumed here -- a catch-up has
        an entry -- and a consumer must not move ``last_seen`` backwards)."""
        return replace(self.saved, last_seen=timestamp(now))

    def cursor(self, now: datetime | None = None) -> Cursor:
        unfinished = [s for s in self.segments if s.started and not s.finished]
        if unfinished:
            return self._parked(unfinished[0], now)  # stopped inside an archive: resume there
        if self.segments and not self.segments[0].started:
            return self.saved  # nothing read yet
        if self.stopped or self.live is None:  # the next run plans from the last archive read
            last = self._last_read()
            return self._parked(last, now) if last else self.saved
        return self.live.cursor(now)

    def _last_read(self) -> Segment | None:
        """The last archive read that the next run can find by its first line: an empty copy
        has none, so the cursor parks before it and it is read again, with nothing to
        repeat."""
        read = [s for s in self.segments if s.ino and s.fingerprint is not None]
        return read[-1] if read else None

    def _parked(self, segment: Segment, now: datetime | None) -> Cursor:
        """The segment's cursor with the log's own real path: it names the directory the
        next plan searches beside the log's and is what a listed link is compared against;
        the archive's would make a link that never moved look repointed."""
        return replace(segment.cursor(now), realpath=self.realpath)

    def close(self) -> None:
        for segment in self.segments:
            if segment.handle is not None:
                segment.handle.close()
        if self.live is not None:
            self.live.close()

    def __enter__(self) -> "CatchUpSource":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


Source = LogFile | CatchUpSource


def open_source(section: str, path: str, saved: Cursor | None, *,
                archive_dir: str | None = None, from_start: bool = False) -> Source | None:
    """What the run loop reads for one (section, file): the live file, with its rotated copies
    first when the cursor says the file rotated or was truncated; None when there is nothing
    to read this run (the file is absent and no copy holds the saved position). The run loop
    calls this, never ``LogFile`` directly, so a rotation is never read from 0 alone."""
    try:
        live: LogFile | None = LogFile(section, path, saved, from_start=from_start)
    except FileNotFoundError:
        live = None
    if live is not None and live.verdict in ("continue", "first-sight"):
        return live
    if saved is None:
        log.debug("[%s] %s: absent this run", section, path)
        return None
    exclude = (live.ino, live.dev) if live is not None else None
    verdict = live.verdict if live is not None else None
    try:
        plan = plan_catch_up(section, path, saved, verdict, archive_dir=archive_dir,
                             exclude=exclude)
    except BaseException:
        if live is not None:
            live.close()
        raise
    if live is None and plan.match is None:
        return None
    try:
        return CatchUpSource(section, path, plan, live, saved)
    except BaseException:
        if plan.verified is not None:
            plan.verified.close()
        if live is not None:
            live.close()
        raise
