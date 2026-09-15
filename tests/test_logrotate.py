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
    assert isinstance(source, CatchUpSource) and source.plan.stage == "inode"
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
