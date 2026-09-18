#!/usr/bin/env python3
"""Refuse a release artifact whose members are world-writable, whose metadata carries CRLF,
or which lacks a file the release depends on (issue #56).

## Why this exists

The 0.1.0 sdist and wheel were built on the Windows dev host: every sdist member was
``-rw-rw-rw-`` (directories ``drwxrwxrwx``), the wheel's external attributes were 0o666
(all but RECORD's), and METADATA and PKG-INFO carried CRLF. pip tolerates all of it -- but
GNU tar as the superuser preserves archive modes by default (``-p`` is root's default), so
``sudo tar xzf`` of that sdist leaves a world-writable source tree (measured in the
sandbox: 35 of 36 entries; 0 for a Linux-built sdist). Narrow, and tar's rule rather than a
vulnerability, which is why the answer is a check in the release path rather than a rebuild
of 0.1.0.
The release workflow builds on Linux and runs this before it attaches anything.

## What it checks

For a wheel (a zip) and an sdist (a gzipped tar): every member's mode has no ``o+w`` bit;
``METADATA`` (wheel) and ``PKG-INFO`` (sdist) contain no CRLF; the members the release
depends on are present, directly under the sdist's top directory -- ``README.md``,
``INSTALL.md``, ``CHANGELOG.md``, ``LICENSE``, ``pyproject.toml`` (the release procedure's
``tar tzf`` confirmation, mechanised: a missing file means ``MANIFEST.in`` is wrong and an
incomplete tarball must never ship) and ``PKG-INFO`` (so the CRLF check has something to
read) -- and the wheel's ``METADATA`` in its ``.dist-info``. A file that is neither, or
cannot be read, is could-not-check.

Usage:  python tools/check_artifacts.py ARTIFACT [ARTIFACT ...]
Exit 0 = every artifact is clean. 1 = at least one is not (every defect named). 2 = could
not check (no artifacts, one that could not be read or is not an sdist or a wheel). 2 is
never a pass.
"""

import sys
import tarfile
import zipfile
from pathlib import Path

EXIT_OK = 0
EXIT_DEFECT = 1
EXIT_COULD_NOT_CHECK = 2

SDIST_REQUIRED = ("README.md", "INSTALL.md", "CHANGELOG.md", "LICENSE", "pyproject.toml",
                  "PKG-INFO")
WHEEL_REQUIRED = ("METADATA",)
CRLF = b"\r\n"
WORLD_WRITABLE = 0o002


def _mode_defect(name: str, mode: int) -> list[str]:
    """The one mode defect a member can have (a directory's counts: root's tar restores it)."""
    if mode & WORLD_WRITABLE:
        return [f"{name}: mode {mode & 0o777:04o} is world-writable"]
    return []


def check_wheel(path: Path) -> list[str]:
    defects: list[str] = []
    found: set[str] = set()
    with zipfile.ZipFile(path) as wheel:
        for info in wheel.infolist():
            defects += _mode_defect(info.filename, (info.external_attr >> 16) & 0o777)
            if info.filename.endswith(".dist-info/METADATA"):
                found.add("METADATA")
                if CRLF in wheel.read(info):
                    defects.append(f"{info.filename}: carries CRLF line endings")
    defects += [f"{name}: missing from the wheel" for name in WHEEL_REQUIRED if name not in found]
    return defects


def check_sdist(path: Path) -> list[str]:
    defects: list[str] = []
    found: set[str] = set()  # the members directly under the sdist's one top directory
    with tarfile.open(path, "r:gz") as sdist:
        for member in sdist.getmembers():
            defects += _mode_defect(member.name, member.mode)
            parts = member.name.split("/")
            if len(parts) == 2 and member.isfile():
                found.add(parts[1])
            if member.name.endswith("/PKG-INFO") and member.isfile():
                stream = sdist.extractfile(member)
                if stream is not None and CRLF in stream.read():
                    defects.append(f"{member.name}: carries CRLF line endings")
    defects += [f"{name}: missing from the sdist" for name in SDIST_REQUIRED if name not in found]
    return defects


def check(path: Path) -> list[str] | None:
    """The artifact's defects (empty when clean), or None when it could not be checked."""
    try:
        if path.name.endswith(".whl"):
            return check_wheel(path)
        if path.name.endswith(".tar.gz"):
            return check_sdist(path)
    except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
        print(f"could not check {path}: {exc}")
        return None
    print(f"could not check {path}: neither a wheel (.whl) nor an sdist (.tar.gz)")
    return None


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: check_artifacts.py ARTIFACT [ARTIFACT ...]")
        print("could not check: no artifact given (an empty list is never a pass)")
        return EXIT_COULD_NOT_CHECK
    unchecked = 0
    defective = 0
    for arg in argv:
        path = Path(arg)
        defects = check(path)
        if defects is None:
            unchecked += 1
        elif defects:
            defective += 1
            print(f"{path}: {len(defects)} defect(s)")
            for defect in defects:
                print(f"  {defect}")
        else:
            print(f"{path}: clean (no world-writable member, LF metadata, the required files)")
    if unchecked:
        print(f"{unchecked} artifact(s) could not be checked -- NOT a pass")
        return EXIT_COULD_NOT_CHECK
    if defective:
        print(f"{defective} of {len(argv)} artifact(s) must not ship")
        return EXIT_DEFECT
    print(f"{len(argv)} artifact(s) checked; all clean")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
