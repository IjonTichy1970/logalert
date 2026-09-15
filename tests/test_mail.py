"""Alert email composition (issue #10): headers, summary, report, attachment, sanitising, size.

Pure Python over hand-built reports: real on both platforms, no skips. The scan is the real
one (issue #9's), so the entries have the shape the run loop will hand over.
"""

import email.utils
import socket
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from logalert import __version__
from logalert.__main__ import main
from logalert.config import Config, Watch, load_config
from logalert.cursor import Line
from logalert.mail import (
    Mail,
    Transport,
    attachment_name,
    clean_header,
    clean_text,
    compose,
    render,
)
from logalert.match import GAP, Entry, FileReport, scan

NL = chr(10)
CR = chr(13)
TAB = chr(9)
NUL = chr(0)
FORM_FEED = chr(12)
EM_DASH = chr(0x2014)  # spelled from the code point for the ASCII gate
REPLACEMENT = chr(0xFFFD)
LONE_SURROGATE = chr(0xDCFF)  # what surrogateescape would leave in a str

FILE = "/var/log/router.log"
ARCHIVE = "/var/log/router.log.1.gz"
FILE2 = "/var/log/router2.log"
SENDER = "logalert@router1.example.net"
NOW = datetime(2026, 9, 15, 8, 0, 28, tzinfo=timezone(timedelta(hours=-6)))
STAMP = "20260915T0800"
TRANSPORTS: tuple[Transport, ...] = ("sendmail", "smtp")


def make_config(tmp_path: Path, body: str = "", settings: str = "",
                to: str = "noc@example.net") -> Config:
    """A config built by the real loader: the watch's rules are the loader's."""
    text = (f"[logalert]\n{settings}\n[router-disk]\nsubject = Router disk failure\n"
            f"to = {to}\nfiles = {FILE}\n    {FILE2}\n" + body)
    path = tmp_path / "logalert.conf"
    path.write_text(text, encoding="utf-8", newline="\n")
    return load_config(str(path))


def numbered(texts: list[str], path: str, start: int = 1) -> list[Line]:
    return [Line(text, False, start + i, path) for i, text in enumerate(texts)]


def reports_for(config: Config, *, context: int = 1, cap: int | None = None,
                first: list[Line] | None = None, second: list[Line] | None = None,
                ) -> list[FileReport]:
    """Two files through the real scan: the first with three matches (one [high], two low
    under the section's priority when set), the second rotation-shaped -- an archive's tail,
    then the live file."""
    watch = config.watches[0]
    if first is None:
        first = numbered(["boot", "kernel: ata1: disk failure", "kernel: ata1: retrying",
                          "quiet", "quiet", "link flap on eth0", "link flap on eth1"], FILE)
    if second is None:
        second = (numbered(["old news", "disk failure in the archive"], ARCHIVE, start=1200)
                  + numbered(["fresh start", "disk failure again"], FILE2))
    cap = watch.max_lines if cap is None else cap
    return [scan(FILE, first, watch, context=context, cap=cap),
            scan(FILE2, second, watch, context=context, cap=cap)]


PATTERNS = "patterns =\n    [high] ata1: disk failure\n    disk failure\n    link flap\n"


def composed(tmp_path: Path, body: str = PATTERNS, settings: str = "",
             attach: bool = False) -> Mail:
    config = make_config(tmp_path, body, settings)
    return compose(config.watches[0], reports_for(config), sender=SENDER,
                   settings=config.settings, now=NOW, host="router1", attach=attach)


def body_of(msg: EmailMessage) -> str:
    text = msg.get_body(("plain",))
    assert text is not None
    return str(text.get_content())


def wire_lines(mail: Mail, transport: Transport) -> list[bytes]:
    return mail.flatten(transport).split(b"\n")


def seven_bit(data: bytes) -> bool:
    return all(byte < 0x80 for byte in data)


SUMMARY = (
    "Router disk failure: 5 match(es) on router1 at 2026-09-15 08:00:28 -0600." + NL + NL
    + "Section:    router-disk" + NL
    + "Priority:   high" + NL
    + "Files:      /var/log/router.log -- 3 match(es) in 7 line(s) read" + NL
    + "            /var/log/router2.log -- 2 match(es) in 4 line(s) read" + NL
    + "Message-ID: "
)

REPORT = (
    "==> /var/log/router.log <==" + NL
    + "1- boot" + NL
    + "2: kernel: ata1: disk failure" + NL
    + "3- kernel: ata1: retrying" + NL
    + "--" + NL
    + "5- quiet" + NL
    + "6: link flap on eth0" + NL
    + "7: link flap on eth1" + NL
    + NL
    + "==> /var/log/router.log.1.gz <==" + NL
    + "1200- old news" + NL
    + "1201: disk failure in the archive" + NL
    + NL
    + "==> /var/log/router2.log <==" + NL
    + "1- fresh start" + NL
    + "2: disk failure again" + NL
)


# -- the two modes -----------------------------------------------------------------------------


def test_inline_body_is_the_summary_then_the_report(tmp_path: Path) -> None:
    mail = composed(tmp_path)
    body = body_of(mail.message)
    assert body.startswith(SUMMARY + mail.message_id + NL + NL)
    assert body.endswith(NL + NL + REPORT)
    assert mail.message.get_content_type() == "text/plain"
    assert list(mail.message.iter_attachments()) == []
    assert mail.matched == 5 and mail.priority == "high"
    assert mail.recipients == ("noc@example.net",)


def test_attachment_mode_keeps_the_summary_and_attaches_the_report(tmp_path: Path) -> None:
    inline = composed(tmp_path)
    attached = composed(tmp_path, PATTERNS + "report = attachment\n")
    forced = composed(tmp_path, attach=True)
    for mail in (attached, forced):
        assert mail.message.get_content_type() == "multipart/mixed"
        body = body_of(mail.message)
        summary = body_of(inline.message).split(NL + NL + "==>")[0]
        assert body == (summary.replace(inline.message_id, mail.message_id) + NL + NL
                        + f"The report is attached as router-disk-{STAMP}.txt." + NL)
        (part,) = mail.message.iter_attachments()
        assert part.get_content_type() == "text/plain"
        assert part.get_content_disposition() == "attachment"
        assert part.get_filename() == f"router-disk-{STAMP}.txt"
        assert part.get_content() == REPORT
        assert seven_bit(mail.flatten("sendmail"))


# -- headers -----------------------------------------------------------------------------------


def test_priority_headers_carry_the_highest_only_when_configured(tmp_path: Path) -> None:
    high = composed(tmp_path)  # one [high] tag among untagged patterns with no section priority
    assert (high.message["X-Logalert-Priority"], high.message["X-Priority"],
            high.message["Importance"]) == ("high", "1", "high")
    low = composed(tmp_path, "priority = low\npatterns =\n    disk failure\n    link flap\n")
    assert (low.message["X-Logalert-Priority"], low.message["X-Priority"],
            low.message["Importance"]) == ("low", "5", "low")
    assert "Priority:   low" in body_of(low.message)
    off = composed(tmp_path, "patterns =\n    disk failure\n    link flap\n")
    for header in ("X-Logalert-Priority", "X-Priority", "Importance"):
        assert header not in off.message
    assert "riority" not in body_of(off.message)
    assert off.priority is None


def test_x_prepared_by_is_the_version_flag(tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])
    assert composed(tmp_path).message["X-Prepared-By"] == capsys.readouterr().out.strip()
    assert composed(tmp_path).message["X-Prepared-By"] == f"logalert {__version__}"


def test_date_message_id_and_the_fixed_headers(tmp_path: Path) -> None:
    mail = composed(tmp_path)
    msg = mail.message
    assert email.utils.parsedate_to_datetime(msg["Date"]) == NOW
    assert msg["Message-ID"] == mail.message_id
    assert mail.message_id.startswith("<") and mail.message_id.endswith("@router1.example.net>")
    assert msg["Auto-Submitted"] == "auto-generated"
    assert msg["X-Logalert-Section"] == "router-disk"
    assert msg["From"] == SENDER and msg["To"] == "noc@example.net"
    assert "Message-ID: " + mail.message_id in body_of(msg)


def test_header_order_on_the_wire(tmp_path: Path) -> None:
    head = composed(tmp_path).flatten("sendmail").split(b"\n\n")[0]
    names = [line.split(b":")[0] for line in head.split(b"\n") if not line.startswith(b" ")]
    assert names[:11] == [b"From", b"To", b"Subject", b"Date", b"Message-ID", b"Auto-Submitted",
                          b"X-Prepared-By", b"X-Logalert-Section", b"X-Logalert-Priority",
                          b"X-Priority", b"Importance"]
    assert b"MIME-Version" in names[11:] and b"Content-Type" in names[11:]


def test_no_name_lookup_ever(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object) -> str:
        raise AssertionError("a resolver was consulted")

    monkeypatch.setattr(socket, "getfqdn", refuse)
    monkeypatch.setattr(socket, "gethostname", lambda: "router1")
    config = make_config(tmp_path, PATTERNS)
    mail = compose(config.watches[0], reports_for(config), sender="cron@router1",
                   settings=config.settings)  # now and host defaulted
    assert mail.message_id.endswith("@router1>")
    assert " on router1 at " in body_of(mail.message)
    assert email.utils.parsedate_to_datetime(mail.message["Date"]).tzinfo is not None
    with pytest.raises(ValueError, match="'nobody' is not a bare local@domain address; set from"):
        compose(config.watches[0], reports_for(config), sender="nobody", settings=config.settings)


def test_to_joins_every_recipient(tmp_path: Path) -> None:
    config = make_config(tmp_path, PATTERNS, to="noc@example.net, ops@example.net")
    mail = compose(config.watches[0], reports_for(config), sender=SENDER,
                   settings=config.settings, now=NOW, host="router1")
    assert mail.message["To"] == "noc@example.net, ops@example.net"
    assert mail.recipients == ("noc@example.net", "ops@example.net")


# -- the subject -------------------------------------------------------------------------------


def test_subject_suffix_on_and_off(tmp_path: Path) -> None:
    assert composed(tmp_path).subject == "Router disk failure -- 5 match(es)"
    assert composed(tmp_path).message["Subject"] == "Router disk failure -- 5 match(es)"
    bare = composed(tmp_path, settings="subject_suffix = no\n")
    assert bare.subject == bare.message["Subject"] == "Router disk failure"


def test_subject_controls_are_sanitised_and_non_ascii_is_encoded(tmp_path: Path) -> None:
    config = make_config(tmp_path, PATTERNS)
    watch = config.watches[0]
    odd = with_fields(watch, subject="Disk" + TAB + "fail" + FORM_FEED + "ure " + EM_DASH + " now")
    mail = compose(odd, reports_for(config), sender=SENDER, settings=config.settings, now=NOW)
    assert mail.subject == "Disk fail" + REPLACEMENT + "ure " + EM_DASH + " now -- 5 match(es)"
    assert mail.message["Subject"] == mail.subject
    subject_line = [ln for ln in wire_lines(mail, "sendmail") if ln.startswith(b"Subject:")][0]
    assert b"=?utf-8?" in subject_line and seven_bit(mail.flatten("sendmail"))
    assert body_of(mail.message).startswith(mail.subject.split(" --")[0] + ": 5 match(es)")


def test_section_name_reaches_the_header_and_a_safe_attachment_name(tmp_path: Path) -> None:
    text = (f"[a b{TAB}c{EM_DASH}]\nsubject = S\nto = noc@example.net\nfiles = {FILE}\n"
            "patterns = disk\nreport = attachment\n")
    path = tmp_path / "logalert.conf"
    path.write_text(text, encoding="utf-8", newline="\n")
    config = load_config(str(path))
    watch = config.watches[0]
    report = scan(FILE, numbered(["disk"], FILE), watch, context=0, cap=watch.max_lines)
    mail = compose(watch, [report], sender=SENDER, settings=config.settings, now=NOW)
    assert mail.message["X-Logalert-Section"] == "a b c" + EM_DASH
    (part,) = mail.message.iter_attachments()
    assert part.get_filename() == f"a_b_c_-{STAMP}.txt"
    assert attachment_name("", NOW) == f"section-{STAMP}.txt"
    assert "Section:    a b c" + EM_DASH in body_of(mail.message)


# -- sanitising and the wire -------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("a" + CR + "b", "ab"),
    ("crlf line" + CR, "crlf line"),
    ("a" + NUL + "b", "a" + REPLACEMENT + "b"),
    ("a" + FORM_FEED + "b", "a" + REPLACEMENT + "b"),
    ("a" + EM_DASH + "b", "a" + EM_DASH + "b"),
    ("a" + TAB + "b", "a" + TAB + "b"),
    ("a" + LONE_SURROGATE + "b", "a?b"),
    ("y" * 1500, "y" * 1500),
    (".", "."),
    ("a" + chr(0x7F) + "b", "a" + REPLACEMENT + "b"),
])
def test_every_body_shape_travels_7bit_and_round_trips(tmp_path: Path, text: str,
                                                        expected: str) -> None:
    config = make_config(tmp_path, "patterns =\n    a\n    y\n    crlf\n    .\n")
    watch = config.watches[0]
    stream = numbered(["before", text, "then"], FILE)
    reports = [scan(FILE, stream, watch, context=1, cap=watch.max_lines)]
    for attach in (False, True):
        mail = compose(watch, reports, sender=SENDER, settings=config.settings, now=NOW,
                       host="router1", attach=attach)
        for transport in TRANSPORTS:
            lines = wire_lines(mail, transport)
            assert seven_bit(mail.flatten(transport))
            assert max(len(line.rstrip(b"\r")) for line in lines) <= 78
            assert NUL.encode() not in mail.flatten(transport)
        content = REPORT_FOR_ONE.format(text=expected)
        if attach:
            (part,) = mail.message.iter_attachments()
            assert part.get_content() == content
        else:
            assert body_of(mail.message).endswith(NL + NL + content)


REPORT_FOR_ONE = ("==> /var/log/router.log <==" + NL + "1- before" + NL + "2: {text}" + NL
                  + "3- then" + NL)


def test_clean_text_and_clean_header() -> None:
    assert clean_text("a" + CR + NL + "b") == "a" + NL + "b"  # LF is the caller's separator
    assert clean_header("a" + NL + "b" + TAB + "c ") == "a b c"
    assert clean_header(" " + CR + " ") == ""
    assert clean_text("a" + LONE_SURROGATE) == "a?"


def test_a_line_over_78_chars_goes_quoted_printable_and_a_short_one_7bit(tmp_path: Path) -> None:
    config = make_config(tmp_path, "patterns =\n    disk\n")
    watch = config.watches[0]
    short = compose(watch, [scan(FILE, numbered(["disk"], FILE), watch, context=0, cap=1)],
                    sender=SENDER, settings=config.settings, now=NOW, host="router1")
    assert short.message["Content-Transfer-Encoding"] == "7bit"
    long = compose(watch, [scan(FILE, numbered(["disk " + "x" * 100], FILE), watch, context=0,
                                cap=1)], sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    assert long.message["Content-Transfer-Encoding"] == "quoted-printable"
    assert body_of(long.message).endswith("1: disk " + "x" * 100 + NL)


def test_flatten_is_produced_once_per_transport_and_the_sizes_differ_by_the_lines(
        tmp_path: Path) -> None:
    mail = composed(tmp_path)
    lf = mail.flatten("sendmail")
    crlf = mail.flatten("smtp")
    assert mail.flatten("sendmail") is lf and mail.flatten("smtp") is crlf
    assert b"\r\n" not in lf and crlf.count(b"\r\n") == lf.count(b"\n")
    assert len(crlf) - len(lf) == lf.count(b"\n")
    assert crlf.replace(b"\r\n", b"\n") == lf


# -- the report --------------------------------------------------------------------------------


def test_the_summary_names_every_file_with_its_counts(tmp_path: Path) -> None:
    config = make_config(tmp_path, PATTERNS + "exclude = eth1\n")
    watch = config.watches[0]
    reports = reports_for(config, second=numbered(["nothing here"], FILE2))
    mail = compose(watch, reports, sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    body = body_of(mail.message)
    assert "Router disk failure: 2 match(es) on router1" in body
    assert ("Files:      /var/log/router.log -- 2 match(es) in 7 line(s) read, "
            "1 dropped by an exclude" + NL + "            /var/log/router2.log -- no match in "
            "1 line(s) read" + NL) in body
    assert mail.subject.endswith("-- 2 match(es)")


def test_a_cut_line_is_one_report_line_with_a_marker_when_still_cut() -> None:
    report = FileReport(FILE, entries=[
        Entry("match", 7, "head ", True, FILE), Entry("match", 7, "tail", False, FILE),
        Entry("context", 8, "one ", True, FILE), Entry("context", 8, "two ", True, FILE),
    ], matched=1, context=1)
    assert render([report], 10) == ("==> /var/log/router.log <==" + NL + "7: head tail" + NL
                                    + "8- one two  [cut]" + NL)


def test_max_lines_shows_that_many_matches_with_their_context_then_the_trailer(
        tmp_path: Path) -> None:
    config = make_config(tmp_path, "patterns =\n    MATCH\nmax_lines = 2\n")
    watch = config.watches[0]
    stream = [Line(text, False, n, FILE) for n, text in (
        (1, "MATCH one"), (2, "after one"), (9, "before two"), (10, "MATCH two"),
        (11, "after two"), (19, "before three"), (20, "MATCH three"), (30, "MATCH four"))]
    report = scan(FILE, stream, watch, context=1, cap=watch.max_lines)
    assert render([report], watch.max_lines) == (
        "==> /var/log/router.log <==" + NL + "1: MATCH one" + NL + "2- after one" + NL
        + "--" + NL + "9- before two" + NL + "10: MATCH two" + NL + "11- after two" + NL
        + "... and 2 more matching line(s)" + NL)
    mail = compose(watch, [report], sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    assert mail.subject.endswith("-- 4 match(es)") and mail.matched == 4


def test_the_budget_stops_before_the_next_matchs_window_when_windows_touch(
        tmp_path: Path) -> None:
    # after the last budgeted match only its own after-window follows: with context 2 and
    # touching windows the lines between match 2 and match 3 are match 2's after-context
    # for two lines and then match 3's before-context, which the reader never sees
    config = make_config(tmp_path, "patterns =\n    MATCH\nmax_lines = 2\ncontext = 2\n")
    watch = config.watches[0]
    stream = numbered(["MATCH 1", "MATCH 2", "a", "b", "c", "d", "MATCH 3"], FILE)
    report = scan(FILE, stream, watch, context=2, cap=watch.max_lines)
    assert render([report], 2) == ("==> /var/log/router.log <==" + NL + "1: MATCH 1" + NL
                                   + "2: MATCH 2" + NL + "3- a" + NL + "4- b" + NL
                                   + "... and 1 more matching line(s)" + NL)


def test_the_budget_spans_the_files_in_order(tmp_path: Path) -> None:
    config = make_config(tmp_path, "patterns =\n    disk\nmax_lines = 2\n")
    watch = config.watches[0]
    first = scan(FILE, numbered(["disk a", "disk b"], FILE), watch, context=1, cap=2)
    second = scan(FILE2, numbered(["x", "disk c"], FILE2), watch, context=1, cap=2)
    text = render([first, second], 2)
    assert "router2.log" not in text and "1- x" not in text
    assert text.endswith("2: disk b" + NL + "... and 1 more matching line(s)" + NL)
    assert render([first, second], 3).endswith("==> /var/log/router2.log <==" + NL + "1- x" + NL
                                               + "2: disk c" + NL)


def test_a_file_without_entries_contributes_nothing_and_an_empty_report_is_empty(
        tmp_path: Path) -> None:
    config = make_config(tmp_path, "patterns =\n    disk\n")
    watch = config.watches[0]
    quiet = scan(FILE2, numbered(["nothing"], FILE2), watch, context=1, cap=1)
    assert render([quiet], 10) == ""
    loud = scan(FILE, numbered(["disk"], FILE), watch, context=1, cap=1)
    assert render([quiet, loud], 10) == "==> /var/log/router.log <==" + NL + "1: disk" + NL
    mail = compose(watch, [quiet], sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    assert mail.matched == 0 and body_of(mail.message).endswith("Message-ID: "
                                                                + mail.message_id + NL + NL)


def test_the_summarys_host_and_file_names_are_sanitised_too(tmp_path: Path) -> None:
    config = make_config(tmp_path, "patterns =\n    disk\n")
    watch = config.watches[0]
    odd_file = "/var/log/rou" + NUL + "ter.log"
    report = scan(odd_file, numbered(["disk"], odd_file), watch, context=0, cap=1)
    mail = compose(watch, [report], sender=SENDER, settings=config.settings, now=NOW,
                   host="rout" + FORM_FEED + "er1" + TAB)
    body = body_of(mail.message)
    assert " on rout" + REPLACEMENT + "er1 at " in body
    assert "Files:      /var/log/rou" + REPLACEMENT + "ter.log -- 1 match(es)" in body
    assert NUL not in body and FORM_FEED not in body


# -- the review round's pins (wire lens: NEL/LS/PS, the Message-ID domain, the name length) ---


def with_fields(watch: Watch, **fields: object) -> Watch:
    return watch.__class__(**{**watch.__dict__, **fields})


def wire_line(mail: Mail, name: bytes) -> bytes:
    return [ln for ln in mail.flatten("sendmail").split(b"\n") if ln.startswith(name)][0]


@pytest.mark.parametrize("boundary", [chr(0x85), chr(0x2028), chr(0x2029)])
def test_a_line_boundary_the_loader_passes_does_not_raise_at_composition(
        tmp_path: Path, boundary: str) -> None:
    # NEL, LS and PS are one line to configparser and to _optional, and exactly what
    # EmailMessage refuses in a header value (measured: ValueError on every run for the section)
    text = (f"[rou{boundary}ter]\nsubject = Disk failure{boundary}now\nto = noc@example.net\n"
            f"files = {FILE}\npatterns = disk\n")
    path = tmp_path / "logalert.conf"
    path.write_text(text, encoding="utf-8", newline="\n")
    config = load_config(str(path))
    watch = config.watches[0]
    assert boundary in watch.subject and boundary in watch.name  # the loader kept them
    report = scan(FILE, numbered(["disk"], FILE), watch, context=0, cap=1)
    mail = compose(watch, [report], sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    assert mail.message["Subject"] == "Disk failure now -- 1 match(es)"
    assert mail.message["X-Logalert-Section"] == "rou ter"
    assert seven_bit(mail.flatten("sendmail")) and seven_bit(mail.flatten("smtp"))


@pytest.mark.parametrize("sender, domain", [
    (" logalert@example.net ", "example.net"),  # header-clean first, then judged
    ("logalert@example.net" + chr(0x2028), "example.net"),
    ("cron@router1", "router1"),
    ("Log Alert <logalert@example.net>", None),  # the loader forbids display names
    ("logalert@exa mple.net", None),
    ("logalert@exam" + NUL + "ple.net", None),
    ("logalert@", None),  # 3.12.3's header parser raised IndexError on this one
    ("nobody", None),
])
def test_the_message_id_is_well_formed_and_equal_everywhere_whatever_the_sender(
        tmp_path: Path, sender: str, domain: str | None) -> None:
    # a From the loader never saw (--from, default_from on the dev host) is judged by the
    # loader's rule: accepted, the header, the body and Mail.message_id carry one msg-id;
    # refused, the error names the fix -- never a header the stdlib may choke on
    config = make_config(tmp_path, "patterns =\n    disk\n")
    watch = config.watches[0]
    report = scan(FILE, numbered(["disk"], FILE), watch, context=0, cap=1)
    if domain is None:
        with pytest.raises(ValueError, match="is not a bare local@domain address; set from"):
            compose(watch, [report], sender=sender, settings=config.settings, now=NOW)
        return
    mail = compose(watch, [report], sender=sender, settings=config.settings, now=NOW,
                   host="router1")
    assert mail.message_id.endswith("@" + domain + ">")
    assert str(mail.message["Message-ID"]) == mail.message_id
    assert "Message-ID: " + mail.message_id + NL in body_of(mail.message)
    assert mail.sender == str(mail.message["From"]) == clean_header(sender)
    assert NUL.encode() not in mail.flatten("sendmail") and seven_bit(mail.flatten("smtp"))


def test_the_attachment_name_never_starts_with_a_dash_or_dot_and_fits_one_line() -> None:
    assert attachment_name("-rf", NOW) == f"rf-{STAMP}.txt"
    assert attachment_name("..", NOW) == f"section-{STAMP}.txt"
    assert attachment_name(".hidden", NOW) == f"hidden-{STAMP}.txt"
    assert attachment_name("s" * 60, NOW) == "s" * 40 + f"-{STAMP}.txt"


def test_a_long_section_name_keeps_the_filename_on_one_line(tmp_path: Path) -> None:
    text = (f"[{'s' * 60}]\nsubject = S\nto = noc@example.net\nfiles = {FILE}\n"
            "patterns = disk\nreport = attachment\n")
    path = tmp_path / "logalert.conf"
    path.write_text(text, encoding="utf-8", newline="\n")
    config = load_config(str(path))
    watch = config.watches[0]
    report = scan(FILE, numbered(["disk"], FILE), watch, context=0, cap=1)
    mail = compose(watch, [report], sender=SENDER, settings=config.settings, now=NOW)
    assert b"filename*0" not in mail.flatten("sendmail")  # no RFC 2231 continuation
    expected = f'filename="{"s" * 40}-{STAMP}.txt"'.encode()
    assert any(ln.strip() == expected for ln in wire_lines(mail, "sendmail"))  # one line


def test_preview_is_the_decoded_message_for_a_dry_run(tmp_path: Path) -> None:
    config = make_config(tmp_path, PATTERNS)
    watch = with_fields(config.watches[0], subject="Platte " + EM_DASH + " defekt")
    reports = reports_for(config)
    inline = compose(watch, reports, sender=SENDER, settings=config.settings, now=NOW,
                     host="router1")
    text = inline.preview()
    assert text.startswith("From: " + SENDER + NL + "To: noc@example.net" + NL
                           + "Subject: Platte " + EM_DASH + " defekt -- 5 match(es)" + NL)
    assert "=?utf-8?" not in text and "quoted-printable" in text  # headers decoded, not the wire
    assert text.endswith(NL + NL + body_of(inline.message))
    attached = compose(watch, reports, sender=SENDER, settings=config.settings, now=NOW,
                       host="router1", attach=True)
    text = attached.preview()
    assert text.endswith(NL + NL + f"==== router-disk-{STAMP}.txt ====" + NL + REPORT)
    assert "The report is attached as" in text


def test_the_last_budgeted_matchs_window_closes_with_its_file(tmp_path: Path) -> None:
    # the budget runs out on the archive's tail of a rotation-shaped file and the live
    # file's first match sits within context lines of its start: those lines are that
    # unseen match's before-context, never the archive match's after-window (the
    # changelog verification reproduced the live file's header and two of its lines
    # printed before the trailer)
    config = make_config(tmp_path, "patterns =\n    MATCH\nmax_lines = 2\ncontext = 2\n")
    watch = config.watches[0]
    first = scan(FILE, numbered(["MATCH 1"], FILE), watch, context=2, cap=2)
    second = scan(FILE2, numbered(["x", "MATCH 2"], ARCHIVE, start=1200)
                  + numbered(["a", "b", "MATCH 3"], FILE2), watch, context=2, cap=2)
    assert [e.text for e in second.entries] == ["x", "MATCH 2", "", "a", "b", "MATCH 3"]
    assert render([first, second], 2) == (
        "==> /var/log/router.log <==" + NL + "1: MATCH 1" + NL + NL
        + "==> /var/log/router.log.1.gz <==" + NL + "1200- x" + NL + "1201: MATCH 2" + NL
        + "... and 1 more matching line(s)" + NL)


def test_render_with_a_zero_budget_is_the_trailer_alone(tmp_path: Path) -> None:
    config = make_config(tmp_path, "patterns =\n    disk\n")
    watch = config.watches[0]
    report = scan(FILE, numbered(["disk", "disk"], FILE), watch, context=1, cap=5)
    assert render([report], 0) == "... and 2 more matching line(s)" + NL


# -- the mutation reviewer's pins: each names the mutation it catches ------------------------


# -- headers: the medium priority


def test_medium_priority_maps_to_x_priority_3_and_importance_normal(tmp_path: Path) -> None:
    config = make_config(tmp_path, "priority = medium\npatterns =\n    disk\n")
    watch = config.watches[0]
    report = scan(FILE, numbered(["disk"], FILE), watch, context=0, cap=watch.max_lines)
    mail = compose(watch, [report], sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    assert (mail.message["X-Logalert-Priority"], mail.message["X-Priority"],
            mail.message["Importance"]) == ("medium", "3", "normal")
    assert mail.priority == "medium" and "Priority:   medium" in body_of(mail.message)


# -- the mail's priority


def test_the_mails_priority_is_the_highest_across_files_and_beyond_the_cap(
        tmp_path: Path) -> None:
    # the [high] match is in the SECOND file and beyond max_lines, so it is neither the first
    # report's priority nor among the stored matches: the tracked report.priority carries it
    config = make_config(tmp_path, "patterns =\n    disk\n    [high] LAST\nmax_lines = 1\n")
    watch = config.watches[0]
    first = scan(FILE, numbered(["disk a"], FILE), watch, context=0, cap=watch.max_lines)
    second = scan(FILE2, numbered(["disk b", "LAST disk"], FILE2), watch, context=0,
                  cap=watch.max_lines)
    assert first.priority is None and [m.priority for m in second.matches] == [None]
    mail = compose(watch, [first, second], sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    assert mail.priority == "high"
    assert (mail.message["X-Logalert-Priority"], mail.message["X-Priority"]) == ("high", "1")
    assert "Priority:   high" in body_of(mail.message)


# -- compose honours max_lines; the summary counts every match


def test_compose_renders_at_most_max_lines_and_the_summary_counts_every_match(
        tmp_path: Path) -> None:
    # the budget spans the files, so the second file's stored entries exceed what compose
    # may show: only render's max_lines (the watch's) stops it there, not the scan cap
    config = make_config(tmp_path, "patterns =\n    disk\nmax_lines = 2\n")
    watch = config.watches[0]
    first = scan(FILE, numbered(["disk 1"], FILE), watch, context=0, cap=watch.max_lines)
    second = scan(FILE2, numbered(["disk 2", "disk 3", "disk 4"], FILE2), watch, context=0,
                  cap=watch.max_lines)
    assert len(second.matches) == 2 and second.matched == 3
    for attach in (False, True):
        mail = compose(watch, [first, second], sender=SENDER, settings=config.settings,
                       now=NOW, host="router1", attach=attach)
        body = body_of(mail.message)
        assert ("Files:      /var/log/router.log -- 1 match(es) in 1 line(s) read" + NL
                + "            /var/log/router2.log -- 3 match(es) in 3 line(s) read" + NL
                ) in body
        text = (str(next(iter(mail.message.iter_attachments())).get_content()) if attach
                else body)
        assert text.endswith("==> /var/log/router2.log <==" + NL + "1: disk 2" + NL
                             + "... and 2 more matching line(s)" + NL)
        assert mail.matched == 4 and mail.subject.endswith("-- 4 match(es)")


# -- the after-window of the last budgeted match, when the stored entries go on
#


def test_the_last_budgeted_matchs_after_window_is_exactly_context_lines_long(
        tmp_path: Path) -> None:
    # the budget spans files: the second file's stored entries hold a match beyond it, so
    # render (not the scan cap) decides where the report stops
    config = make_config(tmp_path, "patterns =\n    disk\nmax_lines = 2\n")
    watch = config.watches[0]
    first = scan(FILE, numbered(["disk 1"], FILE), watch, context=1, cap=2)
    touching = scan(FILE2, numbered(["disk 2", "a", "b", "disk 3"], FILE2), watch, context=1,
                    cap=2)
    assert [e.text for e in touching.entries] == ["disk 2", "a", "b", "disk 3"]
    assert render([first, touching], 2) == (
        "==> /var/log/router.log <==" + NL + "1: disk 1" + NL + NL
        + "==> /var/log/router2.log <==" + NL + "1: disk 2" + NL + "2- a" + NL
        + "... and 1 more matching line(s)" + NL)  # b is disk 3's before-context: unseen
    overlapping = scan(FILE2, numbered(["disk 2", "a", "disk 3"], FILE2), watch, context=1,
                       cap=2)
    assert render([first, overlapping], 2) == (
        "==> /var/log/router.log <==" + NL + "1: disk 1" + NL + NL
        + "==> /var/log/router2.log <==" + NL + "1: disk 2" + NL + "2- a" + NL
        + "... and 1 more matching line(s)" + NL)  # disk 3 is over the budget, never shown


# -- a gap inside the last after-window


def test_a_gap_inside_the_last_after_window_is_a_skipped_line_not_the_end(tmp_path: Path) -> None:
    # a NUL-only line the reader skipped (line 3) inside the after-window of the last budgeted
    # match: the scan stores "c" as after-context past the gap and the window runs on -- the
    # reviewer's mutation showed the report stopping at the gap and losing c
    config = make_config(tmp_path, "patterns =\n    MATCH\nmax_lines = 1\ncontext = 2\n")
    watch = config.watches[0]
    stream = [Line(text, False, n, FILE) for n, text in (
        (1, "MATCH 1"), (2, "a"), (4, "c"), (5, "d"), (6, "MATCH 2"))]
    report = scan(FILE, stream, watch, context=2, cap=1)
    assert [e.text for e in report.entries] == ["MATCH 1", "a", "", "c"]
    assert render([report], 1) == ("==> /var/log/router.log <==" + NL + "1: MATCH 1" + NL
                                   + "2- a" + NL + "--" + NL + "4- c" + NL
                                   + "... and 1 more matching line(s)" + NL)


# -- the To header on the wire


def test_to_on_the_wire_separates_recipients_with_comma_space(tmp_path: Path) -> None:
    # msg["To"] normalises the separator, so only the wire line proves the join
    config = make_config(tmp_path, "patterns =\n    disk\n", to="noc@example.net, ops@example.net")
    watch = config.watches[0]
    report = scan(FILE, numbered(["disk"], FILE), watch, context=0, cap=watch.max_lines)
    mail = compose(watch, [report], sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    assert wire_line(mail, b"To:") == b"To: noc@example.net, ops@example.net"


# -- From and To are sanitised like every header (from_unsanitised, to_unsanitised); the loader
#    already forbids these shapes in an address, so this pins the defence only -----------------


def test_from_and_to_are_header_clean_even_from_a_hand_built_watch(tmp_path: Path) -> None:
    config = make_config(tmp_path, "patterns =\n    disk\n")
    watch = with_fields(config.watches[0], to=("noc@example.net" + TAB, TAB + "ops@example.net"))
    report = scan(FILE, numbered(["disk"], FILE), watch, context=0, cap=watch.max_lines)
    mail = compose(watch, [report], sender=TAB + SENDER + " ", settings=config.settings, now=NOW,
                   host="router1")
    head = mail.flatten("sendmail").split(b"\n\n")[0]
    assert TAB.encode() not in head
    assert wire_line(mail, b"From:") == b"From: " + SENDER.encode()
    assert wire_line(mail, b"To:") == b"To: noc@example.net, ops@example.net"


# -- the summary's file row


def test_the_summarys_file_row_is_one_header_clean_line(tmp_path: Path) -> None:
    config = make_config(tmp_path, "patterns =\n    disk\n")
    watch = config.watches[0]
    odd_file = "/var/log/rou" + TAB + "ter.log "
    report = scan(odd_file, numbered(["disk"], odd_file), watch, context=0, cap=1)
    mail = compose(watch, [report], sender=SENDER, settings=config.settings, now=NOW,
                   host="router1")
    assert "Files:      /var/log/rou ter.log -- 1 match(es)" in body_of(mail.message)


# -- the attachment name


def test_a_run_of_unsafe_characters_in_the_section_name_is_one_underscore() -> None:
    assert attachment_name("a  b", NOW) == f"a_b-{STAMP}.txt"
    assert attachment_name("!!!", NOW) == f"_-{STAMP}.txt"


# -- match.py: no dangling gap after the cap


def test_the_cap_stores_no_dangling_gap(tmp_path: Path) -> None:
    # a jump before a match beyond the cap: the gap belongs to a match that is not stored
    config = make_config(tmp_path, "patterns =\n    MATCH\n")
    watch = config.watches[0]
    stream = [Line("MATCH 1", False, 1, FILE), Line("a", False, 2, FILE),
              Line("MATCH 2", False, 9, FILE)]
    report = scan(FILE, stream, watch, context=1, cap=1)
    assert [e.text for e in report.entries] == ["MATCH 1", "a"]
    assert GAP not in report.entries and report.matched == 2
