#!/usr/bin/env python3
"""Render CHANGELOG.md to one HTML page for GitHub Pages -- and check its structure (#17).

## Two jobs, one parser

The changelog's `## About this changelog` section, last in the file, is the only definition
of its format: a title and one paragraph first, then `## [Unreleased]` and `## [x.y.z] -- date
-- label` version headings (em dashes), one `### Nitty Gritty` per version, entries `- ` +
the tag in backticks + an optional marker + a bold headline + `(#N)` + a period + the body
wrapped with two-space continuation, the whole entry at most `MAX_ENTRY_LINES` lines (#60:
one bold sentence, then at most three short sentences), then the About section, then the
footer links. This tool parses that STRUCTURE itself and hands only the text to `markdown`
for rendering. The same parse is the changelog's structural gate: the doctrine's "an untagged
entry stops the release" was enforced by reading until now, and the Pages build (and
`tools/gate.sh`, via `--check`) refuse:

  * an entry without a `[contract]` / `[internal]` tag, or without `**headline** (#N).`
  * an entry of more than `MAX_ENTRY_LINES` lines (the reasoning belongs on the issue)
  * an entry outside a `### Nitty Gritty` section, or a second `### Nitty Gritty` in one version
  * a `##` heading that is not a version heading, or a released version without a date
  * no `## [Unreleased]` first, a version heading twice, released versions not descending, or
    a released version with no entries
  * prose where an entry should be, or a version heading after the About section

`--check` parses and validates without writing and without importing `markdown`, so the gate
stage needs only the `dev` extra; rendering needs the `docs` extra (`pip install -e ".[docs]"`).

## Output

`site/index.html`: a small inline stylesheet, a table of contents, one `<section>` per version
with an anchor (`unreleased`, `v0-1-0`), the tag as a badge, the `(#N)` linked to the issue,
the markers left as text, no external assets; the About section last, as in the file.

Exit 0 = rendered (or `--check` found the structure sound). 1 = a structural defect, named with
its line. 2 = could not check (the file cannot be read; never a pass). Diagnostics are ASCII:
a non-ASCII character in a quoted line is shown as its code point, never printed.
"""

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
SITE = REPO_ROOT / "site"
ISSUES = "https://github.com/IjonTichy1970/logalert/issues/"
TITLE = "logalert changelog"
CATEGORY = "### Nitty Gritty"
# One bold sentence, then at most three short sentences (#60): a two-line headline and three
# sentences of 60-80 characters is five lines at 80 columns; the sixth is for code spans.
MAX_ENTRY_LINES = 6
ABOUT = "## About this changelog"

EXIT_OK = 0
EXIT_DEFECT = 1
EXIT_COULD_NOT_CHECK = 2

_EM_DASH = chr(0x2014)
_VERSION = re.compile(
    r"^## \[(?P<name>Unreleased|\d+\.\d+\.\d+)\]"
    r"(?: " + _EM_DASH + r" (?P<date>\d{4}-\d{2}-\d{2}) " + _EM_DASH + r" (?P<label>.+))?$"
)
_TAG_START = re.compile(r"^- `\[(contract|internal)\]` ")
_ENTRY = re.compile(
    r"^- `\[(?P<tag>contract|internal)\]` (?:(?P<marker>[^\x00-\x7f]+) )?"  # a marker is emoji
    r"\*\*(?P<headline>.+?)\*\* \((?P<refs>#\d+(?:, #\d+)*)\)\.(?P<body>.*)$",
    re.S,
)
_FOOTER = re.compile(r"^\[(?P<name>[^\]]+)\]: (?P<url>https?://\S+)$")
# markdown's own BACKTICK_RE shape: a run of backticks not preceded by a backslash opens a span
# that the same run closes; an escaped backtick is a character, not an opener
_CODE_SPAN = re.compile(r"(?<!\\)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)


class Defect(Exception):
    """A structural defect: the message names the line."""


@dataclass(frozen=True)
class Entry:
    line: int
    tag: str
    marker: str | None
    headline: str
    refs: tuple[int, ...]
    body: str


@dataclass
class Version:
    line: int
    name: str  # "Unreleased" or "x.y.z"
    date: str | None
    label: str | None
    entries: list[Entry] = field(default_factory=list)
    categories: int = 0

    @property
    def anchor(self) -> str:
        return "unreleased" if self.name == "Unreleased" else "v" + self.name.replace(".", "-")

    @property
    def heading(self) -> str:
        if self.date is None:
            return self.name
        return f"{self.name} {_EM_DASH} {self.date} {_EM_DASH} {self.label}"


@dataclass
class Changelog:
    header: str  # the markdown before the first version heading
    versions: list[Version]
    about: str  # the About section, heading included; empty when the file has none
    links: dict[str, str]  # footer link references, name -> url


def ascii_only(text: str) -> str:
    """The text with every non-ASCII character shown as its code point (never printed raw)."""
    return "".join(c if c.isascii() else f"U+{ord(c):04X}" for c in text)


def _descends(previous: str, name: str) -> bool:
    """Whether released version ``name`` is older than ``previous`` (newest first)."""
    older = tuple(int(part) for part in name.split("."))
    newer = tuple(int(part) for part in previous.split("."))
    return older < newer


def parse(text: str, name: str = "CHANGELOG.md") -> Changelog:
    """The structure, or a ``Defect`` naming the first line that breaks it."""
    lines = text.split(chr(10))
    header: list[str] = []
    versions: list[Version] = []
    about: list[str] = []
    names: set[str] = set()
    links: dict[str, str] = {}
    current: Version | None = None
    in_category = False
    pending: list[str] = []  # the lines of the entry being read
    pending_line = 0

    def where(number: int, what: str, line: str = "") -> Defect:
        shown = f": {ascii_only(line)!r}" if line else ""
        return Defect(f"{name}:{number}: {what}{shown}")

    def flush() -> None:
        nonlocal pending, pending_line
        if not pending or current is None:  # header bullets never reach pending
            return
        joined = " ".join(part.strip() for part in pending)
        match = _ENTRY.match(joined)
        if match is None:
            if _TAG_START.match(joined):
                raise where(pending_line, "an entry without **headline** (#N). after its tag",
                            pending[0])
            raise where(pending_line, "an entry without a [contract] / [internal] tag",
                        pending[0])
        if len(pending) > MAX_ENTRY_LINES:
            raise where(pending_line, f"an entry of {len(pending)} lines: at most"
                        f" {MAX_ENTRY_LINES} (one bold sentence, then at most three short"
                        " sentences; reflow the block first)", pending[0])
        refs = tuple(int(ref[1:]) for ref in match.group("refs").split(", "))
        current.entries.append(Entry(pending_line, match.group("tag"), match.group("marker"),
                                     match.group("headline"), refs, match.group("body").strip()))
        pending, pending_line = [], 0

    for number, line in enumerate(lines, start=1):
        footer = _FOOTER.match(line)
        if footer is not None:
            flush()
            links[footer.group("name")] = footer.group("url")
            continue
        if about:
            if line.startswith("## "):
                raise where(number, f"a heading after {ABOUT}: it comes last", line)
            about.append(line)
            continue
        if line == ABOUT:
            flush()
            if current is None:
                raise where(number, f"{ABOUT} before the versions: it comes last", line)
            about.append(line)
            continue
        if line.startswith("## "):
            flush()
            heading = _VERSION.match(line)
            if heading is None:
                raise where(number, "not a version heading (## [Unreleased] or "
                            "## [x.y.z] -- date -- label, with em dashes)", line)
            if heading.group("name") != "Unreleased" and heading.group("date") is None:
                raise where(number, "a released version needs its date and label", line)
            if heading.group("name") == "Unreleased" and heading.group("date") is not None:
                raise where(number, "[Unreleased] carries no date", line)
            if not versions and heading.group("name") != "Unreleased":
                raise where(number, "the first version heading must be ## [Unreleased]", line)
            if heading.group("name") in names:
                raise where(number, f"a second version heading for [{heading.group('name')}]",
                            line)
            names.add(heading.group("name"))
            if current is not None and current.name != "Unreleased" and not _descends(
                    current.name, heading.group("name")):
                raise where(number, f"[{heading.group('name')}] after [{current.name}]: newest"
                            f" first", line)
            current = Version(number, heading.group("name"), heading.group("date"),
                              heading.group("label"))
            versions.append(current)
            in_category = False
            continue
        if current is None:
            header.append(line)
            continue
        if line.startswith("### "):
            flush()
            if line != CATEGORY:
                raise where(number, f"the only category is {CATEGORY}", line)
            if current.categories:
                raise where(number, f"a second {CATEGORY} under [{current.name}]", line)
            current.categories += 1
            in_category = True
            continue
        if line.startswith("- "):
            flush()
            if not in_category:
                raise where(number, f"an entry outside {CATEGORY}", line)
            pending, pending_line = [line], number
            continue
        if line.strip() == "":  # before the continuation test: two spaces alone are a blank
            flush()
            continue
        if pending and line.startswith("  "):
            pending.append(line)
            continue
        if line.startswith("#"):
            flush()
            raise where(number, "a heading that is neither a version nor the category", line)
        flush()
        raise where(number, "prose where an entry should be", line)
    flush()
    if not versions:
        raise Defect(f"{name}: no ## [Unreleased] heading")
    for version in versions:
        if version.name != "Unreleased" and not version.entries:
            raise where(version.line, f"a released version with no entries: [{version.name}]")
    return Changelog(chr(10).join(header).strip(), versions, chr(10).join(about).strip(),
                     links)


# -- rendering ---------------------------------------------------------------------------------

_STYLE = """
:root { color-scheme: light dark; --fg: #1b1b1b; --bg: #fdfdfb; --muted: #5a5a5a;
        --rule: #d9d9d4; --contract-bg: #e3edff; --contract-fg: #10346b;
        --internal-bg: #ececec; --internal-fg: #3a3a3a; --link: #0b57a8; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e6e6e2; --bg: #17181a; --muted: #a9a9a4; --rule: #33363a;
          --contract-bg: #1f3a66; --contract-fg: #d6e4ff; --internal-bg: #2c2f33;
          --internal-fg: #d8d8d4; --link: #7fb2ff; } }
body { margin: 0 auto; padding: 1.5rem 1rem 4rem; max-width: 46rem; color: var(--fg);
       background: var(--bg); font: 16px/1.55 system-ui, -apple-system, "Segoe UI", Roboto,
       Ubuntu, sans-serif; }
a { color: var(--link); }
code { font-family: ui-monospace, "Cascadia Mono", Consolas, Menlo, monospace;
       font-size: .92em; }
h1 { font-size: 1.9rem; margin: 0 0 1rem; }
h2 { font-size: 1.35rem; margin: 2.5rem 0 .75rem; padding-top: 1rem;
     border-top: 1px solid var(--rule); }
nav.toc ul { list-style: none; padding: 0; margin: 0 0 1rem; }
nav.toc li { display: inline-block; margin: 0 1rem .25rem 0; }
ul.entries { list-style: none; padding: 0; }
ul.entries > li { margin: 0 0 1.25rem; padding-left: 0; }
ul.entries p { margin: 0; }
.tag { display: inline-block; font: .8em/1.5 ui-monospace, Consolas, monospace;
       padding: 0 .45em; border-radius: .35em; margin-right: .35em; vertical-align: .08em; }
.tag.contract { background: var(--contract-bg); color: var(--contract-fg); }
.tag.internal { background: var(--internal-bg); color: var(--internal-fg); }
.meta { color: var(--muted); font-size: .9em; }
""".strip()


def _escape_outside_code(text: str) -> str:
    """``<`` and ``>`` outside code spans become entities: the changelog carries no HTML, and
    ``markdown`` would otherwise pass ``<pid>``-shaped text through as a tag."""
    out: list[str] = []
    position = 0
    for span in _CODE_SPAN.finditer(text):
        out.append(text[position:span.start()].replace("<", "&lt;").replace(">", "&gt;"))
        out.append(span.group(0))
        position = span.end()
    out.append(text[position:].replace("<", "&lt;").replace(">", "&gt;"))
    return "".join(out)


def render(changelog: Changelog) -> str:
    import markdown  # the docs extra; --check must not need it

    definitions = chr(10).join(f"[{name}]: {url}" for name, url in changelog.links.items())

    def md(text: str, refs: bool = False) -> str:
        # the footer's link definitions were parsed out of the text; a reference in the
        # header or the About section still needs them to resolve
        source = text + chr(10) + chr(10) + definitions if refs and definitions else text
        return markdown.markdown(_escape_outside_code(source))

    parts: list[str] = [
        "<!doctype html>", '<html lang="en">', "<head>", '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{html.escape(TITLE)}</title>", f"<style>{_STYLE}</style>", "</head>", "<body>",
        "<header>", md(changelog.header, refs=True), "</header>",
        '<nav class="toc"><ul>',
    ]
    for version in changelog.versions:
        parts.append(f'<li><a href="#{version.anchor}">{html.escape(version.heading)}</a></li>')
    if changelog.about:
        parts.append('<li><a href="#about">About this changelog</a></li>')
    parts.append("</ul></nav>")
    seen: dict[int, int] = {}
    for version in changelog.versions:
        parts.append(f'<section id="{version.anchor}">')
        url = changelog.links.get(version.name)
        heading = html.escape(version.heading)
        parts.append(f'<h2><a href="{html.escape(url)}">{heading}</a></h2>' if url
                     else f"<h2>{heading}</h2>")
        if not version.entries:
            parts.append('<p class="meta">Nothing yet.</p>')
        else:
            parts.append('<ul class="entries">')
            for entry in version.entries:
                first = entry.refs[0]
                seen[first] = seen.get(first, 0) + 1
                anchor = f"issue-{first}" + (f"-{seen[first]}" if seen[first] > 1 else "")
                refs = ", ".join(f"[#{n}]({ISSUES}{n})" for n in entry.refs)
                marker = f"{entry.marker} " if entry.marker else ""
                text = f"{marker}**{entry.headline}** ({refs}). {entry.body}"
                parts.append(f'<li id="{anchor}"><span class="tag {entry.tag}">{entry.tag}'
                             f"</span>{md(text)}</li>")
            parts.append("</ul>")
        parts.append("</section>")
    if changelog.about:
        parts += ['<footer id="about">', md(changelog.about, refs=True), "</footer>"]
    parts += ["</body>", "</html>", ""]
    return chr(10).join(parts)


# -- command line ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render CHANGELOG.md to site/index.html, "
                                     "checking its structure on the way.")
    parser.add_argument("--check", action="store_true",
                        help="parse and validate only; write nothing, import nothing extra")
    parser.add_argument("--changelog", default=str(CHANGELOG), metavar="PATH")
    parser.add_argument("--out", default=str(SITE), metavar="DIR",
                        help="the directory that receives index.html (default: site/)")
    args = parser.parse_args(argv)
    try:
        with open(args.changelog, encoding="utf-8-sig") as handle:  # a BOM is not text
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:  # a UnicodeDecodeError's text is ASCII
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        print(f"render_changelog: cannot read {args.changelog}: {reason} "
              f"(exit {EXIT_COULD_NOT_CHECK}: could not check, not a pass)")
        return EXIT_COULD_NOT_CHECK
    try:
        changelog = parse(text, Path(args.changelog).name)
    except Defect as exc:
        print(f"render_changelog: {exc}")
        return EXIT_DEFECT
    count = sum(len(v.entries) for v in changelog.versions)
    if args.check:
        print(f"render_changelog: {len(changelog.versions)} version(s), {count} entries, "
              f"structure sound")
        return EXIT_OK
    out = Path(args.out)
    page = out / "index.html"
    try:
        out.mkdir(parents=True, exist_ok=True)
        with open(page, "w", encoding="utf-8", newline=chr(10)) as handle:
            handle.write(render(changelog))
    except OSError as exc:
        print(f"render_changelog: cannot write {page}: {exc.strerror or exc} "
              f"(exit {EXIT_COULD_NOT_CHECK}: could not check, not a pass)")
        return EXIT_COULD_NOT_CHECK
    print(f"render_changelog: wrote {page} ({len(changelog.versions)} version(s), {count} "
          f"entries)")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
