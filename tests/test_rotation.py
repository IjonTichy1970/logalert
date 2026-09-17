"""Rotation catch-up: the archive search, the chain, every naming style (issue #8).

Every scenario is built in the test from bytes, with the stdlib gzip / bz2 / lzma modules --
never a checked-in binary fixture -- so it runs on both platforms. The real-logrotate versions
of the same scenarios are in tests/test_logrotate.py and run where logrotate exists (the
sandbox, CI).
"""

import bz2
import gzip
import logging
import lzma
import os
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from logalert.cursor import Line, LogFile
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
    caplog.set_level(logging.WARNING, logger="logalert")
    everything, archives = scan_directories([str(tmp_path), str(tmp_path / "nope")], "router.log")
    assert [os.path.basename(a.path) for a in everything] == ["router.log.2"]
    assert [a.suffix for a in archives] == [".2"]
    assert any("cannot list" in r.getMessage() for r in caplog.records)


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
    everything, _ = scan_directories([str(tmp_path)], "router.log")
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
