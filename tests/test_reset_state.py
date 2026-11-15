"""``--reset-state``: the operator's way to forget cursors, and the corrupt-state escape hatch."""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from logalert.__main__ import main
from logalert.lock import RunLock
from logalert.state import Cursor, State, load_state, lock_path, timestamp


def config(tmp_path: Path) -> str:
    """A config whose state file lives under tmp_path; returns the config path."""
    log = (tmp_path / "router.log").as_posix()
    text = (
        f"[logalert]\nstate_file = {(tmp_path / 'state' / 'state.json').as_posix()}\n"
        f"log = file:{(tmp_path / 'activity.log').as_posix()}\n"
        # --check-config exits 2 without a usable transport since #11: the interpreter
        # stands in for sendmail (exists, executable, on both platforms)
        f"sendmail_path = {Path(sys.executable).as_posix()}\n"
        "from = alerts@example.net\n"  # no resolver asked for a default From (issue #43)
        "[router-disk]\nsubject = Router disk failure\nto = noc@example.net\n"
        f"files = {log}\npatterns =\n    disk failure\n"
        "[firewall]\nsubject = Denies\nto = noc@example.net\n"
        f"files = {log}\npatterns =\n    DENY\n"
    )
    path = tmp_path / "logalert.conf"
    path.write_text(text, encoding="utf-8", newline="\n")
    (tmp_path / "state").mkdir()
    return str(path)


def seeded(tmp_path: Path) -> tuple[str, Path, str]:
    """A config plus a state file with two sections on one file and one more file."""
    conf = config(tmp_path)
    state_file = tmp_path / "state" / "state.json"
    log = (tmp_path / "router.log").as_posix()
    state = State(str(state_file))
    cursor = Cursor(offset=10, ino=1, dev=2, fingerprint=None, realpath=log,
                    last_seen=timestamp())
    state.set("router-disk", log, cursor)
    state.set("firewall", log, cursor)
    state.set("firewall", "/var/log/other.log", cursor)
    state.save()
    return conf, state_file, log


def test_no_state_file_means_nothing_to_forget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = config(tmp_path)
    assert main(["--reset-state", "-f", conf]) == 0
    assert capsys.readouterr().out.startswith("no state file at ")
    assert not (tmp_path / "state" / "state.json").exists()  # and none was created


def test_reset_everything(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conf, state_file, _ = seeded(tmp_path)
    assert main(["--reset-state", "-f", conf]) == 0
    out = capsys.readouterr().out
    assert out.startswith("forgot 3 cursor(s) for every file;")
    assert load_state(str(state_file)).entries == {}
    with RunLock(lock_path(str(state_file)), 3600, key="state.json"):  # released after the reset
        pass
    assert json.loads(state_file.read_text(encoding="utf-8")) == {
        "version": 1, "entries": {}, "runs": {},
    }


def test_reset_one_file_forgets_every_section_for_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf, state_file, log = seeded(tmp_path)
    assert main(["--reset-state", log, "-f", conf]) == 0
    assert capsys.readouterr().out.startswith(f"forgot 2 cursor(s) for {log};")
    assert sorted(load_state(str(state_file)).entries) == [("firewall", "/var/log/other.log")]


def test_reset_unknown_file_lists_what_the_state_knows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf, state_file, log = seeded(tmp_path)
    before = state_file.stat()
    typo = (tmp_path / "typo.log").as_posix()
    assert main(["--reset-state", typo, "-f", conf]) == 1
    err = capsys.readouterr().err
    known = ", ".join(sorted(["/var/log/other.log", log]))  # the order depends on tmp_path
    assert err == (f"logalert: no entry for {typo}; the state file knows {known}"
                   f" -- and no section lists that file\n")
    after = state_file.stat()  # nothing rewritten: a rewrite is a new inode
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    # a configured file with no cursor yet reads the same way, minus the config note --
    # which is why this is exit 1 (state-dependent), not 2 (a wrong command line)
    assert main(["--reset-state", log, "-f", conf]) == 0  # (it has entries: forgotten)
    capsys.readouterr()
    assert main(["--reset-state", log, "-f", conf]) == 1
    assert capsys.readouterr().err == (f"logalert: no entry for {log}; the state file knows "
                                       f"/var/log/other.log\n")


def test_corrupt_state_needs_the_bare_form(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf, state_file, log = seeded(tmp_path)
    state_file.write_text("{oops", encoding="utf-8")
    assert main(["--reset-state", log, "-f", conf]) == 1
    err = capsys.readouterr().err
    spelled = state_file.as_posix()  # as the config spells it, which is how it is reported
    assert err.startswith(f"logalert: state file {spelled}: not valid JSON")
    assert err.endswith(" -- --reset-state with no PATH replaces the file\n")
    assert "fix it or start over" not in err  # the generic hint names the running command
    assert state_file.read_text(encoding="utf-8") == "{oops"
    assert main(["--reset-state", "-f", conf]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"state file {spelled}: not valid JSON")
    assert out.endswith("; replaced it with an empty state\n")
    assert load_state(str(state_file)).entries == {}


def test_reset_is_refused_while_a_run_holds_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf, state_file, _ = seeded(tmp_path)
    with RunLock(lock_path(str(state_file)), 3600, key="state.json"):
        assert main(["--reset-state", "-f", conf]) == 1
    err = capsys.readouterr().err
    assert err.startswith("logalert: another run (PID ") and err.endswith("; nothing was reset\n")
    assert len(load_state(str(state_file)).entries) == 3


def test_reset_with_a_missing_state_directory_is_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = config(tmp_path)
    (tmp_path / "state").rmdir()
    assert main(["--reset-state", "-f", conf]) == 0
    assert capsys.readouterr().out.startswith("no state file at ")
    assert not (tmp_path / "state").exists()


def test_bad_config_is_a_usage_error_before_any_state_is_touched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf, state_file, _ = seeded(tmp_path)
    Path(conf).write_text("[logalert]\n", encoding="utf-8", newline="\n")
    assert main(["--reset-state", "-f", conf]) == 2
    assert capsys.readouterr().err.startswith("logalert: ")
    assert len(load_state(str(state_file)).entries) == 3


def test_check_config_never_creates_the_state_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf = config(tmp_path)
    assert main(["--check-config", "-f", conf]) == 0
    assert "state_file: " in capsys.readouterr().out
    assert list((tmp_path / "state").iterdir()) == []


def test_help_names_reset_state(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    assert "--reset-state [PATH]" in capsys.readouterr().out


# -- the review's pins: every failure is one `logalert:` line, never a traceback ------------


def test_reset_reports_an_unwritable_state_directory_before_taking_the_lock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conf, state_file, _ = seeded(tmp_path)
    before = state_file.read_bytes()

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("logalert.state.tempfile.mkstemp", denied)
    assert main(["--reset-state", "-f", conf]) == 1
    err = capsys.readouterr().err
    assert err.startswith("logalert: state directory ") and "not writable" in err
    assert state_file.read_bytes() == before
    assert not Path(lock_path(str(state_file))).exists()  # the check precedes the lock


def test_reset_reports_a_write_failure_after_the_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    conf, state_file, _ = seeded(tmp_path)
    before = state_file.read_bytes()

    def full(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("logalert.state.os.replace", full)
    assert main(["--reset-state", "-f", conf]) == 1
    err = capsys.readouterr().err
    assert err == (f"logalert: state file {state_file.as_posix()}: cannot write "
                   f"(No space left on device)\n")
    assert state_file.read_bytes() == before


def test_reset_reports_a_lock_file_it_cannot_open(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the realistic Linux shape: a root run left <state dir>/lock root-owned
    conf, state_file, _ = seeded(tmp_path)
    real_open = os.open

    def denied(path: Any, *args: Any, **kwargs: Any) -> int:
        if os.path.basename(str(path)) == "lock":
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("logalert.lock.os.open", denied)
    assert main(["--reset-state", "-f", conf]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"logalert: cannot take the run lock {lock_path(state_file.as_posix())} "
                          f"(Permission denied) -- it must belong to the user logalert runs as")
    assert len(load_state(str(state_file)).entries) == 3


def test_reset_does_not_read_a_permission_problem_as_absence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.path.exists() answers False for EACCES; a false "nothing to forget" would exit 0
    conf, state_file, _ = seeded(tmp_path)
    real_stat = os.stat

    def denied(path: Any, *args: Any, **kwargs: Any) -> Any:
        if str(path) == state_file.as_posix():
            raise PermissionError(13, "Permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr("logalert.__main__.os.stat", denied)
    assert main(["--reset-state", "-f", conf]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == (f"logalert: state file {state_file.as_posix()}: cannot stat "
                       f"(Permission denied) -- are you the user logalert runs as?\n")


def test_reset_names_a_stale_holder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf, state_file, _ = seeded(tmp_path)
    lock = RunLock(lock_path(str(state_file)), 3600, key="state.json")
    lock.acquire(now=time.time() - 7200)
    try:
        assert main(["--reset-state", "-f", conf]) == 1
    finally:
        lock.release()
    err = capsys.readouterr().err
    assert " (older than lock_stale -- a stuck run?); nothing was reset\n" in err


def test_the_three_exit_modes_exclude_one_another(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf, state_file, _ = seeded(tmp_path)
    before = state_file.read_bytes()
    for argv in (["--check-config", "--reset-state"], ["--example-config", "--reset-state"],
                 ["--check-config", "--example-config"]):
        with pytest.raises(SystemExit) as exc:
            main(argv + ["-f", conf])
        assert exc.value.code == 2
        assert "not allowed with" in capsys.readouterr().err
    assert state_file.read_bytes() == before


def test_a_relative_reset_path_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conf, state_file, _ = seeded(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--reset-state", "router.log", "-f", conf])
    assert exc.value.code == 2
    assert "'router.log' is not an absolute path" in capsys.readouterr().err
    assert len(load_state(str(state_file)).entries) == 3


def test_reset_as_root_against_another_users_state_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the trap: mkstemp + os.replace would hand the file to root; the cron user could not
    # read it next run. Both platforms drive the refusal through the helper; the real
    # euid/owner check runs in the root sandbox (tests/test_state.py)
    conf, state_file, _ = seeded(tmp_path)
    before = state_file.read_bytes()
    monkeypatch.setattr("logalert.state._foreign_owner", lambda path: "logalert")
    assert main(["--reset-state", "-f", conf]) == 1
    err = capsys.readouterr().err
    assert err == (f"logalert: state file {state_file.as_posix()} belongs to logalert; a run "
                   f"as root would leave it root-owned and unreadable by that user -- run as "
                   f"that user instead: sudo -u logalert logalert ...\n")
    assert state_file.read_bytes() == before
    assert not Path(lock_path(str(state_file))).exists()  # refused before the lock


def test_unreadable_state_is_replaced_by_the_bare_form_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # the escape hatch needs only directory write permission; nothing may refuse it earlier
    conf, state_file, log = seeded(tmp_path)

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("logalert.state.open", denied, raising=False)
    assert main(["--reset-state", log, "-f", conf]) == 1
    err = capsys.readouterr().err
    assert err == (f"logalert: state file {state_file.as_posix()}: cannot read (Permission "
                   f"denied) -- is it owned by another user? -- --reset-state with no PATH "
                   f"replaces the file\n")
    assert main(["--reset-state", "-f", conf]) == 0
    assert capsys.readouterr().out.endswith("; replaced it with an empty state\n")
    monkeypatch.undo()
    assert load_state(str(state_file)).entries == {}


def test_unreadable_state_is_replaced_for_real(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("expected in the root sandbox; CI runs it")
    conf, state_file, log = seeded(tmp_path)
    state_file.chmod(0)
    assert main(["--reset-state", log, "-f", conf]) == 1
    assert "is it owned by another user?" in capsys.readouterr().err
    assert main(["--reset-state", "-f", conf]) == 0
    assert load_state(str(state_file)).entries == {}  # readable again: a new 0600 file
