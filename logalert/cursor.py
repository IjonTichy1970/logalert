"""Reading a log file from where a section left off: identity, truncation, the line reader.

The file a cursor points at may have been rotated, truncated, rewritten in place or replaced
by the time the next run opens it. This module opens the file, ``fstat``s the descriptor it
will actually read, and decides how the saved cursor applies:

  * same inode, first line unchanged, size >= offset -> CONTINUE from the offset
  * same inode, first line changed -> ROTATED: ext4 hands a freed inode number straight back,
    so a same-inode file with different content is a different file (measured: after two
    ``delaycompress`` rotations the saved live inode came back as the new live file)
  * same inode, size < offset -> TRUNCATED (``copytruncate``)
  * inode differs -> ROTATED
  * device id differs alone -> continue, with a log line (a remount or a reboot renumbers
    devices; that never declares a rotation by itself)
  * no cursor -> FIRST SIGHT: start at the current end of the file, on a line boundary, so
    the first run is not a flood of old news; ``start = beginning`` / ``--from-start`` read
    from 0. Nothing is emitted; the skipped byte count is logged.

In this issue ROTATED and TRUNCATED read the live file from 0; the rotation issue (#8) reads
the rotated copy first, keyed by the fingerprint saved here. Compressed files (``.gz``,
``.bz2``, ``.xz``; ``.zst`` where the stdlib has ``compression.zstd``) are read through the
decompressor with the offset in the UNCOMPRESSED stream; ``seek`` past its end is silent, so
``tell`` after the seek is what detects a shorter stream.

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
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from logalert.state import Cursor, timestamp

log = logging.getLogger("logalert.cursor")

LINE_CAP = 2000  # bytes; a longer line is cut here and the cut reported
FINGERPRINT_CAP = 4096  # bytes of the first line that identify a file
_CHUNK = 65536
NUL = bytes([0])  # spelled from the code point: gated Python carries no control characters
_NUL_RUN = re.compile(NUL + b"*")
# what gzip / bz2 / lzma raise for a half-written or corrupt archive, besides OSError
_STREAM_ERRORS: tuple[type[Exception], ...] = (EOFError, lzma.LZMAError)

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
    kind = _special_kind(os.stat(path).st_mode)
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


def _special_kind(mode: int) -> str | None:
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
    as the reader skips it. None when the file has no complete first line yet -- and a None
    is never compared. Leaves the position at 0.
    """
    handle.seek(0)
    head = handle.read(FINGERPRINT_CAP)
    handle.seek(0)
    text = head.lstrip(NUL)
    newline = text.find(b"\n")
    if newline >= 0:
        line = text[:newline]
    elif len(text) >= FINGERPRINT_CAP:
        line = text  # the first line is longer than the cap: the cap identifies it
    else:
        return None
    return hashlib.sha256(line).hexdigest()


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
    if size is not None and size < saved.offset:
        return "truncated", f"size {size} < saved offset {saved.offset}"
    if dev != saved.dev:
        return "continue", f"device id changed ({saved.dev} -> {dev}; remount or reboot?)"
    return "continue", None


@dataclass(frozen=True)
class Line:
    text: str
    cut: bool  # longer than LINE_CAP and cut there; the rest follows as further lines


class LineReader:
    """Complete lines from ``offset``; ``offset`` tracks the end of the last one yielded."""

    def __init__(self, handle: BinaryStream, offset: int, cap: int = LINE_CAP) -> None:
        self.handle = handle
        self.offset = offset
        self.cap = cap
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
                if raw.endswith(b"\r"):
                    raw = raw[:-1]
                nul_only = bool(hole) and not raw
                hole = 0
                line_start = not cut
                if nul_only:
                    continue  # a line of nothing but the hole
                yield Line(raw.decode("utf-8", errors="replace"), cut)
            chunk = self.handle.read(_CHUNK)
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
            self.reader = LineReader(self.handle, self.start)
        except _STREAM_ERRORS as exc:
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

    def _log(self) -> None:
        where = f"[{self.section}] {self.path}"
        if self.verdict == "first-sight":
            if self.skipped:
                log.info("%s: first sight; starting at the end, %d bytes skipped",
                         where, self.skipped)
            else:
                log.info("%s: first sight; reading from the beginning", where)
        elif self.note and self.verdict == "continue":
            log.info("%s: %s; continuing at offset %d", where, self.note, self.start)
        elif self.note:
            log.info("%s: %s; %s: reading the live file from the beginning",
                     where, self.note, self.verdict)
        else:
            log.debug("%s: continuing at offset %d", where, self.start)

    def lines(self) -> Iterator[Line]:
        """The complete lines from the start offset; ``OSError`` for a stream that ends early."""
        try:
            yield from self.reader
        except _STREAM_ERRORS as exc:
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
                      last_seen=timestamp(now))

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
