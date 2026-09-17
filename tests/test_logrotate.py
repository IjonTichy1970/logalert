"""The rotation scenarios driven by REAL logrotate (issue #8): sandbox and CI only.

tests/test_rotation.py builds the same layouts from bytes on both platforms; this file proves
the layouts are what logrotate 3.x actually produces, by running it. It skips wherever the
binary is absent -- the Windows dev host -- with the reason in the skip text.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from logalert.cursor import Line
from logalert.globs import expand
from logalert.rotation import CatchUpSource, open_source
from logalert.state import Cursor

LOGROTATE = shutil.which("logrotate")
if sys.platform == "win32":
    pytest.skip("needs logrotate; runs in the sandbox and on CI", allow_module_level=True)
if LOGROTATE is None:
    pytest.fail("logrotate is not on PATH: these tests are required on POSIX (could-not-check "
                "is never a pass)", pytrace=False)

SECTION = "router-disk"
OLD = b"old 1\nold 2\n"
SINCE = b"since 1\nsince 2\n"
LIVE = b"live 1\n"


def texts(lines: list[Line]) -> list[str]:
    return [line.text for line in lines]


def run(path: Path, saved: Cursor | None, *, archive_dir: Path | None = None,
        from_start: bool = False) -> tuple[object, list[str], Cursor | None]:
    source = open_source(SECTION, str(path), saved, from_start=from_start,
                         archive_dir=str(archive_dir) if archive_dir else None)
    if source is None:
        return None, [], None
    with source:
        lines = texts(list(source.lines()))
        return source, lines, source.cursor()


def seen(path: Path, content: bytes) -> Cursor:
    path.write_bytes(content)
    _, _, cursor = run(path, None, from_start=True)
    assert cursor is not None
    return cursor


def append(path: Path, data: bytes) -> None:
    with open(path, "ab") as handle:
        handle.write(data)


def logrotate(tmp_path: Path, path: Path, options: str) -> None:
    """One forced rotation of ``path`` with the given directives."""
    conf = tmp_path / "logrotate.conf"
    conf.write_text(f"{path}\n{{\n    rotate 5\n    missingok\n{options}}}\n",
                    encoding="utf-8", newline="\n")
    conf.chmod(0o644)  # logrotate ignores a group- or world-writable config, silently
    state = tmp_path / "logrotate.state"
    result = subprocess.run(
        [LOGROTATE or "logrotate", "-f", "-s", str(state), str(conf)],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr


def test_create(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    create\n")
    append(path, LIVE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "inode"
    assert lines == ["since 1", "since 2", "live 1"]


def test_compress(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    create\n    compress\n")
    assert (tmp_path / "router.log.1.gz").exists()
    append(path, LIVE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]


def test_delaycompress_three_rounds(tmp_path: Path) -> None:
    """The owner criterion against the real tool: three rotations, the oldest two compressed."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    expected: list[str] = []
    for round_number in (1, 2, 3):
        line = f"round {round_number}"
        append(path, f"{line}\n".encode("ascii"))
        expected.append(line)
        logrotate(tmp_path, path, "    create\n    compress\n    delaycompress\n")
    assert sorted(p.name for p in tmp_path.glob("router.log.*")) == [
        "router.log.1", "router.log.2.gz", "router.log.3.gz",
    ]
    append(path, LIVE)
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource)
    assert [Path(a.path).name for a in source.plan.chain] == ["router.log.2.gz", "router.log.1"]
    assert lines == [*expected, "live 1"]
    _, again, _ = run(path, cursor)
    assert again == []


def test_copytruncate(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    copytruncate\n")
    assert path.stat().st_ino == saved.ino and path.stat().st_size == 0
    append(path, LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]


def test_copytruncate_compress(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    copytruncate\n    compress\n")
    assert (tmp_path / "router.log.1.gz").exists()
    append(path, LIVE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]


def test_dateext_twice_with_the_epoch_form(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    create\n    dateext\n")
    append(path, b"middle\n")
    logrotate(tmp_path, path, "    create\n    dateext\n    dateformat -%Y%m%d-%s\n")
    names = sorted(p.name for p in tmp_path.glob("router.log-*"))
    assert len(names) == 2 and names[0].count("-") == 1 and names[1].count("-") == 2
    append(path, LIVE)
    _, lines, _ = run(path, saved)
    assert lines == ["since 1", "since 2", "middle", "live 1"]


def test_rotate_0_loses_the_interval_and_says_so(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    create\n    rotate 0\n")
    assert list(tmp_path.glob("router.log.*")) == []
    append(path, LIVE)
    caplog.set_level("WARNING", logger="logalert")
    _, lines, _ = run(path, saved)
    assert lines == ["live 1"]
    assert any("rotate 0" in r.getMessage() for r in caplog.records)


def test_nocreate(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    nocreate\n")
    assert not path.exists()
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.verdict == "absent"
    assert lines == ["since 1", "since 2"]
    path.write_bytes(LIVE)  # the writer recreates it
    source, lines, _ = run(path, cursor)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "mtime"  # issue #65
    assert lines == ["live 1"]


def test_olddir(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    old = tmp_path / "old"
    old.mkdir()
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, f"    create\n    compress\n    olddir {old}\n")
    assert (old / "router.log.1.gz").exists()
    append(path, LIVE)
    _, lines, _ = run(path, saved, archive_dir=old)
    assert lines == ["since 1", "since 2", "live 1"]


def test_a_glob_over_the_rotating_log_keeps_matching_the_live_file_only(tmp_path: Path) -> None:
    """Issue #18: through three real rotations ``router*`` expands to the live file alone --
    the renamed and the compressed copies are left out by shape, and would be by mtime as
    well (rename and gzip keep the old file's), so nothing is ever mailed twice."""
    path = tmp_path / "router.log"
    path.write_bytes(OLD)
    pattern = str(tmp_path / "router*")
    stamps: list[int] = []  # the live file's mtime before each rotation, oldest first
    for round_number in (1, 2, 3):
        append(path, f"round {round_number}\n".encode("ascii"))
        stamps.append(path.stat().st_mtime_ns)
        logrotate(tmp_path, path, "    create\n    compress\n    delaycompress\n")
        found = expand(pattern)
        assert found.files == (str(path),), found
        assert [Path(a).name for a in found.archives] == sorted(
            p.name for p in tmp_path.glob("router.log.*"))
        assert found.skipped == () and found.errors == ()
    assert [Path(a).name for a in found.archives] == ["router.log.1", "router.log.2.gz",
                                                       "router.log.3.gz"]
    copies = expand(pattern, include_archives=True)
    assert [Path(p).name for p in copies.files] == ["router.log", "router.log.1",
                                                    "router.log.2.gz", "router.log.3.gz"]
    # rename and gzip keep the old file's mtime: the copies carry the stamps, newest first
    # (the live file's own stamp is not compared: the kernel's coarse clock can give the
    # renamed copy and the created live file the same mtime -- measured in review)
    assert [Path(p).stat().st_mtime_ns for p in copies.files[1:]] == stamps[::-1]


# -- the extension directive (issue #51) --------------------------------------------------------


def test_extension_with_compress(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    create\n    compress\n    extension .log\n")
    assert (tmp_path / "router.1.log.gz").exists()
    append(path, LIVE)
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]
    _, again, _ = run(path, cursor)
    assert again == []


def test_extension_with_dateext_and_compress(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    create\n    compress\n    dateext\n    extension .log\n")
    names = [p.name for p in tmp_path.glob("router-*.log.gz")]
    assert len(names) == 1 and names[0][7:15].isdigit()
    append(path, LIVE)
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.stage == "content"
    assert lines == ["since 1", "since 2", "live 1"]


def test_extension_without_compression_under_a_glob(tmp_path: Path) -> None:
    """The renamed copy router.1.log was a file of its own to the glob (read from 0 under
    the new-file rule) AND found by the catch-up by inode: the interval twice, the whole
    copy once."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    create\n    extension .log\n")
    assert (tmp_path / "router.1.log").exists()
    append(path, LIVE)
    found = expand(str(tmp_path / "router*"))
    assert found.files == (str(path),)
    assert [Path(a).name for a in found.archives] == ["router.1.log"]
    _, lines, cursor = run(path, saved)
    assert lines == ["since 1", "since 2", "live 1"]
    _, again, _ = run(path, cursor)
    assert again == []


def test_extension_with_delaycompress_two_rounds(tmp_path: Path) -> None:
    """The extension-form chain against the real tool: the holder compressed, the member
    plain, then the live file."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    for round_number in (1, 2):
        append(path, f"round {round_number}\n".encode("ascii"))
        logrotate(tmp_path, path,
                  "    create\n    compress\n    delaycompress\n    extension .log\n")
    assert sorted(p.name for p in tmp_path.glob("router.*.log*")) == ["router.1.log",
                                                                       "router.2.log.gz"]
    append(path, LIVE)
    source, lines, cursor = run(path, saved)
    assert isinstance(source, CatchUpSource)
    assert [Path(a.path).name for a in source.plan.chain] == ["router.1.log"]
    assert lines == ["round 1", "round 2", "live 1"]
    _, again, _ = run(path, cursor)
    assert again == []


def test_a_gap_deeper_than_rotate_keeps_reads_the_surviving_copies(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """rotate 1 with two rotations between runs (issue #64): the holder is gone, the copy
    rotated meanwhile is read before the live file, and only the holder's tail is lost."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, SINCE)
    logrotate(tmp_path, path, "    create\n    rotate 1\n")
    append(path, b"middle 1\n")
    logrotate(tmp_path, path, "    create\n    rotate 1\n")  # the holder is dropped
    assert sorted(p.name for p in tmp_path.glob("router.log*")) == ["router.log", "router.log.1"]
    append(path, LIVE)
    caplog.set_level("WARNING", logger="logalert")
    source, lines, _ = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.plan.match is None
    assert lines == ["middle 1", "live 1"]
    warned = [r.getMessage() for r in caplog.records if "no rotated copy" in r.getMessage()]
    assert len(warned) == 1 and "the archive aged out" in warned[0]
    assert "reading the 1 rotated copy written since the last run" in warned[0]


def test_compress_finishing_inside_the_plan_mails_the_rotated_file_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #67, with real logrotate: gzip finishing between the live open and the listing
    made the .gz a chain member beside the handle it was made from ('middle 1' twice)."""
    import logalert.rotation as rotation_module

    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, b"since 1\n")
    logrotate(tmp_path, path, "    create\n    compress\n")
    append(path, b"middle 1\n")
    real_scan = rotation_module.scan_directories
    calls = {"n": 0}

    def rotate_before_the_listing(directories: list[str], base: str) -> object:
        calls["n"] += 1
        if calls["n"] == 1:  # the live file is open already: its rotation and gzip land now
            logrotate(tmp_path, path, "    create\n    compress\n")
            append(path, b"live 1\n")
        return real_scan(directories, base)

    monkeypatch.setattr(rotation_module, "scan_directories", rotate_before_the_listing)
    caplog.set_level("INFO", logger="logalert.rotation")
    source, lines, parked = run(path, saved)
    assert isinstance(source, CatchUpSource) and source.absorbed
    assert lines == ["since 1", "middle 1"]
    assert any("rotated and compressed during the run (router.log.1.gz)" in r.getMessage()
               for r in caplog.records)
    _, rest, _ = run(path, parked)
    assert rest == ["live 1"]


def test_copytruncate_landing_after_the_plan_loses_nothing(tmp_path: Path) -> None:
    """Issue #66, with real logrotate (the #32 review's S2): the truncation under the live
    handle left a cursor pairing the old first line with a post-truncation offset -- 'middle
    1' lost, then a fragment and 'live 1' twice. The live read stops before the truncated
    file's content and the next run chains the copy."""
    path = tmp_path / "router.log"
    saved = seen(path, OLD)
    append(path, b"since 1\n")
    logrotate(tmp_path, path, "    copytruncate\n")
    append(path, b"middle 1\n")
    source = open_source(SECTION, str(path), saved)
    assert isinstance(source, CatchUpSource)
    logrotate(tmp_path, path, "    copytruncate\n")  # after the plan, before the reads
    append(path, b"live 1\n")
    with source:
        second = texts(list(source.lines()))
        parked = source.cursor()
    assert second == ["since 1"] and source.stopped
    _, third, cursor = run(path, parked)
    _, fourth, _ = run(path, cursor)
    assert third == ["middle 1", "live 1"] and fourth == []
