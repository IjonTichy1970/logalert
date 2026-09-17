"""The config loader: the format rules and every row of the validation set (issue #6).

Paths in these configs are built from tmp_path, never a POSIX literal: os.path.isabs('/x') is
True on Windows 3.12 and documented False on 3.13+, so a literal would flip with the dev venv.
Non-ASCII test data is built from code points (the gated-Python ASCII rule).
"""

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from logalert.__main__ import main
from logalert.config import (
    DEFAULT_MAX_LINES,
    DEFAULT_SENDMAIL_PATH,
    DEFAULT_STATE_FILE,
    GLOBAL_KEYS,
    WATCH_KEYS,
    ConfigError,
    describe,
    example_config,
    from_warning,
    is_address,
    load_config,
)

E_ACUTE = chr(0xE9)  # spelled from the code point for the ASCII gate; do not "simplify"


def write(tmp_path: Path, text: str, name: str = "logalert.conf") -> str:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return str(path)


def watch(tmp_path: Path, extra: str = "", *, files: str | None = None) -> str:
    """A minimal valid watch section, plus `extra` lines inside it."""
    log = files if files is not None else (tmp_path / "router.log").as_posix()
    return (
        "[router-disk]\n"
        "subject = Router disk failure\n"
        "to = noc@example.net\n"
        f"files = {log}\n"
        "patterns =\n"
        "    disk failure\n"
        "    Requesting reboot\n"
        f"{extra}"
    )


def error(tmp_path: Path, text: str) -> str:
    with pytest.raises(ConfigError) as exc:
        load_config(write(tmp_path, text))
    return str(exc.value)


# -- the shipped example -----------------------------------------------------------------


def test_example_config_reparses_clean(tmp_path: Path) -> None:
    text = example_config().replace("/var/log/router.log", (tmp_path / "router.log").as_posix())
    config = load_config(write(tmp_path, text))
    assert [w.name for w in config.watches] == ["router-disk"]
    assert config.warnings == ()
    assert config.settings.transport == "auto"
    out = describe(config)
    assert "[router-disk] subject: Router disk failure" in out
    assert "priority: off" in out


def test_example_config_is_ascii_and_documents_every_key() -> None:
    """Every key of the schema appears as a KEY LINE (live or commented), not just in prose."""
    text = example_config()
    assert text.isascii()
    for key in sorted(WATCH_KEYS | GLOBAL_KEYS):
        assert re.search(rf"^#?{key} *=", text, re.M), key


def test_example_config_parses_when_every_line_is_uncommented(tmp_path: Path) -> None:
    """Every commented key, section and list line in the example is a valid line once
    uncommented (the illustration blocks are indented under '#     ' and stay prose)."""
    substitutions = {
        "/var/lib/logalert/state.json": tmp_path / "state.json",
        "/usr/sbin/sendmail": tmp_path / "sendmail",
        "/var/log/router.log": tmp_path / "router.log",
        "/var/log/archive": tmp_path / "archive",
        "/var/log/firewall.log": tmp_path / "firewall.log",
    }
    lines = []
    for line in example_config().splitlines():
        if re.match(r"^#(\[|[a-z_]+ =|    \S)", line):
            line = line[1:]
        lines.append(line)
    text = "\n".join(lines) + "\n"
    for old, new in substitutions.items():
        text = text.replace(old, new.as_posix())
    config = load_config(write(tmp_path, text))
    assert config.warnings == ()
    assert [w.name for w in config.watches] == ["router-disk", "firewall-denies"]
    router, fw = config.watches
    assert router.priority == "medium" and router.report == "inline" and router.context == 0
    assert router.archive_dir == (tmp_path / "archive").as_posix()
    assert [p.text for p in router.patterns if p.regex][1] == "\\#\\d+ .*failed"
    assert fw.report == "attachment" and fw.max_lines == 1000 and len(fw.files) == 1  # #44
    assert fw.patterns[0].ignore_case and fw.excludes[0].regex
    assert config.settings.from_address == "logalert@example.net"
    assert config.settings.smtp_host == "mail.example.net"


# -- a valid watch, parsed ---------------------------------------------------------------


def test_minimal_watch_and_defaults(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, watch(tmp_path)))
    (w,) = config.watches
    assert w.name == "router-disk"
    assert w.subject == "Router disk failure"
    assert w.to == ("noc@example.net",)
    assert w.files == ((tmp_path / "router.log").as_posix(),)
    assert [(p.text, p.regex, p.ignore_case, p.priority) for p in w.patterns] == [
        ("disk failure", False, False, None),
        ("Requesting reboot", False, False, None),
    ]
    assert w.excludes == ()
    assert w.priority is None  # the priority system is off unless configured
    assert w.report == "inline"
    assert w.context is None  # from -c
    assert w.max_lines == DEFAULT_MAX_LINES
    assert w.start == "end"
    assert w.archive_dir is None
    assert config.settings.state_file == DEFAULT_STATE_FILE
    assert config.settings.sendmail_path == DEFAULT_SENDMAIL_PATH
    assert config.settings.from_address is None


def test_recipients_one_per_line_or_comma_separated(tmp_path: Path) -> None:
    text = watch(tmp_path).replace(
        "to = noc@example.net\n",
        "to =\n    noc@example.net, ops@example.net\n    root@router1.example.net\n",
    )
    (w,) = load_config(write(tmp_path, text)).watches
    assert w.to == ("noc@example.net", "ops@example.net", "root@router1.example.net")


def test_four_pattern_keys_and_priority_tags(tmp_path: Path) -> None:
    extra = (
        "ipatterns =\n    link down\n"
        "regex =\n    ^Sep \\d+ .* kernel: .*fail\n"
        "iregex =\n    [high] error|panic\n"
        "priority = low\n"
    )
    text = watch(tmp_path, extra).replace(
        "    disk failure\n",
        "    [high] disk failure\n    [medium] something\n    [oops] literal\n",
    )
    (w,) = load_config(write(tmp_path, text)).watches
    by_text = {p.text: p for p in w.patterns}
    assert by_text["disk failure"].priority == "high"
    assert by_text["something"].priority == "medium"
    assert by_text["[oops] literal"].priority is None  # not a tag: part of the literal
    assert by_text["Requesting reboot"].priority is None  # section priority applies at match time
    assert by_text["link down"].ignore_case and not by_text["link down"].regex
    rx = by_text["^Sep \\d+ .* kernel: .*fail"]
    assert rx.regex and not rx.ignore_case and rx.compiled is not None
    irx = by_text["error|panic"]
    assert irx.regex and irx.ignore_case and irx.priority == "high"
    assert irx.compiled is not None and irx.compiled.flags & re.IGNORECASE
    assert w.priority == "low"


def test_exclude_keys_four_shapes_and_no_tags(tmp_path: Path) -> None:
    extra = (
        "exclude =\n    da9 (known bad)\n"
        "iexclude =\n    test message\n"
        "exclude_regex =\n    kernel: \\[debug\\]\n"
        "iexclude_regex =\n    heartbeat\n    [high] not a tag here\n"
    )
    (w,) = load_config(write(tmp_path, watch(tmp_path, extra))).watches
    shapes = [(p.text, p.regex, p.ignore_case) for p in w.excludes]
    assert shapes == [
        ("da9 (known bad)", False, False),
        ("test message", False, True),
        ("kernel: \\[debug\\]", True, False),
        ("heartbeat", True, True),
        ("[high] not a tag here", True, True),
    ]
    assert all(p.priority is None for p in w.excludes)


def test_percent_in_a_literal_pattern_is_literal(tmp_path: Path) -> None:
    text = watch(tmp_path).replace("    disk failure\n", "    disk 100% full\n")
    (w,) = load_config(write(tmp_path, text)).watches
    assert w.patterns[0].text == "disk 100% full"


def test_comment_leading_continuation_line_is_dropped_by_the_parser(tmp_path: Path) -> None:
    """The documented limitation: configparser treats an indented '#'- or ';'-line as a
    comment, so it never reaches the loader. The escape form in a regex does."""
    text = watch(tmp_path).replace(
        "    disk failure\n",
        "    disk failure\n    # not a pattern\n    ; nor this\n",
    ) + "regex =\n    \\#\\d+ failed\n"
    (w,) = load_config(write(tmp_path, text)).watches
    assert [p.text for p in w.patterns] == ["disk failure", "Requesting reboot", "\\#\\d+ failed"]


def test_odd_line_separators_inside_a_pattern_stay_in_the_pattern(tmp_path: Path) -> None:
    """splitlines() would split on a form feed or NEL; configparser gave us one line."""
    form_feed = chr(0x0C)
    text = watch(tmp_path).replace("    disk failure\n", "    disk" + form_feed + "failure\n")
    (w,) = load_config(write(tmp_path, text)).watches
    assert w.patterns[0].text == "disk" + form_feed + "failure"


def test_priority_tag_text_is_stripped(tmp_path: Path) -> None:
    text = watch(tmp_path).replace("    disk failure\n", "    [high]   disk failure  \n")
    (w,) = load_config(write(tmp_path, text)).watches
    assert (w.patterns[0].text, w.patterns[0].priority) == ("disk failure", "high")


def test_watch_options(tmp_path: Path) -> None:
    archive = (tmp_path / "archive").as_posix()
    extra = (
        "priority = HIGH\nreport = attachment\ncontext = 4\nmax_lines = 50\n"
        f"start = beginning\narchive_dir = {archive}\n"
    )
    (w,) = load_config(write(tmp_path, watch(tmp_path, extra))).watches
    assert (w.priority, w.report, w.context, w.max_lines, w.start, w.archive_dir) == (
        "high", "attachment", 4, 50, "beginning", archive
    )


def test_compressed_files_are_legitimate_inputs(tmp_path: Path) -> None:
    gz = (tmp_path / "router.log.0.gz").as_posix()
    text = watch(tmp_path, files=f"\n    {(tmp_path / 'router.log').as_posix()}\n    {gz}")
    (w,) = load_config(write(tmp_path, text)).watches
    assert w.files[1] == gz


# -- the validation set ------------------------------------------------------------------


@pytest.mark.parametrize("key", ["subject", "to", "files"])
def test_missing_required_key(tmp_path: Path, key: str) -> None:
    text = "\n".join(line for line in watch(tmp_path).splitlines() if not line.startswith(key))
    msg = error(tmp_path, text + "\n")
    assert msg == f"[router-disk] {key}: required"


@pytest.mark.parametrize("key", ["subject", "to", "files"])
def test_present_but_empty_required_value(tmp_path: Path, key: str) -> None:
    text = "\n".join(
        f"{key} =" if line.startswith(key) else line for line in watch(tmp_path).splitlines()
    )
    assert error(tmp_path, text + "\n") == f"[router-disk] {key}: is empty"


@pytest.mark.parametrize("key", ["archive_dir", "smtp_host", "log", "state_file"])
def test_scalar_keys_refuse_a_continuation_line(tmp_path: Path, key: str) -> None:
    value = (tmp_path / "x").as_posix()
    if key in ("archive_dir",):
        text = watch(tmp_path, f"{key} = {value}\n    {value}\n")
        where = "[router-disk]"
    else:
        text = f"[logalert]\n{key} = {value}\n    more\n" + watch(tmp_path)
        where = "[logalert]"
    assert error(tmp_path, text) == f"{where} {key}: must be a single line"


def test_default_section_is_refused(tmp_path: Path) -> None:
    msg = error(tmp_path, "[DEFAULT]\nto = noc@example.net\n" + watch(tmp_path))
    assert "[DEFAULT] is not supported" in msg


def test_unparseable_line_reports_its_number(tmp_path: Path) -> None:
    path = write(tmp_path, watch(tmp_path, "context 4\n"))
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert str(exc.value).startswith(f"{path}:8: not 'key = value'")


def test_no_pattern_key_at_all(tmp_path: Path) -> None:
    text = watch(tmp_path).split("patterns =")[0]
    msg = error(tmp_path, text)
    assert msg.startswith("[router-disk]: at least one of patterns, ipatterns, regex, iregex")


def test_empty_list_and_empty_entry(tmp_path: Path) -> None:
    assert error(tmp_path, watch(tmp_path).replace("    disk failure\n    Requesting reboot\n", "")
                 ) == "[router-disk] patterns: is empty"
    # configparser keeps a blank line inside an indented value (empty_lines_in_values); the
    # loader must refuse it rather than let an empty pattern match every line
    blank_inside = watch(tmp_path).replace("    disk failure\n", "    disk failure\n\n")
    assert error(tmp_path, blank_inside) == (
        "[router-disk] patterns: has an empty entry (a blank line or a stray comma)")
    stray_comma = watch(tmp_path).replace("to = noc@example.net", "to = noc@example.net,")
    assert error(tmp_path, stray_comma) == (
        "[router-disk] to: has an empty entry (a blank line or a stray comma)")


def test_empty_pattern_entry_never_matches_everything(tmp_path: Path) -> None:
    text = watch(tmp_path).replace("    disk failure\n", "    disk failure\n     \n")
    assert error(tmp_path, text) == (
        "[router-disk] patterns: has an empty entry (a blank line or a stray comma)")


def test_relative_paths(tmp_path: Path) -> None:
    assert "is not an absolute path" in error(tmp_path, watch(tmp_path, files="var/log/x.log"))
    assert "archive_dir" in error(tmp_path, watch(tmp_path, "archive_dir = archive\n"))
    assert "[logalert] state_file" in error(
        tmp_path, "[logalert]\nstate_file = state.json\n" + watch(tmp_path))
    assert "[logalert] sendmail_path" in error(
        tmp_path, "[logalert]\nsendmail_path = sendmail\n" + watch(tmp_path))


@pytest.mark.parametrize("char", ["*", "?", "["])
def test_glob_characters_are_accepted_in_files_and_refused_elsewhere(
    tmp_path: Path, char: str
) -> None:
    """Issue #18: a ``files`` entry may be a glob, kept as written for the run to expand;
    every other path is one path."""
    glob = (tmp_path / f"router{char}.log").as_posix()
    (w,) = load_config(write(tmp_path, watch(tmp_path, files=glob))).watches
    assert w.files == (glob,) and w.include_archives is False
    archive = (tmp_path / f"arch{char}").as_posix()
    msg = error(tmp_path, watch(tmp_path, f"archive_dir = {archive}\n"))
    assert msg.startswith("[router-disk] archive_dir:") and "glob character" in msg
    msg = error(tmp_path, f"[logalert]\nstate_file = {archive}/state.json\n" + watch(tmp_path))
    assert msg.startswith("[logalert] state_file:") and "only files may be a glob" in msg
    msg = error(tmp_path, f"[logalert]\nsendmail_path = {archive}\n" + watch(tmp_path))
    assert msg.startswith("[logalert] sendmail_path:") and "glob character" in msg
    msg = error(tmp_path, f"[logalert]\nlog = file:{archive}/activity.log\n" + watch(tmp_path))
    assert msg == (f"[logalert] log: {archive + '/activity.log'!r} contains a glob character; "
                   f"only files may be a glob")


def test_recursive_glob_is_refused(tmp_path: Path) -> None:
    """``**`` would be one level to ``glob`` (measured) and the whole tree with the flag."""
    deep = (tmp_path / "**" / "router.log").as_posix()
    msg = error(tmp_path, watch(tmp_path, files=deep))
    assert msg == f"[router-disk] files: {deep!r}: ** is not supported; name the directories"


def test_a_files_entry_ending_in_a_separator_is_refused(tmp_path: Path) -> None:
    """``hosts/*/`` means directories to the shell; here it would read the files one level
    up, which the operator did not mean (measured in review)."""
    trailing = (tmp_path / "hosts" / "*").as_posix() + "/"
    msg = error(tmp_path, watch(tmp_path, files=trailing))
    assert msg == f"[router-disk] files: {trailing!r} ends in a separator; name the file"
    plain = (tmp_path / "router.log").as_posix() + "/"
    msg = error(tmp_path, watch(tmp_path, files=plain))
    assert msg.endswith("ends in a separator; name the file")


def test_include_archives_is_a_watch_boolean(tmp_path: Path) -> None:
    (w,) = load_config(write(tmp_path, watch(tmp_path, "include_archives = yes\n"))).watches
    assert w.include_archives is True
    msg = error(tmp_path, watch(tmp_path, "include_archives = sometimes\n"))
    assert msg == "[router-disk] include_archives: expected yes or no, got 'sometimes'"
    msg = error(tmp_path, "[logalert]\ninclude_archives = yes\n" + watch(tmp_path))
    assert msg.startswith("[logalert] include_archives: this is a watch key")


@pytest.mark.parametrize(
    "address, fragment",
    [
        ("noc", "not a bare local@domain"),
        ("a@b@c", "not a bare local@domain"),
        ("-oQ/tmp@example.net", "starts with '-'"),
        ("NOC <noc@example.net>", "not a bare local@domain"),
        ("<noc@example.net>", "not a bare local@domain"),
        ('"noc"@example.net', "not a bare local@domain"),
        ("noc @example.net", "not a bare local@domain"),
        ("noc@-example.net", "not a bare local@domain"),
        ("noc@example.net.", "not a bare local@domain"),
        ("no" + E_ACUTE + "@example.net", "not ASCII"),
    ],
)
def test_bad_addresses(tmp_path: Path, address: str, fragment: str) -> None:
    text = watch(tmp_path).replace("to = noc@example.net", "to = " + address)
    msg = error(tmp_path, text)
    assert msg.startswith("[router-disk] to:") and fragment in msg


def test_good_addresses(tmp_path: Path) -> None:
    for address in ("noc@example.net", "first.last+tag@sub.example.net", "root@router1",
                    "o'brien@example.net", "a=b@example.net"):
        text = watch(tmp_path).replace("to = noc@example.net", "to = " + address)
        assert load_config(write(tmp_path, text)).watches[0].to == (address,)


def test_keys_are_case_insensitive_and_values_stripped(tmp_path: Path) -> None:
    text = watch(tmp_path).replace(
        "subject = Router disk failure", "SUBJECT =   Router disk failure   ")
    (w,) = load_config(write(tmp_path, text)).watches
    assert w.subject == "Router disk failure"


def test_example_second_watch_parses_when_uncommented(tmp_path: Path) -> None:
    """Every commented-out line in the example must be a valid line once uncommented."""
    text = example_config().replace("/var/log/router.log", (tmp_path / "router.log").as_posix())
    cut = text.index("#[firewall-denies]")
    head, tail = text[:cut], text[cut:]
    uncommented = "\n".join(
        line[1:] if line.startswith("#") else line for line in tail.splitlines()
    )
    text = head + uncommented.replace("/var/log/firewall.log", (tmp_path / "fw.log").as_posix())
    config = load_config(write(tmp_path, text))
    assert [w.name for w in config.watches] == ["router-disk", "firewall-denies"]
    fw = config.watches[1]
    assert fw.report == "attachment" and fw.max_lines == 1000 and len(fw.files) == 1  # #44
    assert fw.patterns[0].ignore_case and fw.excludes[0].regex


def test_non_finite_timeout(tmp_path: Path) -> None:
    for value in ("nan", "inf", "-1"):
        msg = error(tmp_path, f"[logalert]\nmail_timeout = {value}\n" + watch(tmp_path))
        assert msg.startswith("[logalert] mail_timeout: must be a positive number")


def test_bad_from_address_in_globals(tmp_path: Path) -> None:
    msg = error(tmp_path, "[logalert]\nfrom = root\n" + watch(tmp_path))
    assert msg.startswith("[logalert] from:")


def test_multi_line_subject(tmp_path: Path) -> None:
    text = watch(tmp_path).replace(
        "subject = Router disk failure\n", "subject = Router\n    disk\n"
    )
    assert error(tmp_path, text) == "[router-disk] subject: must be a single line"


def test_uncompilable_regex_names_section_key_pattern_and_reason(tmp_path: Path) -> None:
    with pytest.raises(re.error) as compiled:
        re.compile("[unterminated")
    msg = error(tmp_path, watch(tmp_path, "iregex =\n    [unterminated\n"))
    assert msg == f"[router-disk] iregex: cannot compile '[unterminated': {compiled.value}"


def test_regex_that_overflows_the_compiler_is_a_config_error(tmp_path: Path) -> None:
    msg = error(tmp_path, watch(tmp_path, "regex =\n    a{99999999999}\n"))
    assert msg.startswith("[router-disk] regex: cannot compile 'a{99999999999}':")


def test_unknown_key_warns_and_unknown_value_errors(tmp_path: Path) -> None:
    config = load_config(write(tmp_path, watch(tmp_path, "colour = blue\n")))
    assert config.warnings == ("[router-disk] colour: unknown key, ignored",)
    config = load_config(write(tmp_path, "[logalert]\ncolour = blue\n" + watch(tmp_path)))
    assert config.warnings == ("[logalert] colour: unknown key, ignored",)
    for line, key in (("priority = urgent", "priority"), ("report = html", "report"),
                      ("start = middle", "start")):
        msg = error(tmp_path, watch(tmp_path, line + "\n"))
        assert msg.startswith(f"[router-disk] {key}: expected one of")
    msg = error(tmp_path, "[logalert]\ntransport = carrier-pigeon\n" + watch(tmp_path))
    assert msg.startswith("[logalert] transport: expected one of auto, sendmail, smtp")


def test_numbers_and_booleans(tmp_path: Path) -> None:
    assert "context: must be >= 0" in error(tmp_path, watch(tmp_path, "context = -1\n"))
    assert "max_lines: expected an integer" in error(
        tmp_path, watch(tmp_path, "max_lines = many\n"))
    assert "max_lines: must be >= 1" in error(tmp_path, watch(tmp_path, "max_lines = 0\n"))
    assert "smtp_port: must be between 1 and 65535" in error(
        tmp_path, "[logalert]\nsmtp_port = 70000\n" + watch(tmp_path))
    assert "mail_timeout: must be a positive number" in error(
        tmp_path, "[logalert]\nmail_timeout = 0\n" + watch(tmp_path))
    assert "smtp_starttls: expected yes or no" in error(
        tmp_path, "[logalert]\nsmtp_starttls = maybe\n" + watch(tmp_path))
    settings = load_config(write(
        tmp_path, "[logalert]\nsmtp_starttls = On\nsubject_suffix = 0\nmail_timeout = 2.5\n"
        + watch(tmp_path))).settings
    assert (settings.smtp_starttls, settings.subject_suffix, settings.mail_timeout) == (
        True, False, 2.5)


def test_duplicate_section_and_key_carry_line_numbers(tmp_path: Path) -> None:
    text = watch(tmp_path) + "\n" + watch(tmp_path)
    msg = error(tmp_path, text)
    assert "section [router-disk] appears twice" in msg
    assert re.search(r":\d+: ", msg), msg
    text = watch(tmp_path, "subject = again\n")
    msg = error(tmp_path, text)
    assert re.search(r":8: key 'subject' appears twice in \[router-disk\]$", msg), msg


def test_reserved_section_name_and_misplaced_keys(tmp_path: Path) -> None:
    msg = error(tmp_path, watch(tmp_path).replace("[router-disk]", "[LogAlert]"))
    assert msg.startswith("[LogAlert]: the name is reserved")
    msg = error(tmp_path, "[logalert]\nsubject = x\n" + watch(tmp_path))
    assert msg.startswith("[logalert] subject: this is a watch key")
    msg = error(tmp_path, watch(tmp_path, "smtp_host = mail.example.net\n"))
    assert msg.startswith("[router-disk] smtp_host: this is a [logalert] setting")


def test_smtp_transport_needs_a_host(tmp_path: Path) -> None:
    assert error(tmp_path, "[logalert]\ntransport = smtp\n" + watch(tmp_path)) == (
        "[logalert] smtp_host: required when transport = smtp")
    settings = load_config(write(
        tmp_path, "[logalert]\ntransport = SMTP\nsmtp_host = mail.example.net\n" + watch(tmp_path)
    )).settings
    assert (settings.transport, settings.smtp_host, settings.smtp_port) == (
        "smtp", "mail.example.net", 25)


def test_log_destinations(tmp_path: Path) -> None:
    logfile = (tmp_path / "logalert.log").as_posix()
    for value in ("syslog", "stderr", f"file:{logfile}", "udp:loghost.example.net:514"):
        text = f"[logalert]\nlog = {value}\n" + watch(tmp_path)
        assert load_config(write(tmp_path, text)).settings.log == value
    superscript_two = chr(0xB2)  # isdigit() is True, int() refuses it; from the code point
    for value in ("file:relative.log", "udp:loghost", "udp:loghost:99999", "journal",
                  "udp:loghost:" + superscript_two):
        text = f"[logalert]\nlog = {value}\n" + watch(tmp_path)
        assert "[logalert] log:" in error(tmp_path, text)


def test_file_level_errors(tmp_path: Path) -> None:
    missing = str(tmp_path / "absent.conf")
    with pytest.raises(ConfigError, match="config file not found"):
        load_config(missing)
    assert "must start with a section header" in error(tmp_path, "subject = x\n")
    assert "no watch sections" in error(tmp_path, "[logalert]\nlog = stderr\n")
    bad = tmp_path / "bad.conf"
    bad.write_bytes(b"[router-disk]\nsubject = caf\xff\n")
    with pytest.raises(ConfigError, match="not valid UTF-8"):
        load_config(str(bad))


def test_utf8_bom_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "bom.conf"
    path.write_bytes(b"\xef\xbb\xbf" + watch(tmp_path).encode("utf-8"))
    assert load_config(str(path)).watches[0].name == "router-disk"


# -- describe() and the CLI --------------------------------------------------------------


def test_describe_reports_transport_from_and_watches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "sendmail"
    fake.write_bytes(b"#!/bin/sh\n")
    fake.chmod(0o755)  # a no-op on Windows, real on POSIX
    text = (f"[logalert]\nsendmail_path = {fake.as_posix()}\nfrom = alerts@example.net\n"
            + watch(tmp_path, "priority = high\nreport = attachment\n"))
    out = describe(load_config(write(tmp_path, text)))
    assert f"transport: auto -> sendmail at {fake.as_posix()} (found, executable)" in out
    assert "from: alerts@example.net" in out and "warning:" not in out
    assert "[router-disk] priority: high; report: attachment" in out

    monkeypatch.setattr(socket, "gethostname", lambda: "router1.example.net")
    out = describe(load_config(write(tmp_path, watch(tmp_path))))
    assert "NOT FOUND -- install an MTA or set transport = smtp" in out
    assert re.search(r"^from: \S+@router1\.example\.net \(default: the user logalert runs as",
                     out, re.M), out
    assert "warning:" not in out


def test_describe_warns_about_a_from_address_that_will_not_travel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(socket, "gethostname", lambda: "router1")
    monkeypatch.setattr(socket, "getfqdn", lambda: "router1.localdomain")
    out = describe(load_config(write(tmp_path, watch(tmp_path))))
    assert re.search(r"^from: \S+@router1\.localdomain \(default", out, re.M), out
    assert "warning: From address" in out and "local-only domain" in out
    configured = describe(load_config(write(tmp_path, "[logalert]\nfrom = root@router1\n"
                                            + watch(tmp_path))))
    assert "from: root@router1" in configured and "has no domain part" in configured
    assert from_warning("noc@example.net") is None


def test_describe_reports_a_sendmail_that_is_not_executable(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    fake = tmp_path / "sendmail"
    fake.write_bytes(b"#!/bin/sh\n")
    fake.chmod(0o644)
    text = f"[logalert]\ntransport = sendmail\nsendmail_path = {fake.as_posix()}\n"
    out = describe(load_config(write(tmp_path, text + watch(tmp_path))))
    assert f"transport: sendmail at {fake.as_posix()} (found but NOT EXECUTABLE" in out


def usable(tmp_path: Path, text: str) -> str:
    """A config whose transport can be chosen: the interpreter stands in for sendmail
    (it exists and is executable on both platforms); since #11 --check-config exits 2
    when transport = auto finds nothing."""
    return f"[logalert]\nsendmail_path = {Path(sys.executable).as_posix()}\n" + text


def test_cli_check_config_good_and_bad(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = write(tmp_path, usable(tmp_path, watch(tmp_path)))
    assert main(["--check-config", "-f", good]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("config: ") and out.err == ""
    bad = write(tmp_path, watch(tmp_path).replace("to = noc@example.net", "to = noc"), "bad.conf")
    assert main(["--check-config", "-f", bad]) == 2
    out = capsys.readouterr()
    assert out.out == "" and out.err.startswith("logalert: [router-disk] to:")


def test_cli_example_config_and_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--example-config"]) == 0
    assert capsys.readouterr().out == example_config()
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    help_text = capsys.readouterr().out
    for option in ("--check-config", "--example-config", "-f PATH", "--version"):
        assert option in help_text


def test_module_run_check_config(tmp_path: Path) -> None:
    good = write(tmp_path, usable(tmp_path, watch(tmp_path)))
    result = subprocess.run(
        [sys.executable, "-m", "logalert", "--check-config", "-f", good],
        capture_output=True, encoding="utf-8", errors="replace", check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "[router-disk] subject: Router disk failure" in result.stdout


# -- is_address (#10): the loader's rule as a boolean, for compose and --from -----------------


@pytest.mark.parametrize("value, ok", [
    ("noc@example.net", True),
    ("a+b@example.net", True),
    ("Log Alert <noc@example.net>", False),
    ("-noc@example.net", False),
    ("noc@example.net" + chr(10), False),  # fullmatch: a trailing newline is not a match
    ("n" + chr(0xE9) + "c@example.net", False),
    ("noc@", False),
    ("nobody", False),
])
def test_is_address_is_the_loaders_rule(value: str, ok: bool) -> None:
    assert is_address(value) is ok


# -- issue #44: a listed rotated copy of another listed entry is a --check-config warning ----


@pytest.mark.parametrize("copy, warned", [
    ("router.log.0.gz", True), ("router.log.1", True), ("router.log-20260917", True),
    ("router.log.2026-09-17.xz", True), ("router.log.gz", True),
    ("router.log2", False), ("router.log.bak", False), ("other.log", False),
])
def test_a_listed_rotated_copy_of_a_listed_log_is_a_check_config_warning(
        tmp_path: Path, copy: str, warned: bool) -> None:
    """The example's firewall watch listed firewall.log AND firewall.log.0.gz: read as a live
    log, the copy was mailed whole after every rotation, and its cursor confirmed by a full
    decompression every run. A warning, never a refusal: a static archive listed on purpose
    is legitimate (a .bak is a hand copy the catch-up does not chain, so it is not one)."""
    live = (tmp_path / "router.log").as_posix()
    other = (tmp_path / copy).as_posix()
    config = load_config(write(tmp_path, watch(tmp_path, files=f"{live}\n    {other}")))
    expected = (f"[router-disk] files: {other} is a rotated copy of {live}; the catch-up reads "
                f"the copies itself, and a listed copy is mailed whole after every rotation -- "
                f"list the live file only")
    assert config.warnings == ((expected,) if warned else ())
    assert len(config.watches[0].files) == 2  # the run is unchanged either way


def test_the_rotated_copy_warning_needs_the_same_directory_and_skips_globs(
        tmp_path: Path) -> None:
    (tmp_path / "old").mkdir()
    live = (tmp_path / "router.log").as_posix()
    elsewhere = (tmp_path / "old" / "router.log.1.gz").as_posix()
    config = load_config(write(tmp_path, watch(tmp_path, files=f"{live}\n    {elsewhere}")))
    assert config.warnings == ()  # another directory: archive_dir's business, not a listed copy
    globbed = (tmp_path / "router.log.*").as_posix()
    config = load_config(write(tmp_path, watch(tmp_path, files=f"{live}\n    {globbed}")))
    assert config.warnings == ()  # a glob leaves rotated copies out on its own (#18)
    host = (tmp_path / "192.0.2").as_posix()
    another = (tmp_path / "192.0.2.1").as_posix()
    config = load_config(write(tmp_path, watch(tmp_path, files=f"{host}\n    {another}")))
    assert config.warnings == ()  # a host's file, not a copy (archive_suffix's own rule)
    copy = (tmp_path / "router.log.1").as_posix()
    twice = f"{live}\n    {copy}\n    {live}\n    {copy}"
    config = load_config(write(tmp_path, watch(tmp_path, files=twice)))
    assert len(config.warnings) == 1  # an entry listed twice is read once and warned once


# -- issue #28: scan_timeout and the nested-quantifier warning ------------------------------


def test_scan_timeout_is_a_non_negative_integer_with_zero_off(tmp_path: Path) -> None:
    settings = load_config(write(tmp_path, watch(tmp_path))).settings
    assert settings.scan_timeout == 300
    settings = load_config(write(tmp_path, "[logalert]\nscan_timeout = 0\n"
                                 + watch(tmp_path))).settings
    assert settings.scan_timeout == 0
    assert "scan_timeout: must be >= 0" in error(
        tmp_path, "[logalert]\nscan_timeout = -1\n" + watch(tmp_path))
    assert "scan_timeout: expected an integer" in error(
        tmp_path, "[logalert]\nscan_timeout = soon\n" + watch(tmp_path))
    assert "scan_timeout" in GLOBAL_KEYS and "scan_timeout" in example_config()


def test_a_nested_quantifier_is_a_check_config_warning_never_a_refusal(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """(x+)+ and its kin backtrack without bound on a long line of ordinary words (measured:
    hours, holding the lock). The loader warns; --check-config prints it; the run does not,
    and the pattern is accepted -- the shapes that hang are not enumerable."""
    bs = chr(92)  # a backslash, from the code point: the regex escapes are test data
    good = write(tmp_path, watch(tmp_path, "regex = ^(a+)$\n    ^[a-z]+ failed$\n"
                                           f"    ({bs}d{{2}})+x\n    (a{bs}?)+\n    (a{bs}+)+\n"))
    assert load_config(good).warnings == ()  # a fixed count and escaped literals: benign
    ranged = write(tmp_path, watch(tmp_path, f"regex = ({bs}d{{2,}})+x\n    (x{{1,3}}?)+y\n"))
    assert len(load_config(ranged).warnings) == 2  # a range before the ) is the hazard
    hot = write(tmp_path, usable(tmp_path, watch(
        tmp_path, f"regex = ^({bs}w+{bs}s?)+failed$\nexclude_regex = (x*)*y\n")))
    config = load_config(hot)
    assert config.warnings == (
        f"[router-disk] regex: '^({bs}{bs}w+{bs}{bs}s?)+failed$': a quantified group ending "
        "in a quantifier can backtrack without bound on a long line; simplify it (see "
        "USAGE.md)",
        "[router-disk] exclude_regex: '(x*)*y': a quantified group ending in a quantifier "
        "can backtrack without bound on a long line; simplify it (see USAGE.md)",
    )
    assert main(["--check-config", "-f", hot]) == 0
    out = capsys.readouterr().out
    assert "warning: [router-disk] regex: " in out and "backtrack without bound" in out
    assert "scan_timeout: 300s" in out
    if sys.platform == "win32":
        assert "scan_timeout: 300s (not enforced on this platform)" in out
    off = write(tmp_path, usable(tmp_path, "scan_timeout = 0\n" + watch(tmp_path)))
    assert main(["--check-config", "-f", off]) == 0
    assert "scan_timeout: off;" in capsys.readouterr().out
