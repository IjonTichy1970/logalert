"""The cursor: identity rules, first sight, the line reader, compressed inputs (issue #7).

Every test builds real files under tmp_path, so the inode logic is exercised on both
platforms (NTFS file ids are stable and non-zero). Non-ASCII bytes are spelled as code points.
"""

import bz2
import gzip
import hashlib
import io
import logging
import lzma
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from logalert.cursor import (
    FINGERPRINT_CAP,
    LINE_CAP,
    Line,
    LineReader,
    LogFile,
    compressed_suffix,
    fingerprint,
    identify,
    open_log,
    open_log_file,
)
from logalert.state import Cursor, State, load_state, timestamp

SECTION = "router-disk"
E_ACUTE = chr(0xE9).encode("utf-8")  # spelled from the code point for the ASCII gate
NUL = bytes([0])  # likewise: never a control character in the source


def sha(line: bytes) -> str:
    return hashlib.sha256(line).hexdigest()


def scan(path: Path, saved: Cursor | None, *, from_start: bool = False,
         section: str = SECTION) -> tuple[LogFile, list[Line]]:
    """Open, read everything, close; the LogFile keeps its verdict and cursor."""
    log = open_log_file(section, str(path), saved, from_start=from_start)
    assert log is not None, f"{path} was absent"
    with log:
        lines = list(log.lines())
    return log, lines


def texts(lines: list[Line]) -> list[str]:
    return [line.text for line in lines]


# -- the identity rules, as a pure function ---------------------------------------------------


def saved_cursor(**changes: Any) -> Cursor:
    base = Cursor(offset=100, ino=7, dev=3, fingerprint=sha(b"first"), realpath="/x",
                  last_seen=timestamp())
    return replace(base, **changes)


def test_identify_no_cursor_is_first_sight() -> None:
    assert identify(None, 7, 3, 500, sha(b"first")) == ("first-sight", None)


def test_identify_continues_on_same_inode_same_first_line_and_enough_size() -> None:
    assert identify(saved_cursor(), 7, 3, 100, sha(b"first")) == ("continue", None)
    assert identify(saved_cursor(), 7, 3, 5000, sha(b"first")) == ("continue", None)


def test_identify_inode_change_is_rotation() -> None:
    verdict, note = identify(saved_cursor(), 8, 3, 5000, sha(b"first"))
    assert verdict == "rotated" and note == "inode changed (7 -> 8)"


def test_identify_same_inode_different_first_line_is_rotation() -> None:
    verdict, note = identify(saved_cursor(), 7, 3, 5000, sha(b"other"))
    assert verdict == "rotated" and note is not None and "different first line" in note


def test_identify_size_below_offset_is_truncation() -> None:
    verdict, note = identify(saved_cursor(), 7, 3, 99, sha(b"first"))
    assert verdict == "truncated" and note == "size 99 < saved offset 100"


def test_identify_device_change_alone_continues_with_a_note() -> None:
    verdict, note = identify(saved_cursor(), 7, 4, 5000, sha(b"first"))
    assert verdict == "continue" and note == "device id changed (3 -> 4; remount or reboot?)"


def test_identify_never_compares_a_null_fingerprint() -> None:
    assert identify(saved_cursor(fingerprint=None), 7, 3, 500, sha(b"x")) == ("continue", None)
    assert identify(saved_cursor(), 7, 3, 500, None) == ("continue", None)


def test_identify_size_is_not_consulted_for_compressed_files() -> None:
    assert identify(saved_cursor(), 7, 3, None, sha(b"first")) == ("continue", None)


# -- the fingerprint ---------------------------------------------------------------------------


def test_fingerprint_is_the_first_complete_line(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_bytes(b"first line\nsecond\n")
    with open(path, "rb") as handle:
        assert fingerprint(handle) == sha(b"first line")
        assert handle.tell() == 0
    path.write_bytes(b"no newline yet")
    with open(path, "rb") as handle:
        assert fingerprint(handle) is None
    path.write_bytes(b"")
    with open(path, "rb") as handle:
        assert fingerprint(handle) is None


def test_fingerprint_caps_a_long_first_line(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    head = b"x" * FINGERPRINT_CAP
    path.write_bytes(head + b"yyyy\nsecond\n")
    with open(path, "rb") as handle:
        assert fingerprint(handle) == sha(head)
    path.write_bytes(head)  # exactly the cap, no newline: identified by the cap
    with open(path, "rb") as handle:
        assert fingerprint(handle) == sha(head)
    path.write_bytes(head[:-1])  # one short of the cap, no newline: not yet
    with open(path, "rb") as handle:
        assert fingerprint(handle) is None


# -- the line reader ---------------------------------------------------------------------------


def read_all(data: bytes, offset: int = 0, cap: int = LINE_CAP) -> tuple[list[Line], int]:
    reader = LineReader(io.BytesIO(data), offset, cap)
    return list(reader), reader.offset


def test_reader_yields_complete_lines_and_keeps_the_tail(tmp_path: Path) -> None:
    lines, offset = read_all(b"one\ntwo\r\nthree")
    assert texts(lines) == ["one", "two"]
    assert offset == len(b"one\ntwo\r\n")
    assert all(not line.cut for line in lines)


def test_reader_starts_at_the_offset() -> None:
    data = b"one\ntwo\nthree\n"
    lines, offset = read_all(data, len(b"one\n"))
    assert texts(lines) == ["two", "three"] and offset == len(data)


def test_reader_yields_empty_lines_for_faithful_context() -> None:
    lines, offset = read_all(b"a\n\nb\n")
    assert texts(lines) == ["a", "", "b"] and offset == 5


def test_reader_replaces_invalid_utf8() -> None:
    lines, _ = read_all(b"caf" + E_ACUTE + b" ok\nbad " + bytes([0xFF]) + b" byte\n")
    assert lines[0].text == "caf" + chr(0xE9) + " ok"
    assert lines[1].text == "bad " + chr(0xFFFD) + " byte"


def test_reader_cuts_a_long_line_at_the_cap_deterministically() -> None:
    long = b"L" * (LINE_CAP + 10)
    data = b"short\n" + long + b"\nafter\n"
    lines, offset = read_all(data)
    assert texts(lines) == ["short", "L" * LINE_CAP, "L" * 10, "after"]
    assert [line.cut for line in lines] == [False, True, False, False]
    assert offset == len(data)
    # the same bytes read in two runs land on the same boundaries
    first, mid = read_all(data[: len(b"short\n") + LINE_CAP + 3])
    assert texts(first) == ["short", "L" * LINE_CAP] and mid == len(b"short\n") + LINE_CAP
    rest, end = read_all(data, mid)
    assert texts(rest) == ["L" * 10, "after"] and end == len(data)


def test_reader_line_exactly_at_the_cap_is_not_cut() -> None:
    line = b"E" * LINE_CAP
    lines, offset = read_all(line + b"\nnext\n")
    assert texts(lines) == ["E" * LINE_CAP, "next"] and not lines[0].cut
    assert offset == LINE_CAP + 1 + 5


def test_reader_unterminated_tail_longer_than_the_cap_is_cut_not_held() -> None:
    lines, offset = read_all(b"x\n" + b"T" * (LINE_CAP + 1))
    assert texts(lines) == ["x", "T" * LINE_CAP] and lines[1].cut
    assert offset == 2 + LINE_CAP  # the single trailing byte waits for its newline


def test_reader_skips_nul_only_lines_and_counts_the_bytes() -> None:
    data = NUL * 300 + b"\nreal\n" + NUL * 2 + b"\n"
    reader = LineReader(io.BytesIO(data), 0)
    assert texts(list(reader)) == ["real"]
    assert reader.nul_bytes == 302 and reader.offset == len(data)


def test_reader_nul_run_longer_than_the_cap_is_one_hole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("logalert.cursor._CHUNK", 1000)  # the hole spans chunk refills
    data = NUL * (LINE_CAP * 2 + 5) + b"\nreal\n"
    reader = LineReader(io.BytesIO(data), 0)
    assert texts(list(reader)) == ["real"]
    assert reader.nul_bytes == LINE_CAP * 2 + 5 and reader.offset == len(data)


def test_reader_measures_the_cap_from_the_first_real_byte() -> None:
    # the hole is consumed BEFORE the cap: a hole of cap-5 bytes plus a 13-byte line
    # is one whole line, never 'disk ' cut at the cap and 'failure' after it
    data = NUL * (LINE_CAP - 5) + b"disk failure\n"
    lines, offset = read_all(data)
    assert [(line.text, line.cut) for line in lines] == [("disk failure", False)]
    assert offset == len(data)


def test_reader_unterminated_nul_run_is_not_consumed() -> None:
    # a preallocating writer fills the run in later: the offset must stay before it
    lines, offset = read_all(b"a\n" + NUL * 30)
    assert texts(lines) == ["a"] and offset == 2
    # and the same bytes read in two passes land on the same boundary as one pass
    whole = NUL * 30 + b"\nreal\n"
    one_pass, end = read_all(whole)
    first, mid = read_all(whole[:30])
    rest, end2 = read_all(whole, mid)
    assert (texts(one_pass), end) == (texts(first) + texts(rest), end2) == (["real"], 36)


def test_reader_strips_a_nul_run_before_the_text_and_counts_it() -> None:
    # copytruncate under a writer without O_APPEND: the writer resumes at its old offset,
    # so the hole of NULs and its next line share one line
    data = NUL * 40 + b"Sep 14 router1 disk failure\n" + b"next\n"
    reader = LineReader(io.BytesIO(data), 0)
    assert texts(list(reader)) == ["Sep 14 router1 disk failure", "next"]
    assert reader.nul_bytes == 40 and reader.offset == len(data)
    lines, _ = read_all(b"a" + NUL + b"b\n")  # a NUL inside the text is content
    assert lines[0].text == "a" + chr(0) + "b"
    # after a cut, the next fragment is mid-line: its NULs are content too
    lines, _ = read_all(b"E" * LINE_CAP + NUL * 3 + b"F\n")
    assert texts(lines) == ["E" * LINE_CAP, chr(0) * 3 + "F"]


def test_reader_crosses_chunk_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("logalert.cursor._CHUNK", 7)
    data = b"one line\nsecond line here\nthird\n"
    lines, offset = read_all(data)
    assert texts(lines) == ["one line", "second line here", "third"] and offset == len(data)


# -- LogFile: the rules against real files -------------------------------------------------------


def test_first_sight_starts_at_the_end_and_logs_the_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"old news 1\nold news 2\n")
    caplog.set_level(logging.DEBUG, logger="logalert")
    log, lines = scan(path, None)
    assert log.verdict == "first-sight" and lines == []
    assert log.skipped == 22 and log.offset == 22
    assert [r.getMessage() for r in caplog.records] == [
        f"[{SECTION}] {path}: first sight; starting at the end, 22 bytes skipped"
    ]
    assert caplog.records[0].levelno == logging.INFO
    saved = log.cursor()
    assert saved.offset == 22 and saved.fingerprint == sha(b"old news 1")
    assert saved.ino == path.stat().st_ino and saved.ino != 0
    assert saved.realpath == os.path.realpath(path)


def test_first_sight_lands_on_a_line_boundary(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"done\nin progress")
    log, lines = scan(path, None)
    assert lines == [] and log.offset == 5  # after "done\n"; the partial line waits
    with open(path, "ab") as handle:
        handle.write(b" now done\nnext\n")
    log2, lines2 = scan(path, log.cursor())
    assert log2.verdict == "continue" and texts(lines2) == ["in progress now done", "next"]


def test_first_sight_of_one_unterminated_line_starts_at_zero(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"being written")
    log, lines = scan(path, None)
    assert lines == [] and log.offset == 0 and log.skipped == 0


def test_first_sight_from_start_reads_everything(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"old news 1\nold news 2\n")
    caplog.set_level(logging.INFO, logger="logalert")
    log, lines = scan(path, None, from_start=True)
    assert texts(lines) == ["old news 1", "old news 2"] and log.skipped == 0
    assert caplog.records[-1].getMessage().endswith("first sight; reading from the beginning")


def test_append_yields_only_the_new_lines(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\n")
    log, _ = scan(path, None)
    with open(path, "ab") as handle:
        handle.write(b"c\nd\n")
    log2, lines = scan(path, log.cursor())
    assert log2.verdict == "continue" and texts(lines) == ["c", "d"]
    log3, lines3 = scan(path, log2.cursor())
    assert lines3 == [] and log3.offset == log2.offset


def test_partial_trailing_line_is_reread_intact(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\n")
    log, _ = scan(path, None)
    with open(path, "ab") as handle:
        handle.write(b"disk fai")
    log2, lines = scan(path, log.cursor())
    assert lines == [] and log2.offset == 2
    with open(path, "ab") as handle:
        handle.write(b"lure on sda\n")
    _, lines3 = scan(path, log2.cursor())
    assert texts(lines3) == ["disk failure on sda"]


def test_same_inode_changed_first_line_is_not_resumed_mid_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"first\nsecond\n")
    log, _ = scan(path, None)
    # fabricate the inode reuse: same ino on record, a different first line on record
    saved = replace(log.cursor(), fingerprint=sha(b"a line the old file started with"))
    caplog.set_level(logging.INFO, logger="logalert")
    log2, lines = scan(path, saved)
    assert log2.verdict == "rotated" and texts(lines) == ["first", "second"]
    assert "different first line" in caplog.records[-1].getMessage()
    assert "reading the live file from the beginning" in caplog.records[-1].getMessage()


def test_inode_change_is_rotation(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"first\nsecond\n")
    log, _ = scan(path, None)
    saved = replace(log.cursor(), ino=log.ino + 1)
    log2, lines = scan(path, saved)
    assert log2.verdict == "rotated" and texts(lines) == ["first", "second"]


def test_truncation_reads_the_live_file_from_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"first\nsecond\nthird\n")
    log, _ = scan(path, None)
    path.write_bytes(b"first\n")  # copytruncate keeps the inode and the first line
    assert path.stat().st_ino == log.ino
    caplog.set_level(logging.INFO, logger="logalert")
    log2, lines = scan(path, log.cursor())
    assert log2.verdict == "truncated" and texts(lines) == ["first"]
    assert "size 6 < saved offset 19" in caplog.records[-1].getMessage()


def test_device_change_alone_continues_with_one_log_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\n")
    log, _ = scan(path, None)
    with open(path, "ab") as handle:
        handle.write(b"c\n")
    saved = replace(log.cursor(), dev=log.dev + 1)
    caplog.set_level(logging.DEBUG, logger="logalert")
    log2, lines = scan(path, saved)
    assert log2.verdict == "continue" and texts(lines) == ["c"]
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 and "device id changed" in messages[0]
    assert messages[0].endswith("continuing at offset 4")
    assert log2.cursor().dev == log.dev  # the id seen now is what gets saved


def test_missing_file_is_none_and_logged_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="logalert")
    assert open_log_file(SECTION, str(tmp_path / "gone.log"), None) is None
    record = caplog.records[-1]
    assert record.levelno == logging.DEBUG
    assert record.getMessage() == f"[{SECTION}] {tmp_path / 'gone.log'}: absent this run"


def test_reappearing_file_goes_through_the_identity_rules(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\n")
    log, _ = scan(path, None)
    saved = log.cursor()
    path.unlink()
    assert open_log_file(SECTION, str(path), saved) is None
    path.write_bytes(b"a\nb\nc\n")  # same first line; the inode may or may not come back
    log2, lines = scan(path, saved)
    if log2.ino == saved.ino:
        assert log2.verdict == "continue" and texts(lines) == ["c"]
    else:
        assert log2.verdict == "rotated" and texts(lines) == ["a", "b", "c"]


def test_a_directory_or_device_is_refused_before_it_is_opened(tmp_path: Path) -> None:
    with pytest.raises(OSError, match=r"not a regular file \(a directory\)"):
        open_log_file(SECTION, str(tmp_path), None)
    device = "NUL" if sys.platform == "win32" else "/dev/null"
    with pytest.raises(OSError, match=r"not a regular file \(a device\)"):
        open_log_file(SECTION, device, None)


def test_a_fifo_is_refused_instead_of_blocking_open(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("no FIFOs on Windows; runs in the sandbox and on CI")
    else:
        fifo = tmp_path / "router.log"
        os.mkfifo(fifo)
        # a reader and a writer are held open so a regressed check fails on ESPIPE
        # rather than hanging the test in open(2)
        reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            with pytest.raises(OSError, match=r"not a regular file \(a FIFO\)"):
                open_log_file(SECTION, str(fifo), None)
        finally:
            os.close(writer)
            os.close(reader)


def test_fingerprint_skips_a_copytruncate_hole(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_bytes(NUL * 100 + b"first line\nsecond\n")
    with open(path, "rb") as handle:
        assert fingerprint(handle) == sha(b"first line")
    path.write_bytes(NUL * FINGERPRINT_CAP + b"first line\n")  # hole beyond the cap
    with open(path, "rb") as handle:
        assert fingerprint(handle) is None  # not identifiable yet, never compared


def test_one_file_two_sections_the_failed_section_keeps_its_place(tmp_path: Path) -> None:
    """The at-least-once contract: state is saved per section, after its mail is accepted."""
    path = tmp_path / "router.log"
    state_path = tmp_path / "state.json"
    path.write_bytes(b"seen\n")
    state = State(str(state_path))
    for section in ("a", "b"):
        log, _ = scan(path, None, section=section)
        state.set(section, str(path), log.cursor())
    state.save()
    with open(path, "ab") as handle:
        handle.write(b"disk failure\n")
    # section a: read, mail accepted, saved; section b: read, mail FAILED, not saved
    state = load_state(str(state_path))
    log_a, lines_a = scan(path, state.get("a", str(path)), section="a")
    assert texts(lines_a) == ["disk failure"]
    state.set("a", str(path), log_a.cursor())
    state.save()
    log_b, lines_b = scan(path, state.get("b", str(path)), section="b")
    assert texts(lines_b) == ["disk failure"]
    # (transport fails here; nothing saved for b)
    again = load_state(str(state_path))
    _, rerun_a = scan(path, again.get("a", str(path)), section="a")
    _, rerun_b = scan(path, again.get("b", str(path)), section="b")
    assert rerun_a == [] and texts(rerun_b) == ["disk failure"]


# -- compressed inputs ---------------------------------------------------------------------------


def test_compressed_suffix() -> None:
    assert compressed_suffix("/var/log/router.log") is None
    assert compressed_suffix("/var/log/router.log.1.GZ") == ".gz"
    assert compressed_suffix("/var/log/router.log.0.bz2") == ".bz2"
    assert compressed_suffix("/var/log/router.log.xz") == ".xz"
    assert compressed_suffix("/var/log/router.log.zst") == ".zst"


@pytest.mark.parametrize(
    ("suffix", "pack"),
    [(".gz", gzip.compress), (".bz2", bz2.compress), (".xz", lzma.compress)],
)
def test_compressed_files_read_from_an_uncompressed_offset(
    tmp_path: Path, suffix: str, pack: Callable[[bytes], bytes]
) -> None:
    plain = b"first line\nsecond line\nthird\n"
    path = tmp_path / f"router.log.1{suffix}"
    path.write_bytes(pack(plain))
    with open_log(str(path)) as handle:
        assert fingerprint(handle) == sha(b"first line")  # of the CONTENT, as for the live file
    log, lines = scan(path, None, from_start=True)
    assert texts(lines) == ["first line", "second line", "third"]
    assert log.offset == len(plain)  # the uncompressed stream, not the file size
    assert log.size is None
    saved = replace(log.cursor(), offset=len(b"first line\n"))
    log2, lines2 = scan(path, saved)
    assert log2.verdict == "continue" and texts(lines2) == ["second line", "third"]


def test_compressed_first_sight_starts_at_the_uncompressed_end(tmp_path: Path) -> None:
    plain = b"a\nb\npartial"
    path = tmp_path / "router.log.gz"
    path.write_bytes(gzip.compress(plain))
    log, lines = scan(path, None)
    assert lines == [] and log.offset == 4 and log.skipped == 4


def test_compressed_stream_shorter_than_the_offset_is_truncation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log.gz"
    path.write_bytes(gzip.compress(b"first\nsecond\nthird\n"))
    log, _ = scan(path, None)
    path.write_bytes(gzip.compress(b"first\n"))
    saved = replace(log.cursor(), ino=path.stat().st_ino)  # the rewrite may change the ino
    caplog.set_level(logging.INFO, logger="logalert")
    log2, lines = scan(path, saved)
    assert log2.verdict == "truncated" and texts(lines) == ["first"]
    assert "stream ends at 6 < saved offset 19" in caplog.records[-1].getMessage()


def test_zst_needs_the_stdlib_module(tmp_path: Path) -> None:
    path = tmp_path / "router.log.zst"
    path.write_bytes(b"")
    if sys.version_info < (3, 14):
        with pytest.raises(OSError, match=r"\.zst needs Python 3\.14"):
            open_log(str(path))
    else:
        zstd = pytest.importorskip("compression.zstd")
        path.write_bytes(zstd.compress(b"first\nsecond\n"))
        log, lines = scan(path, None, from_start=True)
        assert texts(lines) == ["first", "second"] and log.fingerprint == sha(b"first")


# -- the review's pins: each names the mutation it catches ----------------------------------


def test_identify_device_change_never_masks_truncation_or_rotation() -> None:
    # the dev check is last: swapped before the size or inode check, a device change plus
    # copytruncate would continue from an offset past EOF and lose lines silently
    assert identify(saved_cursor(), 7, 4, 99, sha(b"first"))[0] == "truncated"
    assert identify(saved_cursor(), 8, 4, 5000, sha(b"first"))[0] == "rotated"
    assert identify(saved_cursor(), 7, 4, 5000, sha(b"other"))[0] == "rotated"


def test_rotation_saves_the_new_identity_so_the_next_run_continues(tmp_path: Path) -> None:
    # a cursor that kept the OLD inode or fingerprint would declare rotation every run and
    # re-send the whole file each time
    path = tmp_path / "router.log"
    path.write_bytes(b"first\nsecond\n")
    log, _ = scan(path, None)
    for saved in (replace(log.cursor(), ino=log.ino + 1),
                  replace(log.cursor(), fingerprint=sha(b"a line the old file started with"))):
        log2, lines = scan(path, saved)
        assert log2.verdict == "rotated" and texts(lines) == ["first", "second"]
        current = log2.cursor()
        assert current.ino == path.stat().st_ino and current.fingerprint == sha(b"first")
        log3, lines3 = scan(path, current)
        assert log3.verdict == "continue" and lines3 == []


def test_reader_line_at_the_cap_whose_newline_is_in_the_next_chunk_is_not_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("logalert.cursor._CHUNK", LINE_CAP)
    data = b"E" * LINE_CAP + b"\nnext\n"
    reader = LineReader(io.BytesIO(data), 0)
    assert [(line.text, line.cut) for line in reader] == [("E" * LINE_CAP, False), ("next", False)]
    assert reader.offset == len(data)


def test_reader_holds_an_unterminated_tail_of_exactly_the_cap() -> None:
    lines, offset = read_all(b"x\n" + b"T" * LINE_CAP)
    assert texts(lines) == ["x"] and offset == 2


def test_reader_crlf_line_of_exactly_the_cap_is_not_cut() -> None:
    data = b"E" * LINE_CAP + b"\r\nnext\n"
    lines, offset = read_all(data)
    assert [(line.text, line.cut) for line in lines] == [("E" * LINE_CAP, False), ("next", False)]
    assert offset == len(data)
    lines, offset = read_all(b"E" * (LINE_CAP + 1) + b"\r\n")  # one more byte: cut at the cap
    assert [(line.text, line.cut) for line in lines] == [("E" * LINE_CAP, True), ("E", False)]


def test_reader_nul_only_crlf_line_is_skipped() -> None:
    lines, offset = read_all(NUL * 5 + b"\r\nok\n")
    assert texts(lines) == ["ok"] and offset == 10


def test_reader_second_pass_resumes_at_the_cursor(tmp_path: Path) -> None:
    # a consumer that stops early and iterates again must not get an empty pass because
    # the first generator read a whole chunk ahead of the cursor
    path = tmp_path / "router.log"
    path.write_bytes(b"one\ntwo\nthree\n")
    log = open_log_file(SECTION, str(path), None, from_start=True)
    assert log is not None
    with log:
        assert next(log.lines()).text == "one" and log.offset == 4
        assert texts(list(log.lines())) == ["two", "three"] and log.offset == 14


def test_first_sight_boundaries_around_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"header\n" + b"x" * LINE_CAP)  # a held tail of exactly the cap
    log, _ = scan(path, None)
    assert log.offset == 7  # the newline before the tail is within reach
    with open(path, "ab") as handle:
        handle.write(b"\nnext\n")
    _, lines = scan(path, log.cursor())
    assert texts(lines) == ["x" * LINE_CAP, "next"]
    path.write_bytes(b"x" * LINE_CAP)  # one unterminated line of exactly the cap: held
    assert scan(path, None)[0].offset == 0
    path.write_bytes(b"x" * (LINE_CAP + 1))  # longer than the cap: the end is the boundary
    assert scan(path, None)[0].offset == LINE_CAP + 1


def test_handle_is_closed_when_identification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\n")
    handles: list[Any] = []
    real_open = open_log

    def spy(target: str) -> Any:
        handles.append(real_open(target))
        return handles[-1]

    def boom(handle: Any) -> str:
        raise RuntimeError("fingerprint failed")

    monkeypatch.setattr("logalert.cursor.open_log", spy)
    monkeypatch.setattr("logalert.cursor.fingerprint", boom)
    with pytest.raises(RuntimeError):
        LogFile(SECTION, str(path), None)
    assert handles and handles[0].closed


def test_realpath_is_resolved_from_the_configured_spelling(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    path = tmp_path / "router.log"
    path.write_bytes(b"a\n")
    dotted = tmp_path / "sub" / ".." / "router.log"
    log, _ = scan(dotted, None)
    assert log.path == str(dotted)  # the configured spelling is the key
    assert log.cursor().realpath == os.path.realpath(path)


def test_compressed_stream_at_its_exact_end_stays_continue(tmp_path: Path) -> None:
    # the steady state of every rotated copy: nothing new, no false truncation
    path = tmp_path / "router.log.gz"
    path.write_bytes(gzip.compress(b"first\nsecond\n"))
    log, _ = scan(path, None, from_start=True)
    log2, lines = scan(path, log.cursor())
    assert log2.verdict == "continue" and lines == [] and log2.offset == 13


def test_half_written_or_corrupt_archives_are_oserrors(tmp_path: Path) -> None:
    # gzip(1) still writing a rotated copy, or a damaged one: the callers see one type
    path = tmp_path / "router.log.gz"
    path.write_bytes(gzip.compress(b"first\nsecond\n")[:-8])
    with pytest.raises(OSError, match="incomplete or corrupt compressed stream"):
        open_log_file(SECTION, str(path), None)
    path = tmp_path / "router.log.xz"
    path.write_bytes(b"not an xz stream at all\n")
    with pytest.raises(OSError, match="incomplete or corrupt compressed stream"):
        open_log_file(SECTION, str(path), None)
