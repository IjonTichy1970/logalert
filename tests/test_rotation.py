"""Rotation catch-up: the archive search, the chain, every naming style (issue #8).

Every scenario is built in the test from bytes, with the stdlib gzip / bz2 / lzma modules --
never a checked-in binary fixture -- so it runs on both platforms. The real-logrotate versions
of the same scenarios are in tests/test_logrotate.py and run where logrotate exists (the
sandbox, CI).
"""

import bz2
import errno
import gzip
import logging
import lzma
import os
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from logalert import rotation
from logalert.cursor import BinaryStream, Line, LogFile, open_log
from logalert.rotation import (
    Archive,
    CatchUpSource,
    classify,
    newer,
    open_source,
    plan_catch_up,
    scan_directories,
)
from logalert.state import Cursor

SECTION = "router-disk"
NLB = chr(10).encode()  # a newline as bytes, for fixtures built in a comprehension
NOWHERE = 2**40 + 7  # an inode number no filesystem in a test will hand out


def texts(lines: list[Line]) -> list[str]:
    return [line.text for line in lines]


def run(path: Path, saved: Cursor | None, *, archive_dir: Path | None = None,
        from_start: bool = False) -> tuple[object, list[str], Cursor | None]:
    """One run over the file: (the source, its lines, the cursor to save)."""
    source = open_source(SECTION, str(path), saved, from_start=from_start,
                         archive_dir=str(archive_dir) if archive_dir else None)
    if source is None:
        return None, [], None
    with source:
        lines = texts(list(source.lines()))
        return source, lines, source.cursor()


def seen(path: Path, content: bytes) -> Cursor:
    """A cursor that has read ``content`` as the live file: what the previous run saved."""
    path.write_bytes(content)
    _, _, cursor = run(path, None, from_start=True)
    assert cursor is not None
    return cursor


def append(path: Path, data: bytes) -> None:
    with open(path, "ab") as handle:
        handle.write(data)


def compress_to(target: Path, data: bytes) -> None:
    packers: dict[str, Callable[[bytes], bytes]] = {
        ".gz": gzip.compress, ".bz2": bz2.compress, ".xz": lzma.compress,
    }
    target.write_bytes(packers[target.suffix](data))


def rotate(path: Path, to: Path, *, compress: bool = False) -> None:
    """A rename rotation (mv), optionally compressed into a NEW file as gzip would."""
    if compress:
        compress_to(to, path.read_bytes())
        path.unlink()
    else:
        path.replace(to)


OLD = b"old 1\nold 2\n"
SINCE = b"since 1\nsince 2\n"  # written after the last run, before the rotation
LIVE = b"live 1\n"  # written to the new live file after the rotation


# -- classification and ordering (pure) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "style", "key", "suffix", "compressed"),
    [
        ("router.log.1", "numeric", (-1,), ".1", False),
        ("router.log.10.gz", "numeric", (-10,), ".10", True),
        ("router.log.0.bz2", "numeric", (0,), ".0", True),
        ("router.log-20260914", "dated", (2026, 9, 14, 0, 0, 0, 0), "-20260914", False),
        ("router.log-2026091412.xz", "dated", (2026, 9, 14, 12, 0, 0, 0), "-2026091412", True),
        ("router.log-20260914-1789440820", "dated", (2026, 9, 14, 2, 53, 40, 1789440820),
         "-20260914-1789440820", False),
        ("router.log-1789440820", "dated", (2026, 9, 15, 2, 53, 40, 1789440820),
         "-1789440820", False),  # dateformat -%s: ten digits, but not a date
        ("router.log-2026-09-14", "dated", (2026, 9, 14, 0, 0, 0, 0), "-2026-09-14", False),
        ("router.log-20260914233340", "dated", (2026, 9, 14, 23, 33, 40, 0),
         "-20260914233340", False),
        ("router.log.2026-09-14", "dated", (2026, 9, 14, 0, 0, 0, 0), ".2026-09-14", False),
        ("router.log.2026-09-14_03-04-05.gz", "dated", (2026, 9, 14, 3, 4, 5, 0),
         ".2026-09-14_03-04-05", True),
        ("router.log.20260914T030405", "dated", (2026, 9, 14, 3, 4, 5, 0), ".20260914T030405",
         False),
        ("router.log.20260914", "dated", (2026, 9, 14, 0, 0, 0, 0), ".20260914", False),
        ("router.log.1789440820.gz", "dated", (2026, 9, 15, 2, 53, 40, 1789440820),
         ".1789440820", True),  # mv router.log router.log.$(date +%s)
        ("router.log.12345", "other", (), ".12345", False),  # five digits: no rotate count
        ("router.log.bak", "other", (), ".bak", False),
        ("router.log.GZ", "other", (), "", True),  # compressed in place, no rotation suffix
        ("router.log-old.zst", "other", (), "-old", True),
        # logrotate's extension directive (issue #51): the suffix before the log's own
        ("router.1.log", "numeric", (-1,), ".1", False),
        ("router.1.log.gz", "numeric", (-1,), ".1", True),
        ("router-20260914.log.gz", "dated", (2026, 9, 14, 0, 0, 0, 0), "-20260914", True),
        ("router.2026-09-14.log", "dated", (2026, 9, 14, 0, 0, 0, 0), ".2026-09-14", False),
    ],
)
def test_classify_recognises_the_styles(
    name: str, style: str, key: tuple[int, ...], suffix: str, compressed: bool
) -> None:
    assert classify(name, "router.log") == (style, key, suffix, compressed)


def test_classify_rejects_other_logs_and_the_file_itself() -> None:
    assert classify("router.log", "router.log") is None
    assert classify("router.log2", "router.log") is None  # another log
    assert classify("router.logs.1", "router.log") is None
    assert classify("firewall.log.1", "router.log") is None
    assert classify("router1.log", "router.log") is None  # the extension form needs . or -
    assert classify("routers.1.log", "router.log") is None
    assert classify("router.1.txt", "router.log") is None  # not the log's own extension
    assert classify("messages.1", "messages") == ("numeric", (-1,), ".1", False)  # no ext
    assert classify("worker.2.log", "worker.log") == ("numeric", (-2,), ".2", False)
    # a sibling log, never a copy in the extension form (review: app-error.log was chained
    # whole behind a hand-moved app-old.log)
    assert classify("router-disk.log", "router.log") is None
    assert classify("app-error.log", "app.log") is None
    assert classify("app.log-old", "app.log") == ("other", (), "-old", False)  # classic


def archive(name: str, mtime: float = 0.0, **over: object) -> Archive:
    found = classify(name, "router.log") or ("other", (), "", False)
    style, key, suffix, compressed = found
    fields = dict(path=f"/var/log/{name}", of_log=True, style=style, key=key, suffix=suffix,
                  compressed=compressed, ino=1, dev=1, size=10, mtime=mtime)
    fields.update(over)
    return Archive(**fields)  # type: ignore[arg-type]


def test_numeric_lower_is_newer_whatever_the_mtime_says() -> None:
    assert newer(archive("router.log.1", mtime=1), archive("router.log.2.gz", mtime=9))
    assert newer(archive("router.log.0.bz2", mtime=1), archive("router.log.1.bz2", mtime=9))
    assert not newer(archive("router.log.3", mtime=9), archive("router.log.2", mtime=1))


def test_dated_stamp_orders_across_its_spellings() -> None:
    assert newer(archive("router.log-20260914"), archive("router.log-20260913.gz"))
    assert newer(archive("router.log-2026091412"), archive("router.log-20260914"))
    assert newer(archive("router.log-20260914-1789440820"), archive("router.log-20260914"))
    assert newer(archive("router.log.2026-09-14_00-00-01"), archive("router.log.2026-09-14"))


def test_other_style_and_mixed_styles_fall_back_to_mtime() -> None:
    assert newer(archive("router.log.bak", mtime=9), archive("router.log.old", mtime=1))
    assert newer(archive("router.log-20260914", mtime=9), archive("router.log.1", mtime=1))
    assert not newer(archive("router.log-20260914", mtime=1), archive("router.log.1", mtime=9))


# -- the scenarios, end to end through open_source ------------------------------------------------


def test_continue_and_first_sight_never_search(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    (tmp_path / "router.log.1").write_bytes(b"an archive that must not be read\n")
    path.write_bytes(OLD)
    source, lines, cursor = run(path, None)
    assert isinstance(source, LogFile) and source.verdict == "first-sight" and lines == []
    append(path, SINCE)
    source, lines, cursor = run(path, cursor)
    assert isinstance(source, LogFile) and source.verdict == "continue"
    assert lines == ["since 1", "since 2"]


def test_rename_rotation_is_found_by_inode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(LIVE)
    caplog.set_level(logging.INFO, logger="logalert")
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.verdict == "rotated"
    assert lines == ["since 1", "since 2", "live 1"]
    assert cursor is not None and cursor.ino == path.stat().st_ino and cursor.offset == len(LIVE)
    found = [r.getMessage() for r in caplog.records if "found the saved position" in r.getMessage()]
    assert found == [f"[{SECTION}] {path}: found the saved position by inode; reading "
                     f"router.log.1 from {len(OLD)}, then the live file"]
    # the next run continues from the live file: nothing twice
    append(path, b"live 2\n")
    source, lines, _ = run(path, cursor)
    assert isinstance(source, LogFile) and lines == ["live 2"]


@pytest.mark.parametrize("suffix", [".gz", ".bz2", ".xz"])
def test_rotate_and_compress_is_found_by_content(
    tmp_path: Path, suffix: str, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / f"router.log.1{suffix}", compress=True)
    path.write_bytes(LIVE)
    caplog.set_level(logging.INFO, logger="logalert")
    source, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]
    assert any("by content" in r.getMessage() for r in caplog.records)


def test_owner_criterion_two_rotations_one_compressed(tmp_path: Path) -> None:
    """Rotated twice between two runs, once with gzip: every line once, in order."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1")  # rotation 1
    path.write_bytes(b"middle 1\nmiddle 2\n")
    rotate(tmp_path / "router.log.1", tmp_path / "router.log.2.gz", compress=True)  # rotation 2
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(LIVE)
    _, lines, cursor = run(path, saved)
    assert lines == ["since 1", "since 2", "middle 1", "middle 2", "live 1"]
    assert cursor is not None and cursor.ino == path.stat().st_ino
    _, again, _ = run(path, cursor)
    assert again == []


def test_delaycompress_layout(tmp_path: Path) -> None:
    # .1 plain (our old inode), .2.gz compressed: found by inode, .1 is the match, no chain
    path = tmp_path / "router.log"
    (tmp_path / "router.log.2.gz").write_bytes(gzip.compress(b"ancient\n"))
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]


def test_copytruncate_plain_and_compressed(tmp_path: Path) -> None:
    for compress in (False, True):
        path = tmp_path / ("c" if compress else "p") / "router.log"
        path.parent.mkdir()
        saved = seen(path, OLD)
        append(path, SINCE)
        copy = path.with_name("router.log.1.gz" if compress else "router.log.1")
        if compress:
            compress_to(copy, path.read_bytes())
        else:
            copy.write_bytes(path.read_bytes())
        path.write_bytes(b"")  # truncate in place: the inode stays
        assert path.stat().st_ino == saved.ino
        append(path, LIVE)
        source, lines, cursor = run(path, saved)
        # an O_APPEND writer gives the live file a NEW first line, so the identity rules call
        # this "rotated" (same inode, different first line) -- and the copy is found the same way
        assert isinstance(source, CatchUpSource) and source.verdict == "rotated"
        assert source.plan.stage == "content"
        assert lines == ["since 1", "since 2", "live 1"], compress
        assert cursor is not None and cursor.offset == len(LIVE)


def test_copytruncate_under_a_writer_without_o_append(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # the writer keeps its offset, so the truncated file regrows with a NUL hole where the old
    # lines were: its size passes the offset check, and only the vanished first line says
    # what happened. The lines in the copy must still arrive.
    nul = bytes([0])
    big = b"old line\n" * 600  # a real hole is the old file's size: beyond the fingerprint cap
    path = tmp_path / "router.log"
    saved = seen(path, big)
    append(path, SINCE)
    (tmp_path / "router.log.1").write_bytes(path.read_bytes())
    path.write_bytes(nul * len(big + SINCE) + LIVE)
    assert path.stat().st_ino == saved.ino and path.stat().st_size > saved.offset
    caplog.set_level(logging.INFO, logger="logalert")
    source, lines, cursor = run(path, saved)
    # the hole is skipped by the fingerprint too, so the live file reads as a NEW first
    # line (rotated); the copy is found by content either way
    assert isinstance(source, CatchUpSource) and source.verdict == "rotated"
    assert source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]
    assert cursor is not None and cursor.offset == len(big + SINCE + LIVE)
    messages = [r.getMessage() for r in caplog.records]
    assert any(f"skipped {len(big + SINCE)} NUL bytes" in m for m in messages)
    # and the NEXT copytruncate must still be identifiable: the fingerprint is the first
    # line AFTER the hole, so the copy of a holed file matches the holed live file's past
    (tmp_path / "router.log.1").replace(tmp_path / "router.log.2")
    append(path, b"more\n")
    (tmp_path / "router.log.1").write_bytes(path.read_bytes())
    path.write_bytes(nul * (len(big + SINCE + LIVE) + 5) + b"newest\n")
    source, lines, _ = run(path, cursor)
    assert isinstance(source, CatchUpSource) and lines == ["more", "newest"]
    # a hole smaller than the fingerprint cap reads the same way
    small = tmp_path / "small" / "router.log"
    small.parent.mkdir()
    saved = seen(small, OLD)
    append(small, SINCE)
    small.with_name("router.log.1").write_bytes(small.read_bytes())
    small.write_bytes(nul * len(OLD + SINCE) + LIVE)
    source, lines, _ = run(small, saved)
    assert isinstance(source, CatchUpSource) and source.verdict == "rotated"
    assert lines == ["since 1", "since 2", "live 1"]


def test_dateext_with_and_without_a_time_part(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log-20260913", compress=False)
    path.write_bytes(b"middle\n")
    rotate(path, tmp_path / "router.log-2026091412.gz", compress=True)
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]


def test_timed_rotating_file_handler_style(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.2026-09-13")
    path.write_bytes(b"middle\n")
    rotate(path, tmp_path / "router.log.2026-09-14_00-00-00")
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]


def test_newsyslog_numbered_bz2_zero_is_newest(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.0.bz2", compress=True)
    path.write_bytes(b"middle\n")
    (tmp_path / "router.log.0.bz2").replace(tmp_path / "router.log.1.bz2")
    rotate(path, tmp_path / "router.log.0.bz2", compress=True)
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]


def test_savelog_zero_plain_beside_one_gz(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.0")
    path.write_bytes(b"middle\n")
    rotate(tmp_path / "router.log.0", tmp_path / "router.log.1.gz", compress=True)
    rotate(path, tmp_path / "router.log.0")
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]


def test_archive_dir_is_searched(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    old = tmp_path / "old"
    old.mkdir()
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, old / "router.log.1.gz", compress=True)
    path.write_bytes(LIVE)
    _, without, _ = run(path, saved)
    assert without == ["live 1"]  # not searched: not configured
    _, lines, _ = run(path, saved, archive_dir=old)
    assert lines == ["since 1", "since 2", "live 1"]


def test_any_name_is_found_by_inode(tmp_path: Path) -> None:
    # a rename to a name no style recognises: stage 1 does not care about names
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "keep-this-one.txt")
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]


def test_reused_inode_with_another_first_line_is_not_the_copy(tmp_path: Path) -> None:
    # stage 1 needs the first line too: an inode number alone is not an identity (ext4 reuse)
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1.gz", compress=True)
    impostor = tmp_path / "router.log.2"
    impostor.write_bytes(b"a different log entirely\n")
    path.write_bytes(LIVE)
    fabricated = replace(saved, ino=impostor.stat().st_ino, dev=impostor.stat().st_dev)
    _, lines, _ = run(path, fabricated)
    assert lines == ["since 1", "since 2", "live 1"]  # found by content in .1.gz, not by inode


def test_content_stage_needs_a_real_first_line(tmp_path: Path) -> None:
    # an empty archive has no first line; it must not be "matched" by a None fingerprint
    path = tmp_path / "router.log"
    saved = seen(path, b"")
    assert saved.fingerprint is None and saved.offset == 0
    (tmp_path / "router.log.1").write_bytes(b"")
    path.write_bytes(LIVE)
    source, lines, _ = run(path, replace(saved, ino=NOWHERE))
    assert isinstance(source, CatchUpSource) and source.plan.match is None
    assert lines == ["live 1"]


def test_compression_in_progress_prefers_the_uncompressed_copy(tmp_path: Path) -> None:
    # a COPY (new inode), so the choice is stage 2's; the cut .1.gz is what gzip leaves
    # half-way through, and taking it would be an EOFError instead of the lines
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    (tmp_path / "router.log.1").write_bytes(path.read_bytes())
    (tmp_path / "router.log.1.gz").write_bytes(gzip.compress(OLD + b"half\n")[:-6])
    path.write_bytes(b"")
    append(path, LIVE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert source.plan.match is not None and source.plan.match.path.endswith("router.log.1")
    assert lines == ["since 1", "since 2", "live 1"]
    # two compressed files with one suffix are not a pair: both stay candidates
    (tmp_path / "router.log.1").unlink()
    compress_to(tmp_path / "router.log.1.bz2", OLD + SINCE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource)
    assert source.plan.match is not None and source.plan.match.path.endswith("router.log.1.bz2")
    assert lines == ["since 1", "since 2", "live 1"]


def test_other_style_files_never_join_a_recognised_chain(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1.gz", compress=True)
    bak = tmp_path / "router.log.bak"
    bak.write_bytes(b"an operator's copy\n")
    os.utime(bak, (2_000_000_000, 2_000_000_000))  # newer than anything
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]


def test_unrecognised_style_orders_by_mtime_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.older")
    os.utime(tmp_path / "router.log.older", (1_700_000_000, 1_700_000_000))
    path.write_bytes(b"middle\n")
    rotate(path, tmp_path / "router.log.newer")
    os.utime(tmp_path / "router.log.newer", (1_700_000_100, 1_700_000_100))
    path.write_bytes(LIVE)
    caplog.set_level(logging.INFO, logger="logalert")
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]
    assert any("ordering by mtime" in r.getMessage() for r in caplog.records)


def test_cut_archive_in_the_chain_yields_what_it_has_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.2.gz", compress=True)
    whole = gzip.compress(b"middle 1\nmiddle 2\n")
    (tmp_path / "router.log.1.gz").write_bytes(whole[:-8])  # the trailer is missing
    path.write_bytes(LIVE)
    caplog.set_level(logging.WARNING, logger="logalert")
    _, lines, cursor = run(path, saved)
    assert lines == ["since 1", "since 2", "middle 1", "middle 2", "live 1"]
    assert cursor is not None and cursor.offset == len(LIVE)
    warned = [r.getMessage() for r in caplog.records if "could not be read past" in r.getMessage()]
    assert len(warned) == 1 and "router.log.1.gz" in warned[0]
    assert "past offset 18" in warned[0]  # both middle lines were handed over first


def test_gz_named_file_that_is_not_gzip_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.2.gz", compress=True)
    (tmp_path / "router.log.1.gz").write_bytes(b"not gzip at all\n")
    path.write_bytes(LIVE)
    caplog.set_level(logging.WARNING, logger="logalert")
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    # met in the chain ("could not be read past offset 0"), or -- when the filesystem handed
    # the freed inode to the bogus file -- probed and skipped by the inode stage first
    assert len(warned) == 1 and "router.log.1.gz" in warned[0]
    assert "Not a gzipped" in warned[0]


@pytest.mark.parametrize("name", ["router.log.1", "router.log.1.gz"])
def test_archive_shorter_than_the_offset_is_not_ours(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD + SINCE)
    if name.endswith(".gz"):
        (tmp_path / name).write_bytes(gzip.compress(OLD))  # same first line, too short
    else:
        (tmp_path / name).write_bytes(OLD)
    path.write_bytes(LIVE)
    caplog.set_level(logging.WARNING, logger="logalert")
    source, lines, _ = run(path, replace(saved, ino=NOWHERE))
    assert isinstance(source, CatchUpSource) and source.plan.match is None
    assert lines == ["live 1"]
    assert any("no rotated copy holds the saved position" in r.getMessage()
               for r in caplog.records)


def test_nothing_matches_warns_once_and_reads_the_live_file_from_zero(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    path.unlink()
    path.write_bytes(LIVE)  # rotate 0: the old file is simply gone
    caplog.set_level(logging.WARNING, logger="logalert")
    source, lines, _ = run(path, saved)
    assert lines == ["live 1"]
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert f"inode {saved.ino}, offset {saved.offset}" in warned[0]
    assert "rotate 0" in warned[0] and str(tmp_path) in warned[0]
    # the four guesses in their order, and nothing the run did not meet before them
    assert ("likely causes: rotate 0, an olddir or -a elsewhere (set archive_dir), "
            "unsupported compression, the archive aged out;") in warned[0]
    assert "first-line hash " + str(saved.fingerprint)[:12] in warned[0]
    assert "(set archive_dir)" in warned[0]
    assert warned[0].endswith("reading the live file from the beginning, and lines written "
                              "between the last run and the last rotation are lost")


def test_nocreate_absent_live_file_reads_the_archive_now(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1")
    source, lines, cursor = run(path, saved)  # the live file does not exist
    assert isinstance(source, CatchUpSource) and source.verdict == "absent"
    assert lines == ["since 1", "since 2"]
    assert cursor is not None
    assert cursor.ino == (tmp_path / "router.log.1").stat().st_ino
    assert cursor.offset == len(OLD + SINCE)
    # the writer comes back; the next run resolves the archive by inode and reads it no further
    path.write_bytes(LIVE)
    source, lines, cursor2 = run(path, cursor)
    assert isinstance(source, CatchUpSource) and lines == ["live 1"]
    assert source.plan.stage == "inode"  # the parked cursor resolves; no false warning
    assert cursor2 is not None and cursor2.ino == path.stat().st_ino
    # and when the archive was compressed in between: resolved by content, nothing twice
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1")
    _, lines, parked = run(path, saved)
    assert lines == ["since 1", "since 2"] and parked is not None
    rotate(tmp_path / "router.log.1", tmp_path / "router.log.1.gz", compress=True)
    path.write_bytes(LIVE)
    source, lines, _ = run(path, parked)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["live 1"]


def test_absent_with_no_archive_is_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    path.unlink()
    caplog.set_level(logging.INFO, logger="logalert")
    assert run(path, saved) == (None, [], None)
    assert any("absent this run, and no rotated copy" in r.getMessage() for r in caplog.records)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []  # not lost
    assert run(path, None) == (None, [], None)  # no cursor: nothing to look for


def test_repointed_symlink_is_logged_and_the_search_follows_the_target(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    target = real / "router.log"
    link = tmp_path / "router.log"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating a symlink needs a privilege here; runs in the sandbox and on CI")
    saved = seen(link, OLD)
    assert saved.realpath == os.path.realpath(target)
    append(link, SINCE)
    rotate(target, real / "router.log.1.gz", compress=True)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "router.log").write_bytes(LIVE)
    link.unlink()
    link.symlink_to(other / "router.log")
    caplog.set_level(logging.INFO, logger="logalert")
    source, lines, cursor = run(link, saved)
    assert lines == ["since 1", "since 2", "live 1"]  # the OLD target's directory is searched
    assert isinstance(source, CatchUpSource) and str(real) in source.plan.searched
    assert any("the link now points at" in r.getMessage() for r in caplog.records)
    assert cursor is not None and cursor.realpath == os.path.realpath(other / "router.log")


def test_scan_ignores_directories_devices_and_unreadable_dirs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "router.log.1").mkdir()  # a directory with an archive's name
    (tmp_path / "router.log.2").write_bytes(b"x\n")
    everything, archives, problems = scan_directories([str(tmp_path), str(tmp_path / "nope")],
                                                    "router.log")
    assert [os.path.basename(a.path) for a in everything] == ["router.log.2"]
    assert [a.suffix for a in archives] == [".2"]
    assert len(problems) == 1 and problems[0][0].startswith("cannot list ")  # logged by the plan
    assert problems[0][1:] == (errno.ENOENT, str(tmp_path / "nope"))  # a permission only
    assert not caplog.records  # once, by the caller (issue #38), not here


def test_plan_excludes_the_live_file_itself(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    st = path.stat()
    plan = plan_catch_up(SECTION, str(path), saved, "rotated", archive_dir=None,
                         exclude=(st.st_ino, st.st_dev))
    assert plan.match is None and plan.searched == [str(tmp_path)]


def test_zst_archive_is_skipped_where_unsupported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    if sys.version_info >= (3, 14):
        pytest.skip("3.14 reads .zst; the unsupported branch is measured on 3.11-3.13")
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1.zst", compress=False)  # the bytes do not matter here
    path.write_bytes(LIVE)
    caplog.set_level(logging.WARNING, logger="logalert")
    _, lines, _ = run(path, saved)
    assert lines == ["live 1"]
    assert any(".zst needs Python 3.14" in r.getMessage() for r in caplog.records)


# -- the review's pins -----------------------------------------------------------------------------


def test_content_stage_needs_a_complete_first_line_even_when_long_enough(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    (tmp_path / "router.log.1").write_bytes(b"x" * 40)  # longer than the offset, no newline
    path.write_bytes(LIVE)
    source, lines, _ = run(path, replace(saved, ino=NOWHERE))
    assert isinstance(source, CatchUpSource) and source.plan.match is None
    assert lines == ["live 1"]


def test_quiet_rotation_matches_an_archive_exactly_as_long_as_the_offset(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # nothing written since the last run: the copy's length equals the offset, and that
    # is a match (>=), not a too-short archive
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(LIVE)
    caplog.set_level(logging.INFO, logger="logalert")
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "inode"
    assert lines == ["live 1"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    rotate(tmp_path / "router.log.1", tmp_path / "router.log.1.gz", compress=True)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["live 1"]


def test_three_rotations_read_the_chain_in_order(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.3.gz", compress=True)
    path.write_bytes(b"second\n")
    rotate(path, tmp_path / "router.log.2.gz", compress=True)
    path.write_bytes(b"third\n")
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(LIVE)
    caplog.set_level(logging.INFO, logger="logalert")
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource)
    assert [os.path.basename(a.path) for a in source.plan.chain] == ["router.log.2.gz",
                                                                    "router.log.1"]
    assert lines == ["since 1", "since 2", "second", "third", "live 1"]
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.endswith(f"reading router.log.3.gz from {len(OLD)}, then router.log.2.gz, "
                          f"then router.log.1, then the live file") for m in messages)


def test_banner_logs_take_the_oldest_copy_written_after_the_last_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # every generation starts with the same line: the first line cannot tell the copies
    # apart, but the ones written after the last run can only be ours or newer
    banner = b"=== daemon started ===\n"
    path = tmp_path / "router.log"
    compress_to(tmp_path / "router.log.3.gz", banner + b"ancient 1\nancient 2\nancient 3\n")
    os.utime(tmp_path / "router.log.3.gz", (1_700_000_000, 1_700_000_000))  # before the last run
    saved = seen(path, banner + OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.2.gz", compress=True)  # ours
    path.write_bytes(banner + b"middle 1\nmiddle 2\n")
    rotate(path, tmp_path / "router.log.1")  # newer, same first line, long enough
    path.write_bytes(banner + LIVE)
    caplog.set_level(logging.WARNING, logger="logalert")
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.match is not None
    assert source.plan.match.path.endswith("router.log.2.gz")
    assert lines == ["since 1", "since 2", "=== daemon started ===", "middle 1", "middle 2",
                     "=== daemon started ===", "live 1"]
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_banner_logs_fall_back_to_the_newest_older_copy_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    banner = b"=== daemon started ===\n"
    path = tmp_path / "router.log"
    saved = seen(path, banner + OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.2.gz", compress=True)
    compress_to(tmp_path / "router.log.1.gz", banner + b"newer 1\nnewer 2\n")
    for name, stamp in (("router.log.2.gz", 1_700_000_000), ("router.log.1.gz", 1_700_000_100)):
        os.utime(tmp_path / name, (stamp, stamp))  # restored from a backup: old mtimes
    path.write_bytes(banner + LIVE)
    caplog.set_level(logging.WARNING, logger="logalert")
    source, lines, _ = run(path, replace(saved, ino=NOWHERE))  # ext4 may reuse the inode
    assert isinstance(source, CatchUpSource) and source.plan.match is not None
    assert source.plan.match.path.endswith("router.log.1.gz")  # the newest fitting: a guess
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1 and "which is a guess" in warned[0]


def test_corrupt_gzip_body_is_skipped_not_fatal(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # a crash during compression leaves a valid header over zero-filled blocks: zlib.error,
    # which is not an OSError
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.2.gz", compress=True)
    (tmp_path / "router.log.1.gz").write_bytes(gzip.compress(b"")[:10] + bytes(4096))
    path.write_bytes(LIVE)
    caplog.set_level(logging.WARNING, logger="logalert")
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]
    assert any("router.log.1.gz" in r.getMessage() for r in caplog.records)


def test_no_saved_first_line_never_takes_another_logs_archive_by_inode(tmp_path: Path) -> None:
    # the live file was empty at the last run (fingerprint None); its inode was freed by the
    # rotation and handed to another log's archive
    path = tmp_path / "router.log"
    saved = seen(path, b"")
    assert saved.fingerprint is None
    other = tmp_path / "other.log.1.gz"
    compress_to(other, b"other log line 1\nother log line 2\n")
    path.write_bytes(LIVE)
    fabricated = replace(saved, ino=other.stat().st_ino, dev=other.stat().st_dev)
    source, lines, _ = run(path, fabricated)
    assert isinstance(source, CatchUpSource) and source.plan.match is None
    assert lines == ["live 1"]
    # but an archive NAMED as ours holding that inode is taken (the file was empty: read all)
    ours = tmp_path / "router.log.1"
    ours.write_bytes(b"first after empty\n")
    fabricated = replace(saved, ino=ours.stat().st_ino, dev=ours.stat().st_dev)
    source, lines, _ = run(path, fabricated)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "inode"
    assert lines == ["first after empty", "live 1"]


def test_inode_match_must_not_have_lost_its_first_line(tmp_path: Path) -> None:
    # a copy of our file cannot have lost its first line: a same-inode file with none is
    # something else that got the number
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.1.gz", compress=True)
    headless = tmp_path / "router.db"
    headless.write_bytes(b"x" * 765)  # no newline: fingerprint None
    path.write_bytes(LIVE)
    fabricated = replace(saved, ino=headless.stat().st_ino, dev=headless.stat().st_dev)
    source, lines, _ = run(path, fabricated)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]


def test_hand_rolled_date_and_epoch_suffixes_order_ascending(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.20260913")
    path.write_bytes(b"middle\n")
    rotate(path, tmp_path / "router.log.20260914")
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]
    epoch = tmp_path / "e" / "router.log"
    epoch.parent.mkdir()
    saved = seen(epoch, OLD)
    append(epoch, SINCE)
    rotate(epoch, epoch.with_name("router.log.1789440820"))
    epoch.write_bytes(b"middle\n")
    rotate(epoch, epoch.with_name("router.log.1789440900.gz"), compress=True)
    epoch.write_bytes(LIVE)
    _, lines, _ = run(epoch, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]


def test_unnamed_match_never_admits_other_style_files_to_the_chain(tmp_path: Path) -> None:
    # the copy was renamed to something that is not an archive name: found by inode, but a
    # .bak with a fresh mtime is still not a chain member
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "keep-this-one.txt")
    bak = tmp_path / "router.log.bak"
    bak.write_bytes(b"an operator's copy\n")
    os.utime(bak, (2_000_000_000, 2_000_000_000))
    path.write_bytes(LIVE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.chain == []
    assert lines == ["since 1", "since 2", "live 1"]


def test_stale_copy_in_the_live_directory_does_not_shadow_the_archive_dir(tmp_path: Path) -> None:
    # a router.log.1 left over from before olddir was configured, beside old/router.log.1.gz
    path = tmp_path / "router.log"
    old = tmp_path / "old"
    old.mkdir()
    (tmp_path / "router.log.1").write_bytes(b"ancient, from before olddir\n")
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, old / "router.log.1.gz", compress=True)
    path.write_bytes(LIVE)
    source, lines, _ = run(path, saved, archive_dir=old)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]


def test_hard_link_to_an_archive_keeps_the_better_name(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.2.gz", compress=True)
    path.write_bytes(b"middle\n")
    rotate(path, tmp_path / "router.log.1")
    try:
        os.link(tmp_path / "router.log.1", tmp_path / "router.log-copy")  # sorts first
    except OSError:
        pytest.skip("hard links are not available here; runs in the sandbox and on CI")
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]


def test_symlinked_entries_are_never_candidates(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.log.2.gz", compress=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "foreign.log").write_bytes(b"not this log\n")
    try:
        (tmp_path / "router.log.1").symlink_to(elsewhere / "foreign.log")
    except OSError:
        pytest.skip("creating a symlink needs a privilege here; runs in the sandbox and on CI")
    path.write_bytes(LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]


def test_truncation_proper_before_any_new_write(tmp_path: Path) -> None:
    # copytruncate, then a run before the writer wrote anything: an empty live file
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    (tmp_path / "router.log.1").write_bytes(path.read_bytes())
    path.write_bytes(b"")
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.verdict == "truncated"
    assert source.plan.stage == "content" and lines == ["since 1", "since 2"]
    assert cursor is not None and cursor.offset == 0 and cursor.ino == path.stat().st_ino


def test_a_consumer_that_stops_mid_chain_gets_a_resumable_cursor(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, b"since 1\nsince 2\nsince 3\n")
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(LIVE)
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    with source:
        assert source.cursor() == saved  # nothing read yet
        it = source.lines()
        assert next(it).text == "since 1"
        stopped = source.cursor()
    assert stopped.ino == (tmp_path / "router.log.1").stat().st_ino
    assert stopped.offset == len(OLD) + len(b"since 1\n")
    _, rest, cursor = run(path, stopped)  # the next run resumes inside the archive
    assert rest == ["since 2", "since 3", "live 1"]
    assert cursor is not None and cursor.ino == path.stat().st_ino


def test_non_stem_gz_carries_its_compressed_flag(tmp_path: Path) -> None:
    (tmp_path / "keep.gz").write_bytes(gzip.compress(b"x\n"))
    everything, _, _ = scan_directories([str(tmp_path)], "router.log")
    assert [a.compressed for a in everything if a.path.endswith("keep.gz")] == [True]


# -- issue #46: the matched archive is decompressed once, the hook answered from its tail -------


def test_the_matched_archive_is_read_in_one_pass_with_the_context_from_its_tail(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured before this change: the content stage seeks to the saved offset on one
    handle, the segment opens a second and seeks again, and the context hook opens a third
    and seeks back -- 2.9 passes over a 550 KB archive to read a 30 MB tail. Now the
    verified handle travels and the hook is answered from the tail kept on the way: one."""
    counter = {"bytes": 0}
    real_open = open

    def counting_open(fd: object, *args: object, **kwargs: object) -> object:
        handle = real_open(fd, *args, **kwargs)  # type: ignore[call-overload]
        if isinstance(fd, int):
            original1, original = handle.read1, handle.read  # gzip reads its input via read()

            def read1(n: int = -1) -> bytes:
                data: bytes = original1(n)
                counter["bytes"] += len(data)
                return data

            def read(n: int = -1) -> bytes:
                data: bytes = original(n)
                counter["bytes"] += len(data)
                return data

            handle.read1, handle.read = read1, read
        return handle

    monkeypatch.setattr("logalert.cursor.open", counting_open, raising=False)
    path = tmp_path / "router.log"
    old = b"".join(b"line %d %s" % (i, os.urandom(60).hex().encode()) + NLB
                   for i in range(5_000))
    saved = seen(path, old)
    append(path, b"since 1" + NLB + b"since 2" + NLB)
    rotate(path, tmp_path / "router.log.1.gz", compress=True)
    path.write_bytes(b"live 1" + NLB)
    size = (tmp_path / "router.log.1.gz").stat().st_size
    counter["bytes"] = 0
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    with source:
        assert source.plan.verified is not None  # the content stage's handle travelled
        lines = texts(list(source.lines()))
        context = source.context_before(2)
    assert lines == ["since 1", "since 2", "live 1"]
    assert [line.number for line in context] == [4_999, 5_000]
    assert texts(context)[-1].startswith("line 4999 ")
    assert counter["bytes"] <= size + 128 * 1024 + 65536  # one pass, plus gzip's read-ahead
    assert source.plan.verified.handle.closed  # closed with the source


def test_a_context_window_wider_than_the_kept_tail_falls_back_to_a_second_read(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "router.log"
    old = b"".join(b"x" * 39 + NLB for _ in range(10_000))  # 400 KB: past the 256 KiB tail
    saved = seen(path, old)
    append(path, b"since 1" + NLB)
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(b"live 1" + NLB)
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    caplog.set_level(logging.DEBUG, logger="logalert.rotation")
    with source:
        assert texts(list(source.lines())) == ["since 1", "live 1"]
        near = source.context_before(3)  # inside the tail
        far = source.context_before(500)  # 500 * 2001 + 65536 > the tail: the second read
    assert [line.number for line in near] == [9_998, 9_999, 10_000]
    assert len(far) == 500 and far[-1].number == 10_000 and far[0].number == 9_501
    assert any("wider than the kept tail" in r.getMessage() for r in caplog.records)


def test_an_archive_cut_before_the_saved_offset_is_skipped_and_leaks_no_handle(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The confirming read now happens inside the content stage (issue #46): a stream error
    there -- a .gz truncated before the saved offset, gzip(1) still writing it -- must be the
    same WARNING as an unreadable archive, no match, and the handle closed (review)."""
    path = tmp_path / "router.log"
    old = b"".join(b"line %d %s" % (i, os.urandom(40).hex().encode()) + NLB for i in range(3_000))
    saved = seen(path, old)
    append(path, b"since 1" + NLB)
    rotate(path, tmp_path / "router.log.1.gz", compress=True)
    archive = tmp_path / "router.log.1.gz"
    archive.write_bytes(archive.read_bytes()[: archive.stat().st_size // 2])  # cut mid-way
    path.write_bytes(b"live 1" + NLB)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    import gc
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        source = open_source(SECTION, str(path), saved)
        assert isinstance(source, CatchUpSource) and source.plan.match is None
        with source:
            assert texts(list(source.lines())) == ["live 1"]
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)], caught
    assert any("could not be read" in r.getMessage() for r in caplog.records)
    assert any("no rotated copy holds the saved position" in r.getMessage()
               for r in caplog.records)


# -- issue #32: a rotation that lands during the catch-up -----------------------------------------


def _two_rotations(tmp_path: Path) -> tuple[Path, Cursor]:
    """A log seen at OLD, then rotated twice between runs: .2 holds OLD + 'since 1', .1 holds
    'middle 1', the live file 'live 1'. The catch-up's plan: match .2, chain [.1], then live."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, b"since 1" + NLB)
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(b"middle 1" + NLB)
    (tmp_path / "router.log.1").replace(tmp_path / "router.log.2")
    rotate(path, tmp_path / "router.log.1")
    path.write_bytes(b"live 1" + NLB)
    return path, saved


def _shift(tmp_path: Path, deepest: int, fresh: bytes) -> None:
    """logrotate's shift: .N -> .N+1 down to .1 -> .2, the live file -> .1, a fresh live."""
    for n in range(deepest, 0, -1):
        (tmp_path / f"router.log.{n}").replace(tmp_path / f"router.log.{n + 1}")
    (tmp_path / "router.log").replace(tmp_path / "router.log.1")
    (tmp_path / "router.log").write_bytes(fresh + NLB)


def _third_rotation(tmp_path: Path) -> None:
    """The shift once more after _two_rotations: .2 -> .3, .1 -> .2, live -> .1, 'live 2'."""
    _shift(tmp_path, 2, b"live 2")


def test_a_rotation_after_the_plan_stops_the_stream_and_the_next_run_resumes(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Measured in the audit: the old answer ('renamed under us; skipped this run') went on to
    the live file, landed the cursor there, and 'middle 1' was lost for good. Now the stream
    stops at the renamed member, the cursor is the last archive read, and the next run reads
    everything once."""
    if sys.platform == "win32":
        pytest.skip("renaming a log under its open handle is refused on Windows; runs in "
                    "the sandbox and on CI")
    path, saved = _two_rotations(tmp_path)
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    _third_rotation(tmp_path)  # between the plan and the chain member's open
    with source:
        first = texts(list(source.lines()))
        stopped = source.cursor()
    assert first == ["since 1"]  # the match's tail, then the stop
    assert stopped.ino == (tmp_path / "router.log.3").stat().st_ino  # the match, at its end
    assert stopped.offset == len(OLD) + len(b"since 1" + NLB)
    assert any("router.log.1 was renamed or removed under us (a rotation during the run); "
               "stopping here, the next run resumes after router.log.2 (the names are from "
               "before the rotation)" in r.getMessage() for r in caplog.records)
    _, rest, cursor = run(path, stopped)
    assert rest == ["middle 1", "live 1", "live 2"]  # every line once, none twice
    assert cursor is not None and cursor.ino == path.stat().st_ino


def test_a_rotation_mid_chain_lands_the_cursor_on_the_last_archive_read(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Three rotations behind, the rotation lands after the first chain member was read and
    before the second is opened: the cursor is that member's end (not the match's, not the
    live file's), and the next run carries on from it."""
    if sys.platform == "win32":
        pytest.skip("renaming a log under its open handle is refused on Windows; runs in "
                    "the sandbox and on CI")
    path, saved = _two_rotations(tmp_path)
    _third_rotation(tmp_path)  # .3 = OLD + since 1, .2 = middle 1, .1 = live 1, live = live 2
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    with source:
        stream = source.lines()
        first = [next(stream).text, next(stream).text]  # .3's tail, then .2 whole
        _shift(tmp_path, 3, b"live 3")  # .2 is read to its end; .1 is not open yet
        first += texts(list(stream))
        stopped = source.cursor()
    assert first == ["since 1", "middle 1"]
    assert stopped.ino == (tmp_path / "router.log.3").stat().st_ino  # 'middle 1', at its end
    assert stopped.offset == len(b"middle 1" + NLB)
    assert any("the next run resumes after router.log.2" in r.getMessage()
               for r in caplog.records)  # the name the plan saw
    _, rest, cursor = run(path, stopped)
    assert rest == ["live 1", "live 2", "live 3"]  # every line once, none twice
    assert cursor is not None and cursor.ino == path.stat().st_ino


def test_a_chain_member_compressed_away_after_the_plan_stops_the_stream(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """logrotate's compress step: the copy the plan listed as router.log.1 is gzipped into
    router.log.1.gz and unlinked before its turn. Skipping it lost its lines for good (the
    old 'continuing with the next file'); now the run stops and the next one reads the
    .gz. Real on every platform: the member is not open when it goes."""
    path, saved = _two_rotations(tmp_path)
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    compress_to(tmp_path / "router.log.1.gz", (tmp_path / "router.log.1").read_bytes())
    (tmp_path / "router.log.1").unlink()
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    with source:
        got = texts(list(source.lines()))
        stopped = source.cursor()
    assert got == ["since 1"]
    assert stopped.ino == (tmp_path / "router.log.2").stat().st_ino
    assert any("router.log.1 was renamed or removed under us" in r.getMessage()
               for r in caplog.records)
    assert not any("could not be read" in r.getMessage() for r in caplog.records)
    _, rest, cursor = run(path, stopped)
    assert rest == ["middle 1", "live 1"]  # from the .gz, then the live file: once each
    assert cursor is not None and cursor.ino == path.stat().st_ino


def test_an_impostor_at_a_chain_members_name_is_never_read(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """TEST-7: a different file under the name the plan saw -- its lines are not this log's.
    The (ino, dev) guard in Segment.lines is what refuses it; a mutant dropping the guard
    mails the impostor's lines as the log's."""
    path, saved = _two_rotations(tmp_path)
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    (tmp_path / "router.log.1").replace(tmp_path / "elsewhere")  # the real member moved
    (tmp_path / "router.log.1").write_bytes(b"IMPOSTOR" + NLB)  # a new inode under its name
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    with source:
        got = texts(list(source.lines()))
        stopped = source.cursor()
    assert got == ["since 1"] and "IMPOSTOR" not in got
    assert stopped.ino == (tmp_path / "router.log.2").stat().st_ino
    assert any("was renamed or removed under us" in r.getMessage() for r in caplog.records)


def test_a_rotation_between_the_scan_and_the_content_stage_is_found_by_a_rescan(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan scanned the directory, then the rotation moved every name before the first
    candidate was opened: every candidate was 'renamed under us', the plan found nothing, and
    the live file was read from 0 under a warning naming four wrong causes -- the matched
    archive's tail lost. Now the plan scans again, once."""
    if sys.platform == "win32":
        pytest.skip("renaming a log under its open handle is refused on Windows; runs in "
                    "the sandbox and on CI")
    path, saved = _two_rotations(tmp_path)
    real_scan = rotation.scan_directories
    calls = {"n": 0}

    def scan_then_rotate(directories: list[str], base: str) -> object:
        found = real_scan(directories, base)
        calls["n"] += 1
        if calls["n"] == 1:
            _third_rotation(tmp_path)  # after the scan, before any candidate's open
        return found

    monkeypatch.setattr(rotation, "scan_directories", scan_then_rotate)
    caplog.set_level(logging.INFO, logger="logalert")
    _, lines, cursor = run(path, saved)
    assert calls["n"] == 2  # scanned twice: the rescan
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)  # recovered quietly
    # the live file this run opened before the plan is 'router.log.1' by now; its handle
    # is read to the end, and the new live file is the next run's
    assert lines == ["since 1", "middle 1", "live 1"]
    assert cursor is not None and cursor.ino == (tmp_path / "router.log.1").stat().st_ino
    assert any("scanning again" in r.getMessage() for r in caplog.records)
    assert not any("no rotated copy holds" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    _, rest, cursor = run(path, cursor)
    assert rest == ["live 2"]  # every line once across the two runs
    assert cursor is not None and cursor.ino == path.stat().st_ino


def test_a_second_rename_inside_one_plan_reaches_the_warning_with_its_cause(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rescan is once: renamed under again, the plan gives up as before, and the
    warning's first cause says why (the old text named four causes, none of them this)."""
    if sys.platform == "win32":
        pytest.skip("renaming a log under its open handle is refused on Windows; runs in "
                    "the sandbox and on CI")
    path, saved = _two_rotations(tmp_path)
    real_scan = rotation.scan_directories
    calls = {"n": 0}

    def scan_then_rotate(directories: list[str], base: str) -> object:
        found = real_scan(directories, base)
        calls["n"] += 1
        if calls["n"] <= 2:  # the second pass is renamed under too; a third would find it
            _shift(tmp_path, calls["n"] + 1, b"live %d" % (calls["n"] + 1))
        return found

    monkeypatch.setattr(rotation, "scan_directories", scan_then_rotate)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    _, lines, _ = run(path, saved)
    assert calls["n"] == 2  # once more, never a third time
    assert lines == ["live 1"]  # the live file the run opened, from 0; the archives are lost
    warned = [r.getMessage() for r in caplog.records if "no rotated copy" in r.getMessage()]
    assert len(warned) == 1
    assert "likely causes: a second rotation during this run (a copy was renamed twice), " \
        "rotate 0, " in warned[0]  # first, before the four old causes


def test_the_holder_gone_at_the_content_stage_is_found_again_under_its_new_name(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's F1: with dateext (the RHEL default) a rotation reuses no name, so the
    only signal is a listed copy GONE at its open -- gzipped into another name. That was
    'could not be read; skipped' (no rescan, the tail lost); now it is a rescan. Real on
    every platform: nothing open is renamed."""
    path, saved = _two_rotations(tmp_path)
    real_scan = rotation.scan_directories
    calls = {"n": 0}

    def scan_then_compress(directories: list[str], base: str) -> object:
        found = real_scan(directories, base)
        calls["n"] += 1
        if calls["n"] == 1:  # the holder is compressed away before its open
            compress_to(tmp_path / "router.log.2.gz", (tmp_path / "router.log.2").read_bytes())
            (tmp_path / "router.log.2").unlink()
        return found

    monkeypatch.setattr(rotation, "scan_directories", scan_then_compress)
    caplog.set_level(logging.INFO, logger="logalert")
    _, lines, cursor = run(path, saved)
    assert calls["n"] == 2
    assert lines == ["since 1", "middle 1", "live 1"]  # the tail from the .gz, then the rest
    assert cursor is not None and cursor.ino == path.stat().st_ino
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_a_member_gone_when_the_content_stage_opens_it_first_stays_in_the_chain(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S13: a quiet log, so the holder is older than the last run and the newer member
    is opened first by the content stage -- and is gone (compressed away). Filed as
    unreadable it was dropped from the chain for good; as renamed it stays, the stream
    stops at it, and the next run reads its .gz."""
    path, saved = _two_rotations(tmp_path)
    long_ago = time.time() - 60
    os.utime(tmp_path / "router.log.2", (long_ago, long_ago))  # written before the last run
    real_scan = rotation.scan_directories
    calls = {"n": 0}

    def scan_then_compress(directories: list[str], base: str) -> object:
        found = real_scan(directories, base)
        calls["n"] += 1
        if calls["n"] == 1:
            compress_to(tmp_path / "router.log.1.gz", (tmp_path / "router.log.1").read_bytes())
            (tmp_path / "router.log.1").unlink()
        return found

    monkeypatch.setattr(rotation, "scan_directories", scan_then_compress)
    caplog.set_level(logging.WARNING, logger="logalert")
    _, lines, stopped = run(path, saved)
    assert calls["n"] == 1  # the holder was found: no rescan
    assert lines == ["since 1"]  # the stop at the member
    assert stopped is not None and stopped.ino == (tmp_path / "router.log.2").stat().st_ino
    assert any("router.log.1 was renamed or removed under us" in r.getMessage()
               for r in caplog.records)
    assert not any("could not be read" in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    _, rest, cursor = run(path, stopped)
    assert rest == ["middle 1", "live 1"]
    assert cursor is not None and cursor.ino == path.stat().st_ino


def test_a_rotation_inside_the_listing_is_caught_by_the_second_listing(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The review's F2: the shift lands between the directory listing and the per-entry
    stat, so every listed name carries its new inode (no rename signal at any open) and
    the holder's new name was never listed. The plan lists again when nothing matched and
    searches again because the listing changed."""
    if sys.platform == "win32":
        pytest.skip("renaming a log under its open handle is refused on Windows; runs in "
                    "the sandbox and on CI")
    path, saved = _two_rotations(tmp_path)
    real_scandir = os.scandir
    listings = {"n": 0}

    def list_then_rotate(directory: str) -> list[os.DirEntry[str]]:
        entries = list(real_scandir(directory))
        listings["n"] += 1
        if listings["n"] == 1:
            _third_rotation(tmp_path)  # after the names were read, before their stats
        return entries

    monkeypatch.setattr(os, "scandir", list_then_rotate)  # the module rotation reads it from
    caplog.set_level(logging.INFO, logger="logalert")
    _, lines, cursor = run(path, saved)
    assert listings["n"] == 2
    assert lines == ["since 1", "middle 1", "live 1"]  # 'live 2' is the next run's
    assert cursor is not None and cursor.ino == (tmp_path / "router.log.1").stat().st_ino
    assert any("scanning again" in r.getMessage() for r in caplog.records)
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)


def test_one_rename_then_the_copy_gone_names_no_second_rotation(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename signal is cleared before the second pass: a shift that also dropped the
    holder (rotate 2) reaches the warning with 'the archive aged out', not a second
    rotation that never happened."""
    if sys.platform == "win32":
        pytest.skip("renaming a log under its open handle is refused on Windows; runs in "
                    "the sandbox and on CI")
    path, saved = _two_rotations(tmp_path)
    real_scan = rotation.scan_directories
    calls = {"n": 0}

    def scan_then_rotate_and_drop(directories: list[str], base: str) -> object:
        found = real_scan(directories, base)
        calls["n"] += 1
        if calls["n"] == 1:
            _third_rotation(tmp_path)
            (tmp_path / "router.log.3").unlink()  # rotate 2: the holder is dropped
        return found

    monkeypatch.setattr(rotation, "scan_directories", scan_then_rotate_and_drop)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    _, lines, _ = run(path, saved)
    assert calls["n"] == 2
    assert lines == ["live 1"]  # the live file the run opened, from 0
    warned = [r.getMessage() for r in caplog.records if "no rotated copy" in r.getMessage()]
    assert len(warned) == 1 and "the archive aged out" in warned[0]
    assert "a second rotation" not in warned[0]


def test_an_unreadable_chain_member_is_skipped_not_a_stop(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A member the run cannot open (a mode, not a move) is skipped as before, with its
    warning, and the live file is read: stopping there would park every later run before
    the same member until it aged out, the live file never read."""
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("root reads a 0000 file; expected in the root sandbox; CI runs it")
    path, saved = _two_rotations(tmp_path)
    (tmp_path / "router.log.1").chmod(0o000)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    try:
        _, lines, cursor = run(path, saved)
    finally:
        (tmp_path / "router.log.1").chmod(0o644)
    assert lines == ["since 1", "live 1"]
    assert cursor is not None and cursor.ino == path.stat().st_ino
    assert any("could not be read" in r.getMessage() for r in caplog.records)
    assert not any("renamed or removed" in r.getMessage() for r in caplog.records)


def test_the_stop_parks_before_an_empty_copy_that_has_no_first_line(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """An empty rotated copy has no first line to be found by once it is compressed; the
    cursor parks on the last copy that has one, and the empty copy is read again next
    run -- it has nothing to repeat."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, b"since 1" + NLB)
    rotate(path, tmp_path / "router.log.3")
    (tmp_path / "router.log.2").write_bytes(b"")  # a rotation of an empty interval
    (tmp_path / "router.log.1").write_bytes(b"middle 1" + NLB)
    path.write_bytes(b"live 1" + NLB)
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    compress_to(tmp_path / "router.log.1.gz", (tmp_path / "router.log.1").read_bytes())
    (tmp_path / "router.log.1").unlink()
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    with source:
        got = texts(list(source.lines()))
        stopped = source.cursor()
    assert got == ["since 1"]
    assert stopped.ino == (tmp_path / "router.log.3").stat().st_ino  # not the empty .2
    assert stopped.fingerprint == saved.fingerprint
    assert any("resumes after router.log.3" in r.getMessage() for r in caplog.records)
    _, rest, cursor = run(path, stopped)
    assert rest == ["middle 1", "live 1"]
    assert cursor is not None and cursor.ino == path.stat().st_ino


def test_a_parked_cursor_carries_the_logs_real_path_not_the_archives(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A listed link: the archive's path in the cursor made the next plan say the link
    now points elsewhere when it never moved."""
    real = tmp_path / "real"
    real.mkdir()
    target = real / "router.log"
    link = tmp_path / "current.log"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating a symlink needs a privilege here; runs in the sandbox and on CI")
    saved = seen(link, OLD)
    append(link, b"since 1" + NLB)
    rotate(target, real / "router.log.2")
    (real / "router.log.1").write_bytes(b"middle 1" + NLB)
    target.write_bytes(b"live 1" + NLB)
    source = open_source(SECTION, str(link), saved)
    assert isinstance(source, CatchUpSource)
    (real / "router.log.1").unlink()  # the member gone: the stop
    with source:
        assert texts(list(source.lines())) == ["since 1"]
        stopped = source.cursor()
    assert stopped.realpath == os.path.realpath(target)
    caplog.set_level(logging.INFO, logger="logalert.rotation")
    _, rest, _ = run(link, stopped)
    assert rest == ["live 1"]
    assert not any("the link now points at" in r.getMessage() for r in caplog.records)


# -- a permission failure on a rotated copy (issue #38) ------------------------------------------


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_an_unreadable_copy_is_warned_about_once_in_the_errnos_words_and_named_as_the_cause(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured before the fix (sandbox, as a service user, a 0600 root copy): the warning
    printed twice -- the content stage, then the inode stage -- with the path repeated in the
    exception's text, and the causes line naming rotate 0, olddir, compression and ageing.
    The refusal is staged through open_log here, so the shape is real on both platforms;
    the mode-bit twin below runs where modes are."""
    path, saved = _two_rotations(tmp_path)
    holder = str(tmp_path / "router.log.2")
    real_open = open_log

    def refuse(target: str, *, follow_links: bool = False) -> BinaryStream:
        if target == holder:
            raise PermissionError(13, "Permission denied", target)
        return real_open(target, follow_links=follow_links)

    monkeypatch.setattr("logalert.rotation.open_log", refuse)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    _, lines, cursor = run(path, saved)
    assert lines == ["live 1"]
    assert cursor is not None and cursor.ino == path.stat().st_ino
    warned = _warnings(caplog)
    assert warned == [
        f"[{SECTION}] {path}: rotated copy {holder} could not be read (Permission denied); "
        f"skipped",
        warned[-1],
    ]
    assert ("likely causes: a rotated copy this user cannot read (named above), rotate 0"
            in warned[-1])


def test_a_copy_the_mode_refuses_is_one_warning_and_the_cause(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("root reads a 0000 file; expected in the root sandbox; CI runs it")
    path, saved = _two_rotations(tmp_path)
    (tmp_path / "router.log.2").chmod(0o000)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    try:
        _, lines, _ = run(path, saved)
    finally:
        (tmp_path / "router.log.2").chmod(0o644)
    assert lines == ["live 1"]
    warned = _warnings(caplog)
    assert len(warned) == 2 and warned[0].endswith("could not be read (Permission denied); skipped")
    assert "(named above), rotate 0" in warned[1]


def test_an_archive_directory_that_cannot_be_searched_is_one_warning_and_the_cause(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Measured before the fix: a 0644 olddir -- listable, its entries not stat-able -- was
    dropped without a word, and the causes line was wrong."""
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("root searches a 0644 directory; expected in the root sandbox; CI runs it")
    path = tmp_path / "router.log"
    old = tmp_path / "old"
    old.mkdir()
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, old / "router.log.1.gz", compress=True)
    (old / "router.log.2.gz").write_bytes(gzip.compress(b"older" + NLB))
    path.write_bytes(LIVE)
    old.chmod(0o644)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    try:
        _, lines, _ = run(path, saved, archive_dir=old)
    finally:
        old.chmod(0o755)
    assert lines == ["live 1"]
    warned = _warnings(caplog)
    assert warned[0] == (f"[{SECTION}] {path}: cannot examine 2 of the 2 entries of {old} while "
                         f"looking for rotated copies (Permission denied); is the directory "
                         f"searchable?")
    assert len(warned) == 2  # once, although the plan lists the directories twice
    assert ("likely causes: a directory this user cannot list or search (named above), "
            "rotate 0" in warned[1])


def test_an_archive_directory_that_cannot_be_listed_is_one_warning_not_two(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The comparison listing of issue #32 met the same failure and logged it again."""
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("root lists a 0111 directory; expected in the root sandbox; CI runs it")
    path = tmp_path / "router.log"
    old = tmp_path / "old"
    old.mkdir()
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, old / "router.log.1.gz", compress=True)
    path.write_bytes(LIVE)
    old.chmod(0o111)  # searchable, not listable -- for its owner too
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    try:
        _, lines, _ = run(path, saved, archive_dir=old)
    finally:
        old.chmod(0o755)
    assert lines == ["live 1"]
    warned = _warnings(caplog)
    assert warned[0] == (f"[{SECTION}] {path}: cannot list {old} while looking for rotated "
                         f"copies (Permission denied)")
    assert len(warned) == 2
    assert "(named above), rotate 0" in warned[1]


def test_a_corrupt_copy_is_not_named_as_a_permission(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Review of #38: a bogus .gz (or a .zst before 3.14) is unreadable too, and the first
    version named it `a rotated copy this user cannot read` -- the wrong advice."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    path.unlink()
    (tmp_path / "router.log.1.gz").write_bytes(b"not gzip at all" + NLB)
    path.write_bytes(LIVE)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    _, lines, _ = run(path, saved)
    assert lines == ["live 1"]
    warned = _warnings(caplog)
    assert len(warned) == 2 and "could not be read (Not a gzipped file" in warned[0]
    assert "likely causes: rotate 0," in warned[1] and "cannot read" not in warned[1]


def test_a_missing_archive_dir_is_one_warning_and_no_permission_cause(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Both platforms: the listing failure is logged once although the plan lists the
    directories twice (issue #32's comparison), and a directory that is not there is not
    `a directory this user cannot list or search`."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    path.unlink()
    path.write_bytes(LIVE)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    _, lines, _ = run(path, saved, archive_dir=tmp_path / "nowhere")
    assert lines == ["live 1"]
    warned = _warnings(caplog)
    assert len(warned) == 2
    assert warned[0].startswith(f"[{SECTION}] {path}: cannot list {tmp_path / 'nowhere'} while "
                                f"looking for rotated copies (")
    assert "likely causes: rotate 0," in warned[1] and "cannot list or search" not in warned[1]


def test_a_listing_a_permission_refuses_is_one_warning_and_the_cause_on_every_platform(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """The mode-bit twin runs where modes are; this one stages the refusal through
    os.scandir so the directory path of the plan is pinned on Windows too."""
    path = tmp_path / "router.log"
    old = tmp_path / "old"
    old.mkdir()
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, old / "router.log.1.gz", compress=True)
    path.write_bytes(LIVE)
    real_scandir = os.scandir

    def refuse(target: Any = ".", *args: Any, **kwargs: Any) -> Any:
        if os.path.normcase(str(target)) == os.path.normcase(str(old)):
            raise PermissionError(13, "Permission denied", str(target))
        return real_scandir(target, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", refuse)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    source, lines, _ = run(path, saved, archive_dir=old)
    assert lines == ["live 1"]
    assert isinstance(source, CatchUpSource) and source.failed_item() == (
        f"a permission kept the rotated copies out of reach ({old}); the lines before the "
        f"rotation are lost")  # a directory by its path
    warned = _warnings(caplog)
    assert warned[0] == (f"[{SECTION}] {path}: cannot list {old} while looking for rotated "
                         f"copies (Permission denied)")
    assert len(warned) == 2
    assert ("likely causes: a directory this user cannot list or search (named above), "
            "rotate 0" in warned[1])


def test_an_entry_whose_kind_needs_an_lstat_the_directory_refuses_counts_as_denied(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #38, reproduced on ext4 without the filetype feature: where readdir hands
    back no d_type, DirEntry.is_symlink() lstats, an unsearchable directory refuses that
    too, and the first version dropped the entry before counting it -- the issue's silent
    case again. Staged through a scandir proxy so it runs on every platform."""
    old = tmp_path / "old"
    old.mkdir()
    (old / "router.log.1.gz").write_bytes(gzip.compress(OLD))
    (old / "router.log.2.gz").write_bytes(gzip.compress(OLD))
    real_scandir = os.scandir

    class Unknown:
        """A directory entry from a listing without d_type, in an unsearchable directory."""

        def __init__(self, entry: "os.DirEntry[str]") -> None:
            self.name, self.path = entry.name, entry.path

        def is_symlink(self) -> bool:
            raise PermissionError(13, "Permission denied", self.path)

    def listing(target: Any = ".", *args: Any, **kwargs: Any) -> Any:
        entries = real_scandir(target, *args, **kwargs)
        if os.path.normcase(str(target)) == os.path.normcase(str(old)):
            return [Unknown(entry) for entry in entries]
        return entries

    monkeypatch.setattr(os, "scandir", listing)
    everything, archives, problems = scan_directories([str(old)], "router.log")
    assert everything == [] and archives == []
    assert problems == [(f"cannot examine 2 of the 2 entries of {old} while looking for rotated "
                         f"copies (Permission denied); is the directory searchable?", 13,
                         str(old))]


# -- logrotate's extension directive (issue #51) ------------------------------------------------


def test_the_extension_form_is_read_after_a_rotation(tmp_path: Path) -> None:
    """The layout logrotate 3.21 makes under `compress` + `extension .log` (measured); the
    classic-form code lost `since 1` here."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.1.log.gz", compress=True)
    path.write_bytes(LIVE)
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]
    _, again, _ = run(path, cursor)
    assert again == []


def test_the_chain_keeps_to_the_matched_copys_naming_form(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """worker.log rotated twice the classic way (.2 the holder, .1 = middle) beside a live
    per-worker log worker.1.log, whose numeric key (-1) is "newer" than the holder's: without
    the form rule it would join the chain and be mailed whole."""
    path = tmp_path / "worker.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "worker.log.2")
    (tmp_path / "worker.log.1").write_bytes(b"middle 1" + NLB)
    path.write_bytes(LIVE)
    (tmp_path / "worker.1.log").write_bytes(b"another worker" + NLB)
    caplog.set_level(logging.INFO, logger="logalert.rotation")
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource)
    assert [Path(a.path).name for a in source.plan.chain] == ["worker.log.1"]
    assert lines == ["since 1", "since 2", "middle 1", "live 1"]
    assert any("1 rotated copy named in the other form (worker.1.log) left out" in r.getMessage()
               for r in caplog.records)


def test_a_numbered_live_sibling_is_not_the_holder_by_name_alone(tmp_path: Path) -> None:
    """The content stage still decides: worker.1.log with its own first line does not fit."""
    path = tmp_path / "worker.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    (tmp_path / "worker.1.log").write_bytes(b"another worker" + NLB * 4)
    path.unlink()
    path.write_bytes(LIVE)  # rotate 0: the holder is gone
    source, lines, _ = run(path, saved)
    assert lines == ["live 1"]


def test_epoch_keys_are_computed_without_fromtimestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 32-bit time_t raises OverflowError from datetime.fromtimestamp at 2**31 (CPython's
    _PyTime_ObjectToTime_t); the arithmetic path gives the same key on every host."""

    class Refusing(datetime):
        @classmethod
        def fromtimestamp(cls, *args: object, **kwargs: object) -> "Refusing":
            raise OverflowError("timestamp out of range for platform time_t")

        @classmethod
        def utcfromtimestamp(cls, *args: object, **kwargs: object) -> "Refusing":
            raise OverflowError("timestamp out of range for platform time_t")

    monkeypatch.setattr(rotation, "datetime", Refusing)
    assert rotation._epoch_key(2**31) == (2038, 1, 19, 3, 14, 8, 2**31)
    assert rotation._epoch_key(9999999999) == (2286, 11, 20, 17, 46, 39, 9999999999)
    assert rotation._epoch_key(1789440820) == (2026, 9, 15, 2, 53, 40, 1789440820)
    assert classify("router.log-1789440820", "router.log") == (
        "dated", (2026, 9, 15, 2, 53, 40, 1789440820), "-1789440820", False)


def test_a_sibling_log_is_never_chained_behind_a_hand_moved_copy(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Review of #51: with the extension form allowed an other style, app-error.log was a
    copy of app.log and the chain behind a hand-moved app-old.log (an other-style match
    OF THIS LOG, which admits other-style members) mailed the sibling whole."""
    path = tmp_path / "app.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "app-old.log")
    (tmp_path / "app-error.log").write_bytes(b"error 1" + NLB + b"error 2" + NLB)
    path.write_bytes(LIVE)
    caplog.set_level(logging.INFO, logger="logalert.rotation")
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource)
    assert source.plan.match is not None and source.plan.chain == []
    assert Path(source.plan.match.path).name == "app-old.log"
    assert lines == ["since 1", "since 2", "live 1"]
    assert not any("app-error.log" in r.getMessage() for r in caplog.records)


def test_an_extension_form_chain_is_read_in_order(tmp_path: Path) -> None:
    """Two rotations under extension + delaycompress: the holder router.2.log.gz, the
    member router.1.log, then the live file -- the form rule keeps them together."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, b"since 1" + NLB)
    rotate(path, tmp_path / "router.2.log.gz", compress=True)
    (tmp_path / "router.1.log").write_bytes(b"middle 1" + NLB)
    path.write_bytes(LIVE)
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource)
    assert [Path(a.path).name for a in source.plan.chain] == ["router.1.log"]
    assert lines == ["since 1", "middle 1", "live 1"]
    _, again, _ = run(path, cursor)
    assert again == []


def test_an_unnamed_match_keeps_its_classic_chain(tmp_path: Path) -> None:
    """The ext_form of a file the scan cannot name is the classic form (the guard on
    ``found``): a holder found by inode under a hand-made name still chains router.log.1."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "keep-this-one.txt")
    older = time.time() - 10  # the holder is older by mtime, whatever the clock tick
    os.utime(tmp_path / "keep-this-one.txt", (older, older))
    (tmp_path / "router.log.1").write_bytes(b"middle 1" + NLB)
    path.write_bytes(LIVE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "inode"
    assert [Path(a.path).name for a in source.plan.chain] == ["router.log.1"]
    assert lines == ["since 1", "since 2", "middle 1", "live 1"]


def test_a_classic_plain_orphan_is_not_the_twin_of_the_extension_form_copy(
        tmp_path: Path) -> None:
    """Review of #51, reproduced with real logrotate: a switch from classic delaycompress
    to extension + compress leaves router.log.1 behind for good, and the twin collapse --
    keyed on the suffix alone -- kept that plain orphan and dropped router.1.log.gz, the
    copy holding the saved position, at every rotation after the switch."""
    path = tmp_path / "router.log"
    (tmp_path / "router.log.1").write_bytes(b"an orphan of the old form" + NLB)
    saved = seen(path, OLD)
    append(path, SINCE)
    rotate(path, tmp_path / "router.1.log.gz", compress=True)
    path.write_bytes(LIVE)
    _, archives, _ = scan_directories([str(tmp_path)], "router.log")
    assert sorted(Path(a.path).name for a in archives) == ["router.1.log.gz", "router.log.1"]
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]


def test_the_saved_identity_wins_a_key_tie_against_a_sibling_with_the_same_first_line(
        tmp_path: Path) -> None:
    """Review of #51, reproduced on NTFS (which lists worker.1.log before worker.log.1): the
    two tie on the numeric key, and a live sibling that shares the banner first line and is
    long enough fitted by content when the listing put it first -- the holder's lines lost.
    The saved identity breaks the tie."""
    path = tmp_path / "worker.log"
    banner = b"# worker log v1" + NLB
    saved = seen(path, banner + b"old 1" + NLB)
    append(path, SINCE)
    rotate(path, tmp_path / "worker.log.1")  # the holder keeps its inode
    (tmp_path / "worker.1.log").write_bytes(banner + b"w1 line 2" + NLB + b"w1 line 3" + NLB
                                            + b"w1 line 4" + NLB)
    path.write_bytes(LIVE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource)
    assert source.plan.match is not None
    assert Path(source.plan.match.path).name == "worker.log.1"
    assert lines == ["since 1", "since 2", "live 1"]


def test_a_permission_names_the_failed_item_a_corrupt_copy_does_not(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's call on #38: a permission that kept the copies out of reach when nothing
    matched is a failed item the run collects (Plan.denied); a corrupt copy stays a warning."""
    path, saved = _two_rotations(tmp_path)
    holder = str(tmp_path / "router.log.2")
    real_open = open_log

    def refuse(target: str, *, follow_links: bool = False) -> BinaryStream:
        if target == holder:
            raise PermissionError(13, "Permission denied", target)
        return real_open(target, follow_links=follow_links)

    monkeypatch.setattr("logalert.rotation.open_log", refuse)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and lines == ["live 1"]
    assert source.plan.denied == ("a permission kept the rotated copies out of reach "
                                  "(router.log.2); the lines before the rotation are lost")
    monkeypatch.setattr("logalert.rotation.open_log", real_open)
    bogus = tmp_path / "bogus"
    bogus.mkdir()
    other = bogus / "router.log"
    saved = seen(other, OLD)
    other.unlink()
    (bogus / "router.log.1.gz").write_bytes(b"not gzip at all" + NLB)
    other.write_bytes(LIVE)
    source, lines, _ = run(other, saved)
    assert isinstance(source, CatchUpSource) and lines == ["live 1"]
    assert source.plan.denied == ""


def test_a_refused_older_copy_is_no_item_when_the_holder_is_found(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The item is for the loss: an older copy a permission refuses while the holder is
    found by content costs nothing but the warning."""
    path, saved = _two_rotations(tmp_path)
    two = tmp_path / "router.log.2"
    holder = tmp_path / "router.log.2.gz"
    holder.write_bytes(gzip.compress(two.read_bytes()))  # a new inode: the content stage
    two.unlink()
    third = tmp_path / "router.log.3.gz"
    third.write_bytes(gzip.compress(b"ancient" + NLB))
    stamp = holder.stat().st_mtime
    os.utime(third, (stamp, stamp))  # recent enough for the content stage, and tried first
    real_open = open_log
    opened: list[str] = []

    def refuse(target: str, *, follow_links: bool = False) -> BinaryStream:
        opened.append(os.path.basename(target))
        if target == str(third):
            raise PermissionError(13, "Permission denied", target)
        return real_open(target, follow_links=follow_links)

    monkeypatch.setattr("logalert.rotation.open_log", refuse)
    caplog.set_level(logging.WARNING, logger="logalert.rotation")
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and "router.log.3.gz" in opened
    assert lines == ["since 1", "middle 1", "live 1"]
    assert source.failed_item() == ""
    assert [w for w in _warnings(caplog) if "could not be read" in w] == [
        f"[{SECTION}] {path}: rotated copy {third} could not be read (Permission denied); "
        f"skipped"]


def test_a_chain_member_a_permission_refuses_is_the_item_too(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The owner's rationale covers the chain: a member the mode keeps out of reach loses
    its lines the same way, so the run gets the item (review)."""
    path, saved = _two_rotations(tmp_path)
    member = str(tmp_path / "router.log.1")
    real_open = open_log

    def refuse(target: str, *, follow_links: bool = False) -> BinaryStream:
        if target == member:
            raise PermissionError(13, "Permission denied", target)
        return real_open(target, follow_links=follow_links)

    monkeypatch.setattr("logalert.rotation.open_log", refuse)
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource) and lines == ["since 1", "live 1"]
    assert source.failed_item() == ("a permission kept a rotated copy out of reach "
                                    "(router.log.1); its lines are lost")
    assert cursor is not None and cursor.ino == path.stat().st_ino  # moved on
