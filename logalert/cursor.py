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
  * inode differs -> ROTATED
  * device id differs alone -> continue, with a log line (a remount or a reboot renumbers
    devices; that never declares a rotation by itself)
  * no cursor -> FIRST SIGHT: start at the current end of the file, on a line boundary, so
    the first run is not a flood of old news; ``start = beginning`` / ``--from-start`` read
    from 0. Nothing is emitted; the skipped byte count is logged, as a WARNING.

On its own a ``LogFile`` reads a ROTATED or TRUNCATED live file from 0; ``logalert.rotation``
wraps it to read the rotated copy first, found by the inode and the fingerprint saved here.
Compressed files (``.gz``, ``.bz2``, ``.xz``; ``.zst`` where the stdlib has
``compression.zstd``) are read through the decompressor with the offset in the UNCOMPRESSED
stream; ``seek`` past its end is silent, so ``tell`` after the seek is what detects a shorter
stream.

Lines are read in binary and decoded as UTF-8 with replacement. The cursor advances only to
the end of the last COMPLETE line: an unterminated tail is re-read next run. A line longer
than ``LINE_CAP`` bytes is cut there and the cut is reported; the cap is a hard boundary, so
from a given start offset the boundaries are a function of the bytes alone. A run of NUL
bytes (what ``copytruncate`` leaves under a writer without ``O_APPEND``) is dropped, whether
it is a whole line or the start of one.
"""

import bz2
import gzip
import hashlib
import importlib
import io
import logging
import lzma
import os
import re
import stat
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from logalert.state import Cursor, timestamp

log = logging.getLogger("logalert.cursor")

LINE_CAP = 2000  # bytes; a longer line is cut here and the cut reported
FINGERPRINT_CAP = 4096  # bytes of the first line that identify a file
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


def open_log(path: str) -> BinaryStream:
    """Open for binary reading, through the decompressor the suffix names.

    Only a regular file: a FIFO with no writer would block ``open(2)`` for good, and a
    device or socket has no byte offsets to remember. ``FileNotFoundError`` propagates.
    """
    kind = special_kind(os.stat(path).st_mode)
    if kind is not None:
        raise OSError(f"{path}: not a regular file (a {kind})")
    suffix = compressed_suffix(path)
    if suffix == ".gz":
        return gzip.open(path, "rb")
    if suffix == ".bz2":
        return bz2.open(path, "rb")
    if suffix == ".xz":
        return lzma.open(path, "rb")
    if suffix == ".zst":
        try:
            zstd = importlib.import_module("compression.zstd")  # 3.14+
        except ImportError:
            raise OSError(f"{path}: reading .zst needs Python 3.14+ built with zstd support "
                          f"(compression.zstd)") from None
        handle: BinaryStream = zstd.open(path, "rb")
        return handle
    return open(path, "rb")


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
    """sha256 of the first complete line (at most FINGERPRINT_CAP bytes), read from 0.

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
            return None
    if text and len(text) < FINGERPRINT_CAP:
        text += _read_up_to(handle, FINGERPRINT_CAP - len(text))  # a cap of the line itself
    handle.seek(0)
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


def identify(saved: Cursor | None, ino: int, dev: int, size: int | None,
             current: str | None) -> tuple[Verdict, str | None]:
    """Apply the identity rules; returns the verdict and a note worth logging, if any.

    ``size`` is None for a compressed file, whose truncation is detected by seeking instead.
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
                 start_line: int = 0, path: str = "") -> None:
        self.handle = handle
        self.offset = offset
        self.cap = cap
        self.line = start_line
        self.path = path
        self.nul_bytes = 0  # NUL bytes skipped at line starts (a copytruncate hole)
        handle.seek(offset)

    def __iter__(self) -> Iterator[Line]:
        self.handle.seek(self.offset)  # a second pass resumes where the cursor is
        buf = b""
        pos = 0
        hole = 0  # NUL bytes consumed at this line's start, counted once the line ends
        line_start = True
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
            # read1, not read: on a cut archive read() raises before handing over the bytes
            # it did decompress, read1 hands them over first (measured for gz, bz2, xz)
            chunk = self.handle.read1(_CHUNK)
            if not chunk:
                return  # what is left is an unterminated tail: re-read next run
            buf = buf[pos:] + chunk
            pos = 0


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
        self.handle = open_log(path)
        try:
            st = os.fstat(self.handle.fileno())
            self.ino, self.dev = st.st_ino, st.st_dev
            self.compressed = compressed_suffix(path) is not None
            self.size: int | None = None if self.compressed else st.st_size
            self.fingerprint = fingerprint(self.handle)
            self.verdict, self.note = identify(saved, self.ino, self.dev, self.size,
                                               self.fingerprint)
            self.skipped = 0  # bytes passed over on first sight
            self.start = self._start_offset(from_start)
            self.start_line = self._start_line()
            self.reader = LineReader(self.handle, self.start, start_line=self.start_line,
                                     path=path)
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
            with open_log(self.path) as handle:
                st = os.fstat(handle.fileno())
                if (st.st_ino, st.st_dev) != (self.ino, self.dev):
                    return []  # rotated under us since the open: not this file's lines
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

    def lines(self) -> Iterator[Line]:
        """The complete lines from the start offset; ``OSError`` for a stream that ends early."""
        try:
            yield from self.reader
        except STREAM_ERRORS as exc:
            raise OSError(f"{self.path}: incomplete or corrupt compressed stream ({exc})") from exc
        if self.reader.nul_bytes:
            log.info("[%s] %s: skipped %d NUL bytes (copytruncate under a writer without "
                     "O_APPEND?)", self.section, self.path, self.reader.nul_bytes)

    @property
    def offset(self) -> int:
        """The byte just after the last complete line read so far."""
        return self.reader.offset

    def cursor(self, now: datetime | None = None) -> Cursor:
        return Cursor(offset=self.reader.offset, ino=self.ino, dev=self.dev,
                      fingerprint=self.fingerprint, realpath=self.realpath,
                      last_seen=timestamp(now), line=self.reader.line)

    def close(self) -> None:
        self.handle.close()

    def __enter__(self) -> "LogFile":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_log_file(section: str, path: str, saved: Cursor | None, *,
                  from_start: bool = False) -> LogFile | None:
    """``LogFile`` for the path, or None when it is absent this run (the entry is kept)."""
    try:
        return LogFile(section, path, saved, from_start=from_start)
    except FileNotFoundError:
        log.debug("[%s] %s: absent this run", section, path)
        return None


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
    start = max(0, offset - (n * (LINE_CAP + 1) + _CHUNK))
    handle.seek(max(0, start - 1))  # one byte more: it says whether the window starts a line
    buf = _read_up_to(handle, offset - max(0, start - 1))
    handle.seek(0)
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
