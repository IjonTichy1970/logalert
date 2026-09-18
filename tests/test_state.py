"""The state file: schema, atomic writes, expiry, the escape hatches (issue #7).

Mode-bit tests skip on Windows (a chmod there is a no-op) and run natively in the sandbox and
on CI; the permission-denied paths are tested on both platforms by monkeypatching.
"""

import json
import os
import stat
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import try_symlink

from logalert.state import (
    STATE_VERSION,
    Cursor,
    State,
    StateError,
    _foreign_owner,
    check_state_dir,
    load_state,
    lock_path,
    parse_timestamp,
    timestamp,
    write_atomically,
)

NOW = datetime(2026, 9, 14, 12, 0, 0, tzinfo=UTC)


def cursor(offset: int = 10, *, seen: datetime = NOW, fp: str | None = "ab" * 32) -> Cursor:
    return Cursor(offset=offset, ino=1234, dev=56, fingerprint=fp, realpath="/var/log/r.log",
                  last_seen=timestamp(seen))


def test_missing_file_is_an_empty_first_run_state(tmp_path: Path) -> None:
    state = load_state(str(tmp_path / "state.json"))
    assert state.entries == {}
    assert state.get("router-disk", "/var/log/router.log") is None
    assert not (tmp_path / "state.json").exists()  # loading never creates it


def test_round_trip_and_the_on_disk_shape(tmp_path: Path) -> None:
    path = str(tmp_path / "state.json")
    state = State(path)
    state.set("router-disk", "/var/log/router.log", cursor(10))
    state.set("firewall", "/var/log/router.log", cursor(20, fp=None))
    state.save()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["version"] == STATE_VERSION
    entry = data["entries"]["router-disk"]["/var/log/router.log"]
    assert entry == {"offset": 10, "ino": 1234, "dev": 56, "fingerprint": "ab" * 32,
                     "realpath": "/var/log/r.log", "last_seen": "2026-09-14T12:00:00Z",
                     "line": None, "size": None, "mtime": None,  # size/mtime: issue #44
                     "anchor": None}  # the bytes before the offset, hashed (issue #34)
    assert data["entries"]["firewall"]["/var/log/router.log"]["fingerprint"] is None
    again = load_state(path)
    assert again.entries == state.entries
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temp file left behind


def test_two_sections_one_file_have_independent_cursors(tmp_path: Path) -> None:
    state = State(str(tmp_path / "state.json"))
    state.set("a", "/var/log/x.log", cursor(10))
    state.set("b", "/var/log/x.log", cursor(10))
    state.set("a", "/var/log/x.log", cursor(30))
    assert state.get("a", "/var/log/x.log") == cursor(30)
    assert state.get("b", "/var/log/x.log") == cursor(10)


def test_touch_refreshes_last_seen_without_moving_the_cursor(tmp_path: Path) -> None:
    state = State(str(tmp_path / "state.json"))
    old = NOW - timedelta(days=40)
    state.set("a", "/var/log/x.log", cursor(10, seen=old))
    state.touch("a", "/var/log/x.log", NOW)
    got = state.get("a", "/var/log/x.log")
    assert got is not None and got.offset == 10 and got.last_seen == timestamp(NOW)
    state.touch("a", "/var/log/absent.log", NOW)  # no entry: no entry is invented
    assert state.get("a", "/var/log/absent.log") is None


def test_expire_drops_only_entries_older_than_the_ttl(tmp_path: Path) -> None:
    state = State(str(tmp_path / "state.json"))
    state.set("a", "/old", cursor(seen=NOW - timedelta(days=31)))
    state.set("a", "/edge", cursor(seen=NOW - timedelta(days=30)))
    state.set("b", "/new", cursor(seen=NOW - timedelta(hours=1)))
    assert state.expire(30, NOW) == [("a", "/old")]
    assert sorted(state.entries) == [("a", "/edge"), ("b", "/new")]
    assert state.expire(30, NOW) == []


def test_forget_one_file_or_everything(tmp_path: Path) -> None:
    state = State(str(tmp_path / "state.json"))
    state.set("a", "/x", cursor())
    state.set("b", "/x", cursor())
    state.set("a", "/y", cursor())
    assert state.forget("/nope") == 0
    assert state.forget("/x") == 2 and sorted(state.entries) == [("a", "/y")]
    assert state.forget() == 1 and state.entries == {}


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "top level is not an object"),
        ('{"entries": {}}', "no integer 'version'"),
        ('{"version": true, "entries": {}}', "no integer 'version'"),
        ('{"version": 99, "entries": {}}', "schema version 99 was written by a newer logalert"),
        ('{"version": 1}', "'entries' is not an object"),
        ('{"version": 1, "entries": {"a": []}}', "entries['a'] is not an object"),
        ('{"version": 1, "entries": {"a": {"/x": 5}}}', "entries['a']['/x'] is not an object"),
        ('{"version": 1, "entries": {"a": {"/x": {"offset": -1}}}}',
         "'offset' is not a non-negative integer"),
        ('{"version": 1, "entries": {"a": {"/x": {"offset": true}}}}',
         "'offset' is not a non-negative integer"),
        ('{"version": 1, "entries": {"a": {"/x": {"offset": 1, "ino": 2, "dev": 3, '
         '"fingerprint": 7, "realpath": "/x", "last_seen": "2026-09-14T12:00:00Z"}}}}',
         "'fingerprint' is not a string"),
        ('{"version": 1, "entries": {"a": {"/x": {"offset": 1, "ino": 2, "dev": 3, '
         '"fingerprint": null, "realpath": "/x", "last_seen": "yesterday"}}}}',
         "'last_seen' is not a UTC timestamp"),
        ("", "not valid JSON"),  # an empty file is never a silent first run (issue #41)
        ("  " + chr(10), "not valid JSON"),
    ],
)
def test_corrupt_state_is_a_hard_error_naming_the_file_and_the_remedy(
    tmp_path: Path, text: str, fragment: str
) -> None:
    path = tmp_path / "state.json"
    path.write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(StateError) as exc:
        load_state(str(path))
    message = str(exc.value)
    assert message.startswith(f"state file {path}: "), message
    assert fragment in message, message
    assert "--reset-state" in message


def test_state_file_that_is_not_utf8_is_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(b'{"version": 1, "entries": {}, "x": "' + bytes([0xFF]) + b'"}')
    with pytest.raises(StateError, match="not valid JSON"):
        load_state(str(path))


def test_an_older_schema_version_loads(tmp_path: Path) -> None:
    # version 0 never shipped, but a lower number must not be refused as "newer"
    path = tmp_path / "state.json"
    path.write_text('{"version": 0, "entries": {}}', encoding="utf-8")
    assert load_state(str(path)).entries == {}


def test_unreadable_state_file_is_a_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "entries": {}}', encoding="utf-8")

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("logalert.state.open", denied, raising=False)
    with pytest.raises(StateError) as exc:
        load_state(str(path))
    assert str(exc.value).startswith(f"state file {path}: cannot read (Permission denied)")
    assert "--reset-state" in str(exc.value)


def test_write_is_atomic_and_leaves_no_temp_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    real_replace = os.replace

    def failing_replace(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("logalert.state.os.replace", failing_replace)
    with pytest.raises(StateError, match=r"cannot write \(No space left on device\)"):
        write_atomically(str(path), "new")
    assert path.read_text(encoding="utf-8") == "old"  # the old file is intact
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # the temp is gone
    monkeypatch.setattr("logalert.state.os.replace", real_replace)
    write_atomically(str(path), "new")
    assert path.read_text(encoding="utf-8") == "new"


def test_write_uses_a_temp_file_in_the_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # os.replace across devices is EXDEV; the temp file must be born next to the target
    seen: list[str] = []
    real_mkstemp = tempfile.mkstemp

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["dir"])
        return real_mkstemp(*args, **kwargs)

    monkeypatch.setattr("logalert.state.tempfile.mkstemp", spy)
    write_atomically(str(tmp_path / "state.json"), "{}")
    assert seen == [str(tmp_path)]


def test_check_state_dir_names_the_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(StateError) as exc:
        check_state_dir(str(tmp_path / "nope" / "state.json"))
    assert str(exc.value) == (f"state directory {tmp_path / 'nope'} does not exist -- create "
                              f"it, owned by the user logalert runs as")
    assert not (tmp_path / "nope").exists()  # never created on the operator's behalf


def test_check_state_dir_passes_and_leaves_nothing_behind(tmp_path: Path) -> None:
    check_state_dir(str(tmp_path / "state.json"))
    assert list(tmp_path.iterdir()) == []


def test_check_state_dir_reports_denied_before_anything_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("logalert.state.tempfile.mkstemp", denied)
    with pytest.raises(StateError) as exc:
        check_state_dir(str(tmp_path / "state.json"))
    message = str(exc.value)
    assert message.startswith(f"state directory {tmp_path} is not writable (Permission denied)")
    assert "nothing was sent" in message


def test_check_state_dir_real_permission_denied(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    if os.geteuid() == 0:
        pytest.skip("expected in the root sandbox; CI runs it")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        with pytest.raises(StateError, match="is not writable"):
            check_state_dir(str(locked / "state.json"))
    finally:
        locked.chmod(0o700)


def test_a_symlinked_state_file_is_refused_and_its_target_untouched(tmp_path: Path) -> None:
    """A link at state_file never worked as a redirect: the first save replaced the link
    itself with a regular file (issue #39). It is refused up front, with the log's wording,
    before anything is written -- a link to a valid state and a dangling one alike."""
    real = tmp_path / "real.json"
    State(str(real)).save()
    before = real.read_bytes()
    link = tmp_path / "state.json"
    try_symlink(link, real)
    with pytest.raises(StateError) as exc:
        check_state_dir(str(link))
    assert str(exc.value) == f"state file {link} is a symbolic link -- name the real path"
    assert link.is_symlink() and real.read_bytes() == before
    dangling = tmp_path / "gone.json"
    dangling.symlink_to(tmp_path / "nowhere.json")
    with pytest.raises(StateError, match="is a symbolic link -- name the real path"):
        check_state_dir(str(dangling))
    assert dangling.is_symlink()
    check_state_dir(str(real))  # the real path itself passes


def test_state_file_is_written_0600(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    path = tmp_path / "state.json"
    State(str(path)).save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)  # an operator's chmod does not survive a rewrite: the file is replaced
    State(str(path)).save()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_timestamps_round_trip_in_utc() -> None:
    assert timestamp(NOW) == "2026-09-14T12:00:00Z"
    assert parse_timestamp("2026-09-14T12:00:00Z") == NOW
    local = datetime(2026, 9, 14, 14, 0, 0, tzinfo=UTC).astimezone()  # any zone, same instant
    assert timestamp(local) == "2026-09-14T14:00:00Z"


def test_lock_path_sits_next_to_the_state_file() -> None:
    assert lock_path("/var/lib/logalert/state.json") == os.path.join("/var/lib/logalert", "lock")
    assert lock_path("state.json") == "lock"


# -- the review's pins: each names the mutation it catches ----------------------------------


def test_save_goes_through_the_atomic_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a State.save that wrote the file directly would pass every write_atomically test
    path = tmp_path / "state.json"
    State(str(path)).save()
    before = path.read_bytes()

    def failing_replace(src: str, dst: str) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("logalert.state.os.replace", failing_replace)
    state = State(str(path))
    state.set("a", "/x", cursor())
    with pytest.raises(StateError, match=r"cannot write \(No space left on device\)"):
        state.save()
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_write_fsyncs_the_temp_file_before_replacing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd: int) -> None:
        calls.append("fsync")
        real_fsync(fd)

    def replace(src: str, dst: str) -> None:
        calls.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr("logalert.state.os.fsync", fsync)
    monkeypatch.setattr("logalert.state.os.replace", replace)
    write_atomically(str(tmp_path / "state.json"), "{}")
    assert calls == ["fsync", "replace"]


def test_write_cleans_up_the_temp_file_on_an_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SIGTERM arrives as an exception too since issue #33 (the handler raises one); the temp
    # must not litter the state dir
    def interrupted(src: str, dst: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("logalert.state.os.replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        write_atomically(str(tmp_path / "state.json"), "{}")
    assert list(tmp_path.iterdir()) == []


def test_offset_beyond_what_seek_accepts_is_corrupt_but_ids_are_unbounded(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.json"
    entry = ('{"version": 1, "entries": {"a": {"/x": {"offset": %s, "ino": %s, "dev": %s, '
             '"fingerprint": null, "realpath": "/x", "last_seen": "2026-09-14T12:00:00Z"}}}}')
    path.write_text(entry % (2**63, 1, 1), encoding="utf-8")
    with pytest.raises(StateError, match="'offset' is not a non-negative integer"):
        load_state(str(path))
    # a Windows st_dev is an unsigned 64-bit volume serial; NTFS file ids can be as large
    path.write_text(entry % (2**63 - 1, 2**64 - 1, 2**64 - 1), encoding="utf-8")
    got = load_state(str(path)).entries[("a", "/x")]
    assert got.ino == 2**64 - 1 and got.dev == 2**64 - 1


def test_naive_datetimes_are_refused() -> None:
    # a naive value would silently take the local offset; the two clocks must agree
    with pytest.raises(ValueError, match="naive datetime"):
        timestamp(datetime(2026, 9, 14, 12, 0, 0))
    with pytest.raises(ValueError, match="naive datetime"):
        State("x").expire(30, datetime(2026, 9, 14, 12, 0, 0))


def test_foreign_owner_is_only_root_against_another_uid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if sys.platform == "win32":
        assert _foreign_owner(str(tmp_path / "state.json")) is None  # no uids to compare
    else:
        path = tmp_path / "state.json"
        assert _foreign_owner(str(path)) is None  # absent
        path.write_text("{}", encoding="utf-8")
        if os.geteuid() != 0:
            assert _foreign_owner(str(path)) is None  # not root: nothing to refuse
            monkeypatch.setattr("logalert.state.os.geteuid", lambda: 0)
            assert _foreign_owner(str(path)) is not None  # root vs our own file
        else:
            assert _foreign_owner(str(path)) is None  # root's own file
            os.chown(path, 65534, 65534)  # nobody: the cron user, from root's chair
            assert _foreign_owner(str(path)) in ("nobody", "nogroup", "#65534")
            with pytest.raises(StateError, match="belongs to"):
                check_state_dir(str(path))


# -- the line count (#9) --------------------------------------------------------------------------


def test_the_line_count_round_trips_through_the_state_file(tmp_path: Path) -> None:
    path = str(tmp_path / "state.json")
    state = State(path)
    cursor = Cursor(offset=10, ino=1, dev=2, fingerprint=None, realpath="/var/log/r.log",
                    last_seen="2026-09-14T12:00:00Z", line=7)
    state.set("s", "/var/log/r.log", cursor)
    state.save()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["entries"]["s"]["/var/log/r.log"]["line"] == 7
    assert load_state(path).get("s", "/var/log/r.log") == cursor
@pytest.mark.parametrize("bad", ["-1", "true", '"3"', "1.5"])
def test_a_bad_line_count_is_a_hard_error(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "state.json"
    text = ('{"version": 1, "entries": {"a": {"/x": {"offset": 1, "ino": 2, "dev": 3, '
            '"fingerprint": null, "realpath": "/x", "last_seen": "2026-09-14T12:00:00Z", '
            '"line": BAD}}}}').replace("BAD", bad)
    path.write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(StateError, match="'line' is not a non-negative integer"):
        load_state(str(path))


# -- size and mtime (issue #44) ---------------------------------------------------------------


def test_size_and_mtime_round_trip_and_a_file_without_them_loads(tmp_path: Path) -> None:
    """What a compressed file looked like when it was last read, so an unchanged archive is
    not decompressed again; optional, so 0.1.0's state loads and 0.1.0 drops them on save."""
    path = str(tmp_path / "state.json")
    state = State(path)
    cursor = Cursor(offset=10, ino=1, dev=2, fingerprint=None, realpath="/var/log/r.log.gz",
                    last_seen="2026-09-14T12:00:00Z", line=7, size=4096, mtime=1758067200.25)
    state.set("s", "/var/log/r.log.gz", cursor)
    state.save()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entry = data["entries"]["s"]["/var/log/r.log.gz"]
    assert entry["size"] == 4096 and entry["mtime"] == 1758067200.25
    assert load_state(path).get("s", "/var/log/r.log.gz") == cursor
    Path(path).write_text(
        '{"version": 1, "entries": {"a": {"/x": {"offset": 1, "ino": 2, "dev": 3, '
        '"fingerprint": null, "realpath": "/x", "last_seen": "2026-09-14T12:00:00Z"}}}}',
        encoding="utf-8", newline="\n")
    loaded = load_state(path).get("a", "/x")
    assert loaded is not None and loaded.size is None and loaded.mtime is None


@pytest.mark.parametrize("field, bad, message", [
    ("size", "-1", "'size' is not a non-negative integer"),
    ("size", "1.5", "'size' is not a non-negative integer"),
    ("mtime", '"soon"', "'mtime' is not a number"),
    ("mtime", "true", "'mtime' is not a number"),
    ("mtime", "NaN", "'mtime' is not a number"),
])
def test_a_bad_size_or_mtime_is_a_hard_error(tmp_path: Path, field: str, bad: str,
                                             message: str) -> None:
    path = tmp_path / "state.json"
    text = ('{"version": 1, "entries": {"a": {"/x": {"offset": 1, "ino": 2, "dev": 3, '
            '"fingerprint": null, "realpath": "/x", "last_seen": "2026-09-14T12:00:00Z", '
            '"FIELD": BAD}}}}').replace("FIELD", field).replace("BAD", bad)
    path.write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(StateError, match=message):
        load_state(str(path))


# -- the run records (issue #18) ---------------------------------------------------------


GLOB = "/var/log/web/*.log"


def test_run_records_round_trip_and_an_older_file_has_none(tmp_path: Path) -> None:
    path = str(tmp_path / "state.json")
    state = State(path)
    state.set("web", "/var/log/web/a.log", cursor())
    state.record_run("web", (GLOB, "/var/log/other.log"), {GLOB}, NOW)
    state.save()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["runs"] == {"web": {GLOB: "2026-09-14T12:00:00Z"}}  # listed paths: no moment
    again = load_state(path)
    assert again.last_run("web", GLOB) == "2026-09-14T12:00:00Z"
    assert again.last_run("web", "/var/log/other.log") is None
    assert again.last_run("other", GLOB) is None
    Path(path).write_text('{"version": 1, "entries": {}}', encoding="utf-8")
    assert load_state(path).runs == {}  # from before #18: as if no section had run


def test_a_glob_not_looked_into_keeps_its_moment_and_a_dropped_glob_goes(tmp_path: Path) -> None:
    """The outage rule: a glob whose directory was away or unlistable keeps the moment of the
    last run that saw it; one never seen has none; one no longer configured is dropped."""
    state = State(str(tmp_path / "state.json"))
    other = "/var/log/hosts/*/messages"
    state.record_run("web", (GLOB, other), {GLOB, other}, NOW - timedelta(hours=1))
    state.record_run("web", (GLOB, other, "/var/log/new/*"), {GLOB}, NOW)
    assert state.runs["web"] == {GLOB: timestamp(NOW), other: timestamp(NOW - timedelta(hours=1))}
    state.record_run("web", (GLOB,), {GLOB}, NOW)
    assert state.runs["web"] == {GLOB: timestamp(NOW)}
    state.record_run("web", (GLOB,), set(), NOW)
    assert state.runs["web"] == {GLOB: timestamp(NOW)}  # unchanged, still on record


def test_forget_drops_the_records_of_the_sections_that_lost_an_entry(tmp_path: Path) -> None:
    state = State(str(tmp_path / "state.json"))
    for section, file in (("a", "/x"), ("b", "/x"), ("c", "/y")):
        state.set(section, file, cursor())
        state.record_run(section, (GLOB,), {GLOB}, NOW)
    state.record_run("empty", (GLOB,), {GLOB}, NOW)  # a record without entries
    assert state.forget("/x") == 2 and sorted(state.runs) == ["c", "empty"]
    assert state.forget() == 1 and state.runs == {} and state.entries == {}


def test_run_records_of_unconfigured_sections_expire_configured_ones_never(
    tmp_path: Path
) -> None:
    state = State(str(tmp_path / "state.json"))
    state.record_run("old", (GLOB,), {GLOB}, NOW - timedelta(days=31))
    state.record_run("edge", (GLOB,), {GLOB}, NOW - timedelta(days=30))
    state.record_run("kept", (GLOB,), {GLOB}, NOW - timedelta(days=400))
    state.record_run("empty", (GLOB,), set(), NOW)  # never looked into: nothing to keep
    assert state.expire_runs(30, NOW, configured=["kept"]) == ["old", "empty"]
    assert sorted(state.runs) == ["edge", "kept"]
    assert state.expire_runs(30, NOW, configured=["kept"]) == []


@pytest.mark.parametrize(
    ("runs", "fragment"),
    [
        ("[]", "'runs' is not an object"),
        ('{"a": 5}', "runs['a'] is not an object"),
        ('{"a": {"/x/*": 7}}', "runs['a']['/x/*'] is not a string"),
        ('{"a": {"/x/*": "yesterday"}}', "runs['a']['/x/*'] is not a UTC timestamp"),
    ],
)
def test_a_bad_run_record_is_a_hard_error_naming_the_remedy(
    tmp_path: Path, runs: str, fragment: str
) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version": 1, "entries": {}, "runs": ' + runs + "}", encoding="utf-8")
    with pytest.raises(StateError, match="start over with --reset-state") as exc:
        load_state(str(path))
    assert fragment in str(exc.value)


# -- a save that must reach a size (issue #70) --------------------------------------------------


def test_a_write_with_a_reach_proves_the_room_and_lands_the_text_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The padding is there at the first fsync and gone at the second, before the replace."""
    path = tmp_path / "state.json"
    sizes: list[int] = []
    real_fsync = os.fsync

    def fsync(fd: int) -> None:
        sizes.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr("logalert.state.os.fsync", fsync)
    text = '{"version": 1}' + chr(10)
    write_atomically(str(path), text, reach=len(text) + 5000)
    assert path.read_text(encoding="utf-8") == text
    assert sizes == [len(text) + 5000, len(text)]
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    write_atomically(str(path), text, reach=3)  # a reach the text already covers: no padding
    assert sizes[2:] == [len(text)] and path.read_text(encoding="utf-8") == text


def test_a_reach_the_disk_cannot_hold_is_the_usual_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    real_write = os.write

    def no_room(fd: int, data: bytes, /) -> int:  # the padding is what fails
        if len(data) > 64 and data != b"new":
            raise OSError(28, "No space left on device")
        return real_write(fd, data)

    monkeypatch.setattr("logalert.state.os.write", no_room)
    with pytest.raises(StateError, match=r"cannot write \(No space left on device\)"):
        write_atomically(str(path), "new", reach=8192)
    assert path.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_state_save_passes_the_reach_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[int | None] = []
    real = write_atomically

    def spy(path: str, text: str, reach: int | None = None) -> None:
        seen.append(reach)
        real(path, text, reach=reach)

    monkeypatch.setattr("logalert.state.write_atomically", spy)
    state = State(str(tmp_path / "state.json"))
    state.save()
    state.save(reach=9000)
    assert seen == [None, 9000] and state.render() == (tmp_path / "state.json").read_text(
        encoding="utf-8")


def test_a_short_write_is_completed_not_taken_for_the_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.write may write less than asked (measured on a tmpfs near its limit) and says
    nothing; the writer loops until every byte is out, the padding included."""
    path = tmp_path / "state.json"
    real_write = os.write
    sizes: list[int] = []
    real_fsync = os.fsync

    def short(fd: int, data: bytes, /) -> int:
        return real_write(fd, data[:7])

    def fsync(fd: int) -> None:
        sizes.append(os.fstat(fd).st_size)
        real_fsync(fd)

    monkeypatch.setattr("logalert.state.os.write", short)
    monkeypatch.setattr("logalert.state.os.fsync", fsync)
    text = '{"version": 1, "entries": {}, "runs": {}}' + chr(10)
    write_atomically(str(path), text, reach=len(text) + 100)
    assert path.read_text(encoding="utf-8") == text
    assert sizes == [len(text) + 100, len(text)]

# -- the anchor (issue #34) ------------------------------------------------------------------


def test_the_anchor_round_trips_and_a_file_without_it_loads(tmp_path: Path) -> None:
    """What the bytes before the offset hashed to as the run read them; optional, so
    0.1.0's state loads (trusted once) and 0.1.0 drops it on save."""
    path = str(tmp_path / "state.json")
    state = State(path)
    cursor = Cursor(offset=10, ino=1, dev=2, fingerprint="ab" * 32, realpath="/var/log/r.log",
                    last_seen="2026-09-14T12:00:00Z", line=7, anchor="cd" * 32)
    state.set("s", "/var/log/r.log", cursor)
    state.save()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["entries"]["s"]["/var/log/r.log"]["anchor"] == "cd" * 32
    assert load_state(path).get("s", "/var/log/r.log") == cursor
    Path(path).write_text(
        '{"version": 1, "entries": {"a": {"/x": {"offset": 1, "ino": 2, "dev": 3, '
        '"fingerprint": null, "realpath": "/x", "last_seen": "2026-09-14T12:00:00Z"}}}}',
        encoding="utf-8", newline="\n")
    loaded = load_state(path).get("a", "/x")
    assert loaded is not None and loaded.anchor is None
    state.touch("s", "/var/log/r.log")  # a sighting keeps it
    assert state.entries[("s", "/var/log/r.log")].anchor == "cd" * 32


@pytest.mark.parametrize("bad", ["7", "true", "[]"])
def test_a_bad_anchor_is_a_hard_error(tmp_path: Path, bad: str) -> None:
    path = tmp_path / "state.json"
    text = ('{"version": 1, "entries": {"a": {"/x": {"offset": 1, "ino": 2, "dev": 3, '
            '"fingerprint": null, "realpath": "/x", "last_seen": "2026-09-14T12:00:00Z", '
            '"anchor": BAD}}}}').replace("BAD", bad)
    path.write_text(text, encoding="utf-8", newline="\n")
    with pytest.raises(StateError, match="'anchor' is not a string"):
        load_state(str(path))


# -- root's own state and the empty file (issue #41) --------------------------------------------


def test_roots_own_state_is_not_foreign_and_another_users_is(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``uid == 0`` exception in ``_foreign_owner`` was asserted only under a real root
    euid (the sandbox); dropping it survived the suite as a non-root user. Faked uids run it
    everywhere: root's own file and directory are not foreign, another user's are."""
    if sys.platform == "win32":
        pytest.skip("no uids on Windows; runs in the sandbox and on CI")
    path = tmp_path / "state.json"
    path.write_text("{}", encoding="utf-8")
    owner = {"uid": 0}
    real_lstat, real_stat = os.lstat, os.stat

    def lstat_owned(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        st = real_lstat(target, *args, **kwargs)
        return os.stat_result(tuple(st)[:4] + (owner["uid"],) + tuple(st)[5:])  # st_uid

    def stat_owned(target: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        st = real_stat(target, *args, **kwargs)
        return os.stat_result(tuple(st)[:4] + (owner["uid"],) + tuple(st)[5:])

    monkeypatch.setattr("logalert.state.os.geteuid", lambda: 0)
    monkeypatch.setattr("logalert.state.os.lstat", lstat_owned)
    monkeypatch.setattr("logalert.state.os.stat", stat_owned)
    assert _foreign_owner(str(path)) is None  # the file is root's
    assert _foreign_owner(str(tmp_path / "absent.json")) is None  # its directory is root's
    owner["uid"] = 4242  # a uid with no name: reported the way sudo -u accepts it
    assert _foreign_owner(str(path)) == "#4242"
    assert _foreign_owner(str(tmp_path / "absent.json")) == "#4242"


def test_unknown_fields_load_under_version_1_and_are_dropped_on_save(tmp_path: Path) -> None:
    """The rollback property #18, #44, #65 and #34 rely on, unpinned until now: a file a
    newer logalert wrote under schema version 1 with a key or a cursor field this version
    does not know loads, and the save drops what it does not know."""
    path = tmp_path / "state.json"
    path.write_text(
        '{"version": 1, "entries": {"a": {"/x": {"offset": 1, "ino": 2, "dev": 3, '
        '"fingerprint": null, "realpath": "/x", "last_seen": "2026-09-14T12:00:00Z", '
        '"later": true}}}, "runs": {}, "future": {"key": 1}}',
        encoding="utf-8", newline="\n")
    state = load_state(str(path))
    loaded = state.get("a", "/x")
    assert loaded is not None and loaded.offset == 1
    state.save()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "future" not in data and "later" not in data["entries"]["a"]["/x"]
