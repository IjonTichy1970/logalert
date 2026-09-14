"""Behavioural checks for tools/check_ascii.py: bytes in, code points out, never the character.

Every fixture is built at RUNTIME from code points. A literal non-ASCII character in this file
would be the very thing the checker exists to catch, sitting in the file that tests it -- and
the checker runs over this file too (see the self-check at the bottom).
"""

import os
import subprocess
import sys
from pathlib import Path

CHECKER = Path(__file__).resolve().parents[1] / "tools" / "check_ascii.py"

EM_DASH = chr(0x2014)  # escaped for the ASCII gate; do not "simplify" to the literal
# U+2B50 is E2 AD 90 in UTF-8 and 0x90 is undefined in cp1252: a checker that PRINTED the
# character would die with UnicodeEncodeError under a cp1252 console instead of reporting it.
STAR = chr(0x2B50)  # escaped for the ASCII gate; do not "simplify" to the literal


def run(*paths: Path, console: str = "utf-8") -> tuple[int, str]:
    """The checker's exit code and combined output, run as a subprocess under `console`."""
    proc = subprocess.run(
        [sys.executable, str(CHECKER), *(str(p) for p in paths)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=dict(os.environ, PYTHONIOENCODING=console),
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_ascii_file_passes(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_bytes(b'"""Plain ASCII."""\nX = 1\n')
    rc, out = run(clean)
    assert rc == 0
    assert "1 file(s) checked; all ASCII." in out


def test_em_dash_is_reported_as_a_code_point_at_its_position(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_bytes(("X = 1\n# note " + EM_DASH + " here\n").encode("utf-8"))
    rc, out = run(bad)
    assert rc == 1
    assert "bad.py:2:8: non-ASCII U+2014" in out
    assert EM_DASH not in out, "the report must name the code point, never print the character"


def test_wide_character_is_reported_under_a_cp1252_console_without_crashing(tmp_path: Path) -> None:
    """The case that justifies 'code points, never the character'."""
    bad = tmp_path / "star.py"
    bad.write_bytes(("S = '" + STAR + "'\n").encode("utf-8"))
    rc, out = run(bad, console="cp1252")
    assert rc == 1
    assert "star.py:1:6: non-ASCII U+2B50" in out
    assert "Traceback" not in out


def test_byte_order_mark_fails_and_is_named(tmp_path: Path) -> None:
    bom = tmp_path / "bom.py"
    bom.write_bytes(b"\xef\xbb\xbfX = 1\n")
    rc, out = run(bom)
    assert rc == 1
    assert "bom.py:1:1: non-ASCII U+FEFF (UTF-8 byte order mark)" in out


def test_invalid_utf8_byte_is_reported_by_value(tmp_path: Path) -> None:
    latin = tmp_path / "latin.py"
    latin.write_bytes(b"NAME = 'caf\xe9'\n")
    rc, out = run(latin)
    assert rc == 1
    assert "latin.py:1:12: non-ASCII byte 0xE9 (not valid UTF-8)" in out


def test_empty_file_list_is_could_not_check() -> None:
    rc, out = run()
    assert rc == 2
    assert "SCAN FAILED" in out


def test_unreadable_file_is_named_and_never_a_pass(tmp_path: Path) -> None:
    clean = tmp_path / "clean.py"
    clean.write_bytes(b"X = 1\n")
    missing = tmp_path / "missing.py"
    rc, out = run(clean, missing)
    assert rc == 2, "one unreadable file must not be outranked by a clean one"
    assert "COULD NOT READ: " in out and "missing.py" in out


def test_non_ascii_outranks_unreadable(tmp_path: Path) -> None:
    """A found problem is exit 1 even when another file could not be read: 1 carries more
    information than 2 here, and both are non-zero."""
    bad = tmp_path / "bad.py"
    bad.write_bytes(("# " + EM_DASH + "\n").encode("utf-8"))
    rc, out = run(bad, tmp_path / "missing.py")
    assert rc == 1
    assert "COULD NOT READ" in out


def test_checker_and_this_test_are_themselves_ascii() -> None:
    """The rule fails at the edge -- a literal in the checker's own test is the classic slip."""
    rc, out = run(CHECKER, Path(__file__))
    assert rc == 0, out


def test_guard_returns_both_verdicts(tmp_path: Path) -> None:
    """Anti-vacuity: a checker stuck on one answer passes every case above that expects it."""
    clean = tmp_path / "clean.py"
    clean.write_bytes(b"X = 1\n")
    bad = tmp_path / "bad.py"
    bad.write_bytes(("# " + EM_DASH + "\n").encode("utf-8"))
    assert {run(clean)[0], run(bad)[0]} == {0, 1}
