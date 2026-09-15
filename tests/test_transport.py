"""Mail delivery (issue #11): the sendmail pipe, SMTP, timeouts, the From, --test-mail.

Two doubles, both stdlib: ``fake_sendmail.py`` (run as the interpreter's argument through
the ``command`` seam, or through ``sendmail_path`` behind a platform wrapper -- a ``.cmd`` on
Windows, a shebang copy on POSIX -- for the end-to-end paths) and ``esmtp_stub.py`` on
127.0.0.1 port 0. Real on both platforms; the one POSIX-only test says so.
"""

import getpass
import json
import logging
import os
import re
import socket
import stat
import subprocess
import sys
import time
from email import message_from_bytes
from email.message import EmailMessage
from email.policy import default as default_policy
from pathlib import Path
from typing import Any

import pytest
from esmtp_stub import StubConfig, run_stub  # tests/ has no __init__: the rootdir import

import logalert.config
import logalert.transport
from logalert.__main__ import main
from logalert.config import Config, ConfigError, Settings, load_config
from logalert.cursor import Line
from logalert.mail import POLICY, Mail, compose, compose_test
from logalert.match import scan
from logalert.transport import (
    SYSEXITS,
    Delivery,
    DeliveryError,
    _first_line,
    _reply,
    choose,
    deliver,
    resolve_sender,
    via_sendmail,
    via_smtp,
)

if sys.platform != "win32":
    import pwd

NL = chr(10)
CR = chr(13)
FAKE = Path(__file__).with_name("fake_sendmail.py")
FILE = "/var/log/router.log"
SENDER = "logalert@router1.example.net"
MISSING = "/nonexistent/sbin/sendmail" if sys.platform != "win32" else "C:/nonexistent/sendmail.exe"


def fake_mta(tmp_path: Path) -> Path:
    """The fake as a binary ``sendmail_path`` can name: a ``.cmd`` wrapper on Windows, a
    copy with the interpreter's shebang and mode 0755 on POSIX (both measured to pass argv
    and stdin through unchanged)."""
    if sys.platform == "win32":
        wrapper = tmp_path / "sendmail.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*' + NL, encoding="ascii",
                           newline=chr(13) + chr(10))
    else:
        wrapper = tmp_path / "sendmail"
        wrapper.write_text(f"#!{sys.executable}" + NL + FAKE.read_text(encoding="utf-8"),
                           encoding="utf-8", newline=NL)
        wrapper.chmod(0o755)
    return wrapper


def make_config(tmp_path: Path, settings: str = "", to: str = "noc@example.net") -> Config:
    text = (f"[logalert]\n{settings}\n[router-disk]\nsubject = Router disk failure\n"
            f"to = {to}\nfiles = {FILE}\npatterns =\n    disk failure\n")
    path = tmp_path / "logalert.conf"
    path.write_text(text, encoding="utf-8", newline="\n")
    return load_config(str(path))


def a_mail(config: Config, sender: str = SENDER) -> Mail:
    watch = config.watches[0]
    stream = [Line(text, False, n + 1, FILE) for n, text in enumerate(
        ["quiet", "kernel: disk failure on sda", "quiet"])]
    report = scan(FILE, stream, watch, context=1, cap=watch.max_lines)
    return compose(watch, [report], sender=sender, settings=config.settings, host="router1")


def dotted_mail(to: tuple[str, ...] = ("noc@example.net",)) -> Mail:
    """A message whose body has lines a transport must protect: logalert's own reports never
    start a line with ``.`` (every line carries a number), so the rule is pinned by hand."""
    msg = EmailMessage(policy=POLICY)
    msg["From"] = SENDER
    msg["To"] = ", ".join(to)
    msg["Subject"] = "dots"
    msg["Message-ID"] = "<dots@router1.example.net>"
    msg.set_content("before" + NL + "." + NL + "..two" + NL + " .indented" + NL + "after" + NL)
    return Mail(msg, "<dots@router1.example.net>", SENDER, "dots", to, None, 1)


def records(fake_dir: Path) -> tuple[dict[str, Any], bytes]:
    argv: dict[str, Any] = json.loads((fake_dir / "argv.json").read_text(encoding="ascii"))
    return argv, (fake_dir / "stdin.bin").read_bytes()


def stuffed(data: bytes) -> bytes:
    """What smtplib puts on the wire for CRLF-clean bytes: one extra dot per line starting
    with a dot, then the terminator (measured)."""
    lines = data.split(b"\r\n")
    return b"\r\n".join(b"." + ln if ln.startswith(b".") else ln for ln in lines) + b".\r\n"


@pytest.fixture
def fake_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "fake"
    monkeypatch.setenv("LOGALERT_FAKE_DIR", str(where))
    for knob in ("LOGALERT_FAKE_SLEEP", "LOGALERT_FAKE_EXIT", "LOGALERT_FAKE_STDERR_BYTES"):
        monkeypatch.delenv(knob, raising=False)
    return where


# -- choosing the transport ---------------------------------------------------------------------


def test_auto_is_sendmail_when_the_binary_exists_else_the_actionable_error(
        tmp_path: Path) -> None:
    usable = make_config(tmp_path, f"sendmail_path = {fake_mta(tmp_path).as_posix()}\n")
    assert choose(usable.settings) == "sendmail"
    absent = make_config(tmp_path, f"sendmail_path = {MISSING}\n")
    with pytest.raises(ConfigError) as exc:
        choose(absent.settings)
    assert str(exc.value) == (
        f"transport = auto but {MISSING} does not exist: install an MTA (Ubuntu: apt install "
        "postfix, dma or msmtp-mta; FreeBSD 14+: dma is in base) or set transport = smtp and "
        "smtp_host")
    explicit = make_config(tmp_path, f"transport = sendmail\nsendmail_path = {MISSING}\n")
    with pytest.raises(ConfigError, match="transport = sendmail but .* does not exist"):
        choose(explicit.settings)
    smtp = make_config(tmp_path, "transport = smtp\nsmtp_host = 127.0.0.1\n")
    assert choose(smtp.settings) == "smtp"


def test_a_sendmail_that_is_not_executable_is_named(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    binary = tmp_path / "sendmail"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    binary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    config = make_config(tmp_path, f"sendmail_path = {binary.as_posix()}\n")
    with pytest.raises(ConfigError, match="is not executable: fix its mode, or install an MTA"):
        choose(config.settings)


def test_check_config_exits_2_without_a_usable_transport(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_config(tmp_path, f"sendmail_path = {MISSING}\n")
    assert main(["--check-config", "-f", str(tmp_path / "logalert.conf")]) == 2
    out = capsys.readouterr()
    assert "transport: auto -> sendmail at" in out.out  # the settings are still printed
    assert out.err.startswith(f"logalert: transport = auto but {MISSING} does not exist")


# -- the sendmail pipe --------------------------------------------------------------------------


def test_the_argv_and_the_bytes_reach_sendmail_exactly(
        tmp_path: Path, fake_dir: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.transport")
    config = make_config(tmp_path, f"sendmail_path = {MISSING}\n",
                         to="noc@example.net, ops@example.net")
    mail = a_mail(config)
    delivery = deliver(mail, config.settings, command=[sys.executable, str(FAKE)])
    argv, stdin = records(fake_dir)
    assert argv["argv"][1:] == ["-i", "-f", SENDER, "noc@example.net", "ops@example.net"]
    assert argv["dash_i"] is True and argv["envelope_from"] == SENDER
    assert stdin == mail.flatten("sendmail") and b"\r\n" not in stdin
    assert delivery == Delivery("sendmail", SENDER, ("noc@example.net", "ops@example.net"), (),
                                len(stdin), "accepted for queueing (exit 0)")
    assert caplog.messages == [f"sent via sendmail to noc@example.net, ops@example.net: "
                               f"{len(stdin)} bytes, {mail.message_id} "
                               "(accepted for queueing (exit 0))"]


def test_a_dot_only_line_reaches_sendmail_as_data(fake_dir: Path) -> None:
    mail = dotted_mail()
    via_sendmail(mail, [sys.executable, str(FAKE)], timeout=30)
    _, stdin = records(fake_dir)
    assert stdin == mail.flatten("sendmail")
    assert NL.encode() + b"." + NL.encode() in stdin  # the line is data: -i is in the argv


@pytest.mark.parametrize("code, named", [(65, "sendmail exit 65 (EX_DATAERR)"),
                                         (75, "sendmail exit 75 (EX_TEMPFAIL)"),
                                         (3, "sendmail exit 3")])
def test_a_non_zero_exit_names_the_sysexits_code_and_the_first_stderr_line(
        fake_dir: Path, monkeypatch: pytest.MonkeyPatch, code: int, named: str) -> None:
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", str(code))
    with pytest.raises(DeliveryError) as exc:
        via_sendmail(dotted_mail(), [sys.executable, str(FAKE)], timeout=30)
    assert str(exc.value) == f"{named}: sendmail: bad mail input format"
    assert len(SYSEXITS) == 15 and SYSEXITS[64] == "EX_USAGE" and SYSEXITS[78] == "EX_CONFIG"


def test_a_sleeping_sendmail_is_killed_at_mail_timeout(
        fake_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGALERT_FAKE_SLEEP", "20")
    started = time.monotonic()
    with pytest.raises(DeliveryError) as exc:
        via_sendmail(dotted_mail(), [sys.executable, str(FAKE)], timeout=0.5)
    elapsed = time.monotonic() - started
    assert str(exc.value) == "sendmail did not finish within 0.5 s"
    assert elapsed < 5, elapsed  # the child was killed, not waited for
    if sys.platform != "win32":  # os.kill(pid, 0) is not a liveness probe on Windows
        pid = int((fake_dir / "pid.txt").read_text(encoding="ascii"))
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_what_a_hanging_sendmail_said_is_kept(
        fake_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGALERT_FAKE_SLEEP", "20")
    monkeypatch.setenv("LOGALERT_FAKE_STDERR_BYTES", "40")
    with pytest.raises(DeliveryError, match=r"did not finish within 0.5 s \(it said: e+\)"):
        via_sendmail(dotted_mail(), [sys.executable, str(FAKE)], timeout=0.5)


def test_a_missing_sendmail_is_could_not_execute(tmp_path: Path) -> None:
    with pytest.raises(DeliveryError) as exc:
        via_sendmail(dotted_mail(), [MISSING], timeout=5)
    assert str(exc.value).startswith(f"could not execute {MISSING}: ")
    with pytest.raises(DeliveryError, match=f"could not execute {tmp_path.as_posix()}"):
        via_sendmail(dotted_mail(), [tmp_path.as_posix()], timeout=5)  # a directory


# -- SMTP ---------------------------------------------------------------------------------------


def smtp_settings(tmp_path: Path, port: int, extra: str = "", to: str = "noc@example.net",
                  ) -> Config:
    return make_config(tmp_path, f"transport = smtp\nsmtp_host = 127.0.0.1\nsmtp_port = {port}\n"
                       f"mail_timeout = 5\n{extra}", to=to)


def test_smtp_ehlo_carries_the_froms_host_and_the_bytes_are_dot_stuffed_only(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.transport")
    mail = dotted_mail()
    with run_stub() as server:
        config = smtp_settings(tmp_path, server.port)
        delivery = deliver(mail, config.settings)
        server.wait_idle()
    assert server.commands[0].lower() == b"ehlo router1.example.net\r\n"
    assert server.verbs() == ["EHLO", "MAIL", "RCPT", "DATA", "QUIT"]
    data = mail.flatten("smtp")
    assert server.data == [stuffed(data)]
    assert len(stuffed(data)) == len(data) + 2 + 3  # two dot-stuffed lines, the terminator
    assert delivery == Delivery("smtp", SENDER, ("noc@example.net",), (), len(data),
                                f"accepted by 127.0.0.1:{server.port}")
    assert caplog.messages[0].startswith("sent via smtp to noc@example.net: ")


def test_one_refused_recipient_is_a_per_recipient_failure_the_others_delivered(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.transport")
    mail = dotted_mail(("noc@example.net", "ops@example.net"))
    with run_stub(StubConfig(refuse=frozenset({"ops@example.net"}))) as server:
        config = smtp_settings(tmp_path, server.port, to="noc@example.net, ops@example.net")
        delivery = deliver(mail, config.settings)
        server.wait_idle()
    assert delivery.accepted == ("noc@example.net",)
    assert delivery.refused == (("ops@example.net", "550 5.1.1 <ops@example.net>: Recipient "
                                 "address rejected: User unknown"),)
    assert server.data == [stuffed(mail.flatten("smtp"))]  # the message went out
    assert [r.levelname for r in caplog.records] == ["INFO", "WARNING"]
    assert caplog.messages[1] == ("refused by the server: ops@example.net -- 550 5.1.1 "
                                  "<ops@example.net>: Recipient address rejected: User unknown")


def test_every_recipient_refused_is_a_failure_naming_each(tmp_path: Path) -> None:
    mail = dotted_mail(("noc@example.net", "ops@example.net"))
    with run_stub(StubConfig(refuse=frozenset({"noc@example.net", "ops@example.net"}))) as server:
        config = smtp_settings(tmp_path, server.port, to="noc@example.net, ops@example.net")
        with pytest.raises(DeliveryError) as exc:
            deliver(mail, config.settings)
        server.wait_idle()
    assert str(exc.value) == (
        "every recipient refused: noc@example.net -- 550 5.1.1 <noc@example.net>: Recipient "
        "address rejected: User unknown; ops@example.net -- 550 5.1.1 <ops@example.net>: "
        "Recipient address rejected: User unknown")
    assert server.data == [] and "QUIT" in server.verbs()  # nothing sent; the session closed


@pytest.mark.parametrize("stub, expected", [
    (StubConfig(data_code=552), "SMTPDataError: 552 5.3.4 Error: message rejected by stub"),
    (StubConfig(drop_on="MAIL"), "SMTPServerDisconnected: Connection unexpectedly closed"),
    (StubConfig(replies={"MAIL": "421 4.3.2 Service shutting down"}),
     "SMTPSenderRefused: 421 4.3.2 Service shutting down"),
    (StubConfig(replies={"EHLO": "500 5.5.2 no", "HELO": "500 5.5.2 no"}),
     "SMTPHeloError: 500 5.5.2 no"),
])
def test_server_failures_are_named_by_type_and_reply(stub: StubConfig, expected: str) -> None:
    with run_stub(stub) as server:
        with pytest.raises(DeliveryError) as exc:
            via_smtp(dotted_mail(), "127.0.0.1", server.port, starttls=False,
                     local_hostname="router1.example.net", timeout=5)
        server.wait_idle()
    assert str(exc.value).startswith(expected)


def test_a_silent_server_is_bounded_by_the_timeout() -> None:
    started = time.monotonic()
    with run_stub(StubConfig(send_banner=False)) as server:
        with pytest.raises(DeliveryError) as exc:
            via_smtp(dotted_mail(), "127.0.0.1", server.port, starttls=False,
                     local_hostname="router1.example.net", timeout=1)
    assert 0.9 < time.monotonic() - started < 4
    assert str(exc.value) == ("no reply within 1 s (SMTPServerDisconnected: Connection unexpectedly"
                              " closed: timed out); a message the server had read at DATA may"
                              " still be delivered")


def test_nothing_listening_is_a_connection_error() -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(DeliveryError, match="^ConnectionRefusedError: "):  # ~2 s on Windows
        via_smtp(dotted_mail(), "127.0.0.1", port, starttls=False,
                 local_hostname="router1.example.net", timeout=5)


def test_starttls_against_a_server_that_does_not_offer_it_is_loud(tmp_path: Path) -> None:
    with run_stub() as server:
        config = smtp_settings(tmp_path, server.port, "smtp_starttls = yes\n")
        with pytest.raises(DeliveryError) as exc:
            deliver(dotted_mail(), config.settings)
        server.wait_idle()
    assert str(exc.value) == "SMTPNotSupportedError: STARTTLS extension not supported by server."
    assert "MAIL" not in server.verbs() and server.data == []


def test_starttls_accepted_without_a_handshake_is_a_named_failure() -> None:
    # measured: an SSLError (wrong version number) when the peer keeps talking plain text
    with run_stub(StubConfig(advertise_starttls=True)) as server:
        with pytest.raises(DeliveryError) as exc:
            via_smtp(dotted_mail(), "127.0.0.1", server.port, starttls=True,
                     local_hostname="router1.example.net", timeout=5)
    assert str(exc.value).startswith(("SSLError: ", "ConnectionAbortedError: ", "SSLEOFError: "))
    assert "STARTTLS" in server.verbs()


# -- the From -----------------------------------------------------------------------------------


def test_resolve_sender_prefers_the_override_then_the_config_and_never_looks_up(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object) -> str:
        raise AssertionError("a resolver was consulted")

    monkeypatch.setattr(socket, "getfqdn", refuse)
    monkeypatch.setattr(socket, "gethostname", refuse)
    config = make_config(tmp_path, "from = alerts@example.net\n")
    assert resolve_sender(config.settings) == ("alerts@example.net", None)
    assert resolve_sender(config.settings, "ops@example.net") == ("ops@example.net", None)
    assert resolve_sender(config.settings, "cron@router1") == (
        "cron@router1", "From address 'cron@router1' has no domain part; set from = in [logalert]")
    with pytest.raises(ConfigError, match="--from: 'Ops <ops@example.net>' is not a bare"):
        resolve_sender(config.settings, "Ops <ops@example.net>")


def test_the_default_from_is_the_running_user_at_this_host(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    if sys.platform == "win32":
        user = getpass.getuser()
    else:
        user = pwd.getpwuid(os.geteuid()).pw_name
    try:
        address, _ = resolve_sender(config.settings)
    except ConfigError as exc:  # a dev-host user name with a space is not an address
        assert str(exc).startswith(f"the default From: '{user}@")
        return
    assert address.startswith(user + "@") and address == address.strip()


def test_from_that_is_not_an_address_is_a_usage_error(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_config(tmp_path)
    with pytest.raises(SystemExit) as exc:
        main(["--from", "nobody", "--check-config", "-f", str(tmp_path / "logalert.conf")])
    assert exc.value.code == 2
    assert "--from: 'nobody' is not a bare local@domain address" in capsys.readouterr().err


# -- --test-mail end to end ---------------------------------------------------------------------


def test_test_mail_through_sendmail_end_to_end(
        tmp_path: Path, fake_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    binary = fake_mta(tmp_path)
    make_config(tmp_path, f"sendmail_path = {binary.as_posix()}\nfrom = alerts@example.net\n")
    assert main(["--test-mail", "router-disk", "-f", str(tmp_path / "logalert.conf")]) == 0
    out = capsys.readouterr()
    argv, stdin = records(fake_dir)
    msg = message_from_bytes(stdin, policy=default_policy)
    assert argv["dash_i"] is True and argv["envelope_from"] == "alerts@example.net"
    assert argv["recipients"] == ["noc@example.net"]
    assert msg["Subject"] == "logalert test: Router disk failure"
    assert msg["From"] == "alerts@example.net" and msg["To"] == "noc@example.net"
    assert "for the section [router-disk]" in str(msg.get_content())
    lines = out.out.splitlines()
    assert lines[0] == f"sent via sendmail ({binary.as_posix()}): accepted for queueing (exit 0)"
    assert lines[1] == "to: noc@example.net"
    assert lines[2] == f"size: {len(stdin)} bytes; Message-ID: {msg['Message-ID']}"
    assert out.err == ""


def test_test_mail_reports_a_failure_and_a_refusal(
        tmp_path: Path, fake_dir: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    binary = fake_mta(tmp_path)
    make_config(tmp_path, f"sendmail_path = {binary.as_posix()}\nfrom = alerts@example.net\n")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "65")
    assert main(["--test-mail", "router-disk", "-f", str(tmp_path / "logalert.conf")]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == (f"logalert: not delivered via sendmail ({binary.as_posix()}): sendmail "
                       "exit 65 (EX_DATAERR): sendmail: bad mail input format" + NL)
    with run_stub(StubConfig(refuse=frozenset({"ops@example.net"}))) as server:
        smtp_settings(tmp_path, server.port, "from = alerts@example.net\n",
                      to="noc@example.net, ops@example.net")
        assert main(["--test-mail", "router-disk", "-f", str(tmp_path / "logalert.conf")]) == 1
        server.wait_idle()
    out = capsys.readouterr()
    assert out.out.startswith(f"sent via smtp (127.0.0.1:{server.port}): accepted by ")
    assert "to: noc@example.net" + NL in out.out
    assert out.err == ("refused: ops@example.net -- 550 5.1.1 <ops@example.net>: Recipient "
                       "address rejected: User unknown" + NL)


def test_test_mail_names_an_unknown_section_and_the_transport_error(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_config(tmp_path, f"sendmail_path = {MISSING}\n")
    conf = str(tmp_path / "logalert.conf")
    assert main(["--test-mail", "firewall", "-f", conf]) == 2
    assert capsys.readouterr().err == (
        f"logalert: no section [firewall] in {conf}; sections: router-disk" + NL)
    assert main(["--test-mail", "router-disk", "-f", conf]) == 2
    assert capsys.readouterr().err.startswith("logalert: transport = auto but")


def test_from_override_reaches_the_envelope_the_header_and_the_warning(
        tmp_path: Path, fake_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    binary = fake_mta(tmp_path)
    make_config(tmp_path, f"sendmail_path = {binary.as_posix()}\nfrom = alerts@example.net\n")
    assert main(["--test-mail", "router-disk", "-f", str(tmp_path / "logalert.conf"),
                 "--from", "cron@router1"]) == 0
    out = capsys.readouterr()
    argv, stdin = records(fake_dir)
    assert argv["envelope_from"] == "cron@router1"
    assert message_from_bytes(stdin, policy=default_policy)["From"] == "cron@router1"
    assert out.err == ("logalert: warning: From address 'cron@router1' has no domain part; "
                       "set from = in [logalert]" + NL)


def test_compose_test_is_one_paragraph_with_the_standard_headers(tmp_path: Path) -> None:
    config = make_config(tmp_path, to="noc@example.net, ops@example.net")
    mail = compose_test(config.watches[0], sender=SENDER, settings=config.settings,
                        host="router1")
    msg = mail.message
    assert msg["Subject"] == "logalert test: Router disk failure"
    assert msg["X-Logalert-Section"] == "router-disk" and msg["Auto-Submitted"] == "auto-generated"
    assert "X-Priority" not in msg and "Importance" not in msg
    body = str(msg.get_content())
    assert body.startswith("This is a test message from logalert ")
    assert " on router1 at " in body and "noc@example.net, ops@example.net" in body
    assert mail.message_id in body and body.count(NL) == 1
    assert mail.recipients == ("noc@example.net", "ops@example.net") and mail.matched == 0


# -- the review round's pins ------------------------------------------------------------------


def test_a_recipient_listed_twice_is_one_and_nothing_accepted_still_raises(
        tmp_path: Path) -> None:
    # smtplib decides all-refused by counting: two list entries, one dict key, DATA goes out
    config = make_config(tmp_path, to="noc@example.net, noc@example.net")
    assert config.watches[0].to == ("noc@example.net",)  # the loader deduplicates
    mail = dotted_mail(("noc@example.net", "noc@example.net"))  # hand-built: the guard
    with run_stub(StubConfig(refuse=frozenset({"noc@example.net"}))) as server:
        with pytest.raises(DeliveryError, match="^every recipient refused: noc@example.net -- 550"):
            via_smtp(mail, "127.0.0.1", server.port, starttls=False,
                     local_hostname="router1.example.net", timeout=5)
        server.wait_idle()


def test_a_quit_answered_with_500_after_the_data_250_is_still_a_delivery(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # the context manager's __exit__ raised here, and #12 would have re-sent the mail
    caplog.set_level(logging.DEBUG, logger="logalert.transport")
    with run_stub(StubConfig(replies={"QUIT": "500 5.5.2 what"})) as server:
        delivery = deliver(dotted_mail(), smtp_settings(tmp_path, server.port).settings)
        server.wait_idle()
    assert delivery.accepted == ("noc@example.net",)
    assert len(server.data) == 1 and server.verbs()[-1] == "QUIT"
    assert caplog.messages[0] == "QUIT after the outcome was known: 500 5.5.2 what"  # DEBUG


def test_a_multi_line_or_control_laden_reply_is_one_clean_line_everywhere(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.transport")
    crlf = chr(13) + chr(10)
    with run_stub(StubConfig(replies={"MAIL": "550-5.7.1 first" + crlf + "550 5.7.1 second"})
                  ) as server:
        with pytest.raises(DeliveryError) as exc:
            deliver(dotted_mail(), smtp_settings(tmp_path, server.port).settings)
        server.wait_idle()
    assert str(exc.value) == "SMTPSenderRefused: 550 5.7.1 first 5.7.1 second"
    forged = "550-5.1.1 no" + crlf + "550 5.1.1 " + chr(27) + "[2J INFO forged line"
    mail = dotted_mail(("noc@example.net", "ops@example.net"))
    with run_stub(StubConfig(replies={"RCPT": forged})) as server:
        with pytest.raises(DeliveryError) as exc:
            deliver(mail, smtp_settings(tmp_path, server.port,
                                        to="noc@example.net, ops@example.net").settings)
        server.wait_idle()
    text = str(exc.value)
    assert NL not in text and chr(27) not in text and chr(0xFFFD) in text
    assert all(NL not in m for m in caplog.messages)


def test_a_421_mid_rcpt_names_the_recipients_never_tried(tmp_path: Path) -> None:
    mail = dotted_mail(("noc@example.net", "ops@example.net"))
    with run_stub(StubConfig(replies={"RCPT": "421 4.3.2 Service shutting down"})) as server:
        with pytest.raises(DeliveryError) as exc:
            deliver(mail, smtp_settings(tmp_path, server.port,
                                        to="noc@example.net, ops@example.net").settings)
        server.wait_idle()
    assert str(exc.value) == (
        "the server ended the session at RCPT (noc@example.net -- 421 4.3.2 Service shutting "
        "down); nothing was sent, not tried: ops@example.net")
    assert server.data == []


def test_mail_timeout_is_capped_by_the_loader_and_an_unusable_one_is_named(
        tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="mail_timeout: must be at most 3600 seconds"):
        make_config(tmp_path, "mail_timeout = 100000000" + NL)
    # measured: 1e300 overflows subprocess and socket timeouts on both platforms
    with pytest.raises(DeliveryError, match="^mail_timeout 1e\\+300 s is not usable here: "):
        via_sendmail(dotted_mail(), [sys.executable, str(FAKE)], timeout=1e300)
    with run_stub() as server:
        with pytest.raises(DeliveryError, match="^mail_timeout 1e\\+300 s is not usable here: "):
            via_smtp(dotted_mail(), "127.0.0.1", server.port, starttls=False,
                     local_hostname="router1.example.net", timeout=1e300)


def test_exit_0_with_stderr_is_kept_in_the_answer_and_warned(
        fake_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
        tmp_path: Path) -> None:
    caplog.set_level(logging.INFO, logger="logalert.transport")
    monkeypatch.setenv("LOGALERT_FAKE_STDERR_BYTES", "30")
    config = make_config(tmp_path, f"sendmail_path = {MISSING}" + NL)
    delivery = deliver(dotted_mail(), config.settings, command=[sys.executable, str(FAKE)])
    assert delivery.answer == "accepted for queueing (exit 0); it said: " + "e" * 30
    assert [r.levelname for r in caplog.records] == ["WARNING", "INFO"]
    assert caplog.messages[0] == "sendmail exit 0 but it said: " + "e" * 30


def test_a_broken_shebang_says_the_file_exists(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("no shebang semantics on Windows; runs in the sandbox and on CI")
    script = tmp_path / "sendmail"
    script.write_text("#!/nonexistent/interpreter" + NL + "exit 0" + NL, encoding="ascii")
    script.chmod(0o755)
    with pytest.raises(DeliveryError) as exc:
        via_sendmail(dotted_mail(), [script.as_posix()], timeout=5)
    assert str(exc.value) == (f"could not execute {script.as_posix()}: No such file or "
                              "directory (the file exists: the interpreter on its #! line "
                              "does not)")


def test_a_forking_wrapper_dies_with_its_child_at_the_timeout(
        tmp_path: Path, fake_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("no process groups on Windows; runs in the sandbox and on CI")
    # a sudo/runuser-style wrapper forks and waits: only a group kill reaches the fake
    wrapper = tmp_path / "wrapper"
    wrapper.write_text("#!/bin/sh" + NL + f'"{sys.executable}" "{FAKE}" "$@"' + NL,
                       encoding="utf-8")
    wrapper.chmod(0o755)
    monkeypatch.setenv("LOGALERT_FAKE_SLEEP", "20")
    with pytest.raises(DeliveryError, match="did not finish within 0.5 s"):
        via_sendmail(dotted_mail(), [wrapper.as_posix()], timeout=0.5)
    pid = int((fake_dir / "pid.txt").read_text(encoding="ascii"))
    for _ in range(40):  # the group kill is asynchronous by a few milliseconds
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the wrapped fake (pid {pid}) survived the timeout")


def test_check_config_prints_and_checks_the_from_the_run_would_use(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    make_config(tmp_path, "from = alerts@example.net" + NL)
    conf = str(tmp_path / "logalert.conf")
    usable = f"sendmail_path = {Path(sys.executable).as_posix()}" + NL
    make_config(tmp_path, usable + "from = alerts@example.net" + NL)
    assert main(["--check-config", "-f", conf, "--from", "cron@override.example.net"]) == 0
    assert "from: cron@override.example.net (--from)" + NL in capsys.readouterr().out
    # a default From the run would refuse (a host name with an underscore, a user with a
    # space) is a configuration error here too, not a surprise at the first alert
    make_config(tmp_path, usable)
    monkeypatch.setattr(logalert.config, "default_from", lambda: "bad user@router1")
    monkeypatch.setattr(logalert.transport, "default_from", lambda: "bad user@router1")
    assert main(["--check-config", "-f", conf]) == 2
    out = capsys.readouterr()
    assert "from: bad user@router1 (default:" in out.out
    assert out.err == ("logalert: the default From: 'bad user@router1' is not a bare "
                       "local@domain address; set from = in [logalert]" + NL)


def test_test_mail_from_a_real_console_prints_each_line_once(tmp_path: Path) -> None:
    # pytest installs a root handler; a real console has none, and logging.lastResort
    # printed deliver()'s WARNING beside test_mail()'s own line until the NullHandler
    with run_stub(StubConfig(refuse=frozenset({"ops@example.net"}))) as server:
        smtp_settings(tmp_path, server.port, "from = alerts@example.net" + NL,
                      to="noc@example.net, ops@example.net")
        result = subprocess.run(
            [sys.executable, "-m", "logalert", "--test-mail", "router-disk", "-f",
             str(tmp_path / "logalert.conf")],
            capture_output=True, encoding="utf-8", errors="replace", check=False)
        server.wait_idle()
    assert result.returncode == 1
    assert result.stdout.splitlines()[1] == "to: noc@example.net"
    assert result.stderr.splitlines() == ["refused: ops@example.net -- 550 5.1.1 "
                                          "<ops@example.net>: Recipient address rejected: "
                                          "User unknown"]
    with run_stub() as server:
        smtp_settings(tmp_path, server.port, "from = alerts@example.net" + NL)
        result = subprocess.run(
            [sys.executable, "-m", "logalert", "--test-mail", "router-disk", "-f",
             str(tmp_path / "logalert.conf")],
            capture_output=True, encoding="utf-8", errors="replace", check=False)
        server.wait_idle()
    assert result.returncode == 0 and result.stderr == ""
    lines = result.stdout.splitlines()
    assert lines[0] == (f"sent via smtp (127.0.0.1:{server.port}): accepted by "
                        f"127.0.0.1:{server.port}")
    assert lines[1] == "to: noc@example.net" and lines[2].startswith("size: ")


def test_the_fake_ends_the_message_at_a_lone_dot_without_dash_i(fake_dir: Path) -> None:
    # dma's measured rule, so the dot test observes a dropped flag and not its spelling
    body = ("before" + NL + "." + NL + "after" + NL).encode()
    subprocess.run([sys.executable, str(FAKE), "-f", SENDER, "noc@example.net"], input=body,
                   capture_output=True, check=True)
    assert records(fake_dir)[1] == ("before" + NL).encode()
    subprocess.run([sys.executable, str(FAKE), "-i", "-f", SENDER, "noc@example.net"],
                   input=body, capture_output=True, check=True)
    assert records(fake_dir)[1] == body


# -- the mutation reviewer's pins: each names the mutation it catches ------------------------


# -- mutation: _first_line returns the LAST non-empty line ------------------------------------
def test_the_first_stderr_line_wins_when_sendmail_says_more(
        fake_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 80 bytes of noise is one full 79-byte line plus its newline: the dma line comes second
    monkeypatch.setenv("LOGALERT_FAKE_STDERR_BYTES", "80")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "65")
    with pytest.raises(DeliveryError) as exc:
        via_sendmail(dotted_mail(), [sys.executable, str(FAKE)], timeout=30)
    assert str(exc.value) == "sendmail exit 65 (EX_DATAERR): " + "e" * 79


# -- mutations: ascii instead of utf-8 in _first_line and _reply; CR kept (Linux) ---------------
def test_child_and_server_text_is_utf8_decoded_and_cr_stripped() -> None:
    e_acute = chr(0xE9)  # U+00E9, two bytes in UTF-8: 0xC3 0xA9
    utf8_line = bytes([0x64, 0x6D, 0x61, 0x3A, 0x20, 0xC3, 0xA9, 0x0A])  # "dma: <e-acute>\n"
    assert _first_line(utf8_line) == "dma: " + e_acute
    assert _first_line(bytes([0xFF, 0x0A])) == chr(0xFFFD)  # replace, never raise
    # a CRLF child on Linux (the fake writes CRLF only on Windows, so pin it by hand)
    assert _first_line(("  said" + CR + NL + "more" + CR + NL).encode("ascii")) == "said"
    assert _first_line((NL + CR + NL + " second " + CR + NL).encode("ascii")) == "second"
    assert _reply(550, bytes([0x35, 0x2E, 0x31, 0x2E, 0x31, 0x20, 0xC3, 0xA9])) == (
        "550 5.1.1 " + e_acute)
    assert _reply(250, b"") == "250"


# -- mutation: the OSError reason dropped from "could not execute" ------------------------------
def test_could_not_execute_names_the_reason() -> None:
    with pytest.raises(DeliveryError) as exc:
        via_sendmail(dotted_mail(), [MISSING], timeout=5)
    reason = str(exc.value).removeprefix(f"could not execute {MISSING}: ")
    # measured: strerror on Linux and the winerror text on Windows
    assert reason in {"No such file or directory", "The system cannot find the file specified"}


# -- mutation: accepted computed with a lower-cased lookup into the refused dict ----------------
def test_a_refused_recipient_keeps_its_case(tmp_path: Path) -> None:
    mail = dotted_mail(("noc@example.net", "Ops@Example.net"))
    with run_stub(StubConfig(refuse=frozenset({"Ops@Example.net"}))) as server:
        config = smtp_settings(tmp_path, server.port, to="noc@example.net, Ops@Example.net")
        delivery = deliver(mail, config.settings)
        assert server.wait_idle()
    assert delivery.accepted == ("noc@example.net",)
    assert [rcpt for rcpt, _ in delivery.refused] == ["Ops@Example.net"]


# -- mutations: an INFO "sent via" line on the failure path ---------------------------------------
def test_a_failed_delivery_logs_nothing(
        tmp_path: Path, fake_dir: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="logalert.transport")
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    config = make_config(tmp_path, f"sendmail_path = {MISSING}" + NL)
    with pytest.raises(DeliveryError):
        deliver(dotted_mail(), config.settings, command=[sys.executable, str(FAKE)])
    assert caplog.records == []  # the caller logs the failure; no "sent via" line
    with run_stub(StubConfig(refuse=frozenset({"noc@example.net"}))) as server:
        with pytest.raises(DeliveryError):
            deliver(dotted_mail(), smtp_settings(tmp_path, server.port).settings)
        assert server.wait_idle()
    assert caplog.records == []


# -- mutations: the source labels in resolve_sender ---------------------------------------------
def test_resolve_sender_names_the_source_of_a_bad_address(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(logalert.transport, "default_from", lambda: "bad user@router1")
    with pytest.raises(ConfigError, match="^the default From: 'bad user@router1' is not a bare"):
        resolve_sender(Settings())
    # the loader never lets this through; a hand-built Settings can
    with pytest.raises(ConfigError, match="^from: 'Ops <ops@example.net>' is not a bare"):
        resolve_sender(Settings(from_address="Ops <ops@example.net>"))
    with pytest.raises(ConfigError, match="^--from: 'nobody' is not a bare"):
        resolve_sender(Settings(from_address="alerts@example.net"), "nobody")


# -- RED on the original tree: choose() accepts a directory (exists + X_OK), describe() says
# -- NOT FOUND (isfile); --check-config prints NOT FOUND and exits 0, --test-mail exits 1 with
# -- "could not execute <dir>: Permission denied" instead of 2 with the actionable line ----------
def test_a_directory_as_sendmail_path_is_a_configuration_error(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = make_config(tmp_path, f"sendmail_path = {tmp_path.as_posix()}" + NL)
    with pytest.raises(ConfigError, match=f"^transport = auto but {re.escape(tmp_path.as_posix())} "
                                          "(does not exist|is not a regular file)"):
        choose(config.settings)
    assert main(["--check-config", "-f", str(tmp_path / "logalert.conf")]) == 2
    out = capsys.readouterr()
    assert "NOT A FILE" in out.out and out.err.startswith("logalert: transport = auto but")


# -- mutation: the fake reads only the first 4096 bytes of stdin (no test exceeds a pipe buffer)
def test_a_large_message_reaches_sendmail_intact(fake_dir: Path) -> None:
    msg = EmailMessage(policy=POLICY)
    msg["From"] = SENDER
    msg["To"] = "noc@example.net"
    msg["Subject"] = "big"
    msg["Message-ID"] = "<big@router1.example.net>"
    msg.set_content(("x" * 76 + NL) * 4000)  # ~300 KB, past any pipe buffer
    mail = Mail(msg, "<big@router1.example.net>", SENDER, "big", ("noc@example.net",), None, 1)
    delivery = via_sendmail(mail, [sys.executable, str(FAKE)], timeout=30)
    _, stdin = records(fake_dir)
    assert len(stdin) > 65536 and stdin == mail.flatten("sendmail")
    assert delivery.size == len(stdin)


# -- mutation (cosmetic): {timeout} instead of {timeout:g} -- "1.0 s" for a float -----------------
def test_the_timeout_in_the_message_is_a_whole_number(
        fake_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOGALERT_FAKE_SLEEP", "20")
    with pytest.raises(DeliveryError, match=r"^sendmail did not finish within 1 s$"):
        via_sendmail(dotted_mail(), [sys.executable, str(FAKE)], timeout=1.0)
