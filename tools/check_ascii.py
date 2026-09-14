#!/usr/bin/env python3
r"""Refuse non-ASCII bytes in gated Python: service code and its test data stay ASCII.

## Why this exists

Two mechanical reasons, both measured on the Windows dev host:

  * bandit's text formatter writes through the console encoding and dies with
    UnicodeEncodeError on a cp1252 console when a finding's code context carries a non-ASCII
    character -- BEFORE reporting anything, so the finding is lost while CI (UTF-8 Linux) stays
    green and it looks like a local-machine problem. This rule is what keeps bandit able to
    report, which is why the stage runs first.
  * any script that prints a non-ASCII character to that console dies the same way, and because
    the earlier lines already printed it looks like a truncated result rather than a crashed
    reader. A findings printer went down on U+2192 exactly that way in this project's first day.

As a convention with no mechanism the source project's rule drifted to 23 lines across 14 files
in two days, including work that shipped green the same day; that measurement is why it is a
gate stage. ruff cannot express it: across all its rules RUF001/2/3 flag only confusables, and
an em dash, a star and a warning sign produce nothing.

## What it does

Checks BYTES, so a UTF-8 byte order mark and a stray Latin-1 byte both fail. Reports CODE
POINTS, never the character (printing the character IS the crash). Refuses an empty file list:
"0 files checked" and "0 problems found" print almost the same and mean opposite things.
A file it cannot read is named and the run is could-not-check, never a pass.

Deliberate non-ASCII test data is spelled from the code point -- chr(0xE9), chr(0x26A0) -- with
a comment on the line saying it is spelled that way for this gate and must not be "simplified"
back. The code point and the literal are the same string to Python at runtime, so the file
stays ASCII while the data is unchanged. Prefer chr() to a backslash-u escape: this file's first
draft wrote one in this very paragraph, the tool-call transport decoded it before the file was
written, and the checker caught the literal in its own docstring. There is deliberately NO
exemption marker: it would be a per-line escape hatch, and the rule fails at the edge (an em
dash inside a regex, a literal in the checker's own test), never in obvious prose.

Usage:  python tools/check_ascii.py FILE [FILE ...]
Exit 0 = every file is ASCII. 1 = at least one is not. 2 = could not check (no files, or a
file could not be read). 2 is never a pass.
"""

import sys
from pathlib import Path

EXIT_OK = 0
EXIT_NON_ASCII = 1
EXIT_COULD_NOT_CHECK = 2

_BOM = "U+FEFF"


def offenders(data: bytes) -> list[tuple[int, int, str]]:
    """(line, column, what) for every non-ASCII byte in `data`, without ever printing it.

    Lines split on LF only; a CR is 0x0D and ASCII either way. Each offending line is decoded as
    UTF-8 to name the code point. A line that is not valid UTF-8 (a stray Latin-1 byte, a
    truncated sequence) is reported byte by byte instead, so the report never raises.
    """
    found: list[tuple[int, int, str]] = []
    for number, line in enumerate(data.split(b"\n"), start=1):
        if all(byte < 128 for byte in line):
            continue
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            for column, byte in enumerate(line, start=1):
                if byte > 127:
                    found.append((number, column, f"byte 0x{byte:02X} (not valid UTF-8)"))
            continue
        for column, char in enumerate(text, start=1):
            if ord(char) > 127:
                found.append((number, column, f"U+{ord(char):04X}"))
    return found


def main(argv: list[str]) -> int:
    paths = [Path(arg) for arg in argv]
    if not paths:
        print("SCAN FAILED -- no files given. This is NOT a pass: a scan of nothing reports clean.")
        return EXIT_COULD_NOT_CHECK

    problems = 0
    checked = 0
    unreadable: list[str] = []
    for path in paths:
        name = path.as_posix()
        try:
            data = path.read_bytes()
        except OSError as exc:
            unreadable.append(f"{name}: {type(exc).__name__}")
            continue
        checked += 1
        for number, column, what in offenders(data):
            note = " (UTF-8 byte order mark)" if (number, column, what) == (1, 1, _BOM) else ""
            print(f"{name}:{number}:{column}: non-ASCII {what}{note}")
            problems += 1

    # Said out loud: a silent drop reads as coverage.
    for item in unreadable:
        print(f"COULD NOT READ: {item} -- not scanned, so its content is UNKNOWN.")

    if problems:
        print(f"{problems} non-ASCII location(s) in {checked} file(s). Gated Python stays ASCII:")
        print("  spell deliberate non-ASCII data from its code point (chr(0x2014)) with a comment;")
        print("  in prose use markdown emphasis, `--` for a dash, `WARNING:` for the sign.")
        return EXIT_NON_ASCII
    if unreadable:
        print(f"{checked} file(s) checked, but {len(unreadable)} could not be read -- NOT a pass.")
        return EXIT_COULD_NOT_CHECK
    print(f"{checked} file(s) checked; all ASCII.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
