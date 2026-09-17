"""Globs in ``files`` through the run (issue #18): the expansion keyed per path, rotated copies
left out, the new-file rule and its record, the failed listing, ``--check-config`` and
``--reset-state`` over a glob. Real on both platforms; the fixtures are ``tests/test_run.py``'s.
"""

import json
import logging
import os
import quopri
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_run import NL, Site, body_of

import logalert.run
from logalert.__main__ import main
from logalert.state import Cursor, State, load_state, parse_timestamp, timestamp


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Site:
    return Site(tmp_path, monkeypatch)


def daily(site: Site, name: str, *lines: str) -> Path:
    path = site.root / "daily" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + NL for line in lines), encoding="utf-8", newline=NL)
    return path


def glob_of(site: Site) -> str:
    """The daily glob, its directory made so the glob is looked into (and recorded)."""
    (site.root / "daily").mkdir(exist_ok=True)
    return (site.root / "daily" / "*.log").as_posix()


def runs(site: Site) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(site.state_file.read_text(encoding="utf-8"))
    record: dict[str, Any] = data["runs"]
    return record


def set_record_at(site: Site, section: str, moment: datetime) -> None:
    """Pin every glob's moment in a section's record (the run records the wall clock)."""
    state = load_state(str(site.state_file))
    globs = list(state.runs[section])
    assert globs
    state.record_run(section, globs, globs, moment)
    state.save()


def at(site: Site, section: str, glob: str) -> str:
    moment: str = runs(site)[section][glob]
    return moment


def back_date(site: Site, section: str, seconds: int) -> str:
    """Pin every glob's moment in a section's record ``seconds`` ago; returns it."""
    moment = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=seconds)
    set_record_at(site, section, moment)
    return timestamp(moment)


def stamp(path: Path, seconds: int) -> None:
    """Give a file a modification time ``seconds`` ago."""
    when = (datetime.now(UTC) - timedelta(seconds=seconds)).timestamp()
    os.utime(path, (when, when))


def test_a_glob_reads_its_matches_and_keys_the_state_by_each_path(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    one = daily(site, "one.log", "up")
    two = daily(site, "two.log", "up")
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    assert sorted(site.state()["firewall"]) == [one.as_posix(), two.as_posix()]
    site.append(two, "DENY 192.0.2.7")
    assert site.run() == 0 and capsys.readouterr() == ("", "")
    (argv, stdin), = site.calls()
    assert argv["recipients"] == ["fw@example.net"]
    body = quopri.decodestring(stdin).decode("utf-8")  # the long path is soft-wrapped
    assert f"{two.as_posix()} <==" in body and "DENY 192.0.2.7" in body
    assert site.offset("firewall", two) == two.stat().st_size
    assert list(runs(site)["firewall"]) == [glob_of(site)]


def test_a_new_daily_file_is_read_from_the_beginning(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert")
    day1 = daily(site, "2026-09-15.log", "DENY 192.0.2.1 before the first run")
    site.write_config(firewall_files=glob_of(site))
    site.prime()  # day 1 is a plain first sight: nothing mailed
    day2 = daily(site, "2026-09-16.log", "DENY 192.0.2.2 in the new file")
    site.append(day1, "DENY 192.0.2.3 still written to day 1")
    assert site.run() == 0
    (_, stdin), = site.calls()
    body = body_of(stdin)
    assert "DENY 192.0.2.2 in the new file" in body and "192.0.2.3 still written" in body
    assert "192.0.2.1 before" not in body
    assert (f"[firewall] {day2.as_posix()}: new since the last run ({glob_of(site)}); reading "
            f"from the beginning") in caplog.messages
    assert f"[firewall] {day2.as_posix()}: first sight; reading from the beginning" \
        in caplog.messages
    assert site.offset("firewall", day2) == day2.stat().st_size
    assert sorted(site.state()["firewall"]) == [day1.as_posix(), day2.as_posix()]


def test_a_glob_added_to_the_section_first_sights_every_match_at_the_end(site: Site) -> None:
    """The widened-glob trap: ``error.log`` to ``*.log`` must not mail a long-lived file."""
    site.prime()  # the firewall section ran with its literal file only
    site.write_config(firewall_files=site.firewall.as_posix() + NL + f"    {glob_of(site)}")
    big = daily(site, "access.log", "DENY 192.0.2.4 in a file that is not new")
    assert site.run() == 0 and site.calls() == []
    assert site.offset("firewall", big) == big.stat().st_size
    assert list(runs(site)["firewall"]) == [glob_of(site)]  # the listed path has no moment
    daily(site, "fresh.log", "DENY 192.0.2.5 now the glob is on record")
    assert site.run() == 0
    (_, stdin), = site.calls()
    assert "DENY 192.0.2.5" in body_of(stdin)


def test_a_file_older_than_the_last_run_starts_at_the_end(site: Site) -> None:
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    moment = parse_timestamp(at(site, "firewall", glob_of(site))).timestamp()
    restored = daily(site, "restored.log", "DENY 192.0.2.6 from a backup")
    os.utime(restored, (moment - 100, moment - 100))
    assert site.run() == 0 and site.calls() == []
    assert site.offset("firewall", restored) == restored.stat().st_size


def test_a_file_from_the_records_own_second_is_new(site: Site) -> None:
    """The record is whole seconds; a file stamped in that second was not there to be
    matched, so ``>=``, not ``>``."""
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    moment = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)
    set_record_at(site, "firewall", moment)
    same = daily(site, "same-second.log", "DENY 192.0.2.8 stamped on the second")
    os.utime(same, (moment.timestamp(), moment.timestamp()))
    assert site.run() == 0
    (_, stdin), = site.calls()
    assert "DENY 192.0.2.8" in body_of(stdin)


def test_a_listed_path_is_never_new(site: Site) -> None:
    """The rule is a glob's: a listed file's first sight is #7's, however fresh the file."""
    site.prime()
    late = site.root / "late.log"
    site.write_config(firewall_files=site.firewall.as_posix() + NL + f"    {late.as_posix()}")
    assert site.run() == 0  # the record now carries late.log; the file is not there yet
    late.write_text("DENY 192.0.2.9 in a listed file that appeared" + NL, encoding="utf-8",
                    newline=NL)
    assert site.run() == 0 and site.calls() == []
    assert site.offset("firewall", late) == late.stat().st_size


def test_a_listed_path_a_glob_also_matches_is_never_new(site: Site) -> None:
    """A name the operator wrote is a listed file wherever it stands in the list."""
    late = site.root / "daily" / "late.log"
    site.write_config(firewall_files=glob_of(site) + NL + f"    {late.as_posix()}")
    site.prime()
    daily(site, "late.log", "DENY 192.0.2.14 in a listed file the glob matches too")
    assert site.run() == 0 and site.calls() == []
    assert site.offset("firewall", late) == late.stat().st_size


def test_a_failed_delivery_leaves_the_record_so_the_rerun_reads_the_new_file_again(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    moment = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=100)
    set_record_at(site, "firewall", moment)
    new = daily(site, "new.log", "DENY 192.0.2.10 in a file the failed run read")
    between = moment.timestamp() + 10  # after the recorded run, before the failed one
    os.utime(new, (between, between))
    monkeypatch.setenv("LOGALERT_FAKE_EXIT", "75")
    assert site.run() == 1
    assert at(site, "firewall", glob_of(site)) == timestamp(moment)  # the failed run: no move
    # contributed: an entry at the offset the read began, 0 (issue #31; the record stays put)
    assert site.state()["firewall"][new.as_posix()]["offset"] == 0
    monkeypatch.delenv("LOGALERT_FAKE_EXIT")
    assert site.run() == 0
    _, stdin = site.calls()[-1]
    assert "DENY 192.0.2.10" in body_of(stdin)
    assert at(site, "firewall", glob_of(site)) > timestamp(moment)


def test_dry_run_applies_the_rule_and_writes_no_record(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    before = site.state_file.read_bytes()
    daily(site, "new.log", "DENY 192.0.2.11 previewed")
    assert site.run("-n") == 0
    assert "DENY 192.0.2.11 previewed" in capsys.readouterr().out
    assert site.calls() == [] and site.state_file.read_bytes() == before


def test_a_glob_matching_nothing_is_nothing_to_do(
        site: Site, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]) -> None:
    caplog.set_level(logging.DEBUG, logger="logalert.run")
    pattern = glob_of(site)
    (site.root / "daily").rmdir()  # the directory does not exist yet
    site.write_config(firewall_files=pattern)
    assert site.run() == 0 and capsys.readouterr() == ("", "")
    assert f"[firewall] {pattern}: matches nothing this run" in caplog.messages
    assert "firewall" not in site.state()
    assert runs(site)["firewall"] == {}  # not looked into: no moment to judge a file by


def test_an_unlistable_directory_is_a_failed_item_and_the_rest_runs(
        site: Site, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    daily(site, "one.log", "up")
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    site.append(site.router, "kernel: disk failure on sdb")
    locked = (site.root / "daily").as_posix()
    real_scandir = os.scandir

    def scandir(path: str = ".") -> object:
        if os.path.normcase(path) == os.path.normcase(locked):
            raise PermissionError(13, "Permission denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert site.run() == 1
    assert capsys.readouterr().err == (
        f"logalert: 1 of 1 section sent; failed: [firewall] {glob_of(site)}: cannot list "
        f"{locked} (Permission denied); see the log" + NL)
    (argv, _), = site.calls()
    assert argv["recipients"] == ["noc@example.net"]  # the router section went out


def test_rotated_copies_are_left_out_unless_include_archives(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="logalert.run")
    live = daily(site, "fw.log", "up")
    daily(site, "fw.log.1", "DENY 192.0.2.12 in the rotated copy")
    daily(site, "fw.log.2.gz", "not really gzip")
    pattern = (site.root / "daily" / "fw*").as_posix()
    site.write_config(firewall_files=pattern)
    site.prime()
    assert sorted(site.state()["firewall"]) == [live.as_posix()]
    assert f"[firewall] {pattern}: left out 2 rotated copies: fw.log.1, fw.log.2.gz" \
        in caplog.messages
    site.write_config(firewall_files=pattern, firewall="include_archives = yes\n")
    assert site.run() == 1  # the bogus .gz is a failed item, as a listed one would be
    assert sorted(os.path.basename(p) for p in site.state()["firewall"]) == ["fw.log", "fw.log.1"]


def test_a_file_named_and_matched_is_read_once(site: Site) -> None:
    site.write_config(firewall_files=site.firewall.as_posix() + NL
                      + f"    {(site.root / 'fw*.log').as_posix()}")
    site.prime()
    assert list(site.state()["firewall"]) == [site.firewall.as_posix()]
    site.append(site.firewall, "DENY 192.0.2.13 once")
    assert site.run() == 0
    (_, stdin), = site.calls()
    assert body_of(stdin).count("DENY 192.0.2.13 once") == 1


def test_a_glob_matched_entry_expires_like_any_other(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.run")
    site.write_config("state_ttl = 1\n", firewall_files=glob_of(site))
    gone = daily(site, "gone.log", "up")
    site.prime()
    gone.unlink()
    state = State(str(site.state_file))
    old = timestamp(datetime.now(UTC) - timedelta(days=3))
    state = load_state(str(site.state_file))
    saved = state.get("firewall", gone.as_posix())
    assert saved is not None
    state.set("firewall", gone.as_posix(), Cursor(saved.offset, saved.ino, saved.dev,
                                                  saved.fingerprint, saved.realpath, old))
    state.save()
    assert site.run() == 0
    assert "firewall" not in site.state() and "firewall" in runs(site)
    assert f"[firewall] {gone.as_posix()}: forgotten, unseen for 1 days" in caplog.messages


def test_check_config_shows_what_a_glob_matched(
        site: Site, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    one = daily(site, "one.log", "up")
    two = daily(site, "two.log", "up")
    daily(site, "two.log.1", "old")
    (site.root / "daily" / "sub.log").mkdir()
    everything = (site.root / "daily" / "*").as_posix()
    site.write_config(firewall_files=everything)
    assert site.run("--check-config") == 0
    out = capsys.readouterr().out.splitlines()
    assert f"[firewall] files: {everything} -> 2 file(s), 1 rotated copy left out, " \
           f"1 passed over (not regular files)" in out
    assert f"[firewall]   {one.as_posix()}" in out and f"[firewall]   {two.as_posix()}" in out
    assert f"[firewall]   left out: {(site.root / 'daily' / 'two.log.1').as_posix()}" in out
    assert (f"[firewall]   passed over: {(site.root / 'daily' / 'sub.log').as_posix()} "
            f"(a directory)") in out
    assert any(line.endswith("start: end; include_archives: no") for line in out)
    site.write_config(firewall_files=everything, firewall="include_archives = yes\n")
    assert site.run("--check-config") == 0
    out = capsys.readouterr().out.splitlines()
    assert f"[firewall] files: {everything} -> 3 file(s), 1 passed over (not regular files)" \
        in out
    assert f"[firewall]   {(site.root / 'daily' / 'two.log.1').as_posix()}" in out
    assert any(line.endswith("start: end; include_archives: yes") for line in out)
    site.write_config(firewall_files=everything)
    for n in range(30):
        daily(site, f"many-{n:02d}.log", "up")
    assert site.run("--check-config") == 0
    out = capsys.readouterr().out.splitlines()
    assert f"[firewall] files: {everything} -> 32 file(s), 1 rotated copy left out, " \
           f"1 passed over (not regular files)" in out
    matches = [line for line in out if line.startswith("[firewall]   ")
               and "left out: " not in line and "passed over: " not in line]
    assert len(matches) == 21 and "[firewall]   ... and 12 more" in out
    for n in range(18, 30):  # exactly LIST_CAP matches left: no remainder line
        (site.root / "daily" / f"many-{n:02d}.log").unlink()
    assert site.run("--check-config") == 0
    out = capsys.readouterr().out.splitlines()
    assert f"[firewall] files: {everything} -> 20 file(s), 1 rotated copy left out, " \
           f"1 passed over (not regular files)" in out
    assert not any("more" in line for line in out if line.startswith("[firewall]   "))
    locked = (site.root / "daily").as_posix()
    real_scandir = os.scandir

    def scandir(path: str = ".") -> object:
        if os.path.normcase(path) == os.path.normcase(locked):
            raise PermissionError(13, "Permission denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert site.run("--check-config") == 0  # the configuration is valid; the directory is not
    out = capsys.readouterr().out.splitlines()
    assert f"[firewall] files: {everything} -> cannot list {locked} (Permission denied)" in out
    site.write_config(firewall_files=(site.root / "nothing" / "*.log").as_posix())
    assert site.run("--check-config") == 0
    assert "-> matches nothing" in capsys.readouterr().out


def test_reset_state_takes_the_expanded_path_and_drops_the_record(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    one = daily(site, "one.log", "up")
    two = daily(site, "two.log", "up")
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    assert main(["-f", str(site.conf), "--reset-state", one.as_posix()]) == 0
    assert capsys.readouterr().out.startswith(f"forgot 1 cursor(s) for {one.as_posix()};")
    assert list(site.state()["firewall"]) == [two.as_posix()]
    assert sorted(runs(site)) == ["router-disk"]  # the firewall record went with the cursor
    assert site.run() == 0 and site.calls() == []
    assert "firewall" in runs(site) and one.as_posix() in site.state()["firewall"]
    assert main(["-f", str(site.conf), "--reset-state", glob_of(site)]) == 1
    assert capsys.readouterr().err == (
        f"logalert: {glob_of(site)} is a glob; --reset-state takes one of its matches, "
        f"spelled as --check-config lists them (or no PATH, to forget every file)" + NL)
    elsewhere = (site.root / "elsewhere" / "*.log").as_posix()  # a glob nobody lists
    assert main(["-f", str(site.conf), "--reset-state", elsewhere]) == 1
    err = capsys.readouterr().err
    assert err.startswith(f"logalert: no entry for {elsewhere}; the state file knows ")
    assert err.rstrip(NL).endswith(" -- and no section lists that file")
    three = daily(site, "three.log", "up")  # matched now, never seen: named, not known
    assert main(["-f", str(site.conf), "--reset-state", three.as_posix()]) == 1
    assert "no section lists" not in capsys.readouterr().err


# -- the review's cases (issue #18) ------------------------------------------------------


def deny_scandir(monkeypatch: pytest.MonkeyPatch, locked: str) -> None:
    """``os.scandir`` refuses one directory: the unlistable-directory device on both platforms."""
    real_scandir = os.scandir

    def scandir(path: str = ".") -> object:
        if os.path.normcase(path) == os.path.normcase(locked):
            raise PermissionError(13, "Permission denied", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)


def test_a_glob_whose_directory_could_not_be_listed_keeps_its_moment(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """The outage rule: a file created while the directory was unlistable is read whole once
    the directory is back (the moment did not move), and a glob never looked into has no
    moment, so the first successful listing of a directory of live logs mails nothing."""
    pattern = glob_of(site)
    locked = (site.root / "daily").as_posix()
    with pytest.MonkeyPatch.context() as first:
        deny_scandir(first, locked)
        site.write_config(firewall_files=pattern)
        assert site.run() == 1  # cannot list: a failed item, and no moment for the glob
    assert runs(site)["firewall"] == {}
    live = daily(site, "live.log", *(f"DENY 192.0.2.20 line {n}" for n in range(300)))
    assert site.run() == 0 and site.calls() == []  # first listed now: at the end, no flood
    assert site.offset("firewall", live) == live.stat().st_size
    moment = back_date(site, "firewall", 100)  # the last good run, 100 s ago
    during = daily(site, "during.log", "DENY 192.0.2.21 written during the outage")
    stamp(during, 50)  # after that run, before the outage run
    with pytest.MonkeyPatch.context() as second:
        deny_scandir(second, locked)
        assert site.run() == 1 and site.calls() == []
    assert at(site, "firewall", pattern) == moment  # the outage did not move it
    assert site.run() == 0
    (_, stdin), = site.calls()
    assert "DENY 192.0.2.21 written during the outage" in body_of(stdin)
    assert site.offset("firewall", during) == during.stat().st_size


def test_a_directory_away_for_a_run_keeps_the_moment_too(site: Site) -> None:
    pattern = glob_of(site)
    site.write_config(firewall_files=pattern)
    site.prime()
    moment = back_date(site, "firewall", 100)
    away = site.root / "away"
    (site.root / "daily").rename(away)
    (away / "during.log").write_text("DENY 192.0.2.22 made while the directory was away" + NL,
                                     encoding="utf-8", newline=NL)
    stamp(away / "during.log", 50)
    assert site.run() == 0 and site.calls() == []  # matches nothing: exit 0
    assert at(site, "firewall", pattern) == moment
    away.rename(site.root / "daily")
    assert site.run() == 0
    (_, stdin), = site.calls()
    assert "DENY 192.0.2.22" in body_of(stdin)


def test_a_new_file_matched_by_two_globs_is_new_whichever_lists_first(site: Site) -> None:
    """Every glob that matched the path is asked, not the first in the list: a wider glob
    added in front of the recorded one must not lose a new file's content."""
    narrow = glob_of(site)
    wide = (site.root / "daily" / "*").as_posix()
    for order in ((wide, narrow), (narrow, wide)):
        site.write_config(firewall_files=narrow)
        site.state_file.unlink(missing_ok=True)
        shutil.rmtree(site.fake_dir, ignore_errors=True)
        site.prime()
        site.write_config(firewall_files=NL.join(["", *(f"    {g}" for g in order)]))
        new = daily(site, f"new-{order.index(wide)}.log", "DENY 192.0.2.23 in a new file")
        assert site.run() == 0
        _, stdin = site.calls()[-1]
        assert "DENY 192.0.2.23" in body_of(stdin)
        assert site.offset("firewall", new) == new.stat().st_size


def test_the_moment_is_the_runs_start(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file that appears while a long run is in progress must be newer than the moment."""
    fixed = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=45)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Clock":
            return cls.fromtimestamp(fixed.timestamp(), tz)

    monkeypatch.setattr(logalert.run, "datetime", Clock)
    pattern = glob_of(site)
    site.write_config(firewall_files=pattern)
    site.prime()
    assert at(site, "firewall", pattern) == timestamp(fixed)


def test_the_new_since_line_is_logged_for_the_new_file_only(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="logalert.run")
    day1 = daily(site, "day1.log", "up")
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    site.append(day1, "DENY 192.0.2.24 appended to a file with a cursor")
    daily(site, "day2.log", "DENY 192.0.2.25 in the new file")
    caplog.clear()
    assert site.run() == 0
    new_since = [m for m in caplog.messages if "new since the last run" in m]
    assert len(new_since) == 1 and "day2.log" in new_since[0]


def test_a_failed_listing_is_not_also_matches_nothing(
        site: Site, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="logalert.run")
    pattern = glob_of(site)
    site.write_config(firewall_files=pattern)
    deny_scandir(monkeypatch, (site.root / "daily").as_posix())
    assert site.run() == 1
    assert not any("matches nothing" in m for m in caplog.messages)


def test_a_record_of_a_section_no_longer_configured_expires_a_configured_ones_stays(
        site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="logalert.run")
    pattern = glob_of(site)
    site.write_config("state_ttl = 1" + NL, firewall_files=pattern)
    site.prime()
    old = datetime.now(UTC) - timedelta(days=3)
    state = load_state(str(site.state_file))
    state.record_run("gone-section", [pattern], [pattern], old)
    state.record_run("firewall", [pattern], [pattern], old)
    state.save()
    assert site.run() == 0
    assert "gone-section" not in runs(site) and "firewall" in runs(site)
    assert ("[gone-section] the last-run record forgotten: not configured, no run saved for "
            "1 days") in caplog.messages


def test_reset_state_hint_honours_include_archives(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    copy = daily(site, "fw.log.1", "old")
    daily(site, "fw.log", "up")
    pattern = (site.root / "daily" / "fw*").as_posix()
    site.write_config(firewall_files=pattern)
    site.prime()
    assert main(["-f", str(site.conf), "--reset-state", copy.as_posix()]) == 1
    assert capsys.readouterr().err.rstrip(NL).endswith(" -- and no section lists that file")
    site.write_config(firewall_files=pattern, firewall="include_archives = yes" + NL)
    assert main(["-f", str(site.conf), "--reset-state", copy.as_posix()]) == 1
    assert "no section lists" not in capsys.readouterr().err


def test_a_path_the_list_spells_differently_from_the_glob_is_read_once(site: Site) -> None:
    odd = (site.root / "daily").as_posix() + "//fw.log"  # the same file, spelled with //
    daily(site, "fw.log", "up")
    site.write_config(firewall_files=odd + NL + f"    {glob_of(site)}")
    site.prime()
    assert list(site.state()["firewall"]) == [odd]  # the listed spelling is the key
    site.append(site.root / "daily" / "fw.log", "DENY 192.0.2.26 once")
    assert site.run() == 0
    (_, stdin), = site.calls()
    assert body_of(stdin).count("DENY 192.0.2.26 once") == 1


def test_hard_links_a_glob_matches_are_mailed_once(site: Site) -> None:
    fw = daily(site, "fw.log", "up")
    os.link(fw, site.root / "daily" / "fw-current.log")
    site.write_config(firewall_files=glob_of(site))
    site.prime()
    site.append(fw, "DENY 192.0.2.27 through two names")
    assert site.run() == 0
    (_, stdin), = site.calls()
    assert body_of(stdin).count("DENY 192.0.2.27") == 1
    assert list(site.state()["firewall"]) == [(site.root / "daily" / "fw-current.log").as_posix()]


def test_names_from_disk_cannot_forge_a_line_of_check_config_or_reset_state(
        site: Site, capsys: pytest.CaptureFixture[str]) -> None:
    """A file name with a line break in it (POSIX allows one) is folded like every other
    name that reaches an output line (reproduced in review)."""
    if sys.platform == "win32":
        pytest.skip("Windows refuses control characters in names; runs in the sandbox and on CI")
    forged = "x.log" + chr(10) + "[other] forged"
    daily(site, forged, "up")
    site.write_config(firewall_files=(site.root / "daily" / "*").as_posix())  # not *.log
    assert site.run("--check-config") == 0
    out = capsys.readouterr().out.splitlines()
    assert not any(line.startswith("[other]") for line in out)
    assert sum(1 for line in out if line.startswith("[firewall]   ")) == 1
    site.prime()
    assert main(["-f", str(site.conf), "--reset-state", (site.root / "nope.log").as_posix()]) == 1
    err = capsys.readouterr().err
    assert err.count(chr(10)) == 1 and "[other] forged" in err


def test_a_glob_with_an_unlistable_subdirectory_keeps_its_moment(
        site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    """``hosts/*/messages`` with one host directory unlistable: the top directory was listed,
    but the glob had an error, so its moment must not move -- a file made in that host's
    directory during the outage is read whole once it is listable again."""
    pattern = (site.root / "daily" / "*" / "*.log").as_posix()
    r1 = daily(site, "r1/x.log", "up")
    daily(site, "r2/x.log", "up")
    site.write_config(firewall_files=pattern)
    site.prime()
    moment = back_date(site, "firewall", 100)
    during = daily(site, "r2/during.log", "DENY 192.0.2.28 made while r2 was unlistable")
    stamp(during, 50)
    with pytest.MonkeyPatch.context() as outage:
        deny_scandir(outage, (site.root / "daily" / "r2").as_posix())
        site.append(r1, "quiet")
        assert site.run() == 1 and site.calls() == []
    assert at(site, "firewall", pattern) == moment
    assert site.run() == 0
    (_, stdin), = site.calls()
    assert "DENY 192.0.2.28" in body_of(stdin)
    assert site.offset("firewall", during) == during.stat().st_size
