"""Glob expansion (issue #18): what a ``files`` pattern matches, what it leaves out, and how a
directory it cannot list is reported. Real on both platforms except the two POSIX-only
tests (a 0000 directory -- also skipped in the root sandbox, where root lists it -- and a
dangling symbolic link, which Windows cannot create unprivileged), which say so.
"""

import os
import sys
from pathlib import Path

import pytest

from logalert.globs import Expansion, expand, is_glob
from logalert.rotation import archive_suffix

NL = chr(10)


def make(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("line" + NL, encoding="utf-8", newline=NL)


def relative(root: Path, found: Expansion) -> list[str]:
    prefix = root.as_posix() + "/"
    return [p.removeprefix(prefix) for p in found.files]


# -- is_glob -----------------------------------------------------------------------------


@pytest.mark.parametrize("entry, glob", [
    ("/var/log/router.log", False), ("/var/log/*.log", True), ("/var/log/router?.log", True),
    ("/var/log/router[12].log", True), ("/var/log/br[[]1].log", True), ("/var/log/a-b_c.d", False),
])
def test_is_glob(entry: str, glob: bool) -> None:
    assert is_glob(entry) is glob


# -- expansion ---------------------------------------------------------------------------


def test_last_component_wildcard_matches_regular_files_in_name_order(tmp_path: Path) -> None:
    make(tmp_path, "b.log", "a.log", "c.txt", ".hidden.log", "sub/inner.log")
    (tmp_path / "adir.log").mkdir()
    found = expand((tmp_path / "*.log").as_posix())
    assert relative(tmp_path, found) == ["a.log", "b.log"]
    assert found.skipped == (((tmp_path / "adir.log").as_posix(), "a directory"),)
    assert found.archives == () and found.errors == ()
    assert found.pattern == (tmp_path / "*.log").as_posix()


def test_hidden_names_need_a_dotted_component(tmp_path: Path) -> None:
    make(tmp_path, ".hidden.log", "shown.log")
    assert relative(tmp_path, expand((tmp_path / "*").as_posix())) == ["shown.log"]
    assert relative(tmp_path, expand((tmp_path / ".*").as_posix())) == [".hidden.log"]


def test_wildcard_directory_component_then_a_literal_name(tmp_path: Path) -> None:
    """The syslog-server layout: one directory per host, the same file name in each; a
    directory without the file is nothing, a file where a directory was expected is not
    descended into."""
    make(tmp_path, "hosts/router1/messages", "hosts/router2/messages", "hosts/router3/other",
         "hosts/notadir")
    found = expand((tmp_path / "hosts" / "*" / "messages").as_posix())
    assert relative(tmp_path, found) == ["hosts/router1/messages", "hosts/router2/messages"]
    assert found.skipped == () and found.errors == ()


def test_wildcards_in_two_components(tmp_path: Path) -> None:
    make(tmp_path, "hosts/r1/a.log", "hosts/r1/b.txt", "hosts/r2/c.log", "hosts/top.log")
    found = expand((tmp_path / "hosts" / "*" / "*.log").as_posix())
    assert relative(tmp_path, found) == ["hosts/r1/a.log", "hosts/r2/c.log"]


def test_bracket_class_and_the_escaped_bracket(tmp_path: Path) -> None:
    make(tmp_path, "router1.log", "router2.log", "router3.log", "br[1].log")
    found = expand((tmp_path / "router[12].log").as_posix())
    assert relative(tmp_path, found) == ["router1.log", "router2.log"]
    found = expand((tmp_path / "br[[]1].log").as_posix())
    assert relative(tmp_path, found) == ["br[1].log"]


def test_question_mark_is_one_character(tmp_path: Path) -> None:
    make(tmp_path, "r1.log", "r22.log")
    assert relative(tmp_path, expand((tmp_path / "r?.log").as_posix())) == ["r1.log"]


def test_missing_directory_and_a_file_as_a_directory_match_nothing(tmp_path: Path) -> None:
    make(tmp_path, "router.log")
    for pattern in (tmp_path / "nodir" / "*.log", tmp_path / "router.log" / "*"):
        found = expand(pattern.as_posix())
        assert found.files == () and found.errors == () and found.skipped == ()
        assert not found.listed  # nothing was scanned: the run records no moment for it
    found = expand((tmp_path / "*.txt").as_posix())
    assert found.files == () and found.listed  # scanned, empty: a moment is recorded


def test_the_patterns_own_separators_are_kept(tmp_path: Path) -> None:
    """The expanded path is the state's key and the operator's ``--reset-state`` spelling."""
    make(tmp_path, "sub/router.log")
    pattern = (tmp_path / "sub" / "*.log").as_posix()
    found = expand(pattern)
    assert found.files == ((tmp_path / "sub" / "router.log").as_posix(),)
    if sys.platform == "win32":
        native = str(tmp_path / "sub" / "*.log")  # backslashes
        assert expand(native).files == (str(tmp_path / "sub" / "router.log"),)


def test_a_symbolic_link_is_passed_over_wherever_it_points(tmp_path: Path) -> None:
    """A link is a name the operator never wrote (a root run following one planted in a
    watched directory would mail a file outside it -- reproduced in review); a link where a
    wildcard directory component would descend is not followed either."""
    if sys.platform == "win32":
        pytest.skip("creating a symlink needs a privilege here; runs in the sandbox and on CI")
    make(tmp_path, "real.log", "hosts/r1/messages", "outside/secret")
    os.symlink(tmp_path / "real.log", tmp_path / "link.log")
    os.symlink(tmp_path / "gone", tmp_path / "dangling.log")
    os.symlink(tmp_path / "outside" / "secret", tmp_path / "hosts" / "r1" / "planted")
    os.symlink(tmp_path / "outside", tmp_path / "hosts" / "evil")
    found = expand((tmp_path / "*.log").as_posix())
    assert relative(tmp_path, found) == ["real.log"]
    assert found.skipped == (
        ((tmp_path / "dangling.log").as_posix(), "a symbolic link; list it by name"),
        ((tmp_path / "link.log").as_posix(), "a symbolic link; list it by name"),
    )
    found = expand((tmp_path / "hosts" / "*" / "*").as_posix())
    assert relative(tmp_path, found) == ["hosts/r1/messages"]
    assert found.skipped == (((tmp_path / "hosts" / "r1" / "planted").as_posix(),
                              "a symbolic link; list it by name"),)


def test_hard_links_to_one_file_are_read_once(tmp_path: Path) -> None:
    make(tmp_path, "fw.log")
    os.link(tmp_path / "fw.log", tmp_path / "fw-current.log")
    found = expand((tmp_path / "*.log").as_posix())
    assert relative(tmp_path, found) == ["fw-current.log"]  # the first name in sort order
    assert found.skipped == (((tmp_path / "fw.log").as_posix(),
                              "another name of " + (tmp_path / "fw-current.log").as_posix()),)


def test_a_literal_component_behind_a_file_matches_nothing(tmp_path: Path) -> None:
    """``hosts/*/sub/messages`` where one host's ``sub`` is a file: absent, not a failed
    item (``NotADirectoryError`` on POSIX, ``FileNotFoundError`` on Windows)."""
    make(tmp_path, "hosts/r1/sub/messages", "hosts/r2/sub")
    found = expand((tmp_path / "hosts" / "*" / "sub" / "messages").as_posix())
    assert relative(tmp_path, found) == ["hosts/r1/sub/messages"]
    assert found.errors == () and found.skipped == () and found.listed


def test_a_file_whose_stat_is_refused_is_kept_for_the_open_to_fail(tmp_path: Path) -> None:
    """A directory that is listable but not searchable: the name is known, the file is
    not; it is a failed item like a listed one, never a silent drop."""
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("root stats through a 0444 directory; expected in the root sandbox; CI "
                    "runs it")
    make(tmp_path, "locked/a.log")
    (tmp_path / "locked").chmod(0o444)
    try:
        found = expand((tmp_path / "locked" / "*.log").as_posix())
    finally:
        (tmp_path / "locked").chmod(0o755)
    assert found.files == ((tmp_path / "locked" / "a.log").as_posix(),)
    assert found.errors == () and found.skipped == ()


def test_a_fifo_is_passed_over(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("no FIFOs on Windows; runs in the sandbox and on CI")
    make(tmp_path, "a.log")
    os.mkfifo(tmp_path / "pipe.log")
    found = expand((tmp_path / "*.log").as_posix())
    assert relative(tmp_path, found) == ["a.log"]
    assert found.skipped == (((tmp_path / "pipe.log").as_posix(), "a FIFO"),)


# -- a directory that cannot be listed ---------------------------------------------------


def test_unlistable_directory_is_an_error_not_silence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``glob.glob`` returns nothing for it (measured as nobody); this must not."""
    make(tmp_path, "hosts/r1/messages", "hosts/r2/messages")
    locked = (tmp_path / "hosts" / "r1").as_posix()
    real_scandir = os.scandir

    def scandir(path: str = ".") -> object:
        if os.path.normcase(path) == os.path.normcase(locked):
            raise PermissionError(13, "Permission denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    found = expand((tmp_path / "hosts" / "*" / "*").as_posix())
    assert relative(tmp_path, found) == ["hosts/r2/messages"]
    assert found.errors == (f"cannot list {locked} (Permission denied)",)


def test_unlistable_directory_natively(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("root lists a 0000 directory; expected in the root sandbox; CI runs it")
    make(tmp_path, "locked/a.log")
    (tmp_path / "locked").chmod(0o000)
    try:
        found = expand((tmp_path / "locked" / "*.log").as_posix())
    finally:
        (tmp_path / "locked").chmod(0o755)
    assert found.files == ()
    assert found.errors == (f"cannot list {(tmp_path / 'locked').as_posix()} (Permission denied)",)


# -- rotated copies ----------------------------------------------------------------------


@pytest.mark.parametrize("name, suffix", [
    ("router.log.1", ".1"), ("router.log.2.gz", ".2"), ("router.log.0", ".0"),
    ("router.log-20260915", "-20260915"), ("router.log-20260915.xz", "-20260915"),
    ("router.log-2026-09-15", "-2026-09-15"),
    ("router.log-20260915-1789440820", "-20260915-1789440820"),
    ("router.log.2026-09-15", ".2026-09-15"),
    ("router.log.2026-09-15_10-00-00", ".2026-09-15_10-00-00"),
    ("router.log.20260915T100000", ".20260915T100000"), ("router.log.20260915", ".20260915"),
    ("access_log.1726358400", ".1726358400"), ("router.log-1726358400.bz2", "-1726358400"),
    ("app.2024", ".2024"),
    ("router.log.gz", ".gz"), ("router.log.bak", ".bak"), ("messages-old", "-old"),
    ("router.log.bak.gz", ".bak"), ("router.log.orig", ".orig"), ("messages.xz", ".xz"),
    ("192.0.2.1", None), ("198.51.100.254", None), ("2026-09-15", None), ("2001-db8--1", None),
    ("router1.2", None), ("python3.12", None), ("sshd-4.9", None), ("router1-20260915", None),
    ("router.log", None), ("router2.log", None), (".bak", None), (".gz", None),
    ("worker.2.log", None), ("access-20260916.log", None),  # the extension form needs a base
    ("web-2026-09-15.log", None), ("2026-09-15.log", None), ("router.log.10000", None),
    ("messages", None), ("router.log-20261340", None),
])
def test_archive_suffix_is_the_rotation_modules_own_recognition(
    name: str, suffix: str | None
) -> None:
    assert archive_suffix(name) == suffix


def test_rotated_copies_are_left_out_by_shape_and_by_kin(tmp_path: Path) -> None:
    make(tmp_path, "router.log", "router.log.1", "router.log.2.gz", "router.log-20260915",
         "router.log.bak", "router.log.gz", "router2.log", "messages", "messages-old",
         "web-2026-09-15.log", "access_log.1726358400")
    found = expand((tmp_path / "*").as_posix())
    assert relative(tmp_path, found) == ["messages", "router.log", "router2.log",
                                         "web-2026-09-15.log"]
    assert [os.path.basename(p) for p in found.archives] == [
        "access_log.1726358400", "messages-old", "router.log-20260915", "router.log.1",
        "router.log.2.gz", "router.log.bak", "router.log.gz",
    ]


def test_shape_holds_while_the_live_file_is_absent(tmp_path: Path) -> None:
    """A ``nocreate`` window: ``.1`` must not become a file of its own because ``router.log``
    is gone -- or the next rotation would replace it and the catch-up would re-read it."""
    make(tmp_path, "router.log.1", "router.log.2.gz")
    found = expand((tmp_path / "router.log*").as_posix())
    assert found.files == ()
    assert [os.path.basename(p) for p in found.archives] == ["router.log.1", "router.log.2.gz"]


def test_the_extension_form_is_kin_of_its_base_only(tmp_path: Path) -> None:
    """Issue #51: logrotate's `extension .log` names the copies router.1.log and
    router-20260916.log.gz; a glob read the plain one as a file of its own (measured), the
    whole renamed copy mailed. Judged against the matches beside it: worker.2.log alone and
    a rotatelogs-style access-20260916.log without access.log stay files of their own."""
    make(tmp_path, "router.log", "router.1.log", "router.2.log.gz", "router-20260916.log.gz",
         "worker.2.log", "access-20260916.log", "web/access.log", "web/access-20260916.log")
    found = expand((tmp_path / "*").as_posix())
    assert relative(tmp_path, found) == ["access-20260916.log", "router.log", "worker.2.log"]
    assert [os.path.basename(p) for p in found.archives] == [
        "router-20260916.log.gz", "router.1.log", "router.2.log.gz"]
    found = expand((tmp_path / "web" / "*").as_posix())
    assert relative(tmp_path, found) == ["web/access.log"]
    assert [os.path.basename(p) for p in found.archives] == ["access-20260916.log"]


def test_kin_is_judged_within_one_directory(tmp_path: Path) -> None:
    """The kin tier catches the bases the shape rule declines (a base ending in a digit),
    against the names matched in the same directory only."""
    make(tmp_path, "a/router1", "a/router1.2", "a/router1-20260915.gz", "b/router1.2")
    found = expand((tmp_path / "*" / "*").as_posix())
    assert relative(tmp_path, found) == ["a/router1", "b/router1.2"]
    assert [os.path.basename(p) for p in found.archives] == ["router1-20260915.gz",
                                                            "router1.2"]


def test_hosts_are_not_copies_of_each_other(tmp_path: Path) -> None:
    """The ``other`` style is not kin (reproduced in review): a per-host directory holds
    fw and fw-dmz, router1 and router1.example.net, syslog and syslog.log, and files named
    by address -- all live files."""
    names = ["fw", "fw-dmz", "router1", "router1.example.net", "syslog", "syslog.log",
             "192.0.2.1", "198.51.100.7", "2026-09-15", "core", "core-mgmt"]
    make(tmp_path, *names)
    found = expand((tmp_path / "*").as_posix())
    assert relative(tmp_path, found) == sorted(names) and found.archives == ()


def test_include_archives_reads_every_match(tmp_path: Path) -> None:
    make(tmp_path, "access_log.1726358400", "access_log.1726444800", "access_log.bak")
    found = expand((tmp_path / "access_log*").as_posix(), include_archives=True)
    assert relative(tmp_path, found) == ["access_log.1726358400", "access_log.1726444800",
                                         "access_log.bak"]
    assert found.archives == ()


def test_kin_bases_carry_no_duplicates() -> None:
    from logalert.globs import _kin_bases

    bases = _kin_bases("router.log-20260916.log")
    assert len(bases) == len(set(bases))
    assert "router.log" in bases and "router.log-20260916" in bases
