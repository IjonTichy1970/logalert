"""The activity log (issue #13): the destinations, the fallback that is never silent, the
one-line shape, --log, and what --check-config says about syslog.

Real on both platforms except the AF_UNIX tests (a datagram socket the test binds stands in
for /dev/log; skipped on Windows, run in the sandbox and on CI). The journal itself
(``journalctl -t logalert`` after a real run) is the Linux stage's check.
"""

import errno
import inspect
import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_run import Site

import logalert.activity as activity
from logalert.activity import OneLine, attach
from logalert.config import ConfigError, load_config, udp_address

NL = chr(10)
STAMP = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d{4} logalert\[\d+\]: ")


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    return Site(tmp_path, monkeypatch)


def raw_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# -- the file destination, the record set, the shape --------------------------------------------


def test_the_file_destination_records_the_documented_events(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    first = site.activity()
    assert first[0].startswith("start: ") and first[0].endswith("2 section(s)")
    assert (f"warning: [router-disk] {site.router.as_posix()}: first sight; starting at the "
            f"end, 11 bytes skipped") in first
    assert f"[router-disk] {site.router.as_posix()}: 0 line(s) read, 0 matched" in first
    assert first[-1] == "end: exit 0"
    site.append(site.router, "disk failure now")
    assert site.run() == 0
    assert capsys.readouterr() == ("", "")  # the destination is the file: cron-quiet stays
    lines = site.activity()[len(first):]
    assert lines[0].endswith("2 section(s)")
    assert f"[router-disk] {site.router.as_posix()}: 1 line(s) read, 1 matched" in lines
    sent = [line for line in lines if line.startswith("sent via sendmail to noc@example.net: ")]
    assert len(sent) == 1 and "Message-ID" not in sent[0]
    assert re.search(r": \d+ bytes, <[^>]+@example\.net> \(accepted for queueing \(exit 0\)\)$",
                     sent[0])
    assert lines[-1] == "end: exit 0"
    assert not any(line.startswith("debug: ") for line in site.activity())
    for line in raw_lines(site.activity_log):
        assert STAMP.match(line), line


def test_a_failure_is_recorded_at_error_with_the_transports_answer(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.append(site.firewall, "DENY 192.0.2.9")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    assert site.run() == 1
    err = capsys.readouterr().err
    assert err.count(NL) == 1  # the one line; the log itself went to the file
    assert err.startswith("logalert: 0 of 1 section sent; failed: [firewall] sendmail exit 75")
    lines = site.activity()
    errors = [line for line in lines if line.startswith("error: ")]
    assert errors[0].startswith("error: [firewall] not delivered (<") and "exit 75" in errors[0]
    assert errors[1].startswith("error: [firewall] sendmail exit 75 (EX_TEMPFAIL)")
    assert lines[-1] == "end: exit 1"


def test_one_line_per_record_with_the_level_word_and_the_stamp() -> None:
    esc, nel = chr(27), chr(0x85)
    record = logging.LogRecord("logalert.run", logging.WARNING, __file__, 1,
                               "[rou%s[31m%sforged] two" + NL + "lines", (esc, nel), None)
    replacement = chr(0xFFFD)  # what clean_text makes of a control; from the code point
    assert OneLine("logalert: ").format(record) == (
        f"logalert: warning: [rou{replacement}[31m forged] two lines")
    info = logging.LogRecord("logalert.run", logging.INFO, __file__, 1, "start: x", (), None)
    assert OneLine("").format(info) == "start: x"
    debug = logging.LogRecord("logalert.lock", logging.DEBUG, __file__, 1, "lock", (), None)
    assert OneLine("logalert: ").format(debug) == "logalert: debug: lock"
    error = logging.LogRecord("logalert.run", logging.ERROR, __file__, 1, "bad", (), None)
    stamped = OneLine("logalert[7]: ", stamp=True).format(error)
    assert STAMP.match(stamped) and stamped.endswith(" logalert[7]: error: bad")


# -- stderr, --debug, --log ---------------------------------------------------------------------


def test_the_stderr_destination_and_the_log_override(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    recorded = site.activity()
    site.append(site.router, "disk failure now")
    assert site.run("--log", "stderr") == 0
    out = capsys.readouterr()
    assert out.out == ""
    lines = out.err.splitlines()
    assert lines[0].startswith("logalert: start: ") and lines[-1] == "logalert: end: exit 0"
    assert any(line.startswith("logalert: sent via sendmail to ") for line in lines)
    assert not any(line.startswith("logalert: debug: ") for line in lines)
    assert site.activity() == recorded  # --log wins over the configured file


def test_debug_adds_debug_records_on_stderr_and_the_file_stays_at_info(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    assert site.run("-d") == 0
    err = capsys.readouterr().err
    assert "logalert: debug: lock " in err and "logalert: start: " in err
    assert not any(line.startswith("debug: ") for line in site.activity())
    assert site.activity()[-1] == "end: exit 0"
    # the destination IS stderr: one handler, at DEBUG, every record once
    assert site.run("-d", "--log", "stderr") == 0
    err = capsys.readouterr().err
    assert err.count("logalert: start: ") == 1 and "logalert: debug: lock " in err


def test_a_dry_run_and_a_check_log_to_stderr_unless_log_is_explicit(
        site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    recorded = site.activity()
    site.append(site.router, "disk failure now")
    assert site.run("-n") == 0
    err = capsys.readouterr().err
    assert err.splitlines()[0].endswith("2 section(s) (dry run)")
    assert site.activity() == recorded
    elsewhere = tmp_path / "dry.log"
    assert site.run("-n", "--log", f"file:{elsewhere.as_posix()}") == 0
    assert capsys.readouterr().err == ""
    assert any(line.endswith("(dry run)") for line in raw_lines(elsewhere))
    assert site.run("--check-config") == 0
    assert site.activity() == recorded


def test_check_config_prints_the_resolved_destination(
        site: Site, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert site.run("--check-config") == 0
    assert f"log: file:{site.activity_log.as_posix()}" + NL in capsys.readouterr().out
    assert site.run("--check-config", "--log", "stderr") == 0
    assert "log: stderr (--log)" + NL in capsys.readouterr().out
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(site.root / "a"), str(site.root / "b")))
    assert site.run("--check-config", "--log", "syslog") == 0
    out = capsys.readouterr().out
    if sys.platform == "win32":
        assert ("log: syslog -- not available on this platform; the run would log to stderr "
                "(--log)" + NL) in out
    else:
        assert (f"log: syslog -- no usable socket ({site.root / 'a'}: missing; "
                f"{site.root / 'b'}: missing); the run would log to stderr (--log)" + NL) in out


def test_log_is_validated_like_the_setting(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        site.run("--log", "file:relative.log")
    assert raised.value.code == 2
    assert "--log: file: needs an absolute path" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        site.run("--log", "journal")
    assert "--log: expected syslog, stderr, file:/absolute/path or udp:host:port" in (
        capsys.readouterr().err)


# -- the fallback: never silent, never UDP ------------------------------------------------------


def test_no_syslog_socket_falls_back_to_stderr_loudly_and_never_to_udp(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    directory = site.root / "adir"  # /var/run/log is a directory on Ubuntu: exists(), no socket
    directory.mkdir()
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(site.root / "nope"), str(directory)))
    built: list[Any] = []
    original = logging.handlers.SysLogHandler.__init__

    def record(self: Any, *args: Any, **kwargs: Any) -> None:
        built.append(kwargs.get("address", args[0] if args else None))
        original(self, *args, **kwargs)

    monkeypatch.setattr(logging.handlers.SysLogHandler, "__init__", record)
    site.append(site.router, "disk failure now")
    assert site.run("--log", "syslog") == 0
    out = capsys.readouterr()
    lines = out.err.splitlines()
    if sys.platform == "win32":
        assert lines[0] == ("logalert: warning: syslog is not available on this platform; "
                            "logging to stderr")
    else:
        assert lines[0] == (f"logalert: warning: no usable syslog socket ({site.root / 'nope'}: "
                            f"missing; {directory}: not a socket); logging to stderr")
    assert lines[1].startswith("logalert: start: ") and lines[-1] == "logalert: end: exit 0"
    assert "Traceback" not in out.err and out.out == ""
    assert built == []  # no SysLogHandler at all on the fallback path, UDP least of all
    assert site.calls() == []  # the first run is first sight: the fallback did not change the run


def test_a_file_that_cannot_be_opened_falls_back_loudly(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    blocked = site.root / "blocked"
    blocked.mkdir()
    assert site.run("--log", f"file:{blocked.as_posix()}") == 0
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == (f"logalert: warning: cannot open the activity log {blocked.as_posix()} "
                        f"(is a directory); logging to stderr")
    assert lines[-1] == "logalert: end: exit 0"
    missing_parent = site.root / "no-such-dir" / "activity.log"
    assert site.run("--log", f"file:{missing_parent.as_posix()}") == 0
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith(f"logalert: warning: cannot open the activity log "
                               f"{missing_parent.as_posix()} (")
    assert "); logging to stderr" in lines[0]


def test_udp_reaches_the_listener_only_when_configured(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(5)
    port = listener.getsockname()[1]
    try:
        assert site.run("--log", f"udp:127.0.0.1:{port}") == 0
        assert capsys.readouterr() == ("", "")
        first = listener.recv(4096)
    finally:
        listener.close()
    assert first.startswith(f"<14>logalert[{os.getpid()}]: start: ".encode())

    # an unresolvable host raises gaierror at construction (measured on both platforms); the
    # resolver is stubbed because a real lookup of a .invalid name took 11 s on the dev host
    def unresolvable(*args: Any, **kwargs: Any) -> Any:
        raise socket.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", unresolvable)
    assert site.run("--log", "udp:nonexistent.invalid:514") == 0
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == ("logalert: warning: udp:nonexistent.invalid:514: cannot resolve the host "
                        "([Errno -2] Name or service not known); logging to stderr")
    assert lines[-1] == "logalert: end: exit 0"


def test_attach_leaves_no_handler_behind_even_after_a_fallback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    logger = logging.getLogger("logalert")
    before = list(logger.handlers)
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(tmp_path / "none"),))
    with attach("syslog"):
        logging.getLogger("logalert.run").info("inside")
    assert logger.handlers == before and logger.level == logging.NOTSET
    with attach(f"file:{(tmp_path / 'a.log').as_posix()}", debug=True):
        logging.getLogger("logalert.run").debug("fine")
    assert logger.handlers == before and logger.level == logging.NOTSET
    err = capsys.readouterr().err
    assert "logalert: inside" in err and "logalert: debug: fine" in err
    assert raw_lines(tmp_path / "a.log") == []  # DEBUG never reaches the destination


# -- the unix socket: the ident on the wire, a destination that dies mid-run ------------------


def test_the_ident_reaches_the_syslog_socket_and_a_dead_socket_is_reported_once(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform == "win32":
        pytest.skip("no AF_UNIX on Windows; runs in the sandbox and on CI")
    path = site.root / "log"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    server.settimeout(5)
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(site.root / "missing"), str(path)))
    try:
        assert site.run("--log", "syslog") == 0
        assert capsys.readouterr() == ("", "")
        datagrams = [server.recv(4096) for _ in range(4)]
    finally:
        server.close()
    pid = os.getpid()
    assert datagrams[0].startswith(f"<14>logalert[{pid}]: start: ".encode())  # user.info
    warnings = [d for d in datagrams if d.startswith(f"<12>logalert[{pid}]: warning: ".encode())]
    assert warnings and b"first sight; starting at the end" in warnings[0]  # user.warning
    assert all(d.endswith(b"\x00") and b"\n" not in d for d in datagrams)

    # the server is gone but the path stays: the run attaches, the first record fails
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(site.root / "log2"))
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(site.root / "log2"),))
    logger = logging.getLogger("logalert.run")
    with attach("syslog"):
        logger.info("one")
        assert server.recv(4096).endswith(b"one\x00")
        server.close()
        logger.info("two")
        logger.warning("three")
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "logalert: warning: the activity log at syslog (" + str(site.root / "log2")
        + ") failed (ConnectionRefusedError: [Errno 111] Connection refused); logging to "
        "stderr from here on",
        "logalert: two",
        "logalert: warning: three",
    ]


# -- the loader: nothing under the venv ---------------------------------------------------------


def test_a_state_file_or_a_log_file_inside_the_venv_is_refused(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    venv = tmp_path / "venv"
    venv.mkdir()
    monkeypatch.setattr(sys, "prefix", str(venv))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    watch = (f"[router-disk]\nsubject = s\nto = noc@example.net\n"
             f"files = {(tmp_path / 'r.log').as_posix()}\npatterns =\n    x\n")
    conf = tmp_path / "logalert.conf"
    for key, value in (("state_file", (venv / "state.json").as_posix()),
                       ("log", "file:" + (venv / "lib" / "activity.log").as_posix())):
        conf.write_text(f"[logalert]\n{key} = {value}\n" + watch, encoding="utf-8", newline=NL)
        with pytest.raises(ConfigError, match=rf"\[logalert\] {key}: .* is inside the venv"):
            load_config(str(conf))
    # a sibling whose name merely starts with the venv's is outside it
    sibling = tmp_path / "venv-state" / "state.json"
    conf.write_text(f"[logalert]\nstate_file = {sibling.as_posix()}\n" + watch,
                    encoding="utf-8", newline=NL)
    assert load_config(str(conf)).settings.state_file == sibling.as_posix()
    # not a venv at all: the rule is inert
    monkeypatch.setattr(sys, "base_prefix", str(venv))
    conf.write_text(f"[logalert]\nstate_file = {(venv / 'state.json').as_posix()}\n" + watch,
                    encoding="utf-8", newline=NL)
    assert load_config(str(conf)).settings.state_file == (venv / "state.json").as_posix()


def test_reset_state_records_what_it_forgot(site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    assert site.run("--reset-state") == 0
    assert "forgot 2 cursor(s) for every file" in capsys.readouterr().out
    assert site.activity()[-1] == "forgot 2 cursor(s) for every file (--reset-state)"


def test_the_first_sight_skip_is_a_warning_and_from_start_is_not(
        site: Site) -> None:
    site.prime()
    lines = site.activity()
    assert sum(line.startswith("warning: ") and "first sight" in line for line in lines) == 2
    site.write_config(router="start = beginning\n")
    assert site.run("--reset-state") == 0
    assert site.run() == 0
    tail = site.activity()[len(lines):]
    assert any(line == f"[router-disk] {site.router.as_posix()}: first sight; reading from "
               f"the beginning" for line in tail)


# -- the review's pins: a destination that fails at close, the wrong files as the log ---------


def attached(kind: type) -> Any:
    """The one handler of that type on the logalert logger (the library's NullHandler is
    there too)."""
    found = [h for h in logging.getLogger("logalert").handlers if isinstance(h, kind)]
    assert len(found) == 1, found
    return found[0]


class _Full:
    """A stream on a full disk: every write is ENOSPC; flush and close are quiet."""

    def write(self, text: str) -> int:
        raise OSError(errno.ENOSPC, "No space left on device")

    def flush(self) -> None:
        return

    def close(self) -> None:
        return


def test_a_device_as_the_log_destination_is_refused_not_opened(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    """/dev/full would take the records and fail every flush with ENOSPC (the review
    measured the teardown re-raising it, before close() was guarded); a device is not a
    regular file and is refused before that."""
    if sys.platform == "win32":
        pytest.skip("/dev/full is POSIX; runs in the sandbox and on CI")
    assert site.run("--log", "file:/dev/full") == 0
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert lines[0] == ("logalert: warning: cannot open the activity log /dev/full (not a "
                        "regular file); logging to stderr")
    assert lines[-1] == "logalert: end: exit 0" and "Traceback" not in err
    logger = logging.getLogger("logalert")
    assert logger.level == logging.NOTSET
    assert not any(isinstance(h, activity._Stderr) for h in logger.handlers)


def test_a_close_failure_at_teardown_still_unwires_everything(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Both platforms: the failure surfaces in flush(), which FileHandler.close() calls."""
    logger = logging.getLogger("logalert")
    before = list(logger.handlers)

    def full(self: logging.FileHandler) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(logging.FileHandler, "flush", full)
    with attach(f"file:{(tmp_path / 'a.log').as_posix()}", debug=True):
        logging.getLogger("logalert.run").info("one")
        logging.getLogger("logalert.run").info("two")
    assert logger.handlers == before and logger.level == logging.NOTSET
    err = capsys.readouterr().err.splitlines()
    assert err[0].startswith("logalert: warning: the activity log at file:")  # failed on "one"
    assert err[1:] == ["logalert: one", "logalert: two"]  # --debug printed them; once each


def test_a_destination_that_dies_mid_call_is_reported_once_and_nothing_arrives_twice(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    logger = logging.getLogger("logalert.run")
    target = tmp_path / "a.log"
    failed = (f"logalert: warning: the activity log at file:{target.as_posix()} failed "
              f"(OSError: [Errno 28] No space left on device); logging to stderr from here on")
    for debug in (False, True):
        with attach(f"file:{target.as_posix()}", debug=debug):
            handler = attached(logging.FileHandler)
            logger.info("one")
            handler.stream.close()
            handler.stream = _Full()  # the disk fills after the first record
            logger.info("two")
            logger.warning("three")
            logger.debug("four")
        err = capsys.readouterr().err.splitlines()
        if debug:
            # one stderr handler at DEBUG already receives every record: the dead destination
            # forwards nothing, so "two" and "three" arrive once, and "four" only via --debug
            assert err == ["logalert: one", failed, "logalert: two", "logalert: warning: three",
                           "logalert: debug: four"]
        else:
            assert err == [failed, "logalert: two", "logalert: warning: three"]
    assert [line.split("]: ", 1)[1] for line in raw_lines(target)] == ["one", "one"]


def test_attach_closes_the_destination_it_detaches(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with attach(f"file:{(tmp_path / 'a.log').as_posix()}"):
        handler = attached(logging.FileHandler)
        logging.getLogger("logalert.run").info("x")
        assert handler.stream is not None
    assert handler.stream is None  # FileHandler.close() drops the stream; None means closed
    if sys.platform != "win32":
        path = tmp_path / "log"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(path))
        monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(path),))
        try:
            with attach("syslog"):
                handler = attached(logging.handlers.SysLogHandler)
                assert handler.socket.fileno() != -1
            assert handler.socket is None  # SysLogHandler.close() closed and dropped the socket
        finally:
            server.close()


def _config(tmp_path: Path, settings: str) -> Path:
    conf = tmp_path / "logalert.conf"
    watch = (f"[router-disk]{NL}subject = s{NL}to = noc@example.net{NL}"
             f"files = {(tmp_path / 'r.log').as_posix()}{NL}patterns ={NL}    x{NL}")
    conf.write_text(f"[logalert]{NL}{settings}{NL}" + watch, encoding="utf-8", newline=NL)
    return conf


def test_the_state_file_the_lock_and_the_config_are_refused_as_the_log(tmp_path: Path) -> None:
    state = tmp_path / "state" / "state.json"
    for target, what in ((state, "the state file"), (state.parent / "lock", "the run lock"),
                         (tmp_path / "logalert.conf", "the config file")):
        conf = _config(tmp_path,
                       f"state_file = {state.as_posix()}{NL}log = file:{target.as_posix()}")
        with pytest.raises(ConfigError, match=rf"\[logalert\] log: .* is {what}$"):
            load_config(str(conf))


def test_the_log_never_lands_in_the_state_file_through_the_command_line(
        site: Site, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    written = site.state_file.read_bytes()
    assert site.run("--log", f"file:{site.state_file.as_posix()}") == 2
    assert site.state_file.read_bytes() == written
    assert capsys.readouterr().err == (f"logalert: --log: {site.state_file.as_posix()} is the "
                                       f"state file" + NL)
    # --state-file moves the state: the configured log must not be that file either
    elsewhere = tmp_path / "elsewhere" / "state.json"
    elsewhere.parent.mkdir()
    site.write_config(log=f"file:{elsewhere.as_posix()}")
    assert site.run("--state-file", str(elsewhere)) == 2
    assert capsys.readouterr().err == (f"logalert: [logalert] log: {elsewhere.as_posix()} is the "
                                       f"state file" + NL)
    assert not elsewhere.exists()


def test_a_fifo_swapped_in_after_the_lstat_cannot_block_the_open(
        site: Site, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #29: the lstat judged the name; the open came after, and a FIFO with no reader
    put there in between blocked open(2) for good, before the lock. With O_NONBLOCK the
    open is ENXIO at once. The lstat is made to lie (as the swap does), and an alarm turns
    a regression into a failure rather than a hang."""
    if sys.platform == "win32":
        pytest.skip("no FIFOs on Windows; runs in the sandbox and on CI")
    else:
        import signal

        fifo = site.root / "swapped"
        os.mkfifo(fifo)
        regular = os.lstat(__file__)
        real_lstat = os.lstat
        monkeypatch.setattr(os, "lstat", lambda p, *a, **k: regular if str(p) == str(fifo)
                            else real_lstat(p, *a, **k))

        def expired(signum: int, frame: object) -> None:
            raise AssertionError("the open of the FIFO blocked: O_NONBLOCK is gone")

        previous = signal.signal(signal.SIGALRM, expired)
        signal.alarm(10)
        try:
            assert site.run("--log", f"file:{fifo.as_posix()}") == 0
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous)
        lines = capsys.readouterr().err.splitlines()
        assert lines[0] == (f"logalert: warning: cannot open the activity log {fifo.as_posix()} "
                            f"(not a regular file); logging to stderr")
        assert lines[-1] == "logalert: end: exit 0"


def test_a_fifo_as_the_log_destination_is_refused_not_opened(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform == "win32":
        pytest.skip("no FIFOs on Windows; runs in the sandbox and on CI")
    fifo = site.root / "fifo"
    os.mkfifo(fifo)
    # a reader end keeps the test from blocking if the refusal regresses; a refusal never opens
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert site.run("--log", f"file:{fifo.as_posix()}") == 0
        lines = capsys.readouterr().err.splitlines()
        assert lines[0] == (f"logalert: warning: cannot open the activity log {fifo.as_posix()} "
                            f"(not a regular file); logging to stderr")
        assert lines[-1] == "logalert: end: exit 0"
        assert os.read(reader, 4096) == b""  # no writer ever: nothing went into the FIFO
    finally:
        os.close(reader)


def test_a_symlink_at_the_log_path_is_refused_and_the_target_untouched(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform == "win32":
        pytest.skip("creating a symlink needs a privilege here; runs in the sandbox and on CI")
    victim = site.root / "victim"
    victim.write_text("VICTIM LINE" + NL, encoding="utf-8")
    planted = site.root / "planted.log"
    os.symlink(victim, planted)
    assert site.run("--log", f"file:{planted.as_posix()}") == 0
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == (f"logalert: warning: cannot open the activity log {planted.as_posix()} "
                        f"(is a symbolic link -- name the real path); logging to stderr")
    assert victim.read_text(encoding="utf-8") == "VICTIM LINE" + NL


def test_udp_accepts_the_bracketed_ipv6_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    assert udp_address("udp:[::1]:514") == ("::1", 514)
    assert udp_address("udp:[2001:db8::1]:514") == ("2001:db8::1", 514)
    assert udp_address("udp:loghost.example.net:514") == ("loghost.example.net", 514)
    for bad in ("udp:[::1]", "udp::514", "udp:[]:514", "udp:host:0"):
        with pytest.raises(ValueError):
            udp_address(bad)
    built: list[Any] = []

    def record(self: Any, address: Any = None, **kwargs: Any) -> None:
        built.append(address)
        raise OSError("not today")  # the fallback path, nothing sent

    monkeypatch.setattr(logging.handlers.SysLogHandler, "__init__", record)
    handler, notice = activity._open("udp:[::1]:514")
    assert handler is None and notice == "udp:[::1]:514: cannot open (not today); logging to stderr"
    assert built == [("::1", 514)]  # the brackets never reach the resolver


def test_a_nul_in_a_path_value_is_a_config_error(tmp_path: Path) -> None:
    nul = chr(0)  # configparser passes U+0000 through; realpath raises ValueError on it
    for key, value in (("state_file", (tmp_path / f"st{nul}ate.json").as_posix()),
                       ("log", "file:" + (tmp_path / f"act{nul}ivity.log").as_posix())):
        conf = _config(tmp_path, f"{key} = {value}")
        with pytest.raises(ConfigError, match="contains a NUL character"):
            load_config(str(conf))


# -- the review's pins: what the modes do with the destination -----------------------------------


def test_check_config_never_opens_the_destination(
        site: Site, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    assert site.run("--check-config") == 0
    assert capsys.readouterr().err == ""
    assert not site.activity_log.exists()  # a check creates nothing
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(site.root / "none"),))
    assert site.run("--check-config", "--log", "syslog") == 0
    assert capsys.readouterr().err == ""  # reported on stdout, never attached: no fallback line


def test_check_config_names_the_syslog_socket_it_found(
        site: Site, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("no AF_UNIX on Windows; runs in the sandbox and on CI")
    path = site.root / "log"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    server.settimeout(0.5)
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(site.root / "missing"), str(path)))
    try:
        assert site.run("--check-config", "--log", "syslog") == 0
        out = capsys.readouterr()
        assert f"log: syslog ({path}) (--log)" + NL in out.out and out.err == ""
        with pytest.raises(TimeoutError):
            server.recv(4096)  # a check attaches nothing: no record reaches the socket
    finally:
        server.close()


def test_a_syslog_daemon_that_is_down_is_named_as_refusing(
        site: Site, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("no AF_UNIX on Windows; runs in the sandbox and on CI")
    path = site.root / "log"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(path))
    server.close()  # the daemon is gone; its socket file stays, as journald's does
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(path),))
    assert site.run("--log", "syslog") == 0
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == (f"logalert: warning: no usable syslog socket ({path}: connection refused); "
                        f"logging to stderr")
    assert site.run("--check-config", "--log", "syslog") == 0
    assert (f"log: syslog -- no usable socket ({path}: connection refused); the run would log "
            f"to stderr (--log)" + NL) in capsys.readouterr().out


def test_a_stream_syslog_socket_is_used_with_its_type(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    if sys.platform == "win32":
        pytest.skip("no AF_UNIX on Windows; runs in the sandbox and on CI")
    path = site.root / "slog"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(2)
    server.settimeout(5)
    monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(path),))
    data = b""
    try:
        assert site.run("--log", "syslog") == 0
        assert capsys.readouterr() == ("", "")  # nothing fell back: the stream socket served
        for _ in range(2):  # the probe's connection (closed at once) and the handler's
            conn, _addr = server.accept()
            conn.settimeout(5)
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            finally:
                conn.close()
            if b"end: exit 0" in data:
                break
    finally:
        server.close()
    assert data.startswith(f"<14>logalert[{os.getpid()}]: start: ".encode())
    assert b"end: exit 0" + b"\x00" in data


def test_log_wins_for_reset_state_and_test_mail(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    recorded = site.activity()
    assert site.run("--reset-state", "--log", "stderr") == 0
    out = capsys.readouterr()
    assert "forgot 2 cursor(s) for every file" in out.out
    assert "logalert: forgot 2 cursor(s) for every file (--reset-state)" in out.err.splitlines()
    assert site.activity() == recorded
    elsewhere = site.root / "mail.log"
    assert site.run("--test-mail", "router-disk", "--log", f"file:{elsewhere.as_posix()}") == 0
    assert capsys.readouterr().err == ""
    lines = [line.split("]: ", 1)[1] for line in raw_lines(elsewhere)]
    assert any(line.startswith("sent via sendmail to noc@example.net: ") for line in lines), lines
    assert site.activity() == recorded


def test_reset_state_records_the_escape_hatch(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.prime()
    site.state_file.write_text("{oops", encoding="utf-8")
    assert site.run("--reset-state") == 0
    assert capsys.readouterr().out.endswith("; replaced it with an empty state" + NL)
    last = site.activity()[-1]
    assert last.startswith(f"warning: state file {site.state_file.as_posix()}: not valid JSON")
    assert last.endswith("; replaced it with an empty state (--reset-state)")


# -- a syslog socket nobody drains (issue #35) --------------------------------------------------


class _Blocked(BaseException):
    """The test's own bound: a run that blocks in a send is a failure, never a hung suite."""


def _fill(path: Path) -> list[socket.socket]:
    """Throwaway senders fill the daemon's queue until a fresh socket cannot send at all --
    the state a reconnect's fresh socket meets (measured: 278 datagrams fill one sender's
    buffer, 512 fill the queue on Ubuntu 24.04; the queued datagrams outlive their
    senders, which are kept only to be closed at the end)."""
    if sys.platform == "win32":
        pytest.skip("no AF_UNIX on Windows; runs in the sandbox and on CI")  # mypy's narrowing
    senders: list[socket.socket] = []
    for _ in range(128):
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.setblocking(False)
        sender.connect(str(path))
        senders.append(sender)
        sent = 0
        try:
            while sent < 4096:
                sender.send(b"<14>filler")
                sent += 1
        except BlockingIOError:
            if sent == 0:
                return senders
    pytest.fail("could not fill the socket's queue: no per-socket limit on this kernel?")


def test_a_syslog_socket_nobody_drains_costs_two_timeouts_then_the_fallback(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Measured before the fix (sandbox): a plain SysLogHandler blocked inside logger.info()
    for good after 278 records, and a timeout set once after construction was lost at the
    first reconnect (blocked after 0 records). Here the queue is full for a fresh socket
    too, so the retry times out as well and the run must fall back; the mutation that puts
    the timeout back on the constructed socket only reddens through the bound."""
    if sys.platform == "win32":
        pytest.skip("no AF_UNIX on Windows; runs in the sandbox and on CI")
    else:
        site.write_config(settings="scan_timeout = 0" + NL)  # its timer would cancel the bound
        path = site.root / "log"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(str(path))  # never read
        senders = _fill(path)
        monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(path),))
        monkeypatch.setattr(activity, "SYSLOG_SEND_TIMEOUT", 0.2)

        def bail(signum: int, frame: object) -> None:
            raise _Blocked()

        previous = signal.signal(signal.SIGALRM, bail)
        signal.setitimer(signal.ITIMER_REAL, 15)
        try:
            started = time.monotonic()
            assert site.run("--log", "syslog") == 0
            elapsed = time.monotonic() - started
        except _Blocked:
            pytest.fail(f"the run blocked inside a send for {time.monotonic() - started:.0f} s: "
                        f"the timeout did not survive the reconnect")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            for sender in senders:
                sender.close()
            server.close()
        lines = capsys.readouterr().err.splitlines()
        assert lines[0] == (f"logalert: warning: the activity log at syslog ({path}) failed "
                            f"(TimeoutError: timed out); logging to stderr from here on")
        assert lines[1].startswith("logalert: start: ")  # the record that failed, forwarded
        assert lines[-1] == "logalert: end: exit 0"
        assert 0.4 <= elapsed < 3, elapsed  # two timeouts of 0.2 s: not a hang, not 2 s ones


@pytest.mark.parametrize("kind", ["SOCK_DGRAM", "SOCK_STREAM"])
def test_the_send_timeout_is_on_the_constructed_socket_and_on_the_reconnected_one(
        tmp_path: Path, kind: str) -> None:
    if sys.platform == "win32":
        pytest.skip("no AF_UNIX on Windows; runs in the sandbox and on CI")
    else:
        path = tmp_path / "log"
        socktype = getattr(socket, kind)
        server = socket.socket(socket.AF_UNIX, socktype)
        server.bind(str(path))
        if socktype == socket.SOCK_STREAM:
            server.listen(4)  # two connects, never accepted: they fit the backlog
        try:
            handler = activity._Syslog(address=str(path), socktype=socktype)
            first = handler.socket
            assert first.gettimeout() == activity.SYSLOG_SEND_TIMEOUT
            first.close()
            handler._connect_unixsocket(str(path))  # what emit does after a failed send
            assert handler.socket is not first
            assert handler.socket.gettimeout() == activity.SYSLOG_SEND_TIMEOUT
            handler.close()
        finally:
            server.close()


def test_the_stdlib_reconnects_through_the_seam_the_timeout_rides_on() -> None:
    """A Python that changes SysLogHandler.emit's reconnect must redden here, not hang a
    run; 3.11 through 3.14 reconnect through the private method the override replaces."""
    assert hasattr(logging.handlers.SysLogHandler, "_connect_unixsocket")
    source = inspect.getsource(logging.handlers.SysLogHandler.emit)
    assert "self._connect_unixsocket(self.address)" in source
    source = inspect.getsource(logging.handlers.SysLogHandler.createSocket)
    assert "self._connect_unixsocket(address)" in source  # the construction path too


def test_a_stream_listener_that_never_accepts_is_rejected_at_once_not_waited_for(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """Review of #35: the probe's stream connect had no timeout, and a listener whose
    backlog is full (a wedged stream syslog daemon; every run's probe leaves a connection
    in it) blocked the probe before the lock. Bounded, the rejection names the stream
    attempt's reason, not the datagram attempt's EPROTOTYPE."""
    if sys.platform == "win32":
        pytest.skip("no AF_UNIX on Windows; runs in the sandbox and on CI")
    else:
        path = site.root / "slog"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        pending: list[socket.socket] = []
        for _ in range(16):  # connections never accepted, until the backlog refuses one
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.setblocking(False)
            try:
                client.connect(str(path))
            except BlockingIOError:
                client.close()
                break
            pending.append(client)
        else:
            pytest.fail("could not fill the listener's backlog")
        site.write_config(settings="scan_timeout = 0" + NL)  # its timer would cancel the bound
        monkeypatch.setattr(activity, "SYSLOG_SOCKETS", (str(path),))
        monkeypatch.setattr(activity, "SYSLOG_SEND_TIMEOUT", 0.2)

        def bail(signum: int, frame: object) -> None:
            raise _Blocked()

        previous = signal.signal(signal.SIGALRM, bail)
        signal.setitimer(signal.ITIMER_REAL, 15)
        try:
            started = time.monotonic()
            assert site.run("--log", "syslog") == 0
            elapsed = time.monotonic() - started
            lines = capsys.readouterr().err.splitlines()
            assert lines[0] == (f"logalert: warning: no usable syslog socket ({path}: resource "
                                f"temporarily unavailable); logging to stderr")
            assert lines[-1] == "logalert: end: exit 0"
            assert elapsed < 3, elapsed
            assert site.run("--check-config", "--log", "syslog") == 0  # describe() probes too
            assert "resource temporarily unavailable" in capsys.readouterr().out
        except _Blocked:
            pytest.fail("the probe blocked in the stream connect: the backlog was full")
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
            for client in pending:
                client.close()
            server.close()
