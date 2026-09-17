"""docs/USAGE.md (issue #15) cannot drift from the package: the quoted example config IS
``logalert --example-config``, every command-line option and every configuration key is in
the document, and the exit-code table names all five codes. Both platforms."""

import re
import smtplib
from pathlib import Path

from logalert.__main__ import build_parser
from logalert.config import GLOBAL_KEYS, WATCH_KEYS, example_config

DOC = Path(__file__).resolve().parents[1] / "docs" / "USAGE.md"
NL = chr(10)


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_quoted_example_is_the_packages_example_verbatim() -> None:
    text = _text()
    start = text.index("### The example")
    match = re.search(r"```ini" + NL + "(.*?)```", text[start:], re.S)
    assert match, "no ```ini block under 'The example'"
    assert match.group(1) == example_config()


def _heads(section: str) -> list[str]:
    """The first cell of every table row in the section: what the table names."""
    return [row.split("|")[1] for row in section.splitlines() if row.startswith("| `")]


def test_every_command_line_option_is_documented() -> None:
    text = _text()
    heads = _heads(text[text.index("## Options"):text.index("## Exit codes")])
    for action in build_parser()._actions:
        for option in action.option_strings:
            # the option, then a space (its metavar) or the closing backtick: so `--from`
            # is not satisfied by `--from-start`, nor `--attach` by `--attachment`
            pattern = "`" + re.escape(option) + "[ `]"
            assert any(re.search(pattern, head) for head in heads), (
                f"{option} is not in the options table")


def test_every_configuration_key_is_in_the_reference() -> None:
    text = _text()
    heads = _heads(text[text.index("### The `[logalert]` section"):text.index("### The example")])
    for key in sorted(GLOBAL_KEYS | WATCH_KEYS):
        assert any(f"`{key}`" in head for head in heads), (
            f"{key} is not in the configuration reference")


def test_the_exit_code_table_names_every_code() -> None:
    text = _text()
    table = text[text.index("## Exit codes"):text.index("## Configuration reference")]
    for code in ("0", "1", "2", "130", "143"):  # 143: SIGTERM, issue #33
        assert f"| {code} |" in table, f"exit {code} is not in the table"


def test_the_mail_section_names_the_report_cap_and_the_escapes_from_a_refused_message() -> None:
    """Issue #25: a message a relay refuses for its size was rebuilt and refused every run,
    and the document never said so nor named a way out. The cap and the three escapes are
    what an operator needs at 3 a.m.; a dropped sentence reddens here."""
    text = _text()
    reference = text[text.index("## Configuration reference"):text.index("## Position tracking")]
    assert "cut at 1 MiB" in reference  # the max_lines row
    mail = text[text.index("## Mail"):text.index("## Logging")]
    assert "refused for the same reason every" in mail
    for escape in ("`max_lines`", "`exclude_regex`", "`--reset-state <file>`"):
        assert escape in mail, escape
    table = text[text.index("## Troubleshooting"):]
    assert "refusal in cron's mail every run" in table


def test_the_reference_says_lines_are_decoded_as_utf_8_and_the_table_has_the_row() -> None:
    """Issue #57: the decoding rule was stated for the display only; the pattern and
    exclude rows, the shipped example's comment and the troubleshooting table say it now."""
    text = _text()
    reference = text[text.index("### A watch section"):text.index("### The example")]
    rows = {row.split("|")[1].strip(): row for row in reference.splitlines()
            if row.startswith("| `")}
    assert "decoded as UTF-8" in rows["`patterns`"] and "UTF-16" in rows["`patterns`"]
    excludes = rows["`exclude`, `iexclude`, `exclude_regex`, `iexclude_regex`"]
    assert "matched against the same decoded line" in excludes
    comment = example_config().replace(NL + "# ", " ")  # the shipped comment, unwrapped
    assert "Lines are decoded as UTF-8, so non-ASCII text in a pattern" in comment
    table = text[text.index("## Troubleshooting"):]
    row = next(r for r in table.splitlines() if "umlaut" in r)
    assert "UTF-16" in row and "U+FEFF" in row and "--check-config" in row


def test_the_mail_section_says_smtp_auth_is_unsupported_and_names_the_way_through() -> None:
    """Issue #58: the deferral of SMTP AUTH (#11, #19) was on the record and not in the
    operator's contract; a relay's 530 named no way forward. The way is an MTA that can
    log in, as the sendmail transport, and the row quotes the transport's own words."""
    text = _text()
    mail = text[text.index("## Mail"):text.index("## Logging")]
    paragraph = mail[mail.index("**SMTP AUTH is not supported.**"):mail.index("**`--test-mail")]
    for name in ("Postfix", "Exim", "`msmtp-mta`", "`dma`", "`transport = auto`"):
        assert name in paragraph, name
    assert "`530`" in paragraph and "`554`" in paragraph
    reference = text[text.index("## Configuration reference"):text.index("## Position tracking")]
    assert "| `smtp_host` |" in reference and "No login" in reference
    assert "exim's" in reference  # the sendmail_path row
    table = text[text.index("## Troubleshooting"):]
    row = next(r for r in table.splitlines() if "Authentication required" in r)
    assert "every recipient refused" in row and "msmtp-mta" in row and "Exim" in row
    assert "exim4-daemon-light" in table  # the no-MTA row names Debian's default
    package = DOC.parents[1] / "logalert"
    source = (package / "transport.py").read_text(encoding="utf-8")
    assert "every recipient refused: " in source  # the row quotes the transport's text
    assert "not delivered via smtp" in row  # __main__'s prefix for --test-mail
    assert "not delivered via " in (package / "__main__.py").read_text(encoding="utf-8")
    assert "SMTPSenderRefused" in row and hasattr(smtplib, "SMTPSenderRefused")


def test_the_logging_section_names_the_send_timeout_and_the_table_has_the_row() -> None:
    """Issue #35: the document promises the number the code carries (a constant of None
    or 0 would restore the hang or a spurious fallback and pass the socket tests, which
    compare against the same constant)."""
    from logalert.activity import SYSLOG_SEND_TIMEOUT
    text = _text()
    logging_section = text[text.index("## Logging"):text.index("## Troubleshooting")]
    assert f"is given {SYSLOG_SEND_TIMEOUT:g} s per send, twice" in logging_section
    assert "`TimeoutError: timed out`" in logging_section
    table = text[text.index("## Troubleshooting"):]
    row = next(r for r in table.splitlines() if "TimeoutError: timed out" in r)
    assert f"waited {SYSLOG_SEND_TIMEOUT:g} s twice" in row and "systemd-journald" in row


def test_the_position_section_and_the_table_carry_the_full_disk_refusal() -> None:
    """Issue #30: the opening save and its refusal are documented in the program's own words;
    a dropped sentence or row reddens here."""
    text = _text()
    section = text[text.index("## Position tracking"):text.index("## Globs")].replace(NL, " ")
    assert "saved once at the start of every run but a dry run" in section
    assert "marks the `lock` file beside the state with `unsaved <bytes>`" in section  # #70
    table = text[text.index("## Troubleshooting"):]
    rows = [r for r in table.splitlines() if "No space left on device" in r]
    assert len(rows) == 2  # the full disk (issue #30) and the band's marker (issue #70)
    assert "nothing was sent" in rows[0] and "Free space" in rows[0]
    assert "Disk quota exceeded" in rows[0]
    assert "the last run sent mail it could not record" in rows[1] and "once" in rows[1]
    twice = next(r for r in table.splitlines() if r.startswith("| The same lines arrive twice"))
    assert "refused before any mail" in twice and "marks the `lock` file" in twice
    package = DOC.parents[1] / "logalert"
    source = (package / "run.py").read_text(encoding="utf-8")
    assert "nothing was sent, because a run that " in source  # the line breaks there
    assert "cannot save its position would send everything again next time" in source
    assert "cannot write (" in (package / "state.py").read_text(encoding="utf-8")
    assert "the last run sent mail it could not " in source  # the #70 refusal, breaks there


def test_the_position_section_says_a_first_sight_keeps_its_place_on_a_failed_delivery() -> None:
    """Issue #31: the sentence held for files with a saved position only."""
    text = _text()
    section = text[text.index("## Position tracking"):text.index("## Globs")]
    assert "file read for the first time that run is kept too" in section
    assert "position where that" + NL + "read began" in section
    assert "otherwise the end it started at" in section


def test_the_causes_paragraph_quotes_every_cause_in_the_logs_own_words() -> None:
    """Issue #38 (and #41 item 6): the phrases are documented "in the log's own words" and
    were unpinned; every cause the code can name is quoted in the paragraph, wrapped or not."""
    from logalert.rotation import (
        CAUSE_NO_FIRST_LINE,
        CAUSE_SECOND_ROTATION,
        CAUSE_UNREADABLE,
        CAUSE_UNSEARCHABLE,
        CAUSES,
        TAIL_COPIES,
        TAIL_LIVE_ONLY,
    )
    text = _text()
    paragraph = text[text.index('**The "no rotated copy" warning.**'):
                     text.index("**Forgetting a position.**")].replace(NL, " ")
    for cause in (*CAUSES, CAUSE_SECOND_ROTATION, CAUSE_UNREADABLE, CAUSE_UNSEARCHABLE,
                  CAUSE_NO_FIRST_LINE):
        assert "`" + cause + "`" in paragraph, cause
    assert "a permission kept the rotated copies out of reach" in paragraph  # the item
    for tail in (TAIL_COPIES, TAIL_LIVE_ONLY):  # the two ways the line ends (issue #64)
        assert tail in paragraph, tail
    for shape in ("could not be read (Permission denied); skipped",
                  "cannot list /var/log/old while looking for rotated copies (Permission denied)",
                  "entries of /var/log/old while looking for rotated copies (Permission denied); "
                  "is the directory searchable?"):
        assert shape in paragraph, shape


def test_the_rotation_during_the_run_paragraph_quotes_the_two_stop_lines() -> None:
    """Issues #32, #66 and #67: the three lines a run writes when a rotation lands under it
    are quoted in the paragraph, in the code's words."""
    text = _text().replace(NL, " ")
    for shape in ("was renamed or removed under us (a rotation during the run); stopping "
                  "here, the next run resumes after",
                  "truncated under us during the read (a copytruncate during the run?); "
                  "stopping at offset",
                  "the live file was rotated and compressed during the run (router.log.1.gz); "
                  "its lines were read from there"):
        assert shape in text, shape
