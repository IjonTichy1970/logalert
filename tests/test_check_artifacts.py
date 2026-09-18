"""Behavioural checks for tools/check_artifacts.py (issue #56): a world-writable member, CRLF
metadata and a missing required file each refuse the artifact, and every could-not-check
is exit 2, never a pass. The fixtures are synthetic archives built here, so the checks are
real on both platforms (a tar member's mode is a number in the archive, not a filesystem
mode); the real Windows-built pair of this host was measured by hand: 21 and 37 defects.
"""

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

CHECKER = Path(__file__).resolve().parents[1] / "tools" / "check_artifacts.py"
NL = chr(10)
CRLF = chr(13) + chr(10)
SDIST_FILES = ("README.md", "INSTALL.md", "CHANGELOG.md", "LICENSE", "pyproject.toml")


def run(*paths: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(CHECKER), *(str(p) for p in paths)],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def wheel(path: Path, *, mode: int = 0o644, metadata: str = "Name: logalert" + NL,
          members: dict[str, int] | None = None, dist_info: bool = True) -> Path:
    """A wheel-shaped zip: every member's mode in its external attributes."""
    entries = dict(members or {"logalert/__init__.py": mode})
    if dist_info:
        entries["logalert-0.1.0.dist-info/METADATA"] = mode
    with zipfile.ZipFile(path, "w") as z:
        for name, member_mode in entries.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o100000 | member_mode) << 16
            z.writestr(info, metadata if name.endswith("METADATA") else "x" + NL)
    return path


def sdist(path: Path, *, mode: int = 0o644, dir_mode: int = 0o755,
          pkg_info: str = "Name: logalert" + NL, files: tuple[str, ...] = SDIST_FILES) -> Path:
    """An sdist-shaped gzipped tar: one top directory, the files under it, PKG-INFO."""
    with tarfile.open(path, "w:gz") as t:
        top = tarfile.TarInfo("logalert-0.1.0")
        top.type = tarfile.DIRTYPE
        top.mode = dir_mode
        t.addfile(top)
        for name in (*files, "PKG-INFO"):
            data = (pkg_info if name == "PKG-INFO" else "x" + NL).encode("ascii")
            info = tarfile.TarInfo("logalert-0.1.0/" + name)
            info.size = len(data)
            info.mode = mode
            t.addfile(info, io.BytesIO(data))
    return path


def test_a_clean_pair_is_exit_0(tmp_path: Path) -> None:
    rc, out = run(wheel(tmp_path / "a.whl"), sdist(tmp_path / "a.tar.gz"))
    assert rc == 0, out
    assert "2 artifact(s) checked; all clean" in out


def test_a_world_writable_member_refuses_the_wheel_and_the_sdist(tmp_path: Path) -> None:
    """The measured shape: 0o666 everywhere from a Windows build. MUTANT: the mask off by a
    bit (0o020 for 0o002) still catches 0o666 through the group bit; the group-writable
    case below is what reddens it (measured)."""
    rc, out = run(wheel(tmp_path / "w.whl", mode=0o666))
    assert rc == 1 and "logalert/__init__.py: mode 0666 is world-writable" in out
    rc, out = run(sdist(tmp_path / "s.tar.gz", mode=0o666, dir_mode=0o777))
    assert rc == 1 and "logalert-0.1.0: mode 0777 is world-writable" in out
    assert "logalert-0.1.0/README.md: mode 0666 is world-writable" in out
    assert "must not ship" in out


def test_group_writable_alone_is_not_refused(tmp_path: Path) -> None:
    """A build's RECORD arrives 0o664 on Linux (measured); only the world bit is tar's
    hazard for the superuser."""
    rc, out = run(wheel(tmp_path / "g.whl", mode=0o664), sdist(tmp_path / "g.tar.gz", mode=0o664))
    assert rc == 0, out


def test_crlf_metadata_refuses_the_artifact(tmp_path: Path) -> None:
    rc, out = run(wheel(tmp_path / "c.whl", metadata="Name: logalert" + CRLF))
    assert rc == 1 and "METADATA: carries CRLF line endings" in out
    rc, out = run(sdist(tmp_path / "c.tar.gz", pkg_info="Name: logalert" + CRLF))
    assert rc == 1 and "PKG-INFO: carries CRLF line endings" in out


def test_a_missing_required_file_refuses_the_sdist_and_the_wheel(tmp_path: Path) -> None:
    """The release procedure's tar tzf confirmation, mechanised: an incomplete tarball must
    never ship."""
    rc, out = run(sdist(tmp_path / "m.tar.gz", files=("README.md", "LICENSE", "pyproject.toml")))
    assert rc == 1
    assert "INSTALL.md: missing from the sdist" in out
    assert "CHANGELOG.md: missing from the sdist" in out
    rc, out = run(wheel(tmp_path / "m.whl", dist_info=False))
    assert rc == 1 and "METADATA: missing from the wheel" in out
    # a METADATA outside the dist-info is not the wheel's (review's surviving mutant)
    stray = wheel(tmp_path / "stray.whl", dist_info=False, members={"logalert/METADATA": 0o644})
    rc, out = run(stray)
    assert rc == 1 and "METADATA: missing from the wheel" in out


def test_a_file_in_a_subdirectory_does_not_satisfy_a_top_level_requirement(tmp_path: Path) -> None:
    """``docs/LICENSE`` is not ``LICENSE``: the requirement is the sdist's top directory's."""
    path = tmp_path / "sub.tar.gz"
    sdist(path, files=("README.md", "INSTALL.md", "CHANGELOG.md", "pyproject.toml", "docs/LICENSE"))
    rc, out = run(path)
    assert rc == 1 and "LICENSE: missing from the sdist" in out


def test_could_not_check_is_exit_2_never_a_pass(tmp_path: Path) -> None:
    rc, out = run()
    assert rc == 2 and "no artifact given" in out
    rc, out = run(tmp_path / "absent.whl")
    assert rc == 2 and "could not check" in out and "NOT a pass" in out
    other = tmp_path / "notes.txt"
    other.write_text("x" + NL, encoding="ascii")
    rc, out = run(other)
    assert rc == 2 and "neither a wheel" in out
    corrupt = tmp_path / "bad.whl"
    corrupt.write_bytes(b"not a zip")
    rc, out = run(corrupt, wheel(tmp_path / "ok.whl"))
    assert rc == 2  # one clean artifact does not outrank one that could not be read
