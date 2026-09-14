"""Reflow markdown prose to a target terminal width, and prove the reflow changed nothing. #206.

This started as a throwaway for #198 and is now in the tree because it has been needed three times
(#198, #206, #207). Its own #198 docstring argued that a reflow runs about once, so a general tool
would be a mechanism nobody exercises. That argument expired.

NEVER REWRAPPED, and each for a different reason:
  * fenced code blocks -- wrapping a command breaks copy-paste of things the owner runs on the box
  * table rows         -- breaks rendering, and CHECK 1 is blind to it because every word survives
  * headings           -- a wrapped heading stops being a heading
  * long single tokens -- URLs, paths and pytest node ids are left over-length rather than severed

THREE CHECKS, and each exists because the one before it cannot see what it catches:

  CHECK 1  the WORDS: normalised-whitespace equality per paragraph, quote markers stripped first.
           Catches a dropped or duplicated word, two paragraphs merging, and a marker left
           stranded mid-sentence. Blind to a wrapped table row and to a marker cleanly lost.
  CHECK 2  the PROTECTED LINES: every table row and fenced-block line byte-identical.
  CHECK 3  the STRUCTURE: the set of quote depths per paragraph. Owns the one case the other two
           cannot express, which is the reason this file is in the tree.

WARNING: WHY BLOCKQUOTES NEEDED A CHECK OF THEIR OWN. Run without blockquote handling over
`.claude/skills/list/SKILL.md`, the #198 version of this tool turned an eight-line quote into one
quoted line followed by seven lines of prose with the old `>` characters sitting mid-sentence, and
reported OK. Its CHECK 1 compared RAW lines, so it asked whether the same tokens appeared in the
same order -- and `>` is a token, still present, in the wrong place, meaning something else.
Measured across six files: 16 quoted lines collapsed to 3, silently.

WARNING: MAKING CHECK 1 STRIP THE MARKERS WAS NOT OPTIONAL, and it is not merely a tightening.
Wrapping a quoted paragraph correctly ADDS a `> ` to every continuation line it creates, so a
marker-inclusive CHECK 1 fails on this tool's own correct output. Measured, by writing it the raw
way first. Stripping happens to close the stranded-marker half of the blockquote problem as a side
effect -- but a marker cleanly LOST leaves the words identical, and only CHECK 3 sees that.

WARNING: WIDTH IS COUNTED IN TERMINAL COLUMNS, NOT CHARACTERS. This project's prose is emoji-dense
and `U+2B50` is East Asian Wide -- `len()` says 1 and the terminal spends 2. Wrapping on `len()`
puts a star-bearing line at 81 columns in the terminal it was wrapped for.

NOTE: comments here stay ASCII. See CLAUDE.md on bandit and cp1252.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

WIDTH = 80

_FENCE = re.compile(r"^\s*(?:```|~~~)")
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_LIST = re.compile(r"^(\s*)([-*+]\s+|\d+[.)]\s+)(.*)$")
_QUOTE = re.compile(r"^(\s*(?:>\s?)+)(.*)$")

# U+FE0F, the emoji variation selector, written as an ESCAPE SEQUENCE so this file stays ASCII
# while the value is unchanged -- Python resolves it at parse time, and CLAUDE.md requires
# deliberate non-ASCII data to be spelled this way because bandit dies with UnicodeEncodeError
# when it prints a finding whose code context is not ASCII.
_VS16 = "\ufe0f"


def columns(text: str) -> int:
    """Width in terminal columns. East Asian W/F count 2, as does a VS16-presented emoji."""
    total = 0
    previous = ""
    for char in text:
        if char == _VS16:
            # The preceding character renders as an emoji: bump it from one column to two.
            if previous and unicodedata.east_asian_width(previous) not in ("W", "F"):
                total += 1
            previous = char
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        previous = char
    return total


def _is_table(line: str) -> bool:
    """A real table row opens AND closes with a pipe.

    `startswith("|")` alone is wrong and CHECK 2 caught it: this repo's prose wraps
    `HMAC(install_secret, event_id || question_id)` such that a continuation line begins `||`.
    Treating that as a table row desynchronised the fence tracker, which is damage the paragraph
    check cannot see because every word still survives.
    """
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 1


def _is_heading(line: str) -> bool:
    """A heading is one to six hashes followed by WHITESPACE.

    `startswith("#")` alone is wrong, and it left a 100-column line unwrapped: this project cites
    issues as `#184`, so `#184's stated rule (...)` opened a paragraph and was emitted verbatim as
    though it were a heading. Neither CHECK 1 nor CHECK 2 sees this -- both still pass, because no
    word changed and no table moved. It was found by reading what remained over the target width,
    which is the only reason `main` prints that number rather than trusting the checks alone.
    """
    return bool(_HEADING.match(line.lstrip()))


def _wrap(prefix: str, hang: str, text: str, width: int = WIDTH) -> list[str]:
    """Greedy wrap on COLUMNS. A token wider than the line is emitted long, never split."""
    if not text.strip():
        return [prefix.rstrip()]
    lines: list[str] = []
    current = prefix
    used = columns(prefix)
    first = True
    for word in text.split():
        step = columns(word) + (0 if first else 1)
        if not first and used + step > width:
            lines.append(current.rstrip())
            current = hang + word
            used = columns(hang) + columns(word)
            continue
        current += ("" if first else " ") + word
        used += step
        first = False
    lines.append(current.rstrip())
    return lines


def _reflow_block(lines: list[str], width: int) -> list[str]:
    """Reflow one block of markdown that carries no blockquote prefix of its own."""
    out: list[str] = []
    fenced = False
    pending: tuple[str, str, list[str]] | None = None

    def flush() -> None:
        nonlocal pending
        if pending is not None:
            prefix, hang, words = pending
            out.extend(_wrap(prefix, hang, " ".join(words), width))
            pending = None

    for line in lines:
        stripped = line.strip()
        if _FENCE.match(line):
            flush()
            fenced = not fenced
            out.append(line)
            continue
        if fenced or _is_table(line) or _is_heading(line):
            flush()
            out.append(line)
            continue
        if not stripped:
            flush()
            out.append(line)
            continue

        match = _LIST.match(line)
        if match:
            flush()
            indent, marker, rest = match.groups()
            pending = (indent + marker, indent + " " * len(marker), [rest.strip()])
            continue
        if pending is not None:
            pending[2].append(stripped)
            continue
        indent = line[: len(line) - len(line.lstrip())]
        pending = (indent, indent, [stripped])

    flush()
    return out


def _quote_prefix(line: str) -> str:
    """The leading `>` run, normalised to `> ` per level, or `` if the line is not quoted."""
    match = _QUOTE.match(line)
    if not match:
        return ""
    return "> " * match.group(1).count(">")


def reflow(source: str) -> str:
    """Reflow prose, blockquotes included, leaving fences, tables and headings untouched.

    Blockquotes are handled by RECURSION rather than by a special case: strip the `> ` prefix from
    a run of quoted lines, reflow the body at a narrowed width, then put the prefix back on every
    line it produced. That way a list, a fence or a table inside a quote gets the same treatment it
    would get outside one, and there is only one copy of the wrapping rules.
    """
    out: list[str] = []
    plain: list[str] = []
    quoted: list[str] = []
    prefix = ""
    fenced = False

    def flush_plain() -> None:
        if plain:
            out.extend(_reflow_block(plain, WIDTH))
            plain.clear()

    def flush_quote() -> None:
        nonlocal prefix
        if quoted:
            body = _reflow_block(quoted, max(WIDTH - columns(prefix), 20))
            out.extend((prefix + line).rstrip() for line in body)
            quoted.clear()
            prefix = ""

    for line in source.splitlines():
        if _FENCE.match(line) and not _quote_prefix(line):
            fenced = not fenced
        here = "" if fenced else _quote_prefix(line)
        if here:
            if here != prefix:
                flush_quote()
                flush_plain()
                prefix = here
            match = _QUOTE.match(line)
            quoted.append(match.group(2) if match else line)
            continue
        flush_quote()
        plain.append(line)

    flush_quote()
    flush_plain()
    return "\n".join(out) + ("\n" if source.endswith("\n") else "")


# -- the three checks ------------------------------------------------------------------------------


def _strip_quote(line: str) -> str:
    match = _QUOTE.match(line)
    return (match.group(2) if match else line).strip()


def paragraphs(text: str) -> list[str]:
    """CHECK 1: normalised paragraphs -- runs of non-blank lines, whitespace collapsed.

    WARNING: THE QUOTE MARKERS ARE STRIPPED FIRST, and comparing raw lines instead is not a
    stricter version of this -- it is a BROKEN one. Wrapping a quoted paragraph correctly ADDS a
    `> ` to every continuation line it creates, so a marker-inclusive comparison fails on the
    tool's own correct output. That is not a hypothesis: this check was written the raw way and
    reddened the first time it met a blockquote that needed wrapping.

    Stripping also closes one half of the blind spot that CHECK 3 exists for. A reflow that leaves
    `>` characters stranded mid-sentence now shows up here, because a stray marker lands in the
    BODY on one side and not the other. What it still cannot see is a marker cleanly LOST -- the
    words are then identical and only the structure differs. That is CHECK 3.
    """
    out: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip():
            current.append(_strip_quote(line))
        elif current:
            out.append(re.sub(r"\s+", " ", " ".join(current)).strip())
            current = []
    if current:
        out.append(re.sub(r"\s+", " ", " ".join(current)).strip())
    return out


def protected(text: str) -> list[str]:
    """CHECK 2: every table row and fenced-block line, in order, verbatim."""
    out: list[str] = []
    fenced = False
    for line in text.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            out.append(line)
            continue
        if fenced or _is_table(line):
            out.append(line)
    return out


def quote_shape(text: str) -> list[tuple[int, ...]]:
    """CHECK 3: the set of quote depths present in each paragraph.

    THE CASE THIS EXISTS FOR, and the only one neither check above can express: a continuation
    line that cleanly LOSES its `> `. The words are then byte-identical after CHECK 1 strips the
    markers, and no table or fence moved, so CHECK 1 and CHECK 2 both pass -- while the document
    renders as one quoted line followed by unquoted prose. Measured on `.claude/skills/list/`:
    eight quoted lines became one.

    Deliberately depths ONLY, with the words left to CHECK 1. Two checks that both compare the
    text would fail together and say nothing about which property broke; this way the failure
    names itself.

    Depths are a SORTED SET, never a per-line tuple. A reflow legitimately changes how many lines
    a paragraph occupies, so anything length-dependent would fire on every correct run. A quote
    that loses its continuation markers goes from `(1,)` to `(0, 1)` however long it is.
    """
    out: list[tuple[int, ...]] = []
    depths: set[int] = set()
    for line in text.splitlines():
        if not line.strip():
            if depths:
                out.append(tuple(sorted(depths)))
                depths = set()
            continue
        match = _QUOTE.match(line)
        depths.add(match.group(1).count(">") if match else 0)
    if depths:
        out.append(tuple(sorted(depths)))
    return out


def check(before: str, after: str) -> list[str]:
    """Every check that failed, named. Empty means the reflow provably changed only line breaks."""
    failures = []
    if paragraphs(before) != paragraphs(after):
        failures.append("CHECK 1 (paragraph text)")
    if protected(before) != protected(after):
        failures.append("CHECK 2 (tables and fences)")
    if quote_shape(before) != quote_shape(after):
        failures.append("CHECK 3 (blockquote structure)")
    return failures


def _too_wide(text: str) -> list[tuple[int, int]]:
    """Lines still over WIDTH columns, excluding table rows.

    Printed by `main`, never asserted -- see the note at its call site.
    """
    return [
        (number, columns(line))
        for number, line in enumerate(text.splitlines(), 1)
        if columns(line) > WIDTH and not _is_table(line)
    ]


def _wide_lines(text: str) -> list[tuple[int, int]]:
    """Over-width lines that are candidates at all: not a table, a fence or a heading.

    Those three are never rewrapped, so counting them would make any stage built on this
    permanently red on correct files -- and the only way to quiet that is an exemption list, which
    grows until it means nothing.
    """
    hits: list[tuple[int, int]] = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or _is_table(line) or _is_heading(line):
            continue
        if columns(line) > WIDTH:
            hits.append((number, columns(line)))
    return hits


def overwide(text: str) -> list[tuple[int, int]]:
    """Over-width lines that REFLOWING WOULD FIX. The gate stage's rule (#207).

    WARNING: THIS ASKS THE REWRITER RATHER THAN RE-DERIVING ITS RULES, and the first two drafts did
    the latter and were both wrong in the same direction -- reporting "wrappable" on lines the
    rewriter had already done everything it could with:

      * a blockquote: an 85-column pytest node id in `docs/O2-APP-ATTEST.md` is 87 with its `> `,
        and comparing against the bare body missed the prefix.
      * a list item: a long markdown link in `CLAUDE.md` is 86 alone and 88 behind its `- `.

    Both are the same defect -- a second copy of the prefix rules, disagreeing with the first. A
    check that contradicts the tool it fronts is worse than no check: it is a standing red nobody
    can clear, and the pressure is then to add exemptions until it says nothing. So the rule is
    simply: reflow the text, and if the set of over-width lines does not shrink, the file is
    already as narrow as this tool can make it.
    """
    before = _wide_lines(text)
    after = _wide_lines(reflow(text))
    if sorted(width for _, width in before) == sorted(width for _, width in after):
        return []
    return before


# Anti-vacuity floor: fewer markdown files than this read = SCAN FAILED, exit 2. A stage handed
# a bad glob reads nothing and reports a clean sweep with total confidence. 1 is the emptiness
# test; raise it once the tree has grown so a broken glob cannot pass by finding one file.
_FLOOR = 1

# Files this stage deliberately does NOT police, by repo-relative POSIX path. Empty until a
# reason exists: the precedent was a contract document hashed by a lock, where reflowing it
# would have been a spurious contract change. Whatever goes here is printed as NOT POLICED on
# every run -- a silent exclusion reads as coverage.
_NOT_POLICED: tuple[str, ...] = ()


def _check_widths(paths: list[Path]) -> int:
    """The gate stage (#207): refuse markdown that has drifted wide again.

    WARNING: THE FLOOR IS NOT DECORATION. A stage handed a bad glob reads nothing and reports a
    clean sweep with total confidence -- every tree-walking check needs the same guard. The kit
    ships `_FLOOR = 1` (the emptiness test); raise it once the tree has grown, so a broken glob
    cannot pass by finding one file.
    """
    offenders: list[str] = []
    scanned = 0
    skipped: list[str] = []
    unreadable: list[str] = []
    for path in paths:
        name = path.as_posix()
        if name in _NOT_POLICED:
            skipped.append(name)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # WARNING: this used to be a bare `continue` (#303). The count stayed honest, because
            # `scanned` is incremented below rather than above -- but the DROP was silent, and the
            # rule against that is stated eleven lines down for the deliberate exclusion. The
            # deliberate hole was announced and the accidental one was not, in this same function.
            unreadable.append(f"{name}: {type(exc).__name__}")
            continue
        scanned += 1
        offenders.extend(f"{name}:{n}: {w} columns" for n, w in overwide(text))

    # Said out loud on every run. A silent exclusion reads as coverage, and this one is a real
    # hole in the sweep -- the reader has to know it is there to decide whether it still applies.
    for name in skipped:
        print(f"NOT POLICED: {name} -- see _NOT_POLICED in this file for why.")
    # Same rule, applied to the hole nobody chose.
    for item in unreadable:
        print(f"COULD NOT READ: {item} -- not scanned, so its width is UNKNOWN.")

    if scanned < _FLOOR:
        print(f"SCAN FAILED -- only {scanned} file(s) read (expected at least {_FLOOR}).")
        print("This is NOT a pass: a scan that reads nothing reports clean.")
        return 2
    if offenders:
        print(f"{len(offenders)} markdown line(s) are wider than {WIDTH} columns and wrappable:")
        for line in offenders[:40]:
            print(f"  {line}")
        if len(offenders) > 40:
            print(f"  ... and {len(offenders) - 40} more")
        print("Fix with: python tools/reflow_md.py --write <file> -- for a file whose earlier")
        print("sections were never at width (a long changelog), reflow ONLY the new block and")
        print("splice it back; --write on the whole file rewraps its entire history.")
        return 1
    print(f"{scanned} markdown file(s) scanned; none wider than {WIDTH} columns.")
    return 0


def main(argv: list[str]) -> int:
    write = "--write" in argv
    paths = [Path(a) for a in argv if not a.startswith("--")]
    if not paths:
        print("usage: reflow_md.py [--check | --write] FILE [FILE ...]")
        return 2
    if "--check" in argv:
        return _check_widths(paths)

    failed = False
    for path in paths:
        before = path.read_text(encoding="utf-8")
        after = reflow(before)
        failures = check(before, after)
        over = _too_wide(after)

        status = "FAIL" if failures else "OK  "
        print(f"{status} {path}: {len(before.splitlines())} -> {len(after.splitlines())} lines")
        print(f"       still over {WIDTH} columns (non-table): {len(over)}")
        if failures:
            failed = True
            print(f"       failed: {', '.join(failures)}")
            for a, b in zip(paragraphs(before), paragraphs(after), strict=False):
                if a != b:
                    print(f"       first paragraph diff\n         before: {a[:150]}")
                    print(f"         after:  {b[:150]}")
                    break
            shape_before, shape_after = quote_shape(before), quote_shape(after)
            for index, (c, d) in enumerate(zip(shape_before, shape_after, strict=False)):
                if c != d:
                    print(f"       blockquote depths changed at paragraph {index + 1}")
                    print(f"         before: {c}\n         after:  {d}")
                    break
            continue
        # WARNING: what remains over the target is PRINTED, not asserted. Both bugs recorded in
        # this file's docstring were found by reading this number and by neither check; a hard
        # failure here would only teach the next reader to raise the threshold.
        for number, width in over:
            print(f"       line {number}: {width} columns")
        if write:
            path.write_text(after, encoding="utf-8", newline="\n")
            print("       WRITTEN")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
