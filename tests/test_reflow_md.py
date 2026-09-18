"""Guards over `tools/reflow_md.py`.

WARNING: THE LOAD-BEARING TEST IN THIS FILE IS `test_CHECK_3_catches_what_CHECK_1_and_2_pass`.
The tool's two original checks were measured reporting OK while an eight-line blockquote collapsed
into one quoted line plus seven lines of stray `>` characters. That is the case CHECK 3 exists for,
and it is the case on which CHECK 3 and the two checks that preceded it disagree. If this file is
ever trimmed, that is the test to keep.

WARNING: the tool is IMPORTED here, not invoked as a subprocess, and that differs from the
changelog-refs tool test. The reason that one shells out: `from tools.reflow_md import ...`
puts one source under two module names (`tools.reflow_md` and the script run as `__main__`) and
`mypy --strict` exits 2. This file imports nothing -- `_load()` reads the source and executes it
under a private module name, so mypy never sees a second import path for it, and the pure
functions can be exercised on literals instead of through a filesystem and an exit code.

WARNING: a test that loads its subject this way runs whatever Python's bytecode cache thinks is
there. `.pyc` validation keys on (source mtime in seconds, size), so a same-length mutation
restored within the same second serves the MUTANT while `git diff` is clean. For a mutation pass:
purge every `__pycache__` under the subject FIRST, then keep `PYTHONDONTWRITEBYTECODE=1` set for
every run so it stays purged -- the variable stops WRITING bytecode, not reading it, and a `.pyc`
already on disk is still served under it (measured) -- and run `pytest -p no:cacheprovider`. The
kit's gate does the purge before its pytest stage for the same reason.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "reflow_md.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_reflow_md_under_test", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reflow_md = _load()


# -- the reason the tool is in the tree -----------------------------------------------------

_QUOTE = (
    "> **A quoted warning that runs well past the target width and therefore has to wrap "
    "somewhere.** It continues for a second sentence so the wrap is forced.\n"
    ">\n"
    "> A second quoted paragraph, also long enough that it cannot survive at eighty columns "
    "without being broken across lines.\n"
)


def test_a_multi_line_blockquote_stays_a_blockquote() -> None:
    """Every line the reflow emits from a quote must still be quoted."""
    out = reflow_md.reflow(_QUOTE)
    body = [line for line in out.splitlines() if line.strip()]
    assert body, "the reflow produced nothing"
    unquoted = [line for line in body if not line.lstrip().startswith(">")]
    assert unquoted == [], (
        "these lines came out of a blockquote without their marker, which renders as one broken "
        f"paragraph: {unquoted}"
    )


def test_CHECK_3_catches_what_CHECK_1_and_2_pass() -> None:
    """THE CASE THE THREE CHECKS DISAGREE ON.

    `damaged` is a continuation line that cleanly LOST its marker. The words are byte-identical
    once CHECK 1 strips the markers, and no table or fence moved, so those two pass -- while the
    document now renders as one quoted line followed by unquoted prose.

    The first two assertions are the load-bearing ones: they prove the fixture actually slips past
    CHECK 1 and CHECK 2. Without them this test would pass with CHECK 3 deleted, because CHECK 1
    would be doing the work and nobody would notice.
    """
    honest = "> alpha beta\n> gamma delta\n"
    damaged = "> alpha beta\ngamma delta\n"

    assert reflow_md.paragraphs(honest) == reflow_md.paragraphs(damaged), (
        "this fixture no longer slips past CHECK 1, so it proves nothing about CHECK 3"
    )
    assert reflow_md.protected(honest) == reflow_md.protected(damaged), (
        "this fixture no longer slips past CHECK 2, so it proves nothing about CHECK 3"
    )
    assert reflow_md.quote_shape(honest) != reflow_md.quote_shape(damaged)
    assert reflow_md.check(honest, damaged) == ["CHECK 3 (blockquote structure)"]


def test_CHECK_1_catches_a_marker_stranded_MID_SENTENCE() -> None:
    """The other half of the blockquote damage, and CHECK 1 owns it once markers are stripped.

    This is what the tool's first version actually produced: the first line keeps its marker and
    the rest of the quote is emitted as prose carrying the old `>` characters inline. A
    marker-INCLUSIVE CHECK 1 passed on exactly this, because the tokens all survive in order.
    Stripping the markers first is what makes the stray one visible as a word that appears on one
    side only.
    """
    honest = "> alpha beta\n> gamma delta\n"
    littered = "> alpha beta > gamma delta\n"
    assert "CHECK 1 (paragraph text)" in reflow_md.check(honest, littered)


def test_the_three_checks_pass_on_a_real_reflow() -> None:
    """Every check must be green on the tool's own output, or it cannot be used at all."""
    source = _QUOTE + "\nPlain prose that is also long enough to need wrapping at this width.\n"
    assert reflow_md.check(source, reflow_md.reflow(source)) == []


# -- width is columns, not characters --------------------------------------------------------------


def test_width_is_measured_in_TERMINAL_COLUMNS_not_characters() -> None:
    """U+2B50 is East Asian Wide: one code point, two columns.

    Written as an escape so this file stays ASCII -- Python resolves it at parse time, so the
    DATA is the character while the FILE is ASCII. See CLAUDE.md on bandit and cp1252.
    """
    star = "\u2b50"
    assert len(star) == 1
    assert reflow_md.columns(star) == 2
    assert reflow_md.columns("a" + star + "b") == 4
    # A VS16-presented emoji: two code points, two columns.
    assert reflow_md.columns("\u26a0\ufe0f") == 2


def test_no_emitted_prose_line_exceeds_the_target_in_COLUMNS() -> None:
    """A star-dense paragraph wrapped on `len()` would overshoot; wrapped on columns it does not."""
    star = "\u2b50"
    source = " ".join([f"{star}word{index}" for index in range(40)]) + "\n"
    for line in reflow_md.reflow(source).splitlines():
        assert reflow_md.columns(line) <= reflow_md.WIDTH, (
            f"{reflow_md.columns(line)} columns: wrapping counted characters, not columns"
        )


# -- what must never be rewrapped ------------------------------------------------------------------


def test_fences_tables_and_headings_survive_byte_identical() -> None:
    """Wrapping any of these breaks something CHECK 1 cannot see."""
    source = (
        "# A heading that is quite long and would otherwise be wrapped by a naive implementation\n"
        "\n"
        "| a column heading | another column heading | a third one to push this row over eighty |\n"
        "|---|---|---|\n"
        "\n"
        "```sh\n"
        "cd /srv/myproject && git pull --ff-only && systemctl restart myproject && echo done\n"
        "```\n"
    )
    out = reflow_md.reflow(source)
    assert out == source, "a heading, table row or fenced command was rewrapped"


def test_a_long_single_token_is_left_long_rather_than_severed() -> None:
    """A URL or a pytest node id must survive; a severed token is a broken link or a wrong id."""
    node = "tests/test_globs.py::test_last_component_wildcard_matches_regular_files_in_name_order"
    out = reflow_md.reflow(f"See `{node}` for the case that matters here.\n")
    assert node in out


def test_a_continuation_line_beginning_with_a_double_pipe_is_not_a_table_row() -> None:
    """The `||` case CHECK 2 caught once already.

    The source project's prose wrapped an expression with `||` in it such that a continuation
    could begin `||`. Treating it as a table row desynchronises the fence tracker, and the
    paragraph check cannot see that because every word survives.
    """
    assert not reflow_md._is_table("|| question_id)` is derived on the device.")
    assert reflow_md._is_table("| a | b |")


def test_an_issue_reference_opening_a_paragraph_is_not_a_heading() -> None:
    """`#184's stated rule` is prose, not a heading, and reading it as one left a line unwrapped."""
    assert not reflow_md._is_heading("#184's stated rule is the one that applies here.")
    assert reflow_md._is_heading("## A real heading")


# -- the gate stage -------------------------------------------------------------------------


def test_the_width_check_fires_on_a_line_that_DRIFTED_wide() -> None:
    """THE MUTATION FOR THE GATE STAGE: re-widen one prose line and it must redden."""
    narrow = "Alpha beta gamma delta.\nEpsilon zeta eta theta.\n"
    assert reflow_md.overwide(narrow) == []

    wide = " ".join(f"word{index}" for index in range(30)) + "\n"
    assert len(wide.splitlines()) == 1
    assert reflow_md.overwide(wide), "a 200-column prose line was not reported"


def test_the_width_check_SPARES_what_wrapping_cannot_fix() -> None:
    """Tables, fences, headings and single long tokens, each for the reason the tool records.

    Without this the stage is permanently red on correct files, and the only way to quiet it is an
    exemption list -- which is how a gate stops gating without anyone deciding it should.
    """
    node = "tests/test_globs.py::test_last_component_wildcard_matches_regular_files_in_name_order"
    for label, text in (
        ("table", "| a very wide column heading | " + "x" * 60 + " |\n"),
        ("fence", "```sh\n" + "echo " + "y" * 90 + "\n```\n"),
        ("heading", "## " + "A rather long heading that goes well past the target width here\n"),
        # A line that is ONLY the long token. `See \\`{node}\\` now.` is NOT this case and must
        # not be used here: reflowing it really does help, moving `See` and `now.` off the token's
        # line and taking the widest line from 93 columns to 87. Written that way first, and the
        # check was right while the fixture was wrong.
        ("long token", f"`{node}`\n"),
        ("quoted long token", f"> `{node}`,\n"),
        ("listed long link", f"- **[`x`](https://example.invalid/{'a' * 70})**\n"),
    ):
        assert reflow_md.overwide(text) == [], f"the {label} case was reported as wrappable"


def test_the_width_check_REFUSES_a_scan_that_read_almost_nothing(tmp_path: Path) -> None:
    """The anti-vacuity floor, exit 2. A stage handed a bad glob reports clean with confidence.

    Every tree-walking check needs the same guard; it is the failure that turns a broken gate into a
    green one.
    """
    paths = []
    for i in range(reflow_md._FLOOR - 1):
        one = tmp_path / f"ok{i}.md"
        one.write_text("Short enough.\n", encoding="utf-8", newline="\n")
        paths.append(one)
    assert reflow_md._check_widths(paths) == 2


def test_the_width_check_NAMES_a_file_it_could_not_READ(tmp_path: Path, capsys: object) -> None:
    """The DELIBERATE exclusion has always been announced, with a comment in the tool saying
    that a silent exclusion reads as coverage. The ACCIDENTAL one -- a file that fails to decode --
    was a bare `continue` eleven lines above that comment, in the same function.

    WARNING: the COUNT was never wrong here (a sibling tool once counted files it never read).
    `scanned` is incremented
    after the read, so the floor stayed honest. What was missing is any way for the reader to know
    the sweep had a hole in it: the number simply came out lower, against no baseline.

    MUTATION: drop the unreadable file from the announced list -- this reddens. Deleting the
    `unreadable` list entirely also reddens.
    """
    paths = []
    for i in range(reflow_md._FLOOR):
        one = tmp_path / f"ok{i}.md"
        one.write_text("Short enough.\n", encoding="utf-8", newline="\n")
        paths.append(one)
    bad = tmp_path / "undecodable.md"
    # A lone 0xFF is not valid UTF-8. Written as BYTES, so this source file stays ASCII while the
    # DATA does not -- the convention CLAUDE.md sets for deliberate non-ASCII test input.
    bad.write_bytes(b"Short enough \xff here.\n")
    paths.append(bad)

    assert reflow_md._check_widths(paths) == 0, "the floor should be cleared by the readable files"
    out = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "COULD NOT READ" in out, "the drop was silent, which reads as coverage:\n" + out
    assert "undecodable.md" in out, "the unreadable file was not named:\n" + out


def test_a_readable_sweep_announces_NOTHING_unreadable(tmp_path: Path, capsys: object) -> None:
    """Detection leg. Without it, the assertion above is satisfied by a tool that announces
    unconditionally, and "nothing unreadable" is a claim no run has ever been able to falsify.
    """
    paths = []
    for i in range(reflow_md._FLOOR):
        one = tmp_path / f"ok{i}.md"
        one.write_text("Short enough.\n", encoding="utf-8", newline="\n")
        paths.append(one)

    assert reflow_md._check_widths(paths) == 0
    assert "COULD NOT READ" not in capsys.readouterr().out  # type: ignore[attr-defined]
