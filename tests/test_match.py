"""Matching and context: patterns, priorities, exclusion, grep -C windows (issue #9).

The pure part runs over hand-built Line streams; the pre-offset context and the line numbers
are then exercised end to end over real files through open_source, on both platforms.
"""

import gzip
import logging
import os
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from logalert.config import Watch, load_config
from logalert.cursor import _CHUNK, LINE_CAP, Line, LogFile, count_newlines, lines_before, open_log
from logalert.match import GAP, Entry, FileReport, highest, matching, pattern_matches, scan
from logalert.rotation import CatchUpSource, open_source
from logalert.state import Cursor, State, load_state

E_ACUTE = chr(0xE9)  # spelled from the code point for the ASCII gate
FILE = "/var/log/router.log"


def make_watch(tmp_path: Path, body: str) -> Watch:
    """A watch built by the real loader, so tags and case rules are the loader's."""
    text = ("[router-disk]\nsubject = Router disk failure\nto = noc@example.net\n"
            f"files = {(tmp_path / 'router.log').as_posix()}\n" + body)
    path = tmp_path / "logalert.conf"
    path.write_text(text, encoding="utf-8", newline="\n")
    return load_config(str(path)).watches[0]


def lines(*texts: str, start: int = 1) -> list[Line]:
    return [Line(text, False, start + i) for i, text in enumerate(texts)]


def rendered(report: FileReport) -> list[str]:
    """grep -n style, for readable assertions."""
    out = []
    for entry in report.entries:
        if entry.kind == "gap":
            out.append("--")
        else:
            out.append(f"{entry.number}{':' if entry.kind == 'match' else '-'}{entry.text}")
    return out


# -- patterns -------------------------------------------------------------------------------------


def test_literals_are_case_sensitive_unless_ipatterns(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    Disk Failure\nipatterns =\n    reboot\n")
    exact, loose = watch.patterns
    assert pattern_matches(exact, "eth0: Disk Failure on sda") is True
    assert pattern_matches(exact, "eth0: disk failure on sda") is False
    assert pattern_matches(loose, "Requesting REBOOT now") is True
    assert pattern_matches(loose, "Requesting reboot now") is True


def test_regex_uses_search_and_iregex_ignores_case(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "regex =\n    fail(ed|ure)\niregex =\n    ^kernel:.*oom\n")
    exact, loose = watch.patterns
    assert pattern_matches(exact, "disk failure") and pattern_matches(exact, "it failed")
    assert not pattern_matches(exact, "FAILED")
    assert pattern_matches(loose, "KERNEL: OOM killer invoked")
    assert not pattern_matches(loose, "user: oom")  # anchored: search, not a substring


def test_the_first_matching_pattern_is_recorded_in_config_order(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    failure\n    disk\n")
    report = scan(FILE, lines("disk failure"), watch, context=0)
    assert [m.pattern.text for m in report.matches] == ["failure"]
    assert matching("disk failure", watch.patterns) == list(watch.patterns)


def test_a_replaced_byte_still_matches_the_rest_of_the_line(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    failure\n")
    line = Line("caf" + chr(0xFFFD) + " disk failure", False, 1)  # what the reader yields
    report = scan(FILE, [line], watch, context=0)
    assert [m.text for m in report.matches] == [line.text]


# -- priority --------------------------------------------------------------------------------------


def test_priority_is_none_when_nothing_is_configured(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    failure\n")
    report = scan(FILE, lines("disk failure"), watch, context=0)
    assert report.matches[0].priority is None and report.priority is None


def test_priority_section_default_and_per_pattern_override(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "priority = low\npatterns =\n    failure\n    [high] on fire\n")
    report = scan(FILE, lines("disk failure", "rack on fire"), watch, context=0)
    assert [m.priority for m in report.matches] == ["low", "high"]
    assert report.priority == "high"


def test_the_highest_tag_among_matching_patterns_wins(tmp_path: Path) -> None:
    # a [high] pattern listed after a [low] one must not be demoted by config order
    watch = make_watch(tmp_path, "patterns =\n    [low] warning\n    [high] disk\n    plain\n")
    report = scan(FILE, lines("disk warning", "plain warning", "plain"), watch, context=0)
    assert [(m.pattern.text, m.priority) for m in report.matches] == [
        ("warning", "high"), ("warning", "low"), ("plain", None),
    ]


def test_highest() -> None:
    assert highest([]) is None
    assert highest([None, None]) is None
    assert highest(["low", None, "medium"]) == "medium"
    assert highest(["medium", "high", "low"]) == "high"


# -- exclusion -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "value", "dropped", "kept"),
    [
        ("exclude", "expected", "disk failure (expected)", "disk failure (Expected)"),
        ("iexclude", "expected", "disk failure (Expected)", "disk failure (surprise)"),
        ("exclude_regex", r"\(test\d+\)", "disk failure (test7)", "disk failure (TEST7)"),
        ("iexclude_regex", r"\(test\d+\)", "disk failure (TEST7)", "disk failure (test)"),
    ],
)
def test_each_exclude_shape_drops_and_counts(
    tmp_path: Path, key: str, value: str, dropped: str, kept: str
) -> None:
    watch = make_watch(tmp_path, f"patterns =\n    disk failure\n{key} =\n    {value}\n")
    report = scan(FILE, lines(dropped, kept, "nothing here"), watch, context=0)
    assert [m.text for m in report.matches] == [kept]
    assert report.excluded == 1 and report.lines == 3


def test_an_excluded_line_still_serves_as_context(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    failure\nexclude =\n    harmless\n")
    stream = lines("harmless failure", "real failure", "harmless failure")
    report = scan(FILE, stream, watch, context=1)
    assert [m.number for m in report.matches] == [2] and report.excluded == 2
    assert rendered(report) == ["1-harmless failure", "2:real failure", "3-harmless failure"]


# -- context, grep -C semantics as measured --------------------------------------------------------


def numbered_fixture(tmp_path: Path) -> tuple[Watch, list[Line]]:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    texts = [f"line {i}" for i in range(1, 21)]
    for i in (3, 8, 9, 17):
        texts[i - 1] += " MATCH"
    return watch, lines(*texts)


def test_context_zero_is_matches_only_with_gaps_between_them(tmp_path: Path) -> None:
    watch, stream = numbered_fixture(tmp_path)
    report = scan(FILE, stream, watch, context=0)
    assert rendered(report) == ["3:line 3 MATCH", "--", "8:line 8 MATCH", "9:line 9 MATCH", "--",
                                "17:line 17 MATCH"]


def test_context_two_merges_touching_windows_without_a_separator(tmp_path: Path) -> None:
    # lines 1-5 and 6-11 touch: grep prints them contiguous (measured); 12-14 are omitted
    watch, stream = numbered_fixture(tmp_path)
    report = scan(FILE, stream, watch, context=2)
    assert rendered(report) == [
        "1-line 1", "2-line 2", "3:line 3 MATCH", "4-line 4", "5-line 5", "6-line 6", "7-line 7",
        "8:line 8 MATCH", "9:line 9 MATCH", "10-line 10", "11-line 11", "--", "15-line 15",
        "16-line 16", "17:line 17 MATCH", "18-line 18", "19-line 19",
    ]
    assert report.entries[11] is GAP


def test_context_one_separates_windows_with_an_omitted_line(tmp_path: Path) -> None:
    watch, stream = numbered_fixture(tmp_path)
    report = scan(FILE, stream, watch, context=1)
    assert rendered(report) == ["2-line 2", "3:line 3 MATCH", "4-line 4", "--", "7-line 7",
                                "8:line 8 MATCH", "9:line 9 MATCH", "10-line 10", "--",
                                "16-line 16", "17:line 17 MATCH", "18-line 18"]


def test_context_at_both_ends_of_the_stream(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    stream = lines("MATCH first", "b", "c", "d", "e", "MATCH last")
    report = scan(FILE, stream, watch, context=3)
    assert rendered(report) == ["1:MATCH first", "2-b", "3-c", "4-d", "5-e", "6:MATCH last"]


def test_overlapping_windows_merge(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    stream = lines("a", "MATCH", "b", "MATCH", "c", "d", "e", "f")
    report = scan(FILE, stream, watch, context=2)
    assert rendered(report) == ["1-a", "2:MATCH", "3-b", "4:MATCH", "5-c", "6-d"]


def test_fragments_of_a_cut_line_share_the_number_and_keep_the_flag(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    stream = [Line("x", False, 3), Line("head MATCH", True, 4), Line("tail", False, 4),
              Line("after", False, 5), Line("beyond", False, 6)]
    report = scan(FILE, stream, watch, context=1)
    # the line is matched whole and every fragment of it is a match entry; the after-window
    # counts physical lines, so line 5 is the one context line after it
    assert [(e.kind, e.number, e.cut) for e in report.entries] == [
        ("context", 3, False), ("match", 4, True), ("match", 4, False),
        ("context", 5, False),
    ]
    assert report.matches[0].cut is True and report.lines == 4
    assert report.matches[0].text == "head MATCHtail"  # the whole line, joined
    # a match in the tail fragment only is still one match, on the physical line
    stream = [Line("head", True, 4), Line("tail MATCH", False, 4), Line("after", False, 5)]
    report = scan(FILE, stream, watch, context=0)
    assert [(e.kind, e.number) for e in report.entries] == [("match", 4), ("match", 4)]
    assert len(report.matches) == 1 and report.matches[0].cut is True


def test_a_line_is_matched_whole_across_the_cut(tmp_path: Path) -> None:
    # an anchored regex sees the line start, and a word the cap split is still found
    watch = make_watch(tmp_path, "regex =\n    ^kernel: .*oom\npatterns =\n    MATCH\n")
    stream = [Line("x" * 2000, True, 7), Line("kernel: oom killer", False, 7)]
    assert scan(FILE, stream, watch, context=0).matches == []  # the tail is not a line start
    stream = [Line("kernel: " + "x" * 1992, True, 8), Line("oom", False, 8)]
    assert [m.number for m in scan(FILE, stream, watch, context=0).matches] == [8]
    stream = [Line("y" * 1998 + "MA", True, 9), Line("TCH here", False, 9)]
    report = scan(FILE, stream, watch, context=0)
    assert [(m.number, m.pattern.text) for m in report.matches] == [(9, "MATCH")]
    # and an exclude sees the whole line the same way
    watch = make_watch(tmp_path, "patterns =\n    MATCH\nexclude_regex =\n    ^quiet\n")
    stream = [Line("quiet " + "z" * 1994, True, 10), Line("MATCH", False, 10)]
    report = scan(FILE, stream, watch, context=0)
    assert report.matches == [] and report.excluded == 1


def test_a_line_beyond_max_fragments_keeps_its_head(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    stream = [Line(f"f{i}", True, 3) for i in range(20)] + [Line("MATCH", False, 3)]
    report = scan(FILE, stream, watch, context=0)
    assert report.matches == [] and report.lines == 1  # the tail was dropped with the rest
    stream = [Line("MATCH f0", True, 3)] + [Line(f"f{i}", True, 3) for i in range(1, 20)]
    report = scan(FILE, stream, watch, context=0)
    assert len(report.matches) == 1 and len(report.entries) == 8


def test_context_never_crosses_files(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    archive = [Line(t, False, i + 4, "router.log.1") for i, t in enumerate(["a4", "a5"])]
    live = [Line(t, False, i + 1, "router.log") for i, t in enumerate(["MATCH", "l2", "l3"])]
    report = scan(FILE, archive + live, watch, context=2)
    assert rendered(report) == ["1:MATCH", "2-l2", "3-l3"]  # no archive lines before it
    archive = [Line("a5 MATCH", False, 5, "router.log.1")]
    live = [Line("l1", False, 1, "router.log"), Line("l2", False, 2, "router.log")]
    report = scan(FILE, archive + live, watch, context=2)
    assert rendered(report) == ["5:a5 MATCH"]  # no live lines after it


def test_before_hook_serves_only_the_stream_first_file(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    calls: list[int] = []

    def before(n: int) -> list[Line]:
        calls.append(n)
        return []

    archive = [Line("a4", False, 4, "router.log.1")]
    live = [Line("MATCH", False, 1, "router.log")]
    scan(FILE, archive + live, watch, context=2, before=before)
    assert calls == []  # the first match is in the second file
    scan(FILE, [Line("MATCH", False, 4, "router.log.1")] + live, watch, context=2,
         before=before)
    assert calls == [2]


def test_cap_bounds_what_is_stored_but_not_what_is_counted(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "priority = low\npatterns =\n    MATCH\n    [high] LAST\n")
    stream = lines(*(["MATCH"] * 5 + ["x", "LAST MATCH"]))
    report = scan(FILE, stream, watch, context=1, cap=3)
    assert len(report.matches) == 3 and len(report.entries) == 3
    assert report.matched == 6  # three more matches were counted, not stored
    assert report.priority == "high"  # from a match that was not stored
    assert report.lines == 7


def test_cap_counts_matching_lines_and_keeps_the_last_ones_context(tmp_path: Path) -> None:
    # the unit of max_lines is matching lines: with context, the cap-th match keeps its
    # trailing context and the next match is where storing stops (an entry-count cap of
    # the same number would have cut the context of the second match)
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    stream = lines("MATCH 1", "a", "b", "MATCH 2", "c", "d", "MATCH 3", "e", "MATCH 4")
    report = scan(FILE, stream, watch, context=3, cap=2)
    assert [e.text for e in report.entries] == ["MATCH 1", "a", "b", "MATCH 2", "c", "d"]
    assert [m.text for m in report.matches] == ["MATCH 1", "MATCH 2"]
    assert report.matched == 4


def test_section_default_is_each_untagged_patterns_priority(tmp_path: Path) -> None:
    # a line matching an untagged pattern (high, the section's) and a [low] one is high:
    # the tag sets the priority of ITS pattern's matches, and the highest wins
    watch = make_watch(tmp_path, "priority = high\npatterns =\n    failure\n    [low] warning\n")
    report = scan(FILE, lines("disk failure warning", "warning only"), watch, context=0)
    assert [m.priority for m in report.matches] == ["high", "low"]


def test_ipatterns_fold_case_like_iregex(tmp_path: Path) -> None:
    sharp_s = chr(0xDF)  # a character casefold() would expand and IGNORECASE would not
    watch = make_watch(tmp_path, "ipatterns =\n    strasse\niregex =\n    strasse\n")
    literal, regex = watch.patterns
    text = "stra" + sharp_s + "e"
    assert pattern_matches(literal, text) == pattern_matches(regex, text) is False
    assert pattern_matches(literal, "STRASSE") and pattern_matches(regex, "STRASSE")


def test_context_before_tolerates_a_vanished_or_replaced_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\n")
    source = open_source("s", str(path), None)  # at the end: start 4
    assert isinstance(source, LogFile)
    real_open = open_log
    calls = 0

    def gone(target: str, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise FileNotFoundError(2, "No such file", target)  # gone between open and hook

    with source:
        monkeypatch.setattr("logalert.cursor.open_log", gone)
        caplog.set_level(logging.WARNING, logger="logalert")
        assert source.context_before(2) == [] and calls == 1
        assert any("context before the saved position could not be read" in r.getMessage()
                   for r in caplog.records)
        monkeypatch.setattr("logalert.cursor.open_log", real_open)
        # replaced by another file under the same name: not this file's lines
        monkeypatch.setattr("logalert.cursor.os.fstat", lambda fd: os.stat(tmp_path))
        assert source.context_before(2) == []


def test_context_before_after_an_empty_archive_tail_is_nothing(tmp_path: Path) -> None:
    # the archive was fully read last run: the stream starts in the live file, at 0
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = source.cursor()
    (tmp_path / "router.log.1").write_bytes(path.read_bytes())
    path.write_bytes(b"live MATCH\nl2\n")
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    again = open_source("s", str(path), cursor)
    assert isinstance(again, CatchUpSource)
    with again:
        report = scan(str(path), again.lines(), watch, context=2, before=again.context_before)
    assert rendered(report) == ["1:live MATCH", "2-l2"]


def test_lines_before_keeps_a_complete_line_at_the_window_edge(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    limit = 2 * (LINE_CAP + 1) + 65536  # the window for n=2, as lines_before sizes it
    one = b"x" * (limit // 2 - 1) + b"\n"  # two lines fill the window exactly
    path.write_bytes(one * 3)
    with open_log(str(path)) as handle:
        got = lines_before(handle, len(one) * 3, 2, 3)
    assert [x.number for x in got] == [2, 3]  # the window starts on a line start: kept
    with open_log(str(path)) as handle:
        got = lines_before(handle, len(one) * 3, 3, 3)  # a wider window, mid-line start
    assert [x.number for x in got] == [2, 3]  # line 1 is cut by the window's edge: dropped



def test_before_hook_supplies_only_the_missing_lines_once(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    asked: list[int] = []

    def before(n: int) -> list[Line]:
        asked.append(n)
        return lines("older 1", "older 2", start=100 - n + 1)[:n]

    stream = lines("new 1", "MATCH", "MATCH", "new 4", "new 5", "new 6", "MATCH", start=101)
    report = scan(FILE, stream, watch, context=3, before=before)
    assert asked == [2]  # three of context, one already pending: two from before the offset
    assert rendered(report)[:5] == ["99-older 1", "100-older 2", "101-new 1", "102:MATCH",
                                    "103:MATCH"]


def test_before_hook_is_not_asked_when_the_window_is_already_full_or_context_is_zero(
    tmp_path: Path,
) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    calls: list[int] = []

    def before(n: int) -> list[Line]:
        calls.append(n)
        return []

    stream = lines("a", "b", "c", "MATCH", start=50)
    scan(FILE, stream, watch, context=2, before=before)
    scan(FILE, lines("MATCH", start=50), watch, context=0, before=before)
    scan(FILE, lines("a", "b"), watch, context=2, before=before)  # no match: never asked
    assert calls == []


def test_report_counts_lines_and_entries_are_in_order(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    report = scan(FILE, lines("MATCH", "x", "MATCH"), watch, context=0)
    assert report.lines == 3 and len(report.matches) == 2
    assert report.entries == [Entry("match", 1, "MATCH", False), GAP,
                              Entry("match", 3, "MATCH", False)]


# -- line numbers and the pre-offset context over real files ---------------------------------------


def test_reader_numbers_lines_as_grep_does(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    nul = bytes([0])
    path.write_bytes(b"one\n" + nul * 3 + b"\n" + b"x" * 2500 + b"\nfour\n")
    source = open_source("s", str(path), None, from_start=True)
    assert isinstance(source, LogFile)
    with source:
        got = [(line.number, line.cut, line.text[:4]) for line in source.lines()]
        cursor = source.cursor()
    # the NUL-only line 2 is skipped but counted; the cut line is one physical line
    assert got == [(1, False, "one"), (3, True, "xxxx"), (3, False, "xxxx"), (4, False, "four")]
    assert cursor.line == 4


def test_first_sight_counts_the_lines_it_skips(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\npartial")
    source = open_source("s", str(path), None)
    assert isinstance(source, LogFile)
    with source:
        assert list(source.lines()) == []
        cursor = source.cursor()
    assert cursor.line == 3 and cursor.offset == 6
    with open(path, "ab") as handle:
        handle.write(b" done\nd\n")
    again = open_source("s", str(path), cursor)
    assert isinstance(again, LogFile)
    with again:
        assert [(line.number, line.text) for line in again.lines()] == [(4, "partial done"),
                                                                       (5, "d")]


def test_a_cursor_without_a_line_count_is_counted_once(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = source.cursor()
    state = State(str(tmp_path / "state.json"))
    state.set("s", str(path), Cursor(**{**cursor.__dict__, "line": None}))  # a 0.1.0-era entry
    state.save()
    loaded = load_state(str(tmp_path / "state.json")).get("s", str(path))
    assert loaded is not None and loaded.line is None
    with open(path, "ab") as handle:
        handle.write(b"d\n")
    again = open_source("s", str(path), loaded)
    assert again is not None
    with again:
        assert [line.number for line in again.lines()] == [4]
        assert again.cursor().line == 4  # counted, and carried from now on


def test_context_before_reads_the_lines_before_the_offset(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"one\ntwo\r\nthree\nfour\n")
    source = open_source("s", str(path), None)  # first sight: at the end, 4 lines skipped
    assert isinstance(source, LogFile)
    with source:
        list(source.lines())
        cursor = source.cursor()
        pairs = [(line.number, line.text, line.path) for line in source.context_before(2)]
        assert pairs == [(3, "three", str(path)), (4, "four", str(path))]
        assert [line.text for line in source.context_before(10)] == ["one", "two", "three",
                                                                     "four"]
        assert source.context_before(0) == []
    with open(path, "ab") as handle:
        handle.write(b"five MATCH\nsix\n")
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    again = open_source("s", str(path), cursor)
    assert again is not None
    with again:
        report = scan(str(path), again.lines(), watch, context=2, before=again.context_before)
    assert rendered(report) == ["3-three", "4-four", "5:five MATCH", "6-six"]


def test_context_before_is_bounded_and_cuts_long_lines(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    long = b"L" * 2500
    path.write_bytes(b"short\n" + long + b"\nlast\n")
    with open_log(str(path)) as handle:
        got = lines_before(handle, path.stat().st_size, 2, 3)
        assert [(x.number, x.cut, len(x.text)) for x in got] == [(2, True, 2000), (3, False, 4)]
        assert lines_before(handle, path.stat().st_size, 5, 3)[0].text == "short"
        assert lines_before(handle, 0, 5, 0) == []
    big = tmp_path / "big.log"
    big.write_bytes(b"x\n" * 100_000)  # far more than the bound covers
    with open_log(str(big)) as handle:
        got = lines_before(handle, big.stat().st_size, 3, 100_000)
        assert [x.number for x in got] == [99_998, 99_999, 100_000]


def test_context_before_on_a_rotated_archive_and_a_compressed_one(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = source.cursor()
    with open(path, "ab") as handle:
        handle.write(b"d MATCH\ne\n")
    (tmp_path / "router.log.1.gz").write_bytes(gzip.compress(path.read_bytes()))
    path.write_bytes(b"live MATCH\n")
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    again = open_source("s", str(path), cursor)
    assert isinstance(again, CatchUpSource)
    with again:
        report = scan(str(path), again.lines(), watch, context=2, before=again.context_before)
        parked = again.cursor()
    # the archive's tail with its pre-offset context, then the live file from line 1
    assert rendered(report) == ["2-b", "3-c", "4:d MATCH", "5-e", "--", "1:live MATCH"]
    assert parked.line == 1


def test_count_newlines(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_bytes(b"a\nb\nc")
    with open_log(str(path)) as handle:
        assert count_newlines(handle, 5) == 2 and count_newlines(handle, 4) == 2
        assert count_newlines(handle, 0) == 0 and handle.tell() == 0


# -- the mutation reviewer's pins: each names the mutation it catches ------------------------


def test_a_cut_line_is_excluded_when_any_fragment_matches_an_exclude(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    MATCH\nexclude =\n    harmless\n")
    stream = [Line("head MATCH", True, 4), Line("tail harmless", False, 4)]
    report = scan(FILE, stream, watch, context=0)
    assert report.matches == [] and report.excluded == 1 and report.entries == []
@pytest.mark.parametrize(
    ("key", "value", "dropped", "kept"),
    [
        ("exclude", "expected", "disk failure (expected)", "disk failure (Expected)"),
        ("iexclude", "expected", "disk failure (Expected)", "disk failure (surprise)"),
        ("exclude_regex", r"\(test\d+\)", "disk failure (test7)", "disk failure (TEST7)"),
        ("iexclude_regex", r"\(test\d+\)", "disk failure (TEST7)", "disk failure (test)"),
    ],
)
def test_an_exclude_hit_on_a_non_matching_line_is_not_counted(
    tmp_path: Path, key: str, value: str, dropped: str, kept: str
) -> None:
    watch = make_watch(tmp_path, f"patterns =\n    disk failure\n{key} =\n    {value}\n")
    noise = dropped.replace("disk failure", "all well")  # hits the exclude, not a pattern
    report = scan(FILE, lines(dropped, kept, "nothing here", noise), watch, context=0)
    assert [m.text for m in report.matches] == [kept]
    assert report.excluded == 1 and report.lines == 4
def test_excluded_lines_are_counted_at_debug(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    watch = make_watch(tmp_path, "patterns =\n    failure\nexclude =\n    harmless\n")
    with caplog.at_level(logging.DEBUG, logger="logalert.match"):
        scan(FILE, lines("harmless failure", "harmless failure"), watch, context=0)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, f"[router-disk] {FILE}: 2 matching line(s) dropped by an exclude"),
    ]
def test_matches_and_entries_name_the_physical_file(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = source.cursor()
    with open(path, "ab") as handle:
        handle.write(b"d MATCH\ne\n")
    archive = tmp_path / "router.log.1.gz"
    archive.write_bytes(gzip.compress(path.read_bytes()))
    path.write_bytes(b"live MATCH\n")
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    again = open_source("s", str(path), cursor)
    assert isinstance(again, CatchUpSource)
    with again:
        report = scan(str(path), again.lines(), watch, context=2, before=again.context_before)
    assert [m.path for m in report.matches] == [str(archive), str(path)]
    assert [e.path for e in report.entries] == [str(archive)] * 4 + [""] + [str(path)]
def test_a_continue_with_a_line_count_never_recounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = source.cursor()
    assert cursor.line == 3
    counted: list[int] = []

    def spy(handle: object, end: int) -> int:
        counted.append(end)
        return 0

    monkeypatch.setattr("logalert.cursor.count_newlines", spy)
    with open(path, "ab") as handle:
        handle.write(b"d\n")
    again = open_source("s", str(path), cursor)
    assert again is not None
    with again:
        assert [line.number for line in again.lines()] == [4]
    assert counted == []  # the saved count is the count; no pass over the file
def test_count_newlines_excludes_the_byte_at_end(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    path.write_bytes(b"a\nb\nc")
    with open_log(str(path)) as handle:
        assert count_newlines(handle, 1) == 0 and count_newlines(handle, 2) == 1
        assert count_newlines(handle, 3) == 1 and count_newlines(handle, 4) == 2
def test_lines_before_omits_a_line_the_window_cuts(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    bound = 2 * (LINE_CAP + 1) + _CHUNK
    path.write_bytes(b"p\n" * 40_000 + b"H" * 10 + b"T" * (bound + 100) + b"\nlast\n")
    with open_log(str(path)) as handle:
        got = lines_before(handle, path.stat().st_size, 2, 40_002)
    # line 40001 straddles the window's edge: its head is outside, so it is not returned
    assert [(x.number, x.text) for x in got] == [(40_002, "last")]
def test_lines_before_reads_only_the_bound_and_the_bound_holds_n_full_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "big.log"
    full = b"A" * LINE_CAP + b"\n"
    path.write_bytes(b"p\n" * 50_000 + full * 3)  # 100 KB of noise, then 3 cap-length lines
    size = path.stat().st_size
    with open_log(str(path)) as handle:
        spy = Mock(wraps=handle)
        got = lines_before(spy, size, 3, 50_003)
    assert [(x.number, x.cut, len(x.text)) for x in got] == [
        (50_001, False, LINE_CAP), (50_002, False, LINE_CAP), (50_003, False, LINE_CAP),
    ]
    first_seek = spy.seek.call_args_list[0].args[0]
    assert first_seek >= size - (3 * (LINE_CAP + 1) + _CHUNK) - 1  # the bound, plus the probe byte
def test_lines_before_counts_but_does_not_return_nul_only_lines(tmp_path: Path) -> None:
    path = tmp_path / "a.log"
    nul = bytes([0])
    path.write_bytes(b"one\n" + nul * 3 + b"\n" + nul * 2 + b"two\nthree\n")
    with open_log(str(path)) as handle:
        got = lines_before(handle, path.stat().st_size, 4, 4)
    assert [(x.number, x.text) for x in got] == [(1, "one"), (3, "two"), (4, "three")]
def test_a_stop_inside_the_archive_carries_the_archive_line_count(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = source.cursor()
    with open(path, "ab") as handle:
        handle.write(b"d\ne\n")
    (tmp_path / "router.log.1").write_bytes(path.read_bytes())
    path.write_bytes(b"live\n")
    again = open_source("s", str(path), cursor)
    assert isinstance(again, CatchUpSource)
    with again:
        it = again.lines()
        assert next(it).number == 4
        stopped = again.cursor()
    assert stopped.line == 4 and stopped.offset == 8
    third = open_source("s", str(path), stopped)  # resumes inside the archive, numbered
    assert isinstance(third, CatchUpSource)
    with third:
        assert [(line.number, line.text) for line in third.lines()] == [(5, "e"), (1, "live")]
def test_segment_context_before_tolerates_a_vanished_or_renamed_archive(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = source.cursor()
    with open(path, "ab") as handle:
        handle.write(b"d\ne\n")
    (tmp_path / "router.log.1").write_bytes(path.read_bytes())
    path.write_bytes(b"live\n")
    again = open_source("s", str(path), cursor)
    assert isinstance(again, CatchUpSource)
    with again:
        it = again.lines()
        assert next(it).text == "d"
        assert [x.text for x in again.context_before(2)] == ["b", "c"]  # the archive's
        real_fstat = os.fstat
        # renamed under us: the name now belongs to another file, whose lines are not ours
        monkeypatch.setattr("logalert.rotation.os.fstat", lambda fd: os.stat(tmp_path))
        assert again.context_before(2) == []
        monkeypatch.setattr("logalert.rotation.os.fstat", real_fstat)

        def gone(target: str, **kwargs: object) -> object:
            raise FileNotFoundError(2, "No such file", target)

        monkeypatch.setattr("logalert.rotation.open_log", gone)
        caplog.set_level(logging.WARNING, logger="logalert")
        assert again.context_before(2) == []
        assert any("context before the saved position could not be read" in r.getMessage()
                   for r in caplog.records)
def test_report_priority_is_the_highest_match_not_the_last(tmp_path: Path) -> None:
    watch = make_watch(tmp_path, "patterns =\n    [high] first\n    [low] second\n")
    report = scan(FILE, lines("first", "second"), watch, context=0)
    assert [m.priority for m in report.matches] == ["high", "low"]
    assert report.priority == "high"
def test_a_rotation_under_a_cursor_without_a_count_numbers_the_archive(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = replace(source.cursor(), line=None)  # a 0.1.0-era entry
    with open(path, "ab") as handle:
        handle.write(b"d MATCH\ne\n")
    (tmp_path / "router.log.1.gz").write_bytes(gzip.compress(path.read_bytes()))
    path.write_bytes(b"live MATCH\n")
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    again = open_source("s", str(path), cursor)
    assert isinstance(again, CatchUpSource)
    with again:
        report = scan(str(path), again.lines(), watch, context=2, before=again.context_before)
    assert rendered(report) == ["2-b", "3-c", "4:d MATCH", "5-e", "--", "1:live MATCH"]
def test_chain_members_and_the_live_file_are_numbered_from_one(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"a\nb\nc\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        list(source.lines())
        cursor = source.cursor()
    with open(path, "ab") as handle:
        handle.write(b"d MATCH\ne\n")
    (tmp_path / "router.log.2").write_bytes(path.read_bytes())
    (tmp_path / "router.log.1").write_bytes(b"f\ng MATCH\n")
    path.write_bytes(b"live MATCH\n")
    watch = make_watch(tmp_path, "patterns =\n    MATCH\n")
    again = open_source("s", str(path), cursor)
    assert isinstance(again, CatchUpSource)
    with again:
        report = scan(str(path), again.lines(), watch, context=2, before=again.context_before)
    assert rendered(report) == ["2-b", "3-c", "4:d MATCH", "5-e", "--", "1-f", "2:g MATCH", "--",
                                "1:live MATCH"]
def test_a_non_utf8_byte_read_from_the_file_still_matches(tmp_path: Path) -> None:
    path = tmp_path / "router.log"
    path.write_bytes(b"caf" + bytes([0xE9]) + b" disk failure\n")  # latin-1 e-acute, not UTF-8
    watch = make_watch(tmp_path, "patterns =\n    failure\n")
    source = open_source("s", str(path), None, from_start=True)
    assert source is not None
    with source:
        report = scan(str(path), source.lines(), watch, context=0)
    assert [m.text for m in report.matches] == ["caf" + chr(0xFFFD) + " disk failure"]
