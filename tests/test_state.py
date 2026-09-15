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
    assert state.entries == {} and state.dirty is False
    assert state.get("router-disk", "/var/log/router.log") is None
    assert not (tmp_path / "state.json").exists()  # loading never creates it


def test_round_trip_and_the_on_disk_shape(tmp_path: Path) -> None:
    path = str(tmp_path / "state.json")
    state = State(path)
    state.set("router-disk", "/var/log/router.log", cursor(10))
    state.set("firewall", "/var/log/router.log", cursor(20, fp=None))
    assert state.dirty
    state.save()
    assert state.dirty is False
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["version"] == STATE_VERSION
    entry = data["entries"]["router-disk"]["/var/log/router.log"]
    assert entry == {"offset": 10, "ino": 1234, "dev": 56, "fingerprint": "ab" * 32,
                     "realpath": "/var/log/r.log", "last_seen": "2026-09-14T12:00:00Z",
                     "line": None}
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
    state.dirty = False
    state.touch("a", "/var/log/x.log", NOW)
    assert state.dirty
    got = state.get("a", "/var/log/x.log")
    assert got is not None and got.offset == 10 and got.last_seen == timestamp(NOW)
    state.touch("a", "/var/log/absent.log", NOW)  # no entry: no entry is invented
    assert state.get("a", "/var/log/absent.log") is None


def test_expire_drops_only_entries_older_than_the_ttl(tmp_path: Path) -> None:
    state = State(str(tmp_path / "state.json"))
    state.set("a", "/old", cursor(seen=NOW - timedelta(days=31)))
    state.set("a", "/edge", cursor(seen=NOW - timedelta(days=30)))
    state.set("b", "/new", cursor(seen=NOW - timedelta(hours=1)))
    state.dirty = False
    assert state.expire(30, NOW) == [("a", "/old")]
    assert sorted(state.entries) == [("a", "/edge"), ("b", "/new")]
    assert state.dirty
    state.dirty = False
    assert state.expire(30, NOW) == [] and state.dirty is False


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
    assert path.read_bytes() == before and state.dirty
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
    # cron's SIGTERM arrives as an exception too; the temp must not litter the state dir
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
