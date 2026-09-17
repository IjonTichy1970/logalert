"""docs/USAGE.md (issue #15) cannot drift from the package: the quoted example config IS
``logalert --example-config``, every command-line option and every configuration key is in
the document, and the exit-code table names all four codes. Both platforms."""

import re
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
    for code in ("0", "1", "2", "130"):
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
