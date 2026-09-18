"""The run (issue #12): end to end through ``main()`` over fixture logs, the fake sendmail
behind ``sendmail_path``, the exit codes, the one stderr line, dry-run, the lock, expiry.

Real on both platforms; the POSIX-only tests (a 0500 state directory, a planted lock
symlink, another user's state directory -- the first and last also skipped in the root
sandbox -- the scan-timeout test of issue #28 and the SIGTERM tests of issue #33) and the
one Windows-only test (a read-only state file) say so.
"""

import errno
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import NL, SENDER, Site, body_of, run_logalert
from esmtp_stub import StubConfig, run_stub

import logalert.__main__
import logalert.cursor
import logalert.run
import logalert.state
from logalert.cursor import LogFile, anchor_of
from logalert.lock import RunLock
from logalert.match import scan
from logalert.rotation import open_source
from logalert.run import Outcome
from logalert.state import (
    STATE_HEADROOM,
    Cursor,
    State,
    StateError,
    load_state,
    lock_path,
    timestamp,
)

# -- the clean runs -----------------------------------------------------------------------------


def test_matches_in_two_sections_are_two_mails_state_advanced_and_silence(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "kernel: disk failure on sda", "quiet again")
    site.append(site.firewall, "DENY 192.0.2.7", "ALLOW 192.0.2.8")
    assert site.run() == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    calls = site.calls()
    assert [argv["recipients"] for argv, _ in calls] == [["noc@example.net"], ["fw@example.net"]]
    assert [argv["envelope_from"] for argv, _ in calls] == [SENDER, SENDER]
    assert "3: kernel: disk failure on sda" in body_of(calls[0][1])
    assert "2: DENY 192.0.2.7" in body_of(calls[1][1]) and "ALLOW" not in body_of(calls[1][1])
    assert site.offset("router-disk", site.router) == site.router.stat().st_size
    assert site.offset("firewall", site.firewall) == site.firewall.stat().st_size


def test_no_matches_is_no_mail_and_the_state_still_advances(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    before = site.offset("router-disk", site.router)
    site.append(site.router, "quiet", "quiet")
    assert site.run() == 0
    assert capsys.readouterr() == ("", "")
    assert site.calls() == []
    assert site.offset("router-disk", site.router) == site.router.stat().st_size > before


def test_first_sight_starts_at_the_end_unless_from_start(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.append(site.router, "kernel: disk failure early")
    assert site.run() == 0 and site.calls() == []  # first sight: skipped to the end
    assert site.offset("router-disk", site.router) == site.router.stat().st_size
    site.state_file.unlink()
    assert site.run("--from-start") == 0
    (argv, stdin), = site.calls()
    assert argv["recipients"] == ["noc@example.net"]
    assert "3: kernel: disk failure early" in body_of(stdin)
    assert capsys.readouterr() == ("", "")


# -- the failures and the one line --------------------------------------------------------------


def test_an_unreadable_file_is_named_the_rest_is_processed_its_cursor_untouched(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    # the unreadable file comes FIRST in its section, with a readable sibling after it
    sibling = site.root / "fw2.log"
    sibling.write_text("up" + NL, encoding="utf-8", newline=NL)
    site.write_config(firewall_files=site.firewall.as_posix() + NL + f"    {sibling.as_posix()}")
    site.prime()
    primed = site.offset("firewall", site.firewall)
    site.append(site.router, "disk failure now")
    site.append(site.firewall, "DENY 192.0.2.9")
    site.append(sibling, "DENY 192.0.2.10")
    real_open = logalert.cursor.open_log

    def refuse(path: str, **kwargs: Any) -> Any:
        if Path(path) == site.firewall:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
        return real_open(path, **kwargs)

    monkeypatch.setattr(logalert.cursor, "open_log", refuse)
    assert site.run() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == (f"logalert: 2 of 2 sections sent; failed: [firewall] "
                       f"{site.firewall.as_posix()}: Permission denied; see the log" + NL)
    (router, _), (firewall, stdin) = site.calls()
    assert router["recipients"] == ["noc@example.net"]
    assert firewall["recipients"] == ["fw@example.net"]  # the sibling was still read
    assert "DENY 192.0.2.10" in body_of(stdin) and "192.0.2.9" not in body_of(stdin)
    assert site.offset("router-disk", site.router) == site.router.stat().st_size
    assert site.offset("firewall", site.firewall) == primed  # untouched
    assert site.offset("firewall", sibling) == sibling.stat().st_size


def test_a_failed_delivery_keeps_that_sections_place_and_a_rerun_resends_it_only(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    # age the firewall entry past half of state_ttl (30 days), where a refused delivery
    # still touches it (issue #64: a younger entry keeps the moment its position was
    # taken, which bounds what a rotation gap reads back)
    state = load_state(str(site.state_file))
    aged = state.get("firewall", site.firewall.as_posix())
    assert aged is not None
    state.set("firewall", site.firewall.as_posix(),
              replace(aged, last_seen=timestamp(datetime.now(UTC) - timedelta(days=16))))
    state.save()
    primed = site.state()["firewall"][site.firewall.as_posix()]
    site.append(site.router, "disk failure now")
    site.append(site.firewall, "DENY 192.0.2.9")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT_IF_RCPT", "fw@example.net")
    assert site.run() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ("logalert: 1 of 2 sections sent; failed: [firewall] sendmail exit 75 "
                       "(EX_TEMPFAIL): sendmail: bad mail input format; see the log" + NL)
    assert site.offset("router-disk", site.router) == site.router.stat().st_size
    after = site.state()["firewall"][site.firewall.as_posix()]
    assert after["offset"] == primed["offset"]  # not advanced
    assert after["last_seen"] > primed["last_seen"]  # but touched: it never expires
    monkeypatch.delenv("LOGALERT_FAKE_EXIT")
    monkeypatch.delenv("LOGALERT_FAKE_EXIT_IF_RCPT")
    assert site.run() == 0
    assert capsys.readouterr() == ("", "")
    assert [argv["recipients"] for argv, _ in site.calls()] == [["noc@example.net"],
                                                                 ["fw@example.net"],
                                                                 ["fw@example.net"]]
    assert "DENY 192.0.2.9" in body_of(site.calls()[-1][1])


def test_an_unwritable_state_directory_sends_nothing(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "disk failure now")
    monkeypatch.setattr(logalert.run, "check_state_dir",
                        lambda path: (_ for _ in ()).throw(StateError(f"{path}: no")))
    assert site.run() == 1
    out = capsys.readouterr()
    assert out.err == (f"logalert: failed: state directory: {site.state_file.as_posix()}: no; "
                       "see the log" + NL)
    assert site.calls() == []


def test_a_state_directory_that_refuses_the_lock_is_named(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("expected in the root sandbox; CI runs it")
    site.prime()
    site.append(site.router, "disk failure now")
    (site.state_dir / "lock").unlink()
    site.state_dir.chmod(0o500)
    try:
        assert site.run() == 1
    finally:
        site.state_dir.chmod(0o700)
    err = capsys.readouterr().err
    assert err.startswith(f"logalert: failed: state directory: state directory "
                          f"{site.state_dir.as_posix()} is not writable (Permission denied) -- "
                          f"nothing was sent")
    assert site.calls() == [] and not (site.state_dir / "lock").exists()


def test_a_corrupt_state_file_is_a_hard_error_naming_reset_state(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.state_file.write_text("{not json", encoding="utf-8")
    site.append(site.router, "disk failure now")
    assert site.run() == 1
    err = capsys.readouterr().err
    assert err.startswith("logalert: failed: state file: state file ") and "not valid JSON" in err
    assert "--reset-state" in err and err.endswith("; see the log" + NL) and err.count(NL) == 1
    assert site.calls() == []


def test_a_stale_lock_is_exit_1_a_fresh_holder_exit_0_and_quiet(
        site: Site, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.run")
    site.prime()
    lock = RunLock(str(site.state_dir / "lock"), 3600, key="state.json")
    lock.acquire()
    try:
        assert site.run() == 0
        assert capsys.readouterr() == ("", "")
        assert any("this run exits quietly" in m for m in caplog.messages)
    finally:
        lock.release()
    stale = RunLock(str(site.state_dir / "lock"), 3600, key="state.json")
    stale.acquire(now=time.time() - 7200)
    try:
        assert site.run() == 1
    finally:
        stale.release()
    err = capsys.readouterr().err
    assert err.startswith(f"logalert: failed: stale lock: another run (PID {os.getpid()}) has "
                          f"held the lock ") and err.endswith("; see the log" + NL)


def test_state_not_saved_after_a_delivery_is_named_loudly(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "disk failure now")
    real_save = State.save
    # the opening save (issue #30) passes; the section's, the second, fails; later ones work
    failures = iter([None, StateError("disk full")])

    def flaky(self: State) -> None:
        problem = next(failures, None)
        if problem is not None:
            raise problem
        real_save(self)

    monkeypatch.setattr(State, "save", flaky)
    assert site.run() == 1
    assert capsys.readouterr().err == ("logalert: 1 of 1 section sent; failed: [router-disk] "
                                       "state not saved: disk full; see the log" + NL)
    assert len(site.calls()) == 1  # the mail went out; the next run re-sends
    assert site.offset("firewall", site.firewall) == site.firewall.stat().st_size  # went on


def test_a_refused_recipient_is_delivered_named_and_the_state_moves(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "disk failure now")
    with run_stub(StubConfig(refuse=frozenset({"ops@example.net"}))) as server:
        site.write_config(f"transport = smtp\nsmtp_host = 127.0.0.1\nsmtp_port = {server.port}\n",
                          router_to="noc@example.net, ops@example.net")
        assert site.run() == 1
        server.wait_idle()
    assert capsys.readouterr().err == (
        "logalert: 1 of 1 section sent; failed: [router-disk] refused: ops@example.net -- 550 "
        "5.1.1 <ops@example.net>: Recipient address rejected: User unknown; see the log" + NL)
    assert len(server.data) == 1
    assert site.offset("router-disk", site.router) == site.router.stat().st_size


def test_usage_and_configuration_errors_are_exit_2_before_anything_runs(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        site.run("-c", "-1")
    assert exc.value.code == 2 and "-c: -1 is not a number of lines" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        site.run("--state-file", "relative/state.json")
    assert exc.value.code == 2 and "is not an absolute path" in capsys.readouterr().err
    site.write_config("transport = smtp\n")  # smtp_host missing: the loader's error
    assert site.run() == 2
    assert capsys.readouterr().err.startswith("logalert: [logalert] smtp_host: required")
    site.write_config(sendmail=(site.root / "none").as_posix())
    site.append(site.router, "disk failure now")
    assert site.run() == 2
    assert capsys.readouterr().err.startswith("logalert: transport = auto but ")
    assert not site.state_file.exists() and site.calls() == []


# -- dry run, context, attach, state file, debug -----------------------------------------------


def test_dry_run_prints_the_mails_sends_nothing_and_writes_nothing(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    written = site.state_file.read_bytes()
    site.append(site.router, "disk failure now")
    site.append(site.firewall, "DENY 192.0.2.9")
    recorded = site.activity()
    assert site.run("--dry-run") == 0
    out = capsys.readouterr()
    # the dry run's log goes to stderr (issue #13), never to the configured destination
    assert out.err.splitlines()[0].startswith("logalert: start: ")
    assert out.err.splitlines()[0].endswith("2 section(s) (dry run)")
    assert out.err.splitlines()[-1] == "logalert: end: exit 0"
    assert site.activity() == recorded
    assert "Subject: Router disk failure -- 1 match(es)" + NL in out.out
    assert "Subject: Firewall denies -- 1 match(es)" + NL in out.out
    assert "3: disk failure now" in out.out and "=?utf-8?" not in out.out
    assert site.calls() == [] and site.state_file.read_bytes() == written
    # a dry run needs no state directory at all and takes no lock
    elsewhere = site.root / "nowhere" / "state.json"
    assert site.run("-n", "--state-file", str(elsewhere)) == 0
    assert not (site.root / "nowhere").exists()
    assert "logging to stderr" not in capsys.readouterr().err  # no fallback: nothing to open


def test_context_from_the_command_line_and_the_sections_override(
        site: Site) -> None:
    site.write_config(firewall="context = 0\n")
    site.prime()
    site.append(site.router, "before", "disk failure now", "after")
    site.append(site.firewall, "before", "DENY 192.0.2.9", "after")
    assert site.run("-c", "1") == 0
    (_, router), (_, firewall) = site.calls()
    assert "3- before" + NL + "4: disk failure now" + NL + "5- after" in body_of(router)
    assert "2- before" not in body_of(firewall) and "3: DENY 192.0.2.9" in body_of(firewall)


def test_attach_and_state_file_override(site: Site, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert site.run("--state-file", str(other / "state.json")) == 0
    assert (other / "state.json").exists() and not site.state_file.exists()
    site.append(site.router, "disk failure now")
    assert site.run("--state-file", str(other / "state.json"), "--attach") == 0
    (_, stdin), = site.calls()
    assert b"multipart/mixed" in stdin and b'filename="router-disk-' in stdin


def test_debug_puts_the_activity_log_on_stderr(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    logger = logging.getLogger("logalert")
    handlers_before = list(logger.handlers)
    try:
        assert site.run("--debug") == 0
    finally:
        for handler in logger.handlers:
            if handler not in handlers_before:
                logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
    err = capsys.readouterr().err
    assert "logalert: start: " in err
    assert "logalert: end: exit 0" in err


# -- expiry and rotation ------------------------------------------------------------------------


def test_an_entry_unseen_past_state_ttl_is_forgotten_and_logged(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.run")
    site.write_config("state_ttl = 1\n")
    site.prime()
    gone = "/var/log/gone.log"
    old = timestamp(datetime.now(UTC) - timedelta(days=3))
    state = State(str(site.state_file))
    state.set("router-disk", gone, Cursor(0, 1, 1, None, gone, old))
    state.save()
    assert site.run() == 0
    assert gone not in site.state()["router-disk"]
    assert any(m == f"[router-disk] {gone}: forgotten, unseen for 1 days" for m in caplog.messages)


def test_a_rotation_between_runs_mails_every_line_once(site: Site) -> None:
    site.prime()
    site.append(site.router, "disk failure 1")
    rotated = site.router.with_name("router.log.1")
    os.replace(site.router, rotated)
    site.router.write_text("disk failure 2" + NL, encoding="utf-8", newline=NL)
    assert site.run() == 0
    (_, stdin), = site.calls()
    body = body_of(stdin)
    assert body.count("disk failure 1") == 1 and body.count("disk failure 2") == 1
    assert site.offset("router-disk", site.router) == site.router.stat().st_size


# -- the installed console script ---------------------------------------------------------------


def test_the_installed_console_script_runs_the_clean_case(site: Site) -> None:
    script = shutil.which("logalert", path=str(Path(sys.executable).parent))
    if script is None:
        pytest.skip("no console script beside the interpreter (not an installed venv)")
    site.prime()
    site.append(site.router, "disk failure now")
    result = subprocess.run([script, "-f", str(site.conf)], capture_output=True,
                            encoding="utf-8", errors="replace", check=False)
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
    (argv, _), = site.calls()
    assert argv["recipients"] == ["noc@example.net"]


# -- the summary line's grammar -----------------------------------------------------------------


def test_the_summary_line_grammar() -> None:
    one = Outcome(due=1, sent=0, failed=["[a] x"])
    assert one.summary() == "logalert: 0 of 1 section sent; failed: [a] x; see the log"
    two = Outcome(due=3, sent=2, failed=["[a] x", "[b] y"])
    assert two.summary() == "logalert: 2 of 3 sections sent; failed: [a] x; [b] y; see the log"
    none = Outcome()
    none.fail("state directory: no" + NL + "forged")
    assert none.summary() == "logalert: failed: state directory: no forged; see the log"
    dry = Outcome(due=2, sent=2, dry_run=True, failed=["[a] x"])
    assert dry.summary() == "logalert: 2 of 2 sections would be sent; failed: [a] x; see the log"


# -- the review round's pins ------------------------------------------------------------------


def test_a_first_sight_file_in_a_failed_section_keeps_its_position(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    # a touch is a no-op without an entry: the file would be first-sighted again next run
    # and everything written in between lost; a file that matched nothing may move
    site.prime()
    new = site.root / "fw-new.log"
    new.write_text("fresh" + NL, encoding="utf-8", newline=NL)
    site.write_config(firewall_files=site.firewall.as_posix() + NL + f"    {new.as_posix()}")
    site.append(site.firewall, "DENY 192.0.2.9")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT_IF_RCPT", "fw@example.net")
    assert site.run() == 1
    assert site.offset("firewall", new) == new.stat().st_size  # pinned at its first sight
    site.append(new, "DENY 192.0.2.77 written between the runs")
    monkeypatch.delenv("LOGALERT_FAKE_EXIT")
    monkeypatch.delenv("LOGALERT_FAKE_EXIT_IF_RCPT")
    assert site.run() == 0
    _, stdin = site.calls()[-1]  # the refused attempt was recorded too
    assert "DENY 192.0.2.9" in body_of(stdin) and "192.0.2.77 written between" in body_of(stdin)


def test_the_run_only_flags_refuse_the_modes(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    written = site.state_file.read_bytes()
    for argv in (["-n", "--reset-state"], ["-n", "--test-mail", "router-disk"],
                 ["--attach", "--check-config"], ["-c", "2", "--reset-state"]):
        with pytest.raises(SystemExit) as exc:
            site.run(*argv)
        assert exc.value.code == 2
        assert "apply to the run, not to" in capsys.readouterr().err
    assert site.state_file.read_bytes() == written and site.calls() == []
    # --example-config is not in the mode set that refuses the run-only flags: -n beside it
    # is ignored and the example printed, exit 0 (issue #41: unpinned)
    assert site.run("--example-config", "-n") == 0
    assert capsys.readouterr().out.startswith("# logalert configuration")


def test_state_file_override_reaches_reset_state_and_check_config(
        site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = tmp_path / "other"
    other.mkdir()
    target = str(other / "state.json")
    assert site.run("--state-file", target) == 0
    assert site.run("--state-file", target, "--reset-state") == 0
    assert capsys.readouterr().out.startswith("forgot 2 cursor(s)")
    assert json.loads((other / "state.json").read_text(encoding="utf-8"))["entries"] == {}
    assert not site.state_file.exists()  # the configured file was never touched
    assert site.run("--state-file", target, "--check-config") == 0
    assert f"state_file: {target} (--state-file)" + NL in capsys.readouterr().out


def test_a_missing_state_directory_gets_the_remedy_not_the_locks_errno(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    nowhere = site.root / "nowhere" / "state.json"
    site.append(site.router, "disk failure now")
    assert site.run("--state-file", str(nowhere)) == 1
    assert capsys.readouterr().err == (
        f"logalert: failed: state directory: state directory {os.path.dirname(str(nowhere))} "
        "does not exist -- create it, owned by the user logalert runs as; see the log" + NL)
    assert not (site.root / "nowhere").exists() and site.calls() == []


def test_a_state_file_that_is_the_config_or_a_directory_is_refused_before_the_lock(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.append(site.router, "disk failure now")
    before = site.conf.read_bytes()
    assert site.run("--state-file", str(site.conf)) == 2
    assert capsys.readouterr().err == (f"logalert: state_file: {site.conf} is the config file "
                                       "itself" + NL)
    assert site.run("--state-file", str(site.conf), "--reset-state") == 2
    capsys.readouterr()
    assert site.conf.read_bytes() == before
    assert not (site.root / "lock").exists()
    assert site.run("--state-file", str(site.state_dir)) == 1
    err = capsys.readouterr().err
    assert err == (f"logalert: failed: state directory: state file {site.state_dir} "
                   "is not a regular file; see the log" + NL)
    assert not (site.root / "lock").exists() and site.calls() == []


def test_every_failed_item_is_logged_at_error_once(
        site: Site, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.run")
    site.prime()
    site.append(site.firewall, "DENY 192.0.2.9")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    assert site.run() == 1
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [
        "[firewall] not delivered (" + errors[0].split("(")[1].split(")")[0] + "): sendmail exit"
        " 75 (EX_TEMPFAIL): sendmail: bad mail input format",
        "[firewall] sendmail exit 75 (EX_TEMPFAIL): sendmail: bad mail input format"]
    assert errors[0].split("(")[1].startswith("<")  # the Message-ID rides along
    assert caplog.messages[-1] == "end: exit 1"


def test_the_log_frames_every_exit_including_a_configuration_error_and_a_fresh_holder(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.run")
    site.write_config(sendmail=(site.root / "none").as_posix())
    assert site.run() == 2
    assert caplog.messages[0].startswith("start: ") and caplog.messages[-1] == "end: exit 2"
    assert any(r.levelno == logging.ERROR and "transport = auto but" in r.getMessage()
               for r in caplog.records)
    caplog.clear()
    site.write_config()
    site.prime()
    caplog.clear()
    holder = RunLock(str(site.state_dir / "lock"), 3600, key="state.json")
    holder.acquire()
    try:
        assert site.run() == 0
    finally:
        holder.release()
    assert caplog.messages[-1] == "end: exit 0"


def test_a_save_failure_says_whether_a_mail_went_out(
        site: Site, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.ERROR, logger="logalert.run")
    site.prime()
    site.append(site.router, "quiet again")  # nothing matched: no mail
    real_save = State.save
    opening: set[int] = set()  # the states whose first save, the opening one, is due

    def load(path: str) -> State:
        state = load_state(path)
        opening.add(id(state))
        return state

    def save(self: State, reach: int | None = None) -> None:
        if id(self) in opening:  # the opening save (issue #30) passes; the section's fails
            opening.discard(id(self))
            real_save(self)
            return
        raise StateError("full")

    monkeypatch.setattr("logalert.run.load_state", load)
    monkeypatch.setattr(State, "save", save)
    assert site.run() == 1
    assert not any("the mail went out" in m for m in caplog.messages)
    caplog.clear()
    site.append(site.router, "disk failure now")
    assert site.run() == 1
    assert any(m == "[router-disk] the mail went out; unless a later save in this run "
               "succeeds, the next run re-sends it" for m in caplog.messages)


def test_dry_run_words_the_expiry_as_conditional(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.run")
    site.write_config("state_ttl = 1" + NL)
    site.prime()
    gone = "/var/log/gone.log"
    state = load_state(str(site.state_file))
    state.set("router-disk", gone, Cursor(0, 1, 1, None, gone,
                                          timestamp(datetime.now(UTC) - timedelta(days=3))))
    state.save()
    assert site.run("-n") == 0
    assert f"[router-disk] {gone}: would be forgotten, unseen for 1 days" in caplog.messages
    assert gone in site.state()["router-disk"]


def _records_of_the_last_run(site: Site) -> list[str]:
    """The activity log from the last start line on: what one run recorded."""
    records = site.activity()
    starts = [i for i, r in enumerate(records) if r.startswith("start: ")]
    return records[starts[-1]:]


def test_an_interrupt_is_one_line_and_exit_130_with_the_lock_released(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "disk failure now")

    def interrupt(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(logalert.run, "deliver", interrupt)
    assert site.run() == 130
    assert capsys.readouterr().err == "logalert: interrupted" + NL
    probe = RunLock(str(site.state_dir / "lock"), 3600, key="state.json")
    probe.acquire()  # LockBusy here would mean the interrupted run kept it
    probe.release()
    # the one record a signalled run leaves (issue #33), and no end line after it
    records = _records_of_the_last_run(site)
    assert [r for r in records if r.startswith(("warning: interrupted", "end:"))] == [
        "warning: interrupted (SIGINT); no lock is held, the state is as last saved"]


def test_debug_output_is_one_clean_line_per_record_and_leaves_no_handler(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    logger = logging.getLogger("logalert")
    before = list(logger.handlers)
    esc, nel = chr(27), chr(0x85)
    text = site.conf.read_text(encoding="utf-8").replace(
        "[router-disk]", "[rou" + esc + "[31m" + nel + "logalert: forged]")
    site.conf.write_text(text, encoding="utf-8", newline=NL)
    assert site.run("--debug") == 0
    assert logger.handlers == before and logger.level == logging.NOTSET
    err = capsys.readouterr().err
    assert esc not in err and nel not in err
    assert err.count(NL) == len(err.splitlines())
    assert not any(line.startswith("logalert: forged") for line in err.splitlines())
    assert "logalert: start: " in err


def test_a_directory_listed_as_a_file_names_the_reason_once(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    directory = site.root / "not-a-log"
    directory.mkdir()
    site.write_config(firewall_files=directory.as_posix())
    assert site.run() == 1
    err = capsys.readouterr().err
    assert err == (f"logalert: failed: [firewall] {directory.as_posix()}: not a regular file "
                   "(a directory); see the log" + NL)


def test_a_planted_lock_symlink_is_refused(site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform == "win32":
        pytest.skip("no O_NOFOLLOW on Windows; runs in the sandbox and on CI")
    victim = site.root / "victim.txt"
    victim.write_text("precious" + NL, encoding="utf-8")
    (site.state_dir / "lock").symlink_to(victim)
    site.append(site.router, "disk failure now")
    assert site.run() == 1
    assert "state directory: " in capsys.readouterr().err
    assert victim.read_text(encoding="utf-8") == "precious" + NL and site.calls() == []


def test_a_root_run_into_another_users_state_directory_is_refused_before_anything(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform == "win32":
        pytest.skip("ownership is fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("expected in the root sandbox; CI runs it")  # tmp_path is root's there
    # the directory is ours (not root's); pretend to be root and the rule must refuse
    monkeypatch.setattr("logalert.state.os.geteuid", lambda: 0)
    monkeypatch.setattr("logalert.state.pwd.getpwuid",
                        lambda uid: type("pw", (), {"pw_name": "cronuser"})())
    site.append(site.router, "disk failure now")
    assert site.run() == 1
    err = capsys.readouterr().err
    assert err.startswith(f"logalert: failed: state directory: state directory "
                          f"{site.state_dir.as_posix()} belongs to cronuser; ")
    assert "sudo -u cronuser logalert" in err
    assert not (site.state_dir / "lock").exists() and not site.state_file.exists()
    assert site.calls() == []


def test_a_read_only_state_file_on_windows_is_refused_before_any_mail(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform != "win32":
        pytest.skip("the read-only attribute is a Windows shape; the dev host runs it")
    site.prime()
    site.append(site.router, "disk failure now")
    os.chmod(site.state_file, 0o444)
    try:
        assert site.run() == 1
    finally:
        os.chmod(site.state_file, 0o644)
    assert capsys.readouterr().err == (
        f"logalert: failed: state directory: state file {site.state_file.as_posix()} is "
        "read-only -- nothing was sent; see the log" + NL)
    assert site.calls() == []


# -- the mutation reviewer's pins: each names the mutation it catches ------------------------


def age(site: Site, section: str, path: Path, days: int) -> dict[str, Any]:
    """Push one entry's last_seen into the past so a touch (same-second otherwise) shows."""
    state = load_state(str(site.state_file))
    cursor = state.get(section, path.as_posix())
    assert cursor is not None
    state.set(section, path.as_posix(),
              replace(cursor, last_seen=timestamp(datetime.now(UTC) - timedelta(days=days))))
    state.save()
    entry: dict[str, Any] = site.state()[section][path.as_posix()]
    return entry


# -- order: the configuration before the lock ---------------------------------------------------


def test_a_configuration_error_is_exit_2_before_the_lock_even_under_a_holder(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    """Kills m01 (choose()/resolve_sender() after the lock): a config error must be exit 2
    with the loader's line even while another run holds the lock, and must create no lock
    file -- nothing ran."""
    site.write_config(sendmail=(site.root / "none").as_posix())
    holder = RunLock(str(site.state_dir / "lock"), 3600, key="state.json")
    holder.acquire()
    try:
        assert site.run() == 2
    finally:
        holder.release()
    err = capsys.readouterr().err
    assert err.startswith("logalert: transport = auto but ")
    (site.state_dir / "lock").unlink()
    assert site.run() == 2
    assert not (site.state_dir / "lock").exists()  # never constructed: nothing ran


# -- the lock is released whatever happens ------------------------------------------------------


def test_the_lock_is_released_after_an_exception_mid_run_and_after_an_early_exit(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Kills m02 (the finally replaced by a fall-through): a probe acquire after the run
    raises LockBusy when the run leaked its descriptor (msvcrt is per handle, flock per
    open file description: a leak is visible in-process on both platforms)."""
    site.prime()
    lock_file = str(site.state_dir / "lock")
    real_open = open_source

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("probe: an unexpected failure inside a section")

    monkeypatch.setattr(logalert.run, "open_source", boom)
    with pytest.raises(RuntimeError):
        site.run()
    probe = RunLock(lock_file, 3600, key="state.json")
    probe.acquire()  # LockBusy here means the run left the lock behind
    probe.release()
    monkeypatch.setattr(logalert.run, "open_source", real_open)
    # the early exits inside the locked region: a corrupt state file
    site.state_file.write_text("{not json", encoding="utf-8")
    assert site.run() == 1
    assert capsys.readouterr().err.count(NL) == 1
    probe.acquire()
    probe.release()


# -- the activity log's lines -------------------------------------------------------------------


def test_the_per_file_info_line_carries_lines_read_then_matched(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    """Kills m05 (the counts swapped)."""
    caplog.set_level(logging.INFO, logger="logalert.run")
    site.prime()
    site.append(site.router, "kernel: disk failure on sda", "quiet", "quiet")
    assert site.run() == 0
    assert f"[router-disk] {site.router.as_posix()}: 3 line(s) read, 1 matched" in caplog.messages
    assert f"[firewall] {site.firewall.as_posix()}: 0 line(s) read, 0 matched" in caplog.messages


def test_the_start_line_says_dry_run(site: Site, caplog: pytest.LogCaptureFixture) -> None:
    """Kills m51 (the start line never marked as a dry run)."""
    caplog.set_level(logging.INFO, logger="logalert.run")
    assert site.run("-n") == 0
    assert f"start: {site.conf}, 2 section(s) (dry run)" in caplog.messages
    caplog.clear()
    assert site.run() == 0
    assert f"start: {site.conf}, 2 section(s)" in caplog.messages


def test_the_from_warning_is_logged(site: Site, caplog: pytest.LogCaptureFixture) -> None:
    """Kills m16 (resolve_sender's warning dropped): a From that will not travel is said
    once per run, at WARNING, on the run's logger."""
    caplog.set_level(logging.WARNING, logger="logalert.run")
    assert site.run("--from", "root@localhost") == 0
    records = [r for r in caplog.records
               if r.name == "logalert.run" and r.levelno == logging.WARNING]
    assert [r.getMessage() for r in records] == [
        "From address 'root@localhost' has no domain part; set from = in [logalert]"]


def test_debug_puts_debug_records_on_stderr(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    """Kills m41 (--debug attaching the handler at INFO)."""
    logger = logging.getLogger("logalert")
    handlers_before = list(logger.handlers)
    try:
        assert site.run("--debug") == 0
    finally:
        for handler in logger.handlers:
            if handler not in handlers_before:
                logger.removeHandler(handler)
        logger.setLevel(logging.NOTSET)
    err = capsys.readouterr().err
    assert "logalert: debug: lock " in err  # the lock's own DEBUG line


# -- a file absent this run, a read that fails mid-stream ---------------------------------------


def test_a_file_absent_this_run_is_not_touched(site: Site) -> None:
    """Kills m06 (an absent file touched): its last_seen must stay, so it expires in time."""
    missing = site.root / "missing.log"
    missing.write_text("up" + NL, encoding="utf-8", newline=NL)
    site.write_config(firewall_files=missing.as_posix())
    site.prime()
    before = age(site, "firewall", missing, days=2)
    missing.unlink()
    assert site.run() == 0
    after = site.state()["firewall"][missing.as_posix()]
    assert after == before  # neither the cursor nor last_seen moved


def test_a_read_that_fails_mid_stream_is_a_failed_item_with_the_cursor_untouched(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Kills m23 (an OSError from lines() logged but not a failed item): a corrupt compressed
    stream or an archive vanishing mid-read is the other unreadable-file path."""
    site.prime()
    primed = site.offset("firewall", site.firewall)
    site.append(site.router, "disk failure now")
    site.append(site.firewall, "DENY 192.0.2.9")
    real_lines = LogFile.lines

    def broken(self: LogFile) -> Any:
        if Path(self.path) == site.firewall:
            raise OSError(errno.EIO, "stream ended early", self.path)
        return real_lines(self)

    monkeypatch.setattr(LogFile, "lines", broken)
    assert site.run() == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == (f"logalert: 1 of 1 section sent; failed: [firewall] "
                       f"{site.firewall.as_posix()}: stream ended early; see the log" + NL)
    (router, _), = site.calls()
    assert router["recipients"] == ["noc@example.net"]
    assert site.offset("firewall", site.firewall) == primed


# -- the seams: before hook, cap, start, archive_dir ---------------------------------------------


def test_context_before_the_saved_position_comes_from_the_hook(site: Site) -> None:
    """Kills m08 (scan without the before hook): a match on the FIRST new line gets its
    leading context from before the saved offset."""
    site.prime()
    site.append(site.router, "disk failure now")
    assert site.run("-c", "1") == 0
    (_, stdin), = site.calls()
    assert "2- quiet" + NL + "3: disk failure now" + NL in body_of(stdin)


def test_scan_is_handed_the_sections_max_lines_as_the_cap(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills m09 (scan without the cap): the mail is the same either way (render caps too),
    so the seam is pinned -- an uncapped report of a 50 MB log holds every match in memory."""
    site.write_config(router="max_lines = 3" + NL)
    site.prime()
    seen: list[int | None] = []
    real_scan = scan

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("cap"))
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(logalert.run, "scan", spy)
    site.append(site.router, *[f"disk failure {n}" for n in range(5)])
    assert site.run() == 0
    assert seen[0] == 3  # router-disk: its own max_lines
    (_, stdin), = site.calls()
    assert "... and 2 more matching line(s)" in body_of(stdin)


def test_start_beginning_reads_a_first_sight_from_0(site: Site) -> None:
    """Kills m27 (start = beginning ignored)."""
    site.write_config(router="start = beginning" + NL)
    site.append(site.router, "kernel: disk failure early")
    assert site.run() == 0
    (argv, stdin), = site.calls()
    assert argv["recipients"] == ["noc@example.net"]
    assert "3: kernel: disk failure early" in body_of(stdin)


def test_archive_dir_is_searched_for_the_rotated_copy(site: Site) -> None:
    """Kills m28 (archive_dir not handed to open_source): a copy rotated into another
    directory (logrotate olddir) is found only through the section's archive_dir."""
    archive = site.root / "archive"
    archive.mkdir()
    site.write_config(router=f"archive_dir = {archive.as_posix()}" + NL)
    site.prime()
    site.append(site.router, "disk failure 1")
    os.replace(site.router, archive / "router.log.1")
    site.router.write_text("disk failure 2" + NL, encoding="utf-8", newline=NL)
    assert site.run() == 0
    (_, stdin), = site.calls()
    body = body_of(stdin)
    assert body.count("disk failure 1") == 1 and body.count("disk failure 2") == 1


# -- the command line reaching the run ----------------------------------------------------------


def test_from_on_the_command_line_is_the_envelope_and_header_from(site: Site) -> None:
    """Kills m32 (--from not handed to the run)."""
    site.prime()
    site.append(site.router, "disk failure now")
    assert site.run("--from", "other@example.net") == 0
    (argv, stdin), = site.calls()
    assert argv["envelope_from"] == "other@example.net"
    assert b"From: other@example.net" in stdin


def test_state_file_override_moves_the_lock_and_the_directory_check(
        site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Kills m17 (the lock beside the CONFIGURED state file) and m17b (check_state_dir on
    the configured one): with the configured directory gone, only the override's counts,
    and a holder of the override's lock turns the run away."""
    other = tmp_path / "other"
    other.mkdir()
    shutil.rmtree(site.state_dir)
    target = str(other / "state.json")
    assert site.run("--state-file", target) == 0
    assert capsys.readouterr() == ("", "")
    assert (other / "lock").exists() and not site.state_dir.exists()
    holder = RunLock(str(other / "lock"), 3600, key="state.json")
    holder.acquire()
    try:
        site.append(site.router, "disk failure now")
        assert site.run("--state-file", target) == 0
    finally:
        holder.release()
    assert capsys.readouterr() == ("", "")
    assert site.calls() == []  # turned away by the override's lock


def test_a_lock_the_directory_refuses_is_named_on_every_platform(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Kills m20 (an OSError from the lock treated as a fresh holder); the 0500 directory
    test is POSIX-only, so on Windows nothing pins this branch."""
    site.prime()
    site.append(site.router, "disk failure now")

    def refuse(self: RunLock, now: float | None = None) -> None:
        raise PermissionError(errno.EACCES, "Permission denied", self.path)

    monkeypatch.setattr(RunLock, "acquire", refuse)
    assert site.run() == 1
    assert capsys.readouterr().err == (
        f"logalert: failed: state directory: Permission denied "
        f"({lock_path(site.state_file.as_posix())}) -- the lock file must belong to the user "
        "logalert runs as; see the log" + NL)
    assert site.calls() == []


# -- the failed delivery touches every file it read ---------------------------------------------


def test_every_file_read_is_touched_when_the_delivery_fails(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills m48 (only the last file touched). The file with the match keeps its place and
    is touched; the quiet sibling moves (none of its lines is in the message)."""
    sibling = site.root / "fw2.log"
    sibling.write_text("up" + NL, encoding="utf-8", newline=NL)
    site.write_config(firewall_files=site.firewall.as_posix() + NL + f"    {sibling.as_posix()}")
    site.prime()
    first = age(site, "firewall", site.firewall, days=16)  # past half of state_ttl (#64)
    second = age(site, "firewall", sibling, days=2)
    site.append(site.firewall, "DENY 192.0.2.9")
    site.append(sibling, "quiet")  # read, nothing matched: still present
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT_IF_RCPT", "fw@example.net")
    assert site.run() == 1
    after = site.state()["firewall"]
    matched = after[site.firewall.as_posix()]
    assert matched["offset"] == first["offset"] and matched["last_seen"] > first["last_seen"]
    quiet = after[sibling.as_posix()]
    assert quiet["offset"] == sibling.stat().st_size and quiet["last_seen"] > second["last_seen"]


# -- dry run's summary --------------------------------------------------------------------------


def test_dry_run_with_an_unreadable_file_is_exit_1_with_the_line(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Kills m12 (a previewed section not counted): the count clause of a dry run."""
    site.prime()
    site.append(site.router, "disk failure now")
    site.append(site.firewall, "DENY 192.0.2.9")
    real_open = logalert.cursor.open_log

    def refuse(path: str, **kwargs: Any) -> Any:
        if Path(path) == site.firewall:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), path)
        return real_open(path, **kwargs)

    monkeypatch.setattr(logalert.cursor, "open_log", refuse)
    assert site.run("-n") == 1
    out = capsys.readouterr()
    assert "Subject: Router disk failure -- 1 match(es)" + NL in out.out
    assert out.err.splitlines()[-1] == (  # after the dry run's log, the one line
        f"logalert: 1 of 1 section would be sent; failed: [firewall] "
        f"{site.firewall.as_posix()}: Permission denied; see the log")
    assert site.calls() == []


# -- issue #50: a glob's quiet files are one summary, not a record each -------------------------


def test_a_globs_quiet_files_are_debug_records_and_one_info_summary(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    """Measured: 2 000 one-line files under one glob wrote 2 002 INFO records per run, every
    run. A glob's file with nothing new is DEBUG; the glob gets one INFO summary; a glob's
    file WITH new lines and every listed file keep their own INFO record."""
    hosts = site.root / "hosts"
    for name in ("alpha", "beta", "gamma"):
        (hosts / name).mkdir(parents=True)
        (hosts / name / "messages").write_text("up" + NL, encoding="utf-8", newline=NL)
    glob = (hosts / "*" / "messages").as_posix()
    site.write_config(firewall_files=site.firewall.as_posix() + NL + f"    {glob}")
    site.prime()
    site.append(hosts / "beta" / "messages", "DENY 192.0.2.9", "DENY 192.0.2.10")  # two
    caplog.set_level(logging.DEBUG, logger="logalert.run")
    assert site.run() == 0
    records = [(r.levelname, r.getMessage()) for r in caplog.records
               if "line(s) read" in r.getMessage() or "file(s)" in r.getMessage()]
    listed = f"[firewall] {site.firewall.as_posix()}: 0 line(s) read, 0 matched"
    busy = f"[firewall] {(hosts / 'beta' / 'messages').as_posix()}: 2 line(s) read, 2 matched"
    assert ("INFO", listed) in records  # a listed file: its heartbeat, new lines or not
    assert ("INFO", busy) in records  # a glob's file with new lines: its own record
    for quiet in ("alpha", "gamma"):
        message = f"[firewall] {(hosts / quiet / 'messages').as_posix()}: 0 line(s) read, 0 matched"
        assert ("DEBUG", message) in records and ("INFO", message) not in records
    assert ("INFO", f"[firewall] {glob}: 3 file(s), 1 with new lines, 2 matched") in records
    assert len(site.calls()) == 1  # the match was mailed as before


# -- issue #28: a file's scan is bounded by scan_timeout ----------------------------------------


def test_a_regex_that_hangs_is_a_failed_item_within_scan_timeout_and_the_cursor_stays(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    """The measured hang: ^(\\w+\\s?)+failed$ over a line of plain words takes hours from
    thirty characters on; with scan_timeout = 1 the file is a failed item naming the line
    and the pattern, the sibling section runs, the cursor stays (the operator's escapes are
    the documented three), the timer is disarmed afterwards. POSIX only: SIGALRM."""
    if sys.platform == "win32":
        pytest.skip("no interval timer on Windows: scan_timeout is reported, not enforced; "
                    "runs in the sandbox and on CI")
    else:
        import signal
        import time

        bs = chr(92)
        site.write_config(settings="scan_timeout = 1\n",
                          router=f"regex = ^({bs}w+{bs}s?)+failed$\n")
        site.prime()
        before = site.offset("router-disk", site.router)
        site.append(site.router, " ".join(["word"] * 40) + " ok")
        site.append(site.firewall, "DENY 192.0.2.9")
        started = time.monotonic()
        assert site.run() == 1
        elapsed = time.monotonic() - started
        assert elapsed < 20, elapsed  # the bound fired; the regex alone runs for hours
        err = capsys.readouterr().err
        assert err == (f"logalert: 1 of 1 section sent; failed: [router-disk] "
                       f"{site.router.as_posix()}: scanning exceeded scan_timeout (1 s) at "
                       f"line 3 while trying regex '^({bs}{bs}w+{bs}{bs}s?)+failed$'; see the "
                       f"log" + NL)  # the pattern's repr: each backslash doubled
        assert site.offset("router-disk", site.router) == before  # re-read next run
        assert len(site.calls()) == 1  # the firewall section's mail went out
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)  # disarmed
        assert signal.getsignal(signal.SIGALRM) in (signal.SIG_DFL, signal.SIG_IGN, None)
        # with the bound off the same run would hang: not run. 0 is accepted by the loader and
        # scan_bound is a no-op for it -- pinned on the context manager directly
        with logalert.run.scan_bound(0):
            assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


# -- SIGTERM (issue #33) ----------------------------------------------------------------------


def _sigterm_self(*args: Any, **kwargs: Any) -> Any:
    """A stand-in that sends the process SIGTERM; the handler raises before it returns (and
    without the handler the signal ends the test process itself -- the mutation is loud)."""
    os.kill(os.getpid(), signal.SIGTERM)
    time.sleep(5)  # never reached: the signal lands at the next bytecode boundary
    raise AssertionError("the SIGTERM handler did not fire")


def _sigterm_once(real: Any, at: int = 1) -> Any:
    """A stand-in that sends SIGTERM on the ``at``-th call and is the real thing otherwise
    (the WARNING main logs on the way out must not fire it again)."""
    calls: list[bool] = []

    def stand_in(*args: Any, **kwargs: Any) -> Any:
        calls.append(True)
        if len(calls) != at:
            return real(*args, **kwargs)
        return _sigterm_self()

    return stand_in


def test_a_sigterm_during_the_delivery_is_one_line_exit_143_the_lock_free_and_one_record(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Measured before the fix (sandbox): exit status -15, nothing on stderr, no record, the
    sendmail child left to finish on its own, a temp state file left behind."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM reaches no handler on Windows; runs in the sandbox and on CI")
    site.prime()
    before = site.offset("router-disk", site.router)
    site.append(site.router, "disk failure now")
    monkeypatch.setattr(logalert.run, "deliver", _sigterm_self)
    previous = signal.getsignal(signal.SIGTERM)
    assert site.run() == 143
    assert capsys.readouterr() == ("", "logalert: terminated" + NL)
    probe = RunLock(str(site.state_dir / "lock"), 3600, key="state.json")
    probe.acquire()  # LockBusy here would mean the terminated run kept it
    probe.release()
    records = _records_of_the_last_run(site)
    assert [r for r in records if r.startswith(("warning: terminated", "end:"))] == [
        "warning: terminated (SIGTERM); no lock is held, the state is as last saved"]
    assert signal.getsignal(signal.SIGTERM) == previous  # restored on the way out
    assert site.offset("router-disk", site.router) == before  # nothing saved past the mail


def test_a_sigterm_inside_the_save_unlinks_the_temp_file(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform == "win32":
        pytest.skip("SIGTERM reaches no handler on Windows; runs in the sandbox and on CI")
    site.prime()
    before = site.state_file.read_bytes()
    site.append(site.router, "disk failure now")
    # the second fsync: the section's save (the first is the run's opening save, issue #30)
    monkeypatch.setattr(os, "fsync", _sigterm_once(os.fsync, at=2))
    assert site.run() == 143
    assert capsys.readouterr().err == "logalert: terminated" + NL
    assert sorted(p.name for p in site.state_dir.iterdir()) == ["lock", "state.json"]
    assert site.state_file.read_bytes() == before  # the old file, whole
    assert len(site.calls()) == 1  # the mail went out; the save was the step that died


def _gone(pid: int, within: float) -> bool:
    """Whether the process is gone (or a zombie awaiting its reaper) within the bound."""
    deadline = time.monotonic() + within
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:
            with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
                if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                    return True  # awaiting its reaper: as good as gone
        except OSError:
            pass  # no procfs: kill(0) decides
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)


def test_a_sigterm_kills_the_sendmail_child_and_a_wrappers_grandchild(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI as a subprocess, the fake sleeping after it read the message: SIGTERM to
    logalert kills the child -- and, behind a shell wrapper without exec, the grandchild,
    which proc.kill() alone left alive (the interrupt path's gap the issue names)."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM reaches no handler on Windows; runs in the sandbox and on CI")
    site.prime()
    wrapper = site.root / "wrapper.sh"
    wrapper.write_text("#!/bin/sh" + NL + f'"{site.binary.as_posix()}" "$@"' + NL,
                       encoding="ascii", newline=NL)  # no exec: the fake is a grandchild
    wrapper.chmod(0o755)
    monkeypatch.setenv("LOGALERT_FAKE_SLEEP", "30")
    pid_file = site.fake_dir / "pid.txt"
    for binary in (site.binary, wrapper):
        site.write_config(sendmail=binary.as_posix())
        site.append(site.router, "disk failure now")
        if pid_file.exists():
            pid_file.unlink()
        proc = subprocess.Popen([sys.executable, "-m", "logalert", "-f", str(site.conf)],
                                stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 20
            while not (pid_file.exists() and pid_file.read_text(encoding="ascii").strip()):
                assert time.monotonic() < deadline, "the fake never started"
                time.sleep(0.05)
            child = int(pid_file.read_text(encoding="ascii").strip())
            parent = _parent_of(child)
            if parent is not None:  # procfs: the wrapper really is in between
                assert (parent == proc.pid) == (binary is site.binary), binary
            proc.send_signal(signal.SIGTERM)
            _, err = proc.communicate(timeout=20)
        finally:
            proc.kill()  # a red run must not leave a sleeper behind
            proc.wait()
        assert (proc.returncode, err) == (143, b"logalert: terminated" + NL.encode()), binary
        assert _gone(child, within=5), f"{binary.name}: the fake (PID {child}) survived"
    assert site.offset("router-disk", site.router) < site.router.stat().st_size  # re-read


def _parent_of(pid: int) -> int | None:
    """The parent PID from procfs, or None where there is none (a BSD)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii", errors="replace") as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[1])
    except OSError:
        return None


def test_terminating_installs_in_the_main_thread_only_and_restores() -> None:
    # the base class is the whole design: an Exception raised inside a log write would be
    # swallowed by Handler.emit, the log declared dead and the run carried on to the mail
    assert issubclass(logalert.run.Terminated, BaseException)
    assert not issubclass(logalert.run.Terminated, Exception)
    previous = signal.getsignal(signal.SIGTERM)
    with logalert.run.terminating():
        assert signal.getsignal(signal.SIGTERM) is not previous
    assert signal.getsignal(signal.SIGTERM) == previous
    seen: list[object] = []

    def elsewhere() -> None:
        with logalert.run.terminating():
            seen.append(signal.getsignal(signal.SIGTERM))

    thread = threading.Thread(target=elsewhere)
    thread.start()
    thread.join()
    assert seen == [previous]  # nothing installed off the main thread


def test_a_sigterm_inside_a_log_write_still_ends_the_run(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The behavioural twin of the base-class pin: the signal lands inside the file
    handler's flush. An Exception there is swallowed by Handler.emit -- the log declared
    dead, the run carrying on to the mail and exit 0; a BaseException ends the run."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM reaches no handler on Windows; runs in the sandbox and on CI")
    site.prime()
    site.append(site.router, "disk failure now")
    monkeypatch.setattr(logging.StreamHandler, "flush",
                        _sigterm_once(logging.StreamHandler.flush))
    assert site.run() == 143
    assert capsys.readouterr().err.endswith("logalert: terminated" + NL)
    assert site.calls() == []


def test_reset_state_and_test_mail_are_under_the_handler_too(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Every mode dispatches under it: a --reset-state save and a --test-mail child have the
    run's windows (a handler around the run alone ends the test process here)."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM reaches no handler on Windows; runs in the sandbox and on CI")
    site.prime()
    before = site.state_file.read_bytes()
    monkeypatch.setattr(os, "fsync", _sigterm_self)
    assert site.run("--reset-state") == 143
    assert capsys.readouterr().err == "logalert: terminated" + NL
    assert sorted(p.name for p in site.state_dir.iterdir()) == ["lock", "state.json"]
    assert site.state_file.read_bytes() == before
    probe = RunLock(str(site.state_dir / "lock"), 3600, key="state.json")
    probe.acquire()
    probe.release()
    monkeypatch.setattr(logalert.__main__, "deliver", _sigterm_self)
    assert site.run("--test-mail", "router-disk") == 143
    assert capsys.readouterr() == ("", "logalert: terminated" + NL)
    assert site.calls() == []


@pytest.mark.parametrize("window", ["install", "restore"])
def test_a_sigterm_inside_the_handlers_own_install_or_restore_leaves_the_previous_disposition(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
        window: str) -> None:
    """Reproduced in review: a signal landing inside the signal.signal call that installs
    the handler raised before terminating()'s try, and one landing inside the restoring
    call (CPython delivers the pending handlers before changing the disposition) skipped
    the restore -- the raising handler outlived main() either way."""
    if sys.platform == "win32":
        pytest.skip("SIGTERM reaches no handler on Windows; runs in the sandbox and on CI")
    site.prime()
    site.append(site.router, "disk failure now")
    previous = signal.getsignal(signal.SIGTERM)
    real_signal = signal.signal
    fired: list[bool] = []

    def landing(signum: int, handler: Any) -> Any:
        installing = signum == signal.SIGTERM and handler is not previous
        if signum == signal.SIGTERM and not fired and installing == (window == "install"):
            fired.append(True)
            if window == "restore":
                os.kill(os.getpid(), signal.SIGTERM)  # pending before the restore is made
        result = real_signal(signum, handler)
        if fired == [True] and window == "install" and installing:
            fired.append(True)  # once
            os.kill(os.getpid(), signal.SIGTERM)  # delivered as the installing call returns
        return result

    monkeypatch.setattr(signal, "signal", landing)
    assert site.run() == 143
    assert capsys.readouterr().err == "logalert: terminated" + NL
    assert signal.getsignal(signal.SIGTERM) == previous, window


# -- a full filesystem (issue #30) --------------------------------------------------------------


def test_a_full_filesystem_is_refused_before_any_mail(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Measured in the sandbox before the fix (a full tmpfs): the 0-byte probe passed, three
    runs mailed three times and the offset never moved. The opening save is the real write;
    here the replace is what the full disk refuses, as the ENOSPC atomic-write test does."""
    site.prime()
    site.append(site.router, "disk failure now")
    before = site.state_file.read_bytes()
    real_replace = os.replace

    def full(src: str, dst: str) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "replace", full)
    assert site.run() == 1
    out = capsys.readouterr()
    assert out.err == (f"logalert: failed: state file: state file {site.state_file.as_posix()}: "
                       f"cannot write (No space left on device) -- nothing was sent, because a "
                       f"run that cannot save its position would send everything again next "
                       f"time; see the log" + NL)
    assert site.calls() == []  # the whole point
    assert site.state_file.read_bytes() == before
    assert sorted(p.name for p in site.state_dir.iterdir()) == ["lock", "state.json"]
    # the next run, with space back, sends once and moves on
    monkeypatch.setattr(os, "replace", real_replace)
    assert site.run() == 0
    assert len(site.calls()) == 1
    assert site.offset("router-disk", site.router) == site.router.stat().st_size


def test_the_opening_save_is_skipped_by_a_dry_run(site: Site, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "disk failure now")
    monkeypatch.setattr(State, "save", lambda self: (_ for _ in ()).throw(StateError("full")))
    assert site.run("-n") == 0  # never saves, so never refused
    assert "disk failure now" in capsys.readouterr().out


def test_a_lock_the_disk_refuses_names_the_errno_without_the_ownership_hint(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Measured on a full tmpfs: a lock file created for the first time fails its holder
    write with ENOSPC, and the run blamed the file's ownership."""
    site.prime()
    site.append(site.router, "disk failure now")

    def full(self: RunLock, now: float | None = None) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(RunLock, "acquire", full)
    assert site.run() == 1
    err = capsys.readouterr().err
    assert err == (f"logalert: failed: state directory: No space left on device "
                   f"({lock_path(site.state_file.as_posix())}); see the log" + NL)
    assert site.calls() == []
    assert site.run("--reset-state") == 1
    err = capsys.readouterr().err
    assert err.startswith("logalert: cannot take the run lock ") and "(No space left" in err
    assert "must belong" not in err


def test_the_opening_save_writes_the_state_as_loaded_before_the_first_section(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #30: a save of an EMPTY state at the opening (the --reset-state escape hatch's
    own call) passed the refusal test -- the in-memory state survived and the section's save
    rewrote the file -- while a run killed before its first section lost every position."""
    site.prime()
    primed = site.state_file.read_bytes()
    stamp = site.state_file.stat()
    site.append(site.router, "disk failure now")
    seen: list[tuple[bytes, os.stat_result]] = []
    real_section = logalert.run._section

    def peek(*args: Any, **kwargs: Any) -> None:
        if not seen:  # the first section: what the opening left on disk
            seen.append((site.state_file.read_bytes(), site.state_file.stat()))
        real_section(*args, **kwargs)

    monkeypatch.setattr(logalert.run, "_section", peek)
    time.sleep(0.05)  # a moved mtime must be measurable on a coarse clock
    assert site.run() == 0
    (written, stat), = seen
    assert json.loads(written) == json.loads(primed)  # the state as loaded, entries and all
    # rewritten, not left alone: a fresh file replaced the old one
    assert stat.st_mtime_ns != stamp.st_mtime_ns or stat.st_ino != stamp.st_ino


# -- a first sight in a section whose delivery failed (issue #31) -------------------------------


def _mails_with(site: Site, line: str) -> int:
    return sum(body_of(stdin).count(line) for _, stdin in site.calls())


def test_a_first_sight_under_from_start_that_contributed_keeps_its_place_on_a_failure(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured before the fix (both platforms): --from-start with the fake exiting 75 left
    no entry, the next run first-sighted the file at its end and the lines were never sent;
    a later --from-start could not recover them (the file had a cursor by then)."""
    site.append(site.router, "disk failure now")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    assert site.run("--from-start") == 1
    entry = site.state()["router-disk"][site.router.as_posix()]
    assert (entry["offset"], entry["line"]) == (0, 0)  # where the read began
    monkeypatch.delenv("LOGALERT_FAKE_EXIT")
    assert site.run() == 0  # a plain run: the entry is honoured, nothing first-sighted
    after = site.state()["router-disk"][site.router.as_posix()]
    identity = ("ino", "dev", "fingerprint", "realpath")  # the open's, not a blank
    assert [entry[k] for k in identity] == [after[k] for k in identity]
    assert len(site.calls()) == 2  # the refused attempt, then the sent one
    assert _mails_with(site, "disk failure now") == 2
    assert body_of(site.calls()[1][1]).count("disk failure now") == 1
    assert site.run() == 0
    assert len(site.calls()) == 2  # nothing twice


def test_a_first_sight_under_start_beginning_keeps_its_place_on_a_failure(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    site.write_config(router="start = beginning" + NL)
    site.append(site.router, "disk failure now")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    assert site.run() == 1
    assert site.state()["router-disk"][site.router.as_posix()]["offset"] == 0
    monkeypatch.delenv("LOGALERT_FAKE_EXIT")
    assert site.run() == 0
    assert body_of(site.calls()[-1][1]).count("disk failure now") == 1 and len(site.calls()) == 2
    # the re-run continued from the entry: one first sight in the log, not two
    router = [r for r in site.activity() if "first sight" in r and "router.log" in r]
    assert len(router) == 1
    assert site.run() == 0 and len(site.calls()) == 2


def test_a_new_file_under_a_glob_keeps_its_place_on_a_failure(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    site.write_config(firewall_files=(site.root / "fw-*.log").as_posix())
    site.prime()  # the glob's moment is recorded (its directory was listed)
    new = site.root / "fw-new.log"
    new.write_text("DENY 192.0.2.9" + NL, encoding="utf-8", newline=NL)
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT_IF_RCPT", "fw@example.net")
    assert site.run() == 1  # new since the last run: read from 0, mailed, refused
    assert site.state()["firewall"][new.as_posix()]["offset"] == 0
    monkeypatch.delenv("LOGALERT_FAKE_EXIT")
    monkeypatch.delenv("LOGALERT_FAKE_EXIT_IF_RCPT")
    assert site.run() == 0
    assert body_of(site.calls()[-1][1]).count("DENY 192.0.2.9") == 1 and len(site.calls()) == 2
    # the re-run continued from the entry, the rule applied once (the failed run's)
    assert sum("new since the last run" in r for r in site.activity()) == 1
    assert site.run() == 0 and len(site.calls()) == 2


def test_a_plain_first_sight_a_writer_appended_to_keeps_its_place_on_a_failure(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """The trigger is not strictly --from-start: a line appended between the end-seek at
    the open and the read contributes too, and its entry is the end the open saw."""
    late = site.root / "late.log"
    late.write_text("DENY 192.0.2.1 before the first sight" + NL + "up" + NL, encoding="utf-8",
                    newline=NL)
    site.write_config(firewall_files=late.as_posix())
    real_scan = scan

    def scan_after_an_append(path: str, lines: Any, watch: Any, **kw: Any) -> Any:
        if path == late.as_posix():
            site.append(late, "DENY 192.0.2.9")  # the source is open at the old end
        return real_scan(path, lines, watch, **kw)

    monkeypatch.setattr("logalert.run.scan", scan_after_an_append)
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT_IF_RCPT", "fw@example.net")
    assert site.run() == 1
    entry = site.state()["firewall"][late.as_posix()]
    end = len("DENY 192.0.2.1 before the first sight" + NL + "up" + NL)
    assert (entry["offset"], entry["line"]) == (end, 2)  # the end the open saw, line 2
    monkeypatch.setattr("logalert.run.scan", real_scan)
    monkeypatch.delenv("LOGALERT_FAKE_EXIT")
    monkeypatch.delenv("LOGALERT_FAKE_EXIT_IF_RCPT")
    assert site.run() == 0
    body = body_of(site.calls()[-1][1])
    assert body.count("DENY 192.0.2.9") == 1 and len(site.calls()) == 2
    assert "3: DENY 192.0.2.9" in body  # numbered as the file numbers it
    assert "192.0.2.1 before" not in body  # a stale identity would read from 0
    after = site.state()["firewall"][late.as_posix()]
    identity = ("ino", "dev", "fingerprint", "realpath")
    assert [entry[k] for k in identity] == [after[k] for k in identity]


def test_from_start_against_a_saved_position_continues_from_it(
        site: Site) -> None:
    """TEST-1 (#41): USAGE.md's "only a first sight is affected; a file with a saved
    position continues from it" had no test against a saved cursor -- a hoisted "from-start
    means byte 0" in LogFile._start_offset survived the suite and re-mailed the whole log
    every run under start = beginning."""
    site.write_config(router="start = beginning" + NL)
    site.append(site.router, "disk failure 1")
    assert site.run() == 0  # from 0: the first line mailed
    site.append(site.router, "disk failure 2")
    assert site.run("--from-start") == 0
    assert len(site.calls()) == 2
    second = body_of(site.calls()[1][1])
    assert "disk failure 2" in second and "disk failure 1" not in second
    assert site.run() == 0 and len(site.calls()) == 2


def test_a_compressed_first_sight_that_contributed_is_re_read_then_settled(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #31: a start cursor carrying the archive's size and mtime (the shape of
    cursor()) would have made the next run's unchanged-archive short-circuit skip the file
    for good, the refused lines never re-sent; the start cursor carries neither."""
    import gzip
    archive = site.root / "old.log.gz"
    archive.write_bytes(gzip.compress(("up" + NL + "DENY 192.0.2.9 archived" + NL).encode()))
    site.write_config(firewall_files=archive.as_posix())
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    assert site.run("--from-start") == 1
    entry = site.state()["firewall"][archive.as_posix()]
    assert [entry[k] for k in ("offset", "line", "size", "mtime")] == [0, 0, None, None]
    monkeypatch.delenv("LOGALERT_FAKE_EXIT")
    assert site.run() == 0  # decompressed again, the lines re-sent once
    assert len(site.calls()) == 2 and "DENY 192.0.2.9 archived" in body_of(site.calls()[1][1])
    entry = site.state()["firewall"][archive.as_posix()]
    assert entry["size"] == archive.stat().st_size and entry["mtime"] is not None  # settled
    real_open = logalert.cursor.open_log

    def never(path: str, **kw: Any) -> Any:
        assert path != archive.as_posix(), "an unchanged archive was decompressed again"
        return real_open(path, **kw)

    monkeypatch.setattr(logalert.cursor, "open_log", never)
    assert site.run() == 0 and len(site.calls()) == 2  # not opened, nothing twice


def test_a_first_sight_whose_read_fails_keeps_its_place_too(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Review of #31, the failed-READ door: a new-file-rule file whose scan raised mid-read
    had no entry, the section's record moved past it, and the next run first-sighted it at
    the end -- its content never sent. With the entry at the read's start it is re-read."""
    site.write_config(firewall_files=(site.root / "fw-*.log").as_posix())
    site.prime()
    new = site.root / "fw-new.log"
    new.write_text("DENY 192.0.2.9" + NL + "DENY 192.0.2.10" + NL, encoding="utf-8", newline=NL)
    real_scan = scan

    def failing_scan(path: str, lines: Any, watch: Any, **kw: Any) -> Any:
        if path == new.as_posix():
            next(lines)  # one line read, then the stream dies
            raise OSError(f"{path}: incomplete or corrupt compressed stream (staged)")
        return real_scan(path, lines, watch, **kw)

    monkeypatch.setattr("logalert.run.scan", failing_scan)
    assert site.run() == 1
    assert "incomplete or corrupt compressed stream (staged)" in capsys.readouterr().err
    assert site.state()["firewall"][new.as_posix()]["offset"] == 0  # where the read began
    monkeypatch.setattr("logalert.run.scan", real_scan)
    assert site.run() == 0
    body = body_of(site.calls()[-1][1])
    assert "DENY 192.0.2.9" in body and "DENY 192.0.2.10" in body and len(site.calls()) == 1


def test_a_quiet_first_sight_read_from_the_start_moves_on_in_a_failed_section(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """The branch order (review of #31): a first sight that contributed NOTHING moves to
    its end even under --from-start, as the rule says; the no-entry branch is for the
    files whose lines are in the message."""
    quiet = site.root / "fw-quiet.log"
    quiet.write_text("up" + NL + "still up" + NL, encoding="utf-8", newline=NL)
    site.write_config(firewall_files=site.firewall.as_posix() + NL + f"    {quiet.as_posix()}")
    site.append(site.firewall, "DENY 192.0.2.9")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT_IF_RCPT", "fw@example.net")
    assert site.run("--from-start") == 1
    assert site.offset("firewall", quiet) == quiet.stat().st_size  # read from 0, moved on
    assert site.offset("firewall", site.firewall) == 0  # contributed: where its read began


def test_a_permission_on_the_rotated_copy_is_a_failed_item_and_the_position_moves_on(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The owner's call on #38 (option C): exit 1 naming what the permission kept out of
    reach, so cron carries the line once per rotation, while the cursor moves on to the
    live file -- a kept cursor would leave the section stuck behind a permanent mode."""
    site.prime()
    site.append(site.router, "disk failure before the rotation")
    copy = site.root / "router.log.1"
    site.router.replace(copy)  # a rename rotation; the copy holds the position
    site.router.write_text("disk failure after" + NL, encoding="utf-8", newline=NL)
    real_open = logalert.cursor.open_log  # the name rotation looks up

    def refuse(target: str, *, follow_links: bool = False) -> Any:
        if os.path.normcase(target) == os.path.normcase(str(copy)):
            raise PermissionError(13, "Permission denied", target)
        return real_open(target, follow_links=follow_links)

    monkeypatch.setattr("logalert.rotation.open_log", refuse)
    assert site.run() == 1
    err = capsys.readouterr().err
    assert err == (f"logalert: 1 of 1 section sent; failed: [router-disk] "
                   f"{site.router.as_posix()}: a permission kept the rotated copies out of reach "
                   f"(router.log.1); the lines before the rotation are lost; see the log" + NL)
    (_, stdin), = site.calls()
    body = body_of(stdin)
    assert "disk failure after" in body and "before the rotation" not in body
    assert site.offset("router-disk", site.router) == site.router.stat().st_size  # moved on
    assert site.run() == 0 and len(site.calls()) == 1  # the next run: nothing to say


def test_a_refused_delivery_keeps_a_young_entrys_sighting(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Review of #64: a touch on every refusal moved last_seen past copies the refused run had
    read, and a later rotation gap left them out. An entry under half of state_ttl keeps
    the moment its position was taken."""
    site.prime()
    primed = age(site, "firewall", site.firewall, days=2)
    site.append(site.firewall, "DENY 192.0.2.9")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    assert site.run() == 1
    capsys.readouterr()
    after = site.state()["firewall"][site.firewall.as_posix()]
    assert after["offset"] == primed["offset"] and after["last_seen"] == primed["last_seen"]


# -- the unsaved marker: a delivered section whose save failed (issue #70) ----------------------


def _lock_line(site: Site) -> str:
    return (site.state_dir / "lock").read_text(encoding="ascii")


class _Saves:
    """write_atomically with a failure schedule: ``failing(n)`` says whether the n-th call
    (1-based, across runs) fails; every call's text length and reach are recorded.
    ``room`` set is a disk with room for that many bytes and no more: a write reaching
    past it fails, as the padded proof does."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, failing: Callable[[int], bool]) -> None:
        self.calls = 0
        self.sizes: list[int] = []
        self.reaches: list[int | None] = []
        self.failing = failing
        self.room: int | None = None
        real = logalert.state.write_atomically

        def write(path: str, text: str, reach: int | None = None) -> None:
            self.calls += 1
            self.sizes.append(len(text))
            self.reaches.append(reach)
            if self.failing(self.calls) or (self.room is not None
                                            and max(len(text), reach or 0) > self.room):
                raise StateError("disk full")
            real(path, text, reach=reach)

        monkeypatch.setattr("logalert.state.write_atomically", write)


def test_a_delivered_section_whose_save_failed_marks_the_lock_and_the_next_run_refuses(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The band (issue #30's residual): room for the state as loaded, none for its growth.
    Run 1 sends and cannot save; run 2 must refuse before any mail (without the marker it
    would send again: the opening save fits, the section's does not -- the storm); run
    3, with the room back, proves it, sends once and clears the marker."""
    site.prime()
    site.append(site.router, "disk failure now" + " x" * 50)  # the offset gains a digit
    saves = _Saves(monkeypatch, lambda n: False)
    saves.room = len(site.state_file.read_text(encoding="utf-8"))  # as loaded: fits
    assert site.run() == 1
    assert saves.sizes[1] > saves.room  # the band's precondition: the section's save grew
    err = capsys.readouterr().err
    assert err.startswith("logalert: 1 of 1 section sent; failed: [router-disk] state not saved: "
                          "disk full; [firewall] state not saved: disk full; see the log")
    assert len(site.calls()) == 1  # the mail went out
    marked = _lock_line(site)
    assert marked.endswith(f" unsaved {saves.sizes[1]} state.json\n")  # the router section's size
    # run 2: the proving save is refused; nothing is sent, the marker stands
    assert site.run() == 1
    assert capsys.readouterr().err == (
        "logalert: failed: state file: disk full -- the last run sent mail it could not record; "
        "nothing is sent until the state can be saved; see the log" + NL)
    assert len(site.calls()) == 1
    assert saves.reaches[-1] == saves.sizes[1] + STATE_HEADROOM  # what did not fit, plus a block
    assert _lock_line(site).endswith(f" unsaved {saves.sizes[1]} state.json\n")
    assert saves.calls == 4  # nothing was read: the run stopped at the opening
    # run 3: the room is back; the proof passes, the lines go out once more, the marker goes
    saves.room = None
    assert site.run() == 0
    assert len(site.calls()) == 2
    assert body_of(site.calls()[1][1]).count("disk failure now") == 1
    assert " unsaved" not in _lock_line(site)
    assert site.offset("router-disk", site.router) == site.router.stat().st_size
    assert any("the last run sent mail it could not record; the room is there now" in line
               for line in site.activity())


def test_a_later_save_in_the_marked_run_clears_the_marker(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The whole state is one file: the firewall section's save records the router's moved
    position too, so the next run neither refuses nor re-sends."""
    site.prime()
    site.append(site.router, "disk failure now")
    _Saves(monkeypatch, lambda n: n == 2)  # the router section's save alone fails
    assert site.run() == 1
    assert "state not saved" in capsys.readouterr().err
    assert " unsaved" not in _lock_line(site)
    assert site.offset("router-disk", site.router) == site.router.stat().st_size
    assert site.run() == 0 and len(site.calls()) == 1


def test_a_failed_save_after_no_delivery_leaves_no_marker(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "quiet line")  # nothing matches: no mail, the cursors move
    _Saves(monkeypatch, lambda n: n >= 2)
    assert site.run() == 1
    assert "state not saved" in capsys.readouterr().err and site.calls() == []
    assert " unsaved" not in _lock_line(site)


def test_reset_state_clears_the_marker(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "disk failure now")
    saves = _Saves(monkeypatch, lambda n: n >= 2)
    assert site.run() == 1
    assert " unsaved" in _lock_line(site)
    saves.failing = lambda n: False
    assert site.run("--reset-state", site.router.as_posix()) == 0
    capsys.readouterr()
    assert " unsaved" in _lock_line(site)  # one file forgotten: the band is where it was
    assert site.run("--reset-state") == 0
    capsys.readouterr()
    assert " unsaved" not in _lock_line(site)
    # the escape hatch (a state file nothing can read, replaced) clears it too
    assert site.run() == 0  # the first sight after the reset
    site.append(site.router, "disk failure again")
    base = saves.calls
    saves.failing = lambda n: n >= base + 2  # the opening save passes, the rest fail
    assert site.run() == 1 and " unsaved" in _lock_line(site)
    saves.failing = lambda n: False
    site.state_file.write_text("{", encoding="utf-8")
    assert site.run("--reset-state") == 0
    assert capsys.readouterr().out.rstrip().endswith("; replaced it with an empty state")
    assert " unsaved" not in _lock_line(site)


def test_a_dry_run_neither_reads_nor_touches_the_marker(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "disk failure now")
    saves = _Saves(monkeypatch, lambda n: n >= 2)
    assert site.run() == 1
    marked = _lock_line(site)
    saves.failing = lambda n: False
    assert site.run("--dry-run") == 0  # previews, sends nothing, writes nothing
    assert "disk failure now" in capsys.readouterr().out
    assert _lock_line(site) == marked and len(site.calls()) == 1


def test_a_marking_that_fails_is_one_log_line_not_a_traceback(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The marker's write can fail too (an I/O error); the run keeps its one stderr line and
    says the next run re-sends (review: unpinned)."""
    site.prime()
    site.append(site.router, "disk failure now")
    _Saves(monkeypatch, lambda n: n >= 2)

    def refused(self: RunLock, size: int) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(RunLock, "mark_unsaved", refused)
    assert site.run() == 1
    assert capsys.readouterr().err.startswith(
        "logalert: 1 of 1 section sent; failed: [router-disk] state not saved: disk full; ")
    assert len(site.calls()) == 1 and " unsaved" not in _lock_line(site)
    assert any("[router-disk] the lock could not be marked either (No space left on device); "
               "the next run re-sends" in line for line in site.activity())

# -- a log rewritten in place with its banner (issue #34) ---------------------------------------


def test_a_log_rewritten_with_its_banner_past_the_position_is_read_from_the_top(
        site: Site) -> None:
    """Issue #34 end to end, through the state file: a service restart truncates its log
    and rewrites the fixed banner and more than was there; the old rules continued at the
    stale offset (a fragment mailed, the lines before it never). The anchor persists
    between runs, so the third run reads the rewritten file from its beginning."""
    site.prime()
    site.append(site.router, "kernel: disk failure on sda")
    assert site.run() == 0 and len(site.calls()) == 1
    entry = site.state()["router-disk"][site.router.as_posix()]
    assert entry["anchor"] == anchor_of(site.router.read_bytes()[-4096:])
    # the restart: the same file (inode), the same first line, longer than the position
    rewritten = (["boot", "disk failure early in the new log"]
                 + [f"quiet {n}" for n in range(40)] + ["disk failure late"])
    site.router.write_text(NL.join(rewritten) + NL, encoding="utf-8", newline=NL)
    assert site.router.stat().st_size > entry["offset"]
    assert site.run() == 0
    calls = site.calls()
    assert len(calls) == 2
    body = body_of(calls[1][1])
    assert "2: disk failure early in the new log" in body and "43: disk failure late" in body
    assert site.offset("router-disk", site.router) == site.router.stat().st_size
    assert any("the bytes before the saved offset are not the ones read (truncated and "
               "refilled?); truncated" in line for line in site.activity())
    assert site.run() == 0 and len(site.calls()) == 2  # and nothing twice


def test_an_entry_from_0_1_0_is_trusted_once_and_gains_the_anchor_on_the_next_save(
        site: Site) -> None:
    """The upgrade path, through the state file (review: the docstring's "gains it on the
    next save" was pinned by the pure rule only): an entry without an anchor continues,
    the run that continued records one over the bytes it found, and the rewrite after
    that is a truncation."""
    site.prime()
    site.append(site.router, "kernel: disk failure on sda")
    assert site.run() == 0 and len(site.calls()) == 1
    data = json.loads(site.state_file.read_text(encoding="utf-8"))
    entry = data["entries"]["router-disk"][site.router.as_posix()]
    assert entry.pop("anchor") is not None  # what 0.1.0 never wrote
    site.state_file.write_text(json.dumps(data), encoding="utf-8", newline=NL)
    assert site.run() == 0 and len(site.calls()) == 1  # nothing new; trusted once
    entry = site.state()["router-disk"][site.router.as_posix()]
    assert entry["anchor"] == anchor_of(site.router.read_bytes()[-4096:])
    rewritten = ["boot", "quiet", "kernel: disk failure on sdb"] + [f"quiet {n}" for n in range(9)]
    site.router.write_text(NL.join(rewritten) + NL, encoding="utf-8", newline=NL)
    assert site.router.stat().st_size > entry["offset"]
    assert site.run() == 0
    calls = site.calls()
    assert len(calls) == 2 and "3: kernel: disk failure on sdb" in body_of(calls[1][1])


# -- the dry run's console guard (issue #41) ----------------------------------------------------


def test_a_dry_run_prints_a_subject_the_console_cannot_encode(site: Site) -> None:
    """``main`` reconfigures stdout with ``errors="backslashreplace"`` so a subject from the
    config never crashes the reader; pytest captures in-process, so only a child under a
    console encoding that lacks the character exercises it (the mutant: UnicodeEncodeError,
    exit 1). PYTHONIOENCODING overrides UTF-8 mode, so the case holds on newer interpreters."""
    em_dash = chr(0x2014)  # from the code point: gated Python stays ASCII
    text = site.conf.read_text(encoding="utf-8").replace("Router disk failure",
                                                         "Platte " + em_dash + " defekt")
    site.conf.write_text(text, encoding="utf-8", newline=NL)
    site.prime()
    site.append(site.router, "disk failure now")
    env = dict(os.environ, PYTHONIOENCODING="ascii")  # a console that cannot show the dash
    result = run_logalert("-n", "-f", str(site.conf), env=env, binary=True)
    assert result.returncode == 0, result.stderr
    escaped = b"Subject: Platte " + chr(0x5C).encode() + b"u2014 defekt -- 1 match(es)"
    assert escaped in result.stdout


def test_two_configurations_sharing_a_state_directory_keep_their_own_markers(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #73: the lock is the directory's, so configuration B's run read A's marker as
    its own -- refused with A's message, proved the room against A's size and cleared A's
    marker, so A's next run sent its duplicate without a proof. Keyed to the state file:
    B sends normally and carries A's marker along; A's next run refuses; A's proof clears
    only A's. MUTANT: the key ignored -> B's run refuses with A's message."""
    other = site.root / "other.conf"
    other.write_text(site.conf.read_text(encoding="utf-8").replace("state.json", "other.json"),
                     encoding="utf-8", newline=NL)
    site.prime()
    run_b = logalert.__main__.main
    assert run_b(["-f", str(other)]) == 0 and site.calls() == []  # B's first sight, beside A's
    site.append(site.router, "disk failure now" + " x" * 50)
    saves = _Saves(monkeypatch, lambda n: False)
    room = len(site.state_file.read_text(encoding="utf-8"))
    saves.room = room
    assert site.run() == 1  # A: the mail went out, the save was refused, the marker set
    assert len(site.calls()) == 1 and "state not saved" in capsys.readouterr().err
    assert _lock_line(site).endswith(f" unsaved {saves.sizes[1]} state.json\n")
    marker = saves.sizes[1]
    saves.room = None  # the room is back for B's run
    assert run_b(["-f", str(other)]) == 0  # B: sends its own mail, refuses nothing
    assert len(site.calls()) == 2 and capsys.readouterr().err == ""
    line = _lock_line(site)
    assert f" unsaved {marker} state.json" in line and "other.json" not in line  # A's kept
    saves.room = room  # gone again for A's proof
    assert site.run() == 1
    assert capsys.readouterr().err == (
        "logalert: failed: state file: disk full -- the last run sent mail it could not record; "
        "nothing is sent until the state can be saved; see the log" + NL)
    assert len(site.calls()) == 2
    # B out of room too: its own mail out, its own marker beside A's (B's state grew by a
    # line as well); the line carries both, each with its own size
    site.append(site.firewall, "DENY 192.0.2.73" + " y" * 50)
    saves.room = len((site.state_dir / "other.json").read_text(encoding="utf-8"))
    assert run_b(["-f", str(other)]) == 1 and len(site.calls()) == 3
    assert "state not saved" in capsys.readouterr().err
    line = _lock_line(site)
    assert f" unsaved {marker} state.json" in line and " other.json" in line
    saves.room = None
    # A's proof passes: the router lines once more, the firewall line (new to A) once,
    # A's marker gone, B's kept
    assert site.run() == 0
    line = _lock_line(site)
    assert len(site.calls()) == 5 and "state.json" not in line and " other.json" in line
    saves.room = room  # B's proof, against B's own size, refused
    assert run_b(["-f", str(other)]) == 1 and len(site.calls()) == 5
    assert "the last run sent mail it could not record" in capsys.readouterr().err
    saves.room = None
    assert run_b(["-f", str(other)]) == 0 and len(site.calls()) == 6  # B re-sends once
    assert " unsaved" not in _lock_line(site)
