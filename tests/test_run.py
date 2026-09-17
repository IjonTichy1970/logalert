"""The run (issue #12): end to end through ``main()`` over fixture logs, the fake sendmail
behind ``sendmail_path``, the exit codes, the one stderr line, dry-run, the lock, expiry.

Real on both platforms; the three POSIX-only tests (a 0500 state directory, a planted lock
symlink, another user's state directory -- the first and last also skipped in the root
sandbox) and the one Windows-only test (a read-only state file) say so.
"""

import errno
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from esmtp_stub import StubConfig, run_stub
from fake_sendmail import install

import logalert.cursor
import logalert.run
from logalert.__main__ import main
from logalert.cursor import LogFile
from logalert.lock import RunLock
from logalert.match import scan
from logalert.rotation import open_source
from logalert.run import Outcome
from logalert.state import Cursor, State, StateError, load_state, lock_path, timestamp

NL = chr(10)
SENDER = "alerts@example.net"


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
        for knob in ("LOGALERT_FAKE_SLEEP", "LOGALERT_FAKE_EXIT", "LOGALERT_FAKE_EXIT_IF_RCPT",
                     "LOGALERT_FAKE_STDERR_BYTES"):
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
    # age the firewall entry so a touch is visible: same-second timestamps compare equal
    state = load_state(str(site.state_file))
    aged = state.get("firewall", site.firewall.as_posix())
    assert aged is not None
    state.set("firewall", site.firewall.as_posix(),
              replace(aged, last_seen=timestamp(datetime.now(UTC) - timedelta(days=2))))
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
    lock = RunLock(str(site.state_dir / "lock"), 3600)
    lock.acquire()
    try:
        assert site.run() == 0
        assert capsys.readouterr() == ("", "")
        assert any("this run exits quietly" in m for m in caplog.messages)
    finally:
        lock.release()
    stale = RunLock(str(site.state_dir / "lock"), 3600)
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
    failures = iter([StateError("disk full")])  # the first save fails, later ones work

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
    holder = RunLock(str(site.state_dir / "lock"), 3600)
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
    monkeypatch.setattr(State, "save", lambda self: (_ for _ in ()).throw(StateError("full")))
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


def test_an_interrupt_is_one_line_and_exit_130_with_the_lock_released(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.router, "disk failure now")

    def interrupt(*args: Any, **kwargs: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(logalert.run, "deliver", interrupt)
    assert site.run() == 130
    assert capsys.readouterr().err == "logalert: interrupted" + NL
    probe = RunLock(str(site.state_dir / "lock"), 3600)
    probe.acquire()  # LockBusy here would mean the interrupted run kept it
    probe.release()


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
    holder = RunLock(str(site.state_dir / "lock"), 3600)
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
    probe = RunLock(lock_file, 3600)
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
    holder = RunLock(str(other / "lock"), 3600)
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
    first = age(site, "firewall", site.firewall, days=2)
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
