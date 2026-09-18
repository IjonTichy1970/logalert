"""The helpers the test modules share (issue #43): the ``Site`` fixture, the scandir denial,
the ``-m logalert`` subprocess runner, the tried symlink, the fence scanner and the rotation
helpers -- each one rule, written once. pytest loads this module before any test module in
``tests/``; a module imports what it uses by name (``from conftest import ...``), the same
rootdir import the tree has always used between test modules (no ``__init__.py``; pytest's
default import mode puts ``tests/`` on ``sys.path``). Gated Python: ASCII, ruff, mypy strict."""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fake_sendmail import install

from logalert.__main__ import main
from logalert.cursor import Line
from logalert.rotation import open_source
from logalert.state import Cursor

NL = chr(10)
SENDER = "alerts@example.net"
SECTION = "router-disk"
# the fake sendmail's knobs, cleared before every run that spawns it: one list, so a knob a
# test leaves in the environment cannot leak into another module's runs (the two copies had
# drifted by one knob when this module was made)
FAKE_KNOBS = ("LOGALERT_FAKE_SLEEP", "LOGALERT_FAKE_EXIT", "LOGALERT_FAKE_EXIT_IF_RCPT",
              "LOGALERT_FAKE_STDERR_BYTES")


# -- the run, end to end: a config with two sections, a state directory, the fake MTA ----------


class Site:
    """A config with two sections over two fixture logs, a state directory, the fake."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.router = tmp_path / "router.log"
        self.firewall = tmp_path / "fw.log"
        self.router.write_text("boot" + NL + "quiet" + NL, encoding="utf-8", newline=NL)
        self.firewall.write_text("up" + NL, encoding="utf-8", newline=NL)
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()
        self.state_file = self.state_dir / "state.json"
        self.activity_log = tmp_path / "activity.log"  # not in the state dir: tests remove it
        self.fake_dir = tmp_path / "fake"
        monkeypatch.setenv("LOGALERT_FAKE_DIR", str(self.fake_dir))
        for knob in FAKE_KNOBS:
            monkeypatch.delenv(knob, raising=False)
        self.binary = install(tmp_path)
        self.conf = tmp_path / "logalert.conf"
        self.write_config()

    def write_config(self, settings: str = "", router: str = "", firewall: str = "",
                     router_to: str = "noc@example.net", sendmail: str | None = None,
                     firewall_files: str | None = None, log: str | None = None) -> None:
        fw_files = firewall_files or self.firewall.as_posix()
        log = log or f"file:{self.activity_log.as_posix()}"
        text = (f"[logalert]\nsendmail_path = {sendmail or self.binary.as_posix()}\n"
                f"state_file = {self.state_file.as_posix()}\nfrom = {SENDER}\n"
                f"log = {log}\n{settings}\n"
                f"[router-disk]\nsubject = Router disk failure\nto = {router_to}\n"
                f"files = {self.router.as_posix()}\npatterns =\n    disk failure\n{router}\n"
                f"[firewall]\nsubject = Firewall denies\nto = fw@example.net\n"
                f"files = {fw_files}\npatterns =\n    DENY\n{firewall}\n")
        self.conf.write_text(text, encoding="utf-8", newline=NL)

    def run(self, *extra: str) -> int:
        return main(["-f", str(self.conf), *extra])

    def prime(self) -> None:
        """A first run: first sight of both files, nothing mailed, the state created."""
        assert self.run() == 0
        assert self.calls() == []

    def append(self, path: Path, *lines: str) -> None:
        with open(path, "a", encoding="utf-8", newline=NL) as fh:
            for line in lines:
                fh.write(line + NL)

    def calls(self) -> list[tuple[dict[str, Any], bytes]]:
        if not self.fake_dir.exists():
            return []
        out = []
        for name in sorted(n for n in os.listdir(self.fake_dir) if n.startswith("call-")
                           and n.endswith("-argv.json")):
            argv: dict[str, Any] = json.loads((self.fake_dir / name).read_text(encoding="ascii"))
            stdin = (self.fake_dir / name.replace("-argv.json", "-stdin.bin")).read_bytes()
            out.append((argv, stdin))
        return out

    def state(self) -> dict[str, dict[str, dict[str, Any]]]:
        data: dict[str, Any] = json.loads(self.state_file.read_text(encoding="utf-8"))
        entries: dict[str, dict[str, dict[str, Any]]] = data["entries"]
        return entries

    def offset(self, section: str, path: Path) -> int:
        return int(self.state()[section][path.as_posix()]["offset"])

    def activity(self) -> list[str]:
        """The activity log so far, each line without its timestamp and ident."""
        if not self.activity_log.exists():
            return []
        lines = self.activity_log.read_text(encoding="utf-8").splitlines()
        return [line.split("]: ", 1)[1] for line in lines]


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    return Site(tmp_path, monkeypatch)


def body_of(stdin: bytes) -> str:
    return stdin.decode("utf-8")


def run_logalert(*args: str, env: dict[str, str] | None = None,
                 binary: bool = False) -> "subprocess.CompletedProcess[Any]":
    """``python -m logalert <args>`` as a child process: the interpreter running the tests,
    its output decoded as UTF-8 with replacement (raw bytes with ``binary``), never raising
    on the exit code -- the tests read it."""
    argv = [sys.executable, "-m", "logalert", *args]
    if binary:
        return subprocess.run(argv, capture_output=True, check=False, env=env)
    return subprocess.run(argv, capture_output=True, encoding="utf-8", errors="replace",
                          check=False, env=env)


# -- the filesystem's refusals, staged so they run on both platforms ---------------------------


def deny_scandir(monkeypatch: pytest.MonkeyPatch, locked: str) -> None:
    """``os.scandir`` refuses one directory: the unlistable-directory device on both platforms."""
    real_scandir = os.scandir

    def scandir(path: str = ".") -> object:
        if os.path.normcase(path) == os.path.normcase(locked):
            raise PermissionError(13, "Permission denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)


def unsearchable_listing(monkeypatch: pytest.MonkeyPatch, locked: str, *,
                         gone: bool = False, refused: set[str] | None = None) -> None:
    """``os.scandir`` of one directory lists as a filesystem without ``d_type`` does over a
    directory the user can list but not search: every entry's ``is_dir``/``is_symlink`` is
    the lstat the parent refuses (issue #71; #38's shape for the catch-up) -- or, with
    ``refused``, only the named entries', the others answering as the real entry does; with
    ``gone`` the lstat says the entry is gone since the listing (CPython's own entry answers
    False there; a lister that raises is the shape pinned). Both platforms."""

    class Unknown:
        def __init__(self, entry: "os.DirEntry[str]") -> None:
            self.entry = entry
            self.name, self.path = entry.name, entry.path

        def _refuse(self) -> None:
            if refused is not None and self.name not in refused:
                return
            if gone:
                raise FileNotFoundError(2, "No such file or directory", self.path)
            raise PermissionError(13, "Permission denied", self.path)

        def is_dir(self, *, follow_symlinks: bool = True) -> bool:
            self._refuse()
            return self.entry.is_dir(follow_symlinks=follow_symlinks)

        def is_symlink(self) -> bool:
            self._refuse()
            return self.entry.is_symlink()

    class Listing:
        def __init__(self, entries: list[Unknown]) -> None:
            self.entries = entries

        def __enter__(self) -> "Listing":
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def __iter__(self) -> Any:
            return iter(self.entries)

    real_scandir = os.scandir

    def scandir(path: Any = ".", *args: Any, **kwargs: Any) -> Any:
        if os.path.normcase(str(path)) == os.path.normcase(locked):
            with real_scandir(path, *args, **kwargs) as entries:
                return Listing([Unknown(entry) for entry in entries])
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(os, "scandir", scandir)


def try_symlink(link: Path, target: Path) -> None:
    """A symbolic link, or the project's skip where the host refuses one (Windows without
    Developer Mode); a host that can make one measures the case."""
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating a symlink needs a privilege here; runs in the sandbox and on CI")


# -- markdown ------------------------------------------------------------------------------------


def fences(text: str, info: str) -> list[str]:
    """The bodies of the fenced blocks whose info string is ``info`` (``""`` for a bare fence),
    scanned line by line: a regex keyed on the opening fence would take a closing one for it."""
    bodies: list[str] = []
    inside: str | None = None
    body: list[str] = []
    for line in text.split(NL):
        if line.startswith("```"):
            if inside is None:
                inside, body = line[3:].strip(), []
            else:
                if inside == info:
                    bodies.append(NL.join(body) + NL)
                inside = None
        elif inside is not None:
            body.append(line)
    return bodies


# -- the rotation scenarios: one run over a file, the cursor a previous run saved ---------------


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
