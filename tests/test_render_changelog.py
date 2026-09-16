"""The changelog renderer and structural gate (issue #17): the page it writes, the defects it
refuses, its exit codes, and the real CHANGELOG.md. Pure text: real on both platforms."""

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "render_changelog.py"


def _load() -> ModuleType:
    """By file, as tests/test_reflow_md.py does: tools/ is not a package."""
    spec = importlib.util.spec_from_file_location("_render_changelog_under_test", _TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


tool = _load()
EXIT_COULD_NOT_CHECK: int = tool.EXIT_COULD_NOT_CHECK
EXIT_DEFECT: int = tool.EXIT_DEFECT
EXIT_OK: int = tool.EXIT_OK
Defect: type[Exception] = tool.Defect
ascii_only = tool.ascii_only
main = tool.main
parse = tool.parse

NL = chr(10)
EM = chr(0x2014)  # the em dash of a version heading, from the code point (ASCII source)
STAR = chr(0x2B50)  # the insight marker
REPO_ROOT = Path(__file__).resolve().parents[1]

HEADER = NL.join([
    "# Changelog",
    "",
    "All notable changes.",
    "",
])
ABOUT = NL.join([
    "## About this changelog",
    "",
    "Two tags:",
    "",
    "- **`[contract]`** -- the operator's surface.",
    "- **`[internal]`** -- everything else.",
    "",
])
UNRELEASED = NL.join([
    "## [Unreleased]",
    "",
    "### Nitty Gritty",
    "",
    "- `[contract]` **A new option, `--foo`, with a <pid> in its line** (#7). The body",
    "  continues here with `code <kept>` and *\"a quoted claim\"* over two lines.",
    "",
    "- `[internal]` " + STAR + " **A guard, pinned** (#7, #8). One line.",
    "",
])
RELEASED = NL.join([
    "## [0.1.0] " + EM + " 2026-09-16 " + EM + " the first release",
    "",
    "### Nitty Gritty",
    "",
    "- `[contract]` **The first thing** (#1). Its body.",
    "",
])
FOOTER = NL.join([
    "[Unreleased]: https://github.com/IjonTichy1970/logalert/compare/v0.1.0...HEAD",
    "[0.1.0]: https://github.com/IjonTichy1970/logalert/releases/tag/v0.1.0",
    "",
])
FIXTURE = HEADER + NL + UNRELEASED + RELEASED + ABOUT + FOOTER


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(text, encoding="utf-8", newline=NL)
    return path


def test_parse_reads_both_heading_shapes_the_entries_and_the_footer() -> None:
    changelog = parse(FIXTURE)
    assert [v.name for v in changelog.versions] == ["Unreleased", "0.1.0"]
    assert changelog.versions[0].anchor == "unreleased"
    assert changelog.versions[1].anchor == "v0-1-0"
    assert changelog.versions[1].heading == ("0.1.0 " + EM + " 2026-09-16 " + EM
                                             + " the first release")
    assert changelog.header == "# Changelog" + NL + NL + "All notable changes."
    assert changelog.about.startswith("## About this changelog")
    assert "[contract]" in changelog.about
    first, second = changelog.versions[0].entries
    assert (first.tag, first.refs, first.marker) == ("contract", (7,), None)
    assert first.headline == "A new option, `--foo`, with a <pid> in its line"
    assert first.body.startswith("The body continues here") and "two lines." in first.body
    assert (second.tag, second.refs, second.marker) == ("internal", (7, 8), STAR)
    assert changelog.links["0.1.0"].endswith("/releases/tag/v0.1.0")


def test_render_writes_the_page_with_anchors_badges_links_and_a_toc(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write(tmp_path, FIXTURE)
    assert main(["--changelog", str(path), "--out", str(tmp_path / "site")]) == EXIT_OK
    page = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert '<section id="unreleased">' in page and '<section id="v0-1-0">' in page
    assert '<a href="#v0-1-0">0.1.0 ' + EM in page  # the table of contents
    assert '<span class="tag contract">contract</span>' in page
    assert '<span class="tag internal">internal</span>' in page
    assert 'href="https://github.com/IjonTichy1970/logalert/issues/7"' in page
    assert 'href="https://github.com/IjonTichy1970/logalert/issues/8"' in page
    assert 'id="issue-7"' in page and 'id="issue-7-2"' in page  # two entries, one issue
    assert '<h2><a href="https://github.com/IjonTichy1970/logalert/releases/tag/v0.1.0">' in page
    assert ("<strong>A new option, <code>--foo</code>, with a &lt;pid&gt; in its line"
            "</strong>") in page
    assert "<code>code &lt;kept&gt;</code>" in page  # markdown escapes inside a code span
    assert "<pid>" not in page  # a raw tag would have been swallowed by the browser
    assert STAR + " <strong>A guard, pinned</strong>" in page
    assert page.count("<script") == 0 and "http" not in re.sub(r'href="[^"]*"', "", page)
    assert '<li><a href="#about">About this changelog</a></li>' in page
    about = page.index('<footer id="about">')
    assert about > page.index('<section id="v0-1-0">')  # last, as in the file
    assert "<h2>About this changelog</h2>" in page[about:]
    assert "the operator's surface" in page[about:]
    assert "wrote" in capsys.readouterr().out


def test_check_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = write(tmp_path, FIXTURE)
    assert main(["--check", "--changelog", str(path), "--out", str(tmp_path / "site")]) == EXIT_OK
    assert not (tmp_path / "site").exists()
    assert "2 version(s), 3 entries, structure sound" in capsys.readouterr().out


def test_a_missing_file_is_could_not_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--check", "--changelog", str(tmp_path / "nope.md")]) == EXIT_COULD_NOT_CHECK
    assert "could not check, not a pass" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("mutant", "message"),
    [
        (FIXTURE.replace("- `[internal]` " + STAR + " **A guard", "- " + STAR + " **A guard"),
         "an entry without a [contract] / [internal] tag"),
        (FIXTURE.replace("**The first thing** (#1).", "The first thing (#1)."),
         "an entry without **headline** (#N). after its tag"),
        (FIXTURE.replace("### Nitty Gritty" + NL + NL + "- `[contract]` **A new",
                         "### Nitty Gritty" + NL + NL + "### Nitty Gritty" + NL + NL
                         + "- `[contract]` **A new"),
         "a second ### Nitty Gritty under [Unreleased]"),
        (FIXTURE.replace("### Nitty Gritty" + NL + NL + "- `[contract]` **The first",
                         "### Fixed" + NL + NL + "- `[contract]` **The first"),
         "the only category is ### Nitty Gritty"),
        (FIXTURE.replace("## [0.1.0] " + EM + " 2026-09-16 " + EM + " the first release",
                         "## 0.1.0 (2026-09-16)"),
         "not a version heading"),
        (FIXTURE.replace("## [0.1.0] " + EM + " 2026-09-16 " + EM + " the first release",
                         "## [0.1.0]"),
         "a released version needs its date and label"),
        (FIXTURE.replace("## [Unreleased]", "## [Unreleased] " + EM + " 2026-09-16 " + EM + " x"),
         "[Unreleased] carries no date"),
        (FIXTURE.replace(UNRELEASED, ""), "the first version heading must be ## [Unreleased]"),
        (FIXTURE.replace("## [Unreleased]" + NL + NL + "### Nitty Gritty" + NL + NL,
                         "## [Unreleased]" + NL + NL),
         "an entry outside ### Nitty Gritty"),
        (FIXTURE.replace("- `[contract]` **The first thing** (#1). Its body." + NL, ""),
         "a released version with no entries: [0.1.0]"),
        (FIXTURE.replace("### Nitty Gritty" + NL + NL + "- `[contract]` **A new",
                         "### Nitty Gritty" + NL + NL + "Some prose." + NL + NL
                         + "- `[contract]` **A new"),
         "prose where an entry should be"),
        (FIXTURE.replace(ABOUT + FOOTER, ABOUT + RELEASED + FOOTER),
         "a heading after ## About this changelog: it comes last"),
        (HEADER + NL + ABOUT + UNRELEASED + RELEASED + FOOTER,
         "## About this changelog before the versions: it comes last"),
    ],
)
def test_structural_defects_are_exit_1_naming_the_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mutant: str, message: str
) -> None:
    assert mutant != FIXTURE, "the mutant did not apply"
    path = write(tmp_path, mutant)
    assert main(["--check", "--changelog", str(path)]) == EXIT_DEFECT
    out = capsys.readouterr().out
    assert message in out and re.search(r"CHANGELOG\.md(:\d+)?: ", out), out
    with pytest.raises(Defect):
        parse(mutant)


def test_the_about_sections_policy_bullets_are_not_entries() -> None:
    """The About section's `- **`[contract]`** ...` lines describe the tags; they are not
    entries and not defects (a parser keyed on `- ` alone would refuse the real file), and a
    file without an About section is still sound."""
    changelog = parse(FIXTURE)
    assert sum(len(v.entries) for v in changelog.versions) == 3
    assert "- **`[contract]`** -- the operator's surface." in changelog.about
    bare = parse(FIXTURE.replace(ABOUT, ""))
    assert bare.about == "" and sum(len(v.entries) for v in bare.versions) == 3


def test_diagnostics_are_ascii() -> None:
    assert ascii_only("## [0.1.0] " + EM + " x") == "## [0.1.0] U+2014 x"
    text = FIXTURE.replace("## [0.1.0] " + EM + " 2026-09-16 " + EM + " the first release",
                           "## [0.1.0] " + EM + " 2026-09-16")
    with pytest.raises(Defect) as exc:
        parse(text)
    assert str(exc.value).isascii() and "U+2014" in str(exc.value)


def test_the_real_changelog_is_structurally_sound() -> None:
    """The file in the tree, every time: the gate's `changelog structure` stage runs the same
    parse, and this pins that the stage never went quiet on a real defect."""
    changelog = parse((REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert changelog.versions[0].name == "Unreleased"
    assert "**Everything gets an entry.**" in changelog.about  # the doctrine lives there
    assert "**Everything gets an entry.**" not in changelog.header
    assert all(entry.tag in ("contract", "internal")
               for version in changelog.versions for entry in version.entries)
    assert sum(len(v.entries) for v in changelog.versions) >= 15


# -- the review's cases (issue #17) ------------------------------------------------------


def test_the_exit_codes_are_the_documented_ones() -> None:
    """The contract with tools/gate.sh and pages.yml, pinned to the literals: a test that
    compares ``main()`` against the module's own constants would pass with both set to 0."""
    assert (EXIT_OK, EXIT_DEFECT, EXIT_COULD_NOT_CHECK) == (0, 1, 2)


@pytest.mark.parametrize("bad", ["[contact]", "[Internal]", "[]", "[contract, internal]"])
def test_a_misspelled_or_capitalised_tag_is_no_tag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    """The release worklist scans for `[contract]`: a typo must stop the gate, not render as
    an unstyled badge."""
    mutant = FIXTURE.replace("- `[internal]` " + STAR, "- `" + bad + "` " + STAR)
    assert mutant != FIXTURE
    assert main(["--check", "--changelog", str(write(tmp_path, mutant))]) == 1
    assert "an entry without a [contract] / [internal] tag" in capsys.readouterr().out


def test_a_bracketless_version_heading_is_refused(tmp_path: Path) -> None:
    mutant = FIXTURE.replace("## [0.1.0] " + EM, "## 0.1.0 " + EM)
    assert mutant != FIXTURE
    with pytest.raises(Defect, match="not a version heading"):
        parse(mutant)


@pytest.mark.parametrize(
    ("mutant", "message"),
    [
        (FIXTURE.replace(ABOUT, UNRELEASED + ABOUT), "a second version heading for [Unreleased]"),
        (FIXTURE.replace(ABOUT, RELEASED + ABOUT), "a second version heading for [0.1.0]"),
        (FIXTURE.replace(ABOUT, RELEASED.replace("0.1.0", "0.2.0") + ABOUT),
         "[0.2.0] after [0.1.0]: newest first"),
    ],
)
def test_a_repeated_or_misordered_version_heading_is_refused(mutant: str, message: str) -> None:
    """A half-applied release roll leaves two [Unreleased] headings; two sections with one
    anchor would each claim the same id on the page."""
    assert mutant != FIXTURE
    with pytest.raises(Defect) as exc:
        parse(mutant)
    assert message in str(exc.value)


def test_older_releases_below_newer_ones_parse() -> None:
    older = RELEASED.replace("0.1.0", "0.0.9").replace("2026-09-16", "2026-09-01")
    changelog = parse(FIXTURE.replace(ABOUT, older + ABOUT))
    assert [v.name for v in changelog.versions] == ["Unreleased", "0.1.0", "0.0.9"]


def test_an_unreleased_section_with_an_empty_category_parses() -> None:
    """What the release roll leaves behind: a fresh [Unreleased] with its category and no
    entries yet; the next /ship appends to that heading."""
    fresh = "## [Unreleased]" + NL + NL + "### Nitty Gritty" + NL + NL
    changelog = parse(HEADER + NL + fresh + RELEASED + ABOUT + FOOTER)
    assert changelog.versions[0].entries == [] and changelog.versions[0].categories == 1


def test_prose_before_the_bold_headline_is_not_a_marker(tmp_path: Path) -> None:
    """The About section names the markers, all emoji; a word in that slot is a malformed
    headline, not a marker."""
    mutant = FIXTURE.replace("- `[contract]` **The first", "- `[contract]` Note: **The first")
    assert mutant != FIXTURE
    with pytest.raises(Defect, match=r"an entry without \*\*headline\*\* \(#N\)\. after its tag"):
        parse(mutant)


def test_a_bullet_in_the_intro_is_header_text_not_an_entry() -> None:
    intro = HEADER + "- a bullet in the intro" + NL + NL
    changelog = parse(intro + UNRELEASED + RELEASED + ABOUT + FOOTER)
    assert "- a bullet in the intro" in changelog.header
    assert sum(len(v.entries) for v in changelog.versions) == 3


def test_an_escaped_backtick_opens_no_code_span(tmp_path: Path) -> None:
    """markdown treats a backslash-escaped backtick as a character; the escaping of ``<`` and
    ``>`` outside code spans must agree, or raw HTML between an escaped backtick and the next
    real one reaches the page (reproduced in review)."""
    body = "See " + chr(92) + "`<b>bold</b>" + chr(92) + "` and `<kept>` there."
    text = FIXTURE.replace("Its body.", body)
    assert text != FIXTURE
    path = write(tmp_path, text)
    assert main(["--changelog", str(path), "--out", str(tmp_path / "site")]) == 0
    page = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "<b>" not in page and "&lt;b&gt;bold&lt;/b&gt;" in page
    assert "<code>&lt;kept&gt;</code>" in page


def test_a_reference_link_in_the_about_section_resolves(tmp_path: Path) -> None:
    """The footer's definitions are parsed out of the text; markdown still needs them for a
    `[ref]` written in the About prose (or the intro)."""
    text = FIXTURE.replace("Two tags:", "Two tags (see [the tag rule][rule]):").replace(
        FOOTER, FOOTER + "[rule]: https://example.net/rule" + NL)
    assert text != FIXTURE
    path = write(tmp_path, text)
    assert main(["--changelog", str(path), "--out", str(tmp_path / "site")]) == 0
    page = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert '<a href="https://example.net/rule">the tag rule</a>' in page
    assert "[rule]" not in page


def test_a_bom_is_not_text(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_bytes((chr(0xFEFF) + FIXTURE).encode("utf-8"))
    assert main(["--changelog", str(path), "--out", str(tmp_path / "site")]) == 0
    page = (tmp_path / "site" / "index.html").read_text(encoding="utf-8")
    assert "<h1>Changelog</h1>" in page


def test_a_non_utf8_file_and_an_unwritable_site_are_could_not_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "CHANGELOG.md"
    latin = FIXTURE.replace("All notable changes.", "All " + EM + " changes.")
    path.write_bytes(latin.replace(STAR + " ", "").encode("cp1252"))  # 0x97 is not UTF-8
    assert main(["--check", "--changelog", str(path)]) == 2
    out = capsys.readouterr().out
    assert "cannot read" in out and "could not check" in out and out.isascii()
    path = write(tmp_path, FIXTURE)
    blocker = tmp_path / "site"
    blocker.write_text("not a directory", encoding="utf-8")
    assert main(["--changelog", str(path), "--out", str(blocker)]) == 2
    assert "cannot write" in capsys.readouterr().out


def test_check_needs_no_markdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate's `changelog structure` stage runs on the dev extra alone: the import must stay
    inside render()."""
    monkeypatch.setitem(sys.modules, "markdown", None)  # an import now raises ImportError
    fresh = _load()
    assert fresh.main(["--check", "--changelog", str(write(tmp_path, FIXTURE))]) == 0
    with pytest.raises(ImportError):
        fresh.main(["--changelog", str(tmp_path / "CHANGELOG.md"), "--out", str(tmp_path / "s")])


def test_the_real_changelog_renders_without_raw_markup() -> None:
    changelog = parse((REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    page = tool.render(changelog)
    assert page.count('<section id="') == len(changelog.versions)
    assert page.count("<li id=\"issue-") == sum(len(v.entries) for v in changelog.versions)
    assert "<pid>" not in page and "<glob>" not in page and "<script" not in page
    assert '<footer id="about">' in page and "<h2>About this changelog</h2>" in page
