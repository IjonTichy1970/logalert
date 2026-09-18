"""Reading a log file from where a section left off: identity, truncation, the line reader.

The file a cursor points at may have been rotated, truncated, rewritten in place or replaced
by the time the next run opens it. This module opens the file, ``fstat``s the descriptor it
will actually read, and decides how the saved cursor applies:

  * same inode, first line unchanged, size >= offset -> CONTINUE from the offset
  * same inode, first line changed -> ROTATED: ext4 hands a freed inode number straight back,
    so a same-inode file with different content is a different file (measured: after two
    ``delaycompress`` rotations the saved live inode came back as the new live file)
  * same inode, size < offset -> TRUNCATED (``copytruncate``); so is a file whose first line
    is GONE (a NUL hole from ``copytruncate`` under a writer without ``O_APPEND``), whatever
    its size
  * same inode, first line and size in order, but the bytes before the saved offset are not
    the ones the last run read -> TRUNCATED too (issue #34). A file truncated and refilled
    past the position with the same first line (a restart script's ``>`` with a fixed
    banner, a report rewritten whole) was read on from the stale offset: a fragment, and
    the refill's lines before it lost, silently (measured on both platforms). The cursor's
    ``anchor`` is the sha256 of the last ``ANCHOR_CAP`` bytes before the offset AS READ --
    the reader keeps them as it goes, so a refill landing after the read's last check is
    not recorded as the file -- and a cursor without one (0.1.0's, a parked copy's from
    before #74) is trusted once. What it cannot see: a refill whose last ``ANCHOR_CAP``
    bytes before the position are the ones read (a file of nothing but identical lines,
    aligned). The plan
    behind the verdict is the rotation module's, which compares the anchor too: a copy
    with other bytes before the position is never taken (issue #74).
  * inode differs -> ROTATED
  * device id differs alone -> continue, with a log line (a remount or a reboot renumbers
    devices; that never declares a rotation by itself)
  * no cursor -> FIRST SIGHT: start at the current end of the file, on a line boundary, so
    the first run is not a flood of old news; ``start = beginning`` / ``--from-start`` read
    from 0. Nothing is emitted; the skipped byte count is logged, as a WARNING.

A listed COMPRESSED file whose inode, device, size and mtime are what the cursor recorded
is not opened at all (issue #44): nothing new can be in an archive that has not changed,
and the confirming seek to the saved offset was a full decompression on every run. The
cursor records the two only when its offset IS the end of the stream (a read that reached
EOF with no unterminated tail), so a cursor of a LISTED file that carries them never has
unread bytes behind it (a cursor parked on a rotated copy carries them as that copy's
identity, issue #65, and lives under the log's own key, which is never a compressed
path with that inode). Its first sight is one pass: the stream's end, the last line
boundary and the
line count come from a single forward read. A plain file changes size and mtime with
every append and is never short-circuited; nor is a symbolic link, which goes through the
owner rule below on every run.

On its own a ``LogFile`` reads a ROTATED or TRUNCATED live file from 0; ``logalert.rotation``
wraps it to read the rotated copy first, found by the inode and the fingerprint saved here.
Compressed files (``.gz``, ``.bz2``, ``.xz``; ``.zst`` where the stdlib has
``compression.zstd``) are read through the decompressor with the offset in the UNCOMPRESSED
stream; ``seek`` past its end is silent, so ``tell`` after the seek is what detects a shorter
stream.

Files are opened by DESCRIPTOR (issues #29 and #26): ``os.open`` with ``O_NONBLOCK`` and
``O_NOFOLLOW``, ``fstat`` on what was opened, and only a regular file is read -- the kind is
judged on the descriptor, never on the name and then opened, so a FIFO swapped in between
cannot block ``open(2)`` (measured: the non-blocking open of a FIFO with no writer returns
at once). The decompressors are built over that file object. A symbolic link at a LISTED
path (``follow_links=True``; an archive found by the catch-up is never followed) is
followed under one rule: its owner is root, the running user, or the owner of the file it
points to -- so root's ``/var/log/foo -> /data/foo`` and an application's own ``current ->
today.log`` work, and a link the owner of a log directory plants towards a file that is
not theirs is a failed item, not a mail. The link is judged through an ``O_PATH``
descriptor and its target read through the same descriptor (``readlink("", dir_fd=...)``),
so a link swapped between the check and the follow is the link that was checked (measured);
a target that is itself a link is refused. Windows has none of these flags: there a link
opens as before.

A ``copytruncate`` that lands UNDER the open handle (issue #66) is caught by the reader
itself: after every read -- each chunk, and the empty read at the end -- a plain live
file is asked whether it is still the file that was opened -- its size not below the
reader's position, its first line the one from the open -- and a chunk read from a
file that is not is dropped unread and the read ends (a truncation met as EOF, the
writer quiet, is reported the same way: review -- the live cursor at 0 it left matched
the truncation's copy again next run when the plan had chained it already).
The check runs after the read, so a chunk that passes was read before any truncation
the check can see, and a good chunk dropped by a truncation landing between its read
and the check costs nothing: its lines stay unread and come from the copy next run.
Every yielded line is therefore from the file as opened, and the cursor is a (first
line, offset) pair from before the truncation -- what the next run resolves against
the copy by content. Without it the cursor paired the old first line with an offset
into the new content: lines lost, then a fragment and a duplicate (measured with real
logrotate). The check compares the bytes before the position too, against the reader's
rolling tail (issue #34), so a refill with the same first line past the position is
caught within the chunk that read it; one whose bytes before the position are the ones
read is invisible to it, as to the next run. Archives never change and
a seek in a compressed stream is a decompression, so neither is asked. The check reads
where the first line BEGINS, recorded at the open: past a NUL hole (a ``copytruncate``
under a writer without ``O_APPEND``), which ``fingerprint`` would otherwise skip again
on every chunk (review, measured: a 48 MiB hole made an 8 MiB read take 49 s); the
cost is two small reads per 64 KiB (measured: 5 ms over a 64 MiB read).

Lines are read in binary and decoded as UTF-8 with replacement. The cursor advances only to
the end of the last COMPLETE line: an unterminated tail is re-read next run. A line longer
than ``LINE_CAP`` bytes is cut there and the cut is reported; the cap is a hard boundary, so
from a given start offset the boundaries are a function of the bytes alone. A run of NUL
bytes (what ``copytruncate`` leaves under a writer without ``O_APPEND``) is dropped, whether
it is a whole line or the start of one.
"""

import bz2
import errno
import gzip
import hashlib
import importlib
import io
import logging
import lzma
import os
import re
import stat
import sys
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from logalert.state import Cursor, timestamp

if sys.platform != "win32":
    import fcntl
    import pwd

log = logging.getLogger("logalert.cursor")

LINE_CAP = 2000  # bytes; a longer line is cut here and the cut reported
FINGERPRINT_CAP = 4096  # bytes of the first line that identify a file
ANCHOR_CAP = 4096  # bytes before the position that identify what was read (issue #34)
HOLE_CAP = 64 * 1024 * 1024  # NUL bytes fingerprint() will skip before giving up
_CHUNK = 65536
NUL = bytes([0])  # spelled from the code point: gated Python carries no control characters
_NUL_RUN = re.compile(NUL + b"*")


def _stream_errors() -> tuple[type[Exception], ...]:
    """What the decompressors raise for a half-written or corrupt archive, besides OSError.

    gzip: EOFError for a missing trailer, zlib.error for a bad deflate body (a crash during
    compression leaves a valid header over zero-filled blocks). bz2: OSError. lzma:
    LZMAError. zstd (3.14+): ZstdError, whose base is Exception, not OSError.
    """
    errors: list[type[Exception]] = [EOFError, zlib.error, lzma.LZMAError]
    try:
        zstd = importlib.import_module("compression.zstd")
    except ImportError:
        return tuple(errors)
    zstd_error = getattr(zstd, "ZstdError", None)
    if isinstance(zstd_error, type) and issubclass(zstd_error, Exception):
        errors.append(zstd_error)
    return tuple(errors)


STREAM_ERRORS: tuple[type[Exception], ...] = _stream_errors()
_HOOK_ERRORS: tuple[type[Exception], ...] = (OSError, *STREAM_ERRORS)

Verdict = Literal["first-sight", "continue", "rotated", "truncated"]
# what open(), gzip.open(), bz2.open() and lzma.open() have in common, per typeshed
BinaryStream = io.BufferedIOBase
COMPRESSED_SUFFIXES: tuple[str, ...] = (".gz", ".bz2", ".xz", ".zst")


def compressed_suffix(path: str) -> str | None:
    """The compression suffix logalert understands, or None for a plain file."""
    lower = path.lower()
    for suffix in COMPRESSED_SUFFIXES:
        if lower.endswith(suffix):
            return suffix
    return None


_OPEN_FLAGS = (os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
               | getattr(os, "O_BINARY", 0))
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_LINK_RULE = ("a listed link is followed when its owner is root, the running user or the "
              "file's owner")


def open_log(path: str, *, follow_links: bool = False) -> BinaryStream:
    """Open for binary reading, through the decompressor the suffix names.

    Only a regular file, judged on the descriptor (see the module docstring): a FIFO with
    no writer would block ``open(2)`` for good, and a device or socket has no byte offsets
    to remember. A symbolic link is refused unless ``follow_links`` (a listed path) and the
    link passes the owner rule. ``FileNotFoundError`` propagates.
    """
    if sys.platform == "win32":
        # no O_NONBLOCK and nothing that blocks an open: the kind is judged by name, where
        # os.open of a directory is EACCES rather than a descriptor to fstat
        kind = special_kind(os.stat(path).st_mode)
        if kind is not None:
            raise OSError(f"{path}: not a regular file (a {kind})")
    try:
        fd = os.open(path, _OPEN_FLAGS | _NOFOLLOW)
    except OSError as exc:
        if exc.errno != errno.ELOOP or not _NOFOLLOW:
            raise
        if not follow_links:
            raise OSError(f"{path}: is a symbolic link; an archive is never followed") from None
        fd = _open_through_link(path)
    try:
        kind = special_kind(os.fstat(fd).st_mode)
        if kind is not None:
            raise OSError(f"{path}: not a regular file (a {kind})")
        _blocking(fd)
        raw = open(fd, "rb")  # inside the guard: a failure here would leak the descriptor
    except BaseException:
        os.close(fd)
        raise
    return _wrap(path, raw)


def _open_through_link(path: str) -> int:
    """A descriptor on the file a listed link points to, when the link passes the owner
    rule; the link itself is judged through an ``O_PATH`` descriptor so the content read is
    that link's (a swap between the check and the follow cannot substitute another). Where
    ``O_PATH`` is absent (a BSD) the link is read by name: the same rule, a microsecond
    window between the judgement and the follow."""
    if sys.platform == "win32":  # never reached: no O_NOFOLLOW, so no ELOOP; mypy's branch
        raise OSError(errno.ELOOP, "a symbolic link", path)
    else:
        o_path = getattr(os, "O_PATH", None)
        if o_path is not None:
            link_fd = os.open(path, o_path | _NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
            try:
                link = os.fstat(link_fd)
                target = os.readlink("", dir_fd=link_fd)
            finally:
                os.close(link_fd)
        else:
            link = os.lstat(path)
            target = os.readlink(path)
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(path), target)
        try:
            fd = os.open(target, _OPEN_FLAGS | _NOFOLLOW)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                # the target's spelling is the link planter's text: not quoted in a message
                raise OSError(f"{path}: points at another symbolic link; name the file "
                              f"itself") from None
            raise
        try:
            st = os.fstat(fd)
            if link.st_uid not in (0, os.geteuid()) and link.st_uid != st.st_uid:
                raise OSError(f"{path}: is a symbolic link owned by {_user(link.st_uid)} to "
                              f"a file owned by {_user(st.st_uid)}; not followed "
                              f"({_LINK_RULE})")
        except BaseException:
            os.close(fd)
            raise
        return fd


def _user(uid: int) -> str:
    if sys.platform == "win32":
        return f"#{uid}"
    else:  # mypy narrows the platform per branch, not past an early return
        try:
            return pwd.getpwuid(uid).pw_name
        except KeyError:
            return f"#{uid}"


def _blocking(fd: int) -> None:
    """``O_NONBLOCK`` served the open; the reads are ordinary."""
    if sys.platform != "win32":
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)


def _wrap(path: str, raw: io.BufferedReader) -> BinaryStream:
    """The decompressor the suffix names, over the checked file object, its ``close`` closing
    both (measured: none of the stdlib decompressors closes a file object it was given)."""
    suffix = compressed_suffix(path)
    stream: BinaryStream
    if suffix == ".gz":
        stream = gzip.GzipFile(fileobj=raw, mode="rb")
    elif suffix == ".bz2":
        stream = bz2.BZ2File(raw, "rb")
    elif suffix == ".xz":
        stream = lzma.LZMAFile(raw, "rb")
    elif suffix == ".zst":
        try:
            zstd = importlib.import_module("compression.zstd")  # 3.14+
        except ImportError:
            raw.close()
            raise OSError(f"{path}: reading .zst needs Python 3.14+ built with zstd support "
                          f"(compression.zstd)") from None
        stream = zstd.open(raw, "rb")
    else:
        return raw
    inner_close = stream.close

    def close() -> None:
        try:
            inner_close()
        finally:
            raw.close()

    setattr(stream, "close", close)  # noqa: B010  # an instance attribute shadows the method
    return stream


def special_kind(mode: int) -> str | None:
    """What a non-regular file is, for a message, or None for a regular file; what a glob
    passes over (``logalert.globs``) and what ``open_log`` refuses."""
    if stat.S_ISREG(mode):
        return None
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISFIFO(mode):
        return "FIFO"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    if stat.S_ISSOCK(mode):
        return "socket"
    return "special file"


def fingerprint(handle: BinaryStream) -> str | None:
    """sha256 of the first complete line (at most FINGERPRINT_CAP bytes), read from 0; see
    ``first_line``, which also says where that line begins."""
    return first_line(handle)[0]


def first_line(handle: BinaryStream) -> tuple[str | None, int]:
    """The first line's hash (sha256 of the first complete line, at most FINGERPRINT_CAP
    bytes) and the offset where that line begins, read from 0.

    A run of NULs at the start (a copytruncate hole) is not the first line and is skipped,
    as the reader skips it -- however long it is, up to HOLE_CAP: the hole is the old file's
    size, and a file whose fingerprint stayed None across rotations could never be matched
    to its copies again. None when the file has no complete first line yet -- and a None is
    never compared. Leaves the position at 0.
    """
    handle.seek(0)
    skipped = 0
    while True:
        head = _read_up_to(handle, FINGERPRINT_CAP)
        text = head.lstrip(NUL)
        if text or len(head) < FINGERPRINT_CAP:
            break
        skipped += len(head)
        if skipped >= HOLE_CAP:
            handle.seek(0)
            return None, skipped
    begins = skipped + len(head) - len(text)
    if text and len(text) < FINGERPRINT_CAP:
        text += _read_up_to(handle, FINGERPRINT_CAP - len(text))  # a cap of the line itself
    handle.seek(0)
    return _hash_of(text), begins


def _hash_of(text: bytes) -> str | None:
    """``first_line``'s rule over the bytes at the line's start: the line up to its
    newline, the whole cap when the line is longer than it, None when it is incomplete."""
    newline = text.find(b"\n")
    if newline >= 0:
        line = text[:newline]
    elif len(text) >= FINGERPRINT_CAP:
        line = text  # the first line is longer than the cap: the cap identifies it
    else:
        return None
    return hashlib.sha256(line).hexdigest()


def _read_up_to(handle: BinaryStream, size: int) -> bytes:
    """Up to ``size`` bytes, keeping what a cut archive yields before it fails.

    ``read()`` on a decompressor raises before handing over the bytes it did decompress;
    ``read1()`` hands them over first (measured for gz, bz2, xz), and a stream error on a
    later call ends the read with what came through. The caller sees the error when it
    reads the file itself.
    """
    parts: list[bytes] = []
    got = 0
    while got < size:
        try:
            chunk = handle.read1(size - got)
        except STREAM_ERRORS:
            break
        if not chunk:
            break
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def tail_of(handle: BinaryStream, offset: int, keep: int) -> bytes:
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


def anchor_of(tail: bytes) -> str | None:
    """The cursor's ``anchor``: sha256 of the bytes before the position (at most
    ``ANCHOR_CAP``, the last of them); None when there are none."""
    return hashlib.sha256(tail).hexdigest() if tail else None


def identify(saved: Cursor | None, ino: int, dev: int, size: int | None,
             current: str | None, anchor: str | None = None) -> tuple[Verdict, str | None]:
    """Apply the identity rules; returns the verdict and a note worth logging, if any.

    ``size`` is None for a compressed file, whose truncation is detected by seeking instead;
    ``anchor`` is what the bytes before the saved offset hash to now (``anchor_of``), None
    when they were not read (a compressed file, an offset of 0) -- and a None on either
    side is never compared, like a None fingerprint.
    """
    if saved is None:
        return "first-sight", None
    if ino != saved.ino:
        return "rotated", f"inode changed ({saved.ino} -> {ino})"
    if saved.fingerprint is not None and current is not None and current != saved.fingerprint:
        return "rotated", "same inode, different first line (inode reuse or a rewrite)"
    if saved.fingerprint is not None and current is None:
        # A file cannot lose its first line by being appended to. copytruncate under a writer
        # without O_APPEND leaves a NUL hole where it was -- and a size that passes the check
        # below, which is why this comes first.
        return "truncated", "the first line is gone (a copytruncate hole?)"
    if size is not None and size < saved.offset:
        return "truncated", f"size {size} < saved offset {saved.offset}"
    if saved.anchor is not None and anchor is not None and anchor != saved.anchor:
        # the same first line and a size past the offset, but not the bytes the last run
        # read before it (issue #34): truncated and refilled, or rewritten in place
        return "truncated", ("the bytes before the saved offset are not the ones read "
                             "(truncated and refilled?)")
    if dev != saved.dev:
        return "continue", f"device id changed ({saved.dev} -> {dev}; remount or reboot?)"
    return "continue", None


@dataclass(frozen=True)
class Line:
    text: str
    cut: bool  # longer than LINE_CAP and cut there; the rest follows as further lines
    number: int  # the physical line's number in the file (1-based); fragments share it
    path: str = ""  # the physical file it was read from (an archive, after a rotation)


class LineReader:
    """Complete lines from ``offset``; ``offset`` tracks the end of the last one yielded and
    ``line`` the number of complete lines before it (``start_line`` is that count at
    ``offset``), so every Line carries the number the file would show in ``grep -n``."""

    def __init__(self, handle: BinaryStream, offset: int, cap: int = LINE_CAP,
                 start_line: int = 0, path: str = "",
                 same_file: Callable[[], bool] | None = None,
                 tail: bytes | None = None) -> None:
        self.handle = handle
        self.offset = offset
        self.cap = cap
        self.line = start_line
        self.path = path
        self.same_file = same_file  # asked after each chunk (issue #66); None: never
        self.truncated = False  # a chunk came from another file: dropped, the read ended
        self.nul_bytes = 0  # NUL bytes skipped at line starts (a copytruncate hole)
        self.at_end = False  # the last read reached EOF with no unterminated tail behind it
        # the last ANCHOR_CAP bytes before ``offset`` as read (issue #34): seeded with the
        # bytes before the start, folded from each chunk's consumed bytes at the next read
        # -- never per line -- and, between folds, completed on demand from the buffer;
        # None: not kept (a compressed stream, an archive)
        self._tail = tail
        self._buf = b""  # the buffer the reader is consuming, for ``tail`` between folds
        self._mark = 0  # the position in it just after the last line end: what offset is at
        self._carried = 0  # NULs of the current line's hole consumed from earlier buffers
        handle.seek(offset)

    @property
    def tail(self) -> bytes | None:
        """The bytes before ``offset``, at most ``ANCHOR_CAP`` of them, as this reader saw
        them (or was seeded with); None when they are not kept."""
        if self._tail is None or self._mark == 0:
            return self._tail
        return (self._tail + NUL * min(self._carried, ANCHOR_CAP)
                + self._buf[:self._mark])[-ANCHOR_CAP:]

    @property
    def anchor(self) -> str | None:
        return anchor_of(self.tail or b"")

    def _fold(self, buf: bytes, mark: int, carried: int) -> None:
        """Fold the consumed bytes of ``buf`` (up to ``mark``, behind ``carried`` NULs from
        earlier buffers) into the kept tail; only the last ``ANCHOR_CAP`` survive, so a
        hole longer than the cap counts as the cap."""
        if self._tail is not None and mark > 0:
            self._tail = (self._tail + NUL * min(carried, ANCHOR_CAP)
                          + buf[:mark])[-ANCHOR_CAP:]
        self._mark = 0

    def __iter__(self) -> Iterator[Line]:
        self.handle.seek(self.offset)  # a second pass resumes where the cursor is
        self._fold(self._buf, self._mark, self._carried)  # what a stopped pass consumed
        buf = b""
        pos = 0
        hole = 0  # NUL bytes consumed at this line's start, counted once the line ends
        line_start = True
        mark = 0  # the position in buf just after the last line end (or cap cut)
        carried = 0  # NULs of the current line's hole that earlier buffers held
        self._buf, self._mark, self._carried = buf, mark, carried
        while True:
            while True:
                if line_start:
                    # A run of NULs at a line start is a copytruncate hole, not text. It is
                    # consumed before the cap is measured, so the real line stays whole, and
                    # it only counts once the line ends: an unterminated run is re-read.
                    run = _NUL_RUN.match(buf, pos)
                    end = run.end() if run is not None else pos  # `*` always matches
                    hole += end - pos
                    pos = end
                    if pos == len(buf):
                        break  # the run may go on in the next chunk
                # a newline within cap+1 bytes ends the line; one byte further only if the
                # byte at the cap is the CR of a CRLF pair (the text is still cap bytes)
                newline = buf.find(b"\n", pos, pos + self.cap + 2)
                if newline == pos + self.cap + 1 and buf[pos + self.cap] != 0x0D:
                    newline = -1
                if newline >= 0:
                    raw, cut = buf[pos:newline], False
                    pos = newline + 1
                elif len(buf) - pos > self.cap:
                    raw, cut = buf[pos:pos + self.cap], True  # the cap is a hard boundary
                    pos += self.cap
                else:
                    break
                self.offset += hole + len(raw) + (0 if cut else 1)
                mark = self._mark = pos  # the tail's reach: offset is here (issue #34)
                self.nul_bytes += hole
                number = self.line + 1  # a fragment is still part of this physical line
                if not cut:
                    self.line += 1
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                nul_only = bool(hole) and not raw
                hole = 0
                line_start = not cut
                if nul_only:
                    continue  # a line of nothing but the hole
                yield Line(raw.decode("utf-8", errors="replace"), cut, number, self.path)
            # the consumed bytes join the tail before the check below asks for it; what
            # was consumed past the last line end is a hole still open (NULs only), and
            # only its length is needed once the buffer moves on
            self._fold(buf, mark, carried)
            carried = (carried if mark == 0 else 0) + (pos - mark)
            # read1, not read: on a cut archive read() raises before handing over the bytes
            # it did decompress, read1 hands them over first (measured for gz, bz2, xz)
            chunk = self.handle.read1(_CHUNK)
            if self.same_file is not None and not self.same_file():
                # the file was truncated under the handle (issue #66): the chunk may be
                # the new content at the old position; dropped, the offset stays at
                # the last complete line from before -- and an EOF met on a truncated
                # file (a quiet writer) is the same event, not the stream's end
                self.truncated = True
                return
            if not chunk:
                # the stream is exhausted: an unterminated tail may remain, and re-reading
                # it from this offset yields nothing until the file changes (the boundaries
                # are a function of the bytes alone), which is what the short-circuit needs
                self.at_end = True
                return  # what is left is an unterminated tail: re-read next run
            buf = buf[pos:] + chunk
            pos = mark = 0
            self._buf, self._mark, self._carried = buf, mark, carried


class LogFile:
    """One log file opened for a run, its identity decided and the start offset chosen.

    Use as a context manager; iterate ``lines()``; then ``cursor()`` is what to save once the
    section's mail is accepted.
    """

    def __init__(self, section: str, path: str, saved: Cursor | None, *,
                 from_start: bool = False) -> None:
        self.section = section
        self.path = path
        self.saved = saved
        self.unchanged = False  # a compressed file the cursor already describes: not opened
        self.handle: BinaryStream
        self.file_size: int | None  # what the cursor records (issue #44)
        self.file_mtime: float | None
        self.size: int | None
        self.verdict: Verdict
        self.note: str | None
        self.seed: bytes | None  # the bytes before the start, as read at the open (#34)
        if saved is not None and compressed_suffix(path) is not None and _unchanged(path, saved):
            self.unchanged = True
            self.handle = io.BytesIO()  # nothing to read; closes like any handle
            self.ino, self.dev = saved.ino, saved.dev
            self.compressed = True
            self.size = None
            self.fingerprint = saved.fingerprint
            self.line_at = 0
            self.verdict, self.note = "continue", None
            self.skipped = 0
            self.start, self.start_line = saved.offset, saved.line or 0
            self.seed = None
            self.reader = LineReader(self.handle, 0, start_line=self.start_line, path=path)
            self.reader.offset, self.reader.line = saved.offset, saved.line or 0
            self.file_size, self.file_mtime = saved.size, saved.mtime
            self.realpath = os.path.realpath(path)
            log.debug("[%s] %s: unchanged since the last read (size %s, mtime %s); not opened",
                      section, path, saved.size, saved.mtime)
            return
        self.handle = open_log(path, follow_links=True)  # a listed path; see open_log
        try:
            st = os.fstat(self.handle.fileno())
            self.ino, self.dev = st.st_ino, st.st_dev
            self.file_size, self.file_mtime = st.st_size, st.st_mtime
            self.compressed = compressed_suffix(path) is not None
            self.size = None if self.compressed else st.st_size
            self.fingerprint, self.line_at = first_line(self.handle)  # where the line begins
            probe = b""  # the bytes before the saved offset, as the file has them now (#34)
            if saved is not None and not self.compressed and saved.offset > 0:
                probe = tail_of(self.handle, saved.offset, ANCHOR_CAP)
            self.verdict, self.note = identify(saved, self.ino, self.dev, self.size,
                                               self.fingerprint, anchor_of(probe))
            self.skipped = 0  # bytes passed over on first sight
            self._counted: int | None = None  # lines before the start, when a pass counted them
            self.start = self._start_offset(from_start)
            self.start_line = self._start_line()
            # the reader's tail starts as the bytes before its start (issue #34): the probe
            # when it passed (or was trusted), the end's tail on a first sight, nothing
            # from 0; a compressed stream keeps none
            if self.compressed:
                self.seed = None
            elif self.start == 0:
                self.seed = b""
            elif self.verdict == "continue":
                self.seed = probe
            else:
                self.seed = tail_of(self.handle, self.start, ANCHOR_CAP)
            self.reader = LineReader(self.handle, self.start, start_line=self.start_line,
                                     path=path, same_file=None if self.compressed
                                     else self._still_the_file, tail=self.seed)
        except STREAM_ERRORS as exc:
            self.handle.close()
            raise OSError(f"{path}: incomplete or corrupt compressed stream ({exc})") from exc
        except BaseException:
            self.handle.close()
            raise
        self.realpath = os.path.realpath(path)
        self._log()

    def _start_offset(self, from_start: bool) -> int:
        if self.verdict == "first-sight":
            if from_start:
                return 0
            if self.compressed:
                # one forward pass gives the end, the last boundary and the line count
                self.skipped, self._counted = _end_of_last_line_counting(self.handle)
                return self.skipped
            self.skipped = _end_of_last_line(self.handle)
            return self.skipped
        if self.verdict == "continue" and self.saved is not None:
            offset = self.saved.offset
            if self.compressed:
                # a seek past the end of a decompressed stream is silent: tell() is the check
                self.handle.seek(offset)
                end = self.handle.tell()
                if end < offset:
                    self.verdict = "truncated"
                    self.note = f"stream ends at {end} < saved offset {offset}"
                    return 0
            return offset
        return 0  # rotated / truncated: the live file from the top (the copy is issue #8)

    def _start_line(self) -> int:
        """The complete lines before ``start``: 0 from the top, the cursor's count when it
        has one, and otherwise -- first sight at the end, or a state file from before the
        count existed -- one pass over the bytes before the start, once."""
        if self.start == 0:
            return 0
        if self.verdict == "continue" and self.saved is not None and self.saved.line is not None:
            return self.saved.line
        if self._counted is not None:
            return self._counted  # counted during the first-sight pass (issue #44)
        counted = count_newlines(self.handle, self.start)
        log.debug("[%s] %s: counted %d lines before offset %d (once)", self.section, self.path,
                  counted, self.start)
        return counted

    def context_before(self, n: int) -> list[Line]:
        """The last ``n`` complete lines before the start offset, numbered as the file numbers
        them -- the context a match early in a run would otherwise lack. A second handle, so
        the reader's buffer is never disturbed; the read is bounded (see ``lines_before``)."""
        if n <= 0 or self.start == 0:
            return []
        try:
            with open_log(self.path, follow_links=True) as handle:
                st = os.fstat(handle.fileno())
                if (st.st_ino, st.st_dev) != (self.ino, self.dev):
                    return []  # rotated under us since the open: not this file's lines
                if st.st_size < self.start or (self.fingerprint is not None
                                                and self._line_hash(handle) != self.fingerprint):
                    return []  # truncated under us (issue #66): the new content's lines
                if self.seed and tail_of(handle, self.start, len(self.seed)) != self.seed:
                    return []  # refilled past the start with the same first line (issue #34)
                return lines_before(handle, self.start, n, self.start_line, self.path)
        except _HOOK_ERRORS as exc:
            log.warning("[%s] %s: context before the saved position could not be read (%s)",
                        self.section, self.path, exc)
            return []

    def _log(self) -> None:
        where = f"[{self.section}] {self.path}"
        if self.verdict == "first-sight":
            if self.skipped:
                # a WARNING (issue #13): the one time lines are deliberately never mailed
                log.warning("%s: first sight; starting at the end, %d bytes skipped",
                            where, self.skipped)
            else:
                log.info("%s: first sight; reading from the beginning", where)
        elif self.note and self.verdict == "continue":
            log.info("%s: %s; continuing at offset %d", where, self.note, self.start)
        elif self.note:
            log.info("%s: %s; %s", where, self.note, self.verdict)
        else:
            log.debug("%s: continuing at offset %d", where, self.start)

    def _still_the_file(self) -> bool:
        """Whether the handle still reads the file that was opened (issue #66): its size not
        below the reader's position, its first line the one from the open, and the bytes
        before the position the ones the reader saw (issue #34) -- re-read through the
        handle with the position put back. A plain file only."""
        st = os.fstat(self.handle.fileno())
        if st.st_size < self.reader.offset:
            return False
        position = self.handle.tell()
        try:
            # no first line at the open (an empty file that gained lines): nothing to compare
            if self.fingerprint is not None and self._line_hash(self.handle) != self.fingerprint:
                return False
            tail = self.reader.tail
            return not tail or tail_of(self.handle, self.reader.offset, len(tail)) == tail
        finally:
            self.handle.seek(position)

    def _line_hash(self, handle: BinaryStream) -> str | None:
        """The hash of what is at the first line's recorded start now: the same line for
        the same file; NULs (a longer hole), another line (a refill) or nothing
        otherwise. One read of the cap, wherever the hole ended."""
        handle.seek(self.line_at)
        return _hash_of(_read_up_to(handle, FINGERPRINT_CAP))

    def lines(self) -> Iterator[Line]:
        """The complete lines from the start offset; ``OSError`` for a stream that ends early."""
        try:
            yield from self.reader
        except STREAM_ERRORS as exc:
            raise OSError(f"{self.path}: incomplete or corrupt compressed stream ({exc})") from exc
        if self.reader.truncated:
            log.warning("[%s] %s: truncated under us during the read (a copytruncate during "
                        "the run?); stopping at offset %d", self.section, self.path,
                        self.reader.offset)
        if self.reader.nul_bytes:
            log.info("[%s] %s: skipped %d NUL bytes (copytruncate under a writer without "
                     "O_APPEND?)", self.section, self.path, self.reader.nul_bytes)

    @property
    def offset(self) -> int:
        """The byte just after the last complete line read so far."""
        return self.reader.offset

    def start_cursor(self, now: datetime | None = None) -> Cursor:
        """The cursor that would read again what this run read: the start offset and its
        line count, with the identity taken at the open -- what a failed delivery saves for
        a file that had no entry (issue #31), so the next run re-sends those lines instead
        of first-sighting the file at its end. No size or mtime: an unread stream is not
        settled."""
        return Cursor(offset=self.start, ino=self.ino, dev=self.dev,
                      fingerprint=self.fingerprint, realpath=self.realpath,
                      last_seen=timestamp(now), line=self.start_line,
                      anchor=anchor_of(self.seed or b""))

    def cursor(self, now: datetime | None = None) -> Cursor:
        return Cursor(offset=self.reader.offset, ino=self.ino, dev=self.dev,
                      fingerprint=self.fingerprint, realpath=self.realpath,
                      last_seen=timestamp(now), line=self.reader.line,
                      # recorded only for a compressed file read to its end: a cursor that
                      # carries them says its offset IS the unchanged stream's end
                      size=self.file_size if self._settled() else None,
                      mtime=self.file_mtime if self._settled() else None,
                      anchor=self.reader.anchor)  # the bytes before offset, as read (#34)

    def _settled(self) -> bool:
        return self.compressed and (self.unchanged or self.reader.at_end)

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> "LogFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def count_newlines(handle: BinaryStream, end: int) -> int:
    """The complete lines in ``[0, end)``: one pass, chunked. Leaves the position at 0."""
    handle.seek(0)
    count = 0
    remaining = end
    while remaining > 0:
        chunk = _read_up_to(handle, min(_CHUNK, remaining))
        if not chunk:
            break
        count += chunk.count(b"\n")
        remaining -= len(chunk)
    handle.seek(0)
    return count


def lines_before(handle: BinaryStream, offset: int, n: int, line: int,
                 path: str = "") -> list[Line]:
    """The last ``n`` complete lines ending at ``offset`` (a line boundary), numbered so that
    the last of them is line ``line``. One read of the ``n * (LINE_CAP + 1) + _CHUNK`` bytes
    before the offset -- a single seek, which on a compressed stream is one decompression
    up to that point. Lines over the cap are cut as the reader cuts them; NUL-only lines are
    counted but not returned, as the reader skips them."""
    if n <= 0 or offset <= 0:
        return []
    start = max(0, offset - window_for(n))
    handle.seek(max(0, start - 1))  # one byte more: it says whether the window starts a line
    buf = _read_up_to(handle, offset - max(0, start - 1))
    handle.seek(0)
    return lines_from_window(buf, start, n, line, path)


def window_for(n: int) -> int:
    """The bytes before the offset that ``n`` context lines can need."""
    return n * (LINE_CAP + 1) + _CHUNK


def lines_from_window(buf: bytes, start: int, n: int, line: int, path: str = "") -> list[Line]:
    """The parsing half of ``lines_before``: ``buf`` holds the bytes from ``max(0, start - 1)``
    to the offset (the byte before the window, when there is one, says whether the window
    starts a line). The rotation catch-up keeps that window from its confirming seek and
    answers the hook from it without a second decompression (issue #46)."""
    if start > 0:
        # the window's first line is complete only if the byte before it is a newline;
        # otherwise it is the tail of a line that began earlier, and is dropped
        complete = buf[:1] == b"\n"
        buf = buf[1:]
        parts = buf.split(b"\n")[:-1]
        if not complete:
            parts = parts[1:]
    else:
        parts = buf.split(b"\n")[:-1]  # the last element is the empty string after the boundary
    numbered: list[Line] = []
    number = line
    for raw in reversed(parts[-n:]):
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        text = raw.lstrip(NUL)
        if text or not raw:
            cut = len(text) > LINE_CAP
            numbered.append(Line(text[:LINE_CAP].decode("utf-8", errors="replace"), cut, number,
                                 path))
        number -= 1
    numbered.reverse()
    return numbered


def _unchanged(path: str, saved: Cursor) -> bool:
    """Whether a compressed file is what the cursor recorded: same inode and device, same
    size and mtime. ``lstat``, so a symbolic link is never unchanged: a listed link goes
    through ``open_log``'s owner rule (issue #26) every run, whatever it points to; an
    absent file is not unchanged either (the open then says so)."""
    if saved.size is None or saved.mtime is None:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISREG(st.st_mode) and (st.st_ino, st.st_dev) == (saved.ino, saved.dev)
            and st.st_size == saved.size and st.st_mtime == saved.mtime)


def _end_of_last_line_counting(handle: BinaryStream) -> tuple[int, int]:
    """``_end_of_last_line`` for a compressed stream, in ONE forward pass that also counts
    the complete lines before the boundary (issue #44: a seek to the end, a seek back and
    ``count_newlines`` were three decompressions). Returns (offset, lines before it), with
    the stream positioned at the offset when it is the end -- the common case."""
    handle.seek(0)
    size = 0
    newlines = 0
    last_newline = -1  # absolute offset of the last newline seen
    while True:
        chunk = handle.read1(_CHUNK)  # a stream error propagates: a cut archive is an OSError
        if not chunk:
            break
        found = chunk.rfind(b"\n")
        if found >= 0:
            last_newline = size + found
        newlines += chunk.count(b"\n")
        size += len(chunk)
    # the one rule _end_of_last_line applies (review): a tail longer than LINE_CAP without a
    # newline is pathological and the end of the stream is the boundary; a shorter held
    # tail is re-read once it is complete
    boundary = last_newline + 1  # 0 without a newline
    offset = size if size - boundary > LINE_CAP else boundary
    if offset != size:
        handle.seek(offset)  # a held tail after the boundary: back to it (rare)
    return offset, newlines


def _end_of_last_line(handle: BinaryStream) -> int:
    """The offset just after the last newline, so a first sight starts on a line boundary.

    A file that is one unterminated line of at most LINE_CAP bytes starts at 0 (the line is
    read once it is complete). A tail longer than LINE_CAP without a newline is pathological:
    the end of the file is the boundary, and the newline that eventually completes that
    line is reported as an empty line.
    """
    handle.seek(0, io.SEEK_END)
    size = handle.tell()
    start = max(0, size - LINE_CAP - 1)  # a held tail is at most LINE_CAP bytes long
    handle.seek(start)
    tail = handle.read()
    newline = tail.rfind(b"\n")
    if newline < 0:
        return size if size > LINE_CAP else 0
    return start + newline + 1
