"""Matching and context: the lines of a file become the matches an alert reports.

Pure functions over the ``Line`` stream a source yields (``logalert.cursor``,
``logalert.rotation``); the only I/O is the ``before`` hook that supplies context from before
the saved offset. The rules (decided in issue #9):

  * Every pattern is tried in config order: a literal as a substring (``lower()`` on both
    sides for ``ipatterns``, the same simple folding ``re.IGNORECASE`` applies), a regex with
    ``search`` (``iregex`` compiled case-insensitive by the loader). The recorded pattern is
    the FIRST that matched. A tag sets the priority for the lines its pattern matches and an
    untagged pattern carries the section's ``priority``; a line matching several patterns
    takes the HIGHEST of those. With nothing configured a match has no priority: the system
    is off by default.
  * Noise exclusion: a matching line that also matches any ``exclude`` shape is dropped from
    the matches and counted. It stays in the stream, so it can still be CONTEXT for a
    neighbouring match: an exclude means "do not alert on this", not "never show this".
  * Context, ``grep -C`` semantics as measured: ``n`` lines before and after each match;
    windows that overlap or touch merge; a stretch of omitted lines between windows is a
    ``gap`` entry (``--``). Under ``-c 0`` adjacent matches are contiguous and a jump is a gap.
    The lines before the first match of a run may lie before the saved offset: the ``before``
    hook is asked for exactly the missing ones, once.
  * Line numbers are the file's, as ``grep -n`` shows them (the reader counts them); the
    fragments of a cut line share the physical line's number and keep their ``cut`` flag.
    After a rotation the stream spans files (the archive's tail, then the live file): each
    line and entry names the physical file it came from, and a change of file is a gap. A
    NUL-only line the reader skipped is a gap too (a line was omitted), and a pre-offset
    context line over the cap is its head fragment only.
  * A physical line is matched WHOLE (its fragments joined, up to ``MAX_FRAGMENTS`` of
    them), so an anchored regex and a word the cap split behave as on the file; every
    fragment of a matching line is a ``match`` entry. Context never crosses files: a
    change of physical file empties the window, and the ``before`` hook serves only the
    file the stream started in.
  * A report is bounded: after ``cap`` matching lines (the unit of ``max_lines``) and the
    context that follows the last of them nothing more is stored, only counted, so a first
    run from the top of a large log cannot hold the whole log in memory. The priority is
    tracked as the matches arrive, never from the stored list.
"""

import logging
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Literal

from logalert.config import PRIORITIES, Pattern, Priority, Watch
from logalert.cursor import Line

log = logging.getLogger("logalert.match")

_RANK: dict[str, int] = {name: rank for rank, name in enumerate(reversed(PRIORITIES), 1)}

Kind = Literal["match", "context", "gap"]


@dataclass(frozen=True)
class Match:
    """One line an alert reports."""

    file: str  # the configured path
    number: int
    text: str
    cut: bool
    pattern: Pattern  # the first pattern that matched, in config order
    priority: Priority | None  # the effective one; None when the system is off for this line
    path: str = ""  # the physical file the line was read from (an archive after a rotation)


@dataclass(frozen=True)
class Entry:
    """One line of the report for a file, in file order."""

    kind: Kind
    number: int  # 0 for a gap
    text: str
    cut: bool
    path: str = ""  # the physical file (an archive after a rotation); "" for a gap


@dataclass
class FileReport:
    """What one file contributes to a section's alert."""

    file: str
    matches: list[Match] = field(default_factory=list)  # the first ``cap`` of them
    entries: list[Entry] = field(default_factory=list)  # those matches and their context
    matched: int = 0  # every match, stored or not: ``matched - len(matches)`` were not
    excluded: int = 0  # matching lines dropped by an exclude
    lines: int = 0  # lines examined
    priority: Priority | None = None  # the highest among every match, stored or not
    context: int = 0  # the window the entries were built with


def highest(priorities: Iterable[Priority | None]) -> Priority | None:
    """The highest of the priorities, or None when none is set."""
    best: Priority | None = None
    for priority in priorities:
        if priority is not None and (best is None or _RANK[priority] > _RANK[best]):
            best = priority
    return best


def pattern_matches(pattern: Pattern, text: str) -> bool:
    if pattern.compiled is not None:
        return pattern.compiled.search(text) is not None
    if pattern.ignore_case:
        return pattern.text.lower() in text.lower()
    return pattern.text in text


def matching(text: str, patterns: Iterable[Pattern]) -> list[Pattern]:
    """Every pattern that matches, in config order."""
    return [p for p in patterns if pattern_matches(p, text)]


def is_excluded(text: str, excludes: Iterable[Pattern]) -> bool:
    return any(pattern_matches(p, text) for p in excludes)


GAP = Entry("gap", 0, "", False)

Before = Callable[[int], list[Line]]


MAX_FRAGMENTS = 8  # of a physical line that are kept: 16 KB of text; the rest is dropped


def physical(lines: Iterable[Line]) -> Iterator[list[Line]]:
    """The stream grouped by physical line: the fragments of a cut line travel together, so
    the context windows count lines as the file does and a pattern sees the whole line
    (an anchor, or a word the cap split). A line beyond MAX_FRAGMENTS fragments keeps the
    first of them; a report line that long serves nobody, and memory stays bounded."""
    group: list[Line] = []
    for line in lines:
        if group and (line.number, line.path) != (group[0].number, group[0].path):
            yield group
            group = []
        if len(group) < MAX_FRAGMENTS:
            group.append(line)
    if group:
        yield group


def scan(file: str, lines: Iterable[Line], watch: Watch, *, context: int,
         before: Before | None = None, cap: int | None = None) -> FileReport:
    """The matches and the report entries for one file's new lines.

    ``context`` is the section's, or the command line's when the section sets none; ``before``
    supplies up to ``n`` lines from before the stream (the saved offset) when the first match
    comes within ``context`` lines of it, and is called at most once. A physical line matches
    when any of its fragments does, and is excluded when any of its fragments is. ``cap``
    bounds the stored matches (with the context of each); everything is still counted.
    """
    report = FileReport(file=file, context=max(context, 0))
    pending: deque[list[Line]] = deque(maxlen=max(context, 0))
    after = 0
    last = 0  # the number of the last line emitted; 0 before any
    last_path = ""
    emitted = False
    first_path: str | None = None  # the file the stream started in: the hook's file
    current_path: str | None = None
    capped = False  # the cap-th match and its trailing context are in; count, store nothing

    def emit(line: Line, kind: Kind) -> None:
        nonlocal last, last_path, emitted
        # omitted lines, or another physical file (the archive before the live file)
        gap = emitted and (line.number > last + 1 or line.path != last_path)
        last, last_path, emitted = line.number, line.path, True
        if capped:
            return
        if gap:
            report.entries.append(GAP)
        report.entries.append(Entry(kind, line.number, line.text, line.cut, line.path))

    for group in physical(lines):
        if group[0].path != current_path:
            # another physical file (the live file after an archive): context never
            # crosses files, as with grep
            pending.clear()
            after = 0
            current_path = group[0].path
            if first_path is None:
                first_path = current_path
        report.lines += 1
        text = "".join(fragment.text for fragment in group)  # the whole line, for anchors
        found = matching(text, watch.patterns)
        if found and is_excluded(text, watch.excludes):
            report.excluded += 1
            found = []
        if found:
            head = group[0]
            priority = highest(p.priority or watch.priority for p in found)
            report.matched += 1
            report.priority = highest((report.priority, priority))
            if cap is not None and report.matched > cap:
                capped = True
            else:
                report.matches.append(Match(file=file, number=head.number, text=text,
                                            cut=any(f.cut for f in group), pattern=found[0],
                                            priority=priority, path=head.path))
            if (context and not emitted and before is not None
                    and len(pending) < context and current_path == first_path):
                for earlier in before(context - len(pending)):  # the first match only
                    emit(earlier, "context")
            for waiting in pending:
                for earlier in waiting:
                    emit(earlier, "context")
            pending.clear()
            for fragment in group:
                emit(fragment, "match")
            after = context
        elif after > 0:
            for fragment in group:
                emit(fragment, "context")
            after -= 1
        elif context:
            pending.append(group)
    if report.excluded:
        log.debug("[%s] %s: %d matching line(s) dropped by an exclude", watch.name, file,
                  report.excluded)
    return report
