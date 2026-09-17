"""The run lock: a real OS lock on both platforms, the holder info, the stale rule (issue #7).

The overlap tests spawn tests/lock_holder.py as a second process, so the lock is exercised
across processes on the gate (msvcrt.locking on Windows, fcntl.flock in the sandbox and CI).
"""

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

import logalert.lock as lock_module
from logalert.lock import LockBusy, RunLock

HELPER = Path(__file__).with_name("lock_holder.py")


@contextmanager
def holder(path: Path, stale_after: int = 3600, started: float | None = None) -> Iterator[int]:
    """A second process holding the lock; yields its PID; releases on exit."""
    argv = [sys.executable, str(HELPER), str(path), str(stale_after)]
    if started is not None:
        argv.append(f"{started:.0f}")
    with subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace",
    ) as proc:
        assert proc.stdout is not None and proc.stdin is not None
        line = proc.stdout.readline().split()
        assert line and line[0] == "held", (line, proc.stderr.read() if proc.stderr else "")
        try:
            yield int(line[1])
        finally:
            proc.stdin.close()
            proc.wait(timeout=30)
    assert proc.returncode == 0


def try_holder(path: Path, stale_after: int = 3600) -> tuple[int, list[str]]:
    """Run the helper once; it should find the lock taken."""
    result = subprocess.run(
        [sys.executable, str(HELPER), str(path), str(stale_after)],
        stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace",
        check=False, timeout=30,
    )
    return result.returncode, result.stdout.split()


def test_acquire_writes_pid_and_start_time_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    lock = RunLock(str(path), 3600)
    lock.acquire(now=1_700_000_000.0)
    assert lock.fd is not None and not os.get_inheritable(lock.fd)  # never passed to children
    assert path.read_bytes() == f"{os.getpid()} 1700000000\n".encode("ascii")  # no CRLF
    lock.release()
    assert lock.fd is None
    assert path.read_bytes() == f"{os.getpid()} 1700000000\n".encode("ascii")  # kept: cat lock
    lock.release()  # idempotent
    with RunLock(str(path), 3600):  # re-acquirable after release
        pass


def test_second_acquire_in_the_same_process_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with RunLock(str(path), 3600):
        second = RunLock(str(path), 3600)
        with pytest.raises(LockBusy) as exc:
            second.acquire()
        assert exc.value.pid == os.getpid() and exc.value.stale is False
        assert second.fd is None


def test_second_process_is_refused_and_told_who_holds_it(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with holder(path) as pid:
        assert pid != os.getpid()
        with pytest.raises(LockBusy) as exc:
            RunLock(str(path), 3600).acquire()
        busy = exc.value
        assert busy.pid == pid and busy.stale is False
        assert busy.age is not None and 0 <= busy.age < 60
        assert str(busy) == f"another run (PID {pid}) has held the lock {path} for {busy.age:.0f}s"
    # the holder is gone: the lock is free again
    with RunLock(str(path), 3600):
        pass


def test_our_hold_refuses_a_second_process(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with RunLock(str(path), 3600):
        code, words = try_holder(path)
        assert code == 3 and words[0] == "busy", words
        assert int(words[1]) == os.getpid() and words[3] == "False"


def test_stale_holder_is_reported_as_such(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    two_hours_ago = time.time() - 7200
    with holder(path, stale_after=3600, started=two_hours_ago) as pid:
        with pytest.raises(LockBusy) as exc:
            RunLock(str(path), 3600).acquire()
        assert exc.value.pid == pid and exc.value.stale is True
        assert exc.value.age is not None and 7100 < exc.value.age < 7300
        # the same holder is not stale under a longer threshold
        with pytest.raises(LockBusy) as exc2:
            RunLock(str(path), 8000).acquire()
        assert exc2.value.stale is False


def test_unreadable_holder_info_is_still_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    with RunLock(str(path), 3600):
        monkeypatch.setattr("logalert.lock._read_holder", lambda fd: (None, None))
        with pytest.raises(LockBusy) as exc:
            RunLock(str(path), 3600).acquire()
        assert exc.value.pid is None and exc.value.age is None and exc.value.stale is False
        assert str(exc.value).startswith("another run (an unknown process) has held the lock")


def test_lock_busy_age_never_goes_negative() -> None:
    busy = LockBusy("/x/lock", 42, started=2_000.0, stale_after=10, now=1_000.0)
    assert busy.age == 0.0 and busy.stale is False


def test_other_open_errors_propagate(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        RunLock(str(tmp_path / "nope" / "lock"), 3600).acquire()


# -- the review's pins --------------------------------------------------------------------------


def test_refused_acquire_returns_at_once_and_leaks_no_descriptor(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with RunLock(str(path), 3600):
        probe = os.open(os.devnull, os.O_RDONLY)
        os.close(probe)
        started = time.monotonic()
        with pytest.raises(LockBusy):
            RunLock(str(path), 3600).acquire()
        assert time.monotonic() - started < 1.0  # LK_NBLCK / LOCK_NB, not the blocking form
        again = os.open(os.devnull, os.O_RDONLY)
        os.close(again)
    assert again == probe  # lowest free descriptor: the refused open was closed


def test_garbage_holder_info_is_busy_with_an_unknown_holder(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    with RunLock(str(path), 3600):
        for garbage in ("", "garbage", "4242", "x y", "4242 -inf", "4242 nan"):
            path.write_text(garbage, encoding="ascii")  # the info bytes are not locked
            with pytest.raises(LockBusy) as exc:
                RunLock(str(path), 3600).acquire()
            assert exc.value.pid is None and exc.value.age is None
            assert exc.value.stale is False, garbage


def test_info_write_failure_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "lock"
    real = lock_module._write_holder

    def disk_full(fd: int, info: bytes) -> None:
        if info:
            raise OSError(28, "No space left on device")
        real(fd, info)  # the release's emptying still works

    monkeypatch.setattr("logalert.lock._write_holder", disk_full)
    lock = RunLock(str(path), 3600)
    with pytest.raises(OSError, match="No space left"):
        lock.acquire()
    assert lock.fd is None
    monkeypatch.undo()
    with RunLock(str(path), 3600):  # not stuck held by the failed attempt
        pass


def test_stale_verdict_is_confirmed_by_a_second_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a run refused between the holder's lock and its write sees a CRASHED earlier holder's
    # line; the re-read after the window sees the real one
    path = tmp_path / "lock"
    stale_line = f"4242 {time.time() - 86400:.0f}" + chr(10)
    naps: list[float] = []

    def holder_writes_during_the_nap(seconds: float) -> None:
        naps.append(seconds)
        path.write_text(f"{os.getpid()} {time.time():.0f}" + chr(10), encoding="ascii")

    monkeypatch.setattr("logalert.lock.time.sleep", holder_writes_during_the_nap)
    with RunLock(str(path), 3600):
        path.write_text(stale_line, encoding="ascii")
        with pytest.raises(LockBusy) as exc:
            RunLock(str(path), 3600).acquire()
    assert naps == [0.2]
    assert exc.value.stale is False and exc.value.pid == os.getpid()
    # and a line that stays stale through the re-read is stale
    monkeypatch.setattr("logalert.lock.time.sleep", lambda seconds: naps.append(seconds))
    with RunLock(str(path), 3600):
        path.write_text(stale_line, encoding="ascii")
        with pytest.raises(LockBusy) as exc:
            RunLock(str(path), 3600).acquire()
    assert exc.value.stale is True and exc.value.pid == 4242 and len(naps) == 2


# -- issue #27: the lock's mode, and a stale line that does not blame a dead PID ---------------


def _dead_pid() -> int:
    """A PID that was a process and is not: spawned, exited, reaped."""
    with subprocess.Popen([sys.executable, "-c", "pass"]) as proc:
        proc.wait(timeout=30)
    return proc.pid


def test_the_lock_is_0600_and_an_older_0644_lock_is_tightened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """flock needs no write access: a lock others could open read-only was a lock others
    could hold (issue #27). A lock left at 0644 by 0.1.0 heals when its owner next takes it.
    The creation mode is checked with the heal disabled: otherwise a lock created 0644 and
    tightened a microsecond later would pass, and that microsecond is an open."""
    if sys.platform == "win32":
        pytest.skip("mode bits are fabricated on Windows; runs in the sandbox and on CI")
    path = tmp_path / "lock"
    monkeypatch.setattr(lock_module, "_tighten", lambda fd: None)
    with RunLock(str(path), 3600):
        assert path.stat().st_mode & 0o777 == 0o600  # created so, not healed so
    monkeypatch.undo()
    path.chmod(0o644)
    with RunLock(str(path), 3600):
        assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_stale_line_names_a_gone_holder_instead_of_blaming_its_pid() -> None:
    """The stale verdict is about the recorded start time; whether that PID still exists is
    a second question, answered on POSIX (``os.kill(pid, 0)``) and not on Windows, where the
    probe would terminate the process."""
    gone = _dead_pid()
    busy = LockBusy("/var/lib/logalert/lock", gone, started=time.time() - 7200, stale_after=3600)
    assert busy.stale is True
    if sys.platform == "win32":
        assert busy.holder_gone is None
        assert busy.describe() == (f"another run (PID {gone}) has held the lock "
                                   f"/var/lib/logalert/lock for 7200s")
        return
    assert busy.holder_gone is True
    assert busy.describe() == (f"the recorded holder PID {gone} is gone; another process holds "
                               f"the lock /var/lib/logalert/lock (fuser /var/lib/logalert/lock "
                               f"names it)")
    # a stale holder that still exists (this process) is named as before
    alive = LockBusy("/var/lib/logalert/lock", os.getpid(), started=time.time() - 7200,
                     stale_after=3600)
    assert alive.holder_gone is False
    assert alive.describe().startswith(f"another run (PID {os.getpid()}) has held the lock")
    # and a fresh holder is never probed: cron overlap is normal
    fresh = LockBusy("/var/lib/logalert/lock", gone, started=time.time() - 5, stale_after=3600)
    assert fresh.stale is False and fresh.holder_gone is None
    assert fresh.describe().startswith(f"another run (PID {gone}) has held the lock")


def test_holder_gone_answers_nothing_for_no_pid_or_a_non_positive_one() -> None:
    assert lock_module.holder_gone(None) is None
    assert lock_module.holder_gone(0) is None  # os.kill(0, 0) would signal the process group
    assert lock_module.holder_gone(-1) is None
    # a hand-edited line with a number no kernel could hand out: os.kill would raise
    # OverflowError past the OSError handlers and out of acquire() as a traceback (review)
    busy = LockBusy("/x/lock", 10**30, started=0.0, stale_after=1, now=100.0)
    assert busy.stale is True and busy.holder_gone is None
    assert busy.describe().startswith("another run (PID 1000000000000000000000000000000)")


def test_a_live_process_of_another_user_with_the_recorded_pid_is_not_the_holder(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """EPERM from the probe means a process exists as another user's: with a 0600 lock that
    cannot be the holder, so the PID was reused (review). Simulated: the real answer needs
    another user's process, which the sandbox's root run has (PID 1)."""
    if sys.platform == "win32":
        pytest.skip("os.kill(pid, 0) is a termination on Windows; the probe is POSIX-only")

    def eperm(pid: int, sig: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", eperm)  # the one os module, as lock.py sees it
    assert lock_module.holder_gone(4242) is True


# -- the unsaved marker in the holder line (issue #70) ------------------------------------------


def test_the_unsaved_marker_is_written_in_place_carried_forward_and_cleared(
        tmp_path: Path) -> None:
    path = tmp_path / "lock"
    lock = RunLock(str(path), 3600)
    lock.acquire(now=1_700_000_000.0)
    assert lock.unsaved is None
    lock.mark_unsaved(6100)
    assert lock.unsaved == 6100
    assert path.read_bytes() == f"{os.getpid()} 1700000000 unsaved 6100\n".encode("ascii")
    lock.release()
    # the next holder reads it before writing its own line, and carries it forward
    again = RunLock(str(path), 3600)
    again.acquire(now=1_700_000_010.0)
    assert again.unsaved == 6100
    assert path.read_bytes() == f"{os.getpid()} 1700000010 unsaved 6100\n".encode("ascii")
    again.clear_unsaved()
    assert again.unsaved is None
    assert path.read_bytes() == f"{os.getpid()} 1700000010\n".encode("ascii")
    again.release()
    third = RunLock(str(path), 3600)
    third.acquire()
    assert third.unsaved is None
    third.release()


def test_the_holder_line_is_written_in_place_never_truncated_first(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of #70: a page given up (ftruncate to 0, then the write) went to a competing
    writer on a full disk 216 times in 300, the holder line and its marker with it;
    written in place and cut afterwards, 0 in 300. The order is pinned here."""
    calls: list[tuple[str, int]] = []
    real_write, real_truncate = os.write, os.ftruncate

    def write(fd: int, data: bytes, /) -> int:
        calls.append(("write", len(data)))
        return real_write(fd, data)

    def truncate(fd: int, length: int, /) -> None:
        calls.append(("truncate", length))
        real_truncate(fd, length)

    monkeypatch.setattr("logalert.lock.os.write", write)
    monkeypatch.setattr("logalert.lock.os.ftruncate", truncate)
    path = tmp_path / "lock"
    lock = RunLock(str(path), 3600)
    lock.acquire(now=1_700_000_000.0)
    lock.mark_unsaved(6100)
    lock.clear_unsaved()
    lock.release()
    kinds = [kind for kind, _ in calls]
    assert kinds == ["write", "truncate"] * 3  # never a truncate before a write
    assert all(length > 0 for kind, length in calls if kind == "truncate")


def test_the_readers_parse_the_first_line_only(tmp_path: Path) -> None:
    """A kill between a write and its cut leaves the old tail after the new line: a
    cleared line with the old marker behind it is no marker."""
    path = tmp_path / "lock"
    path.write_bytes(b"12345 1700000000\nunsaved 6100\n")
    lock = RunLock(str(path), 3600)
    lock.acquire()
    assert lock.unsaved is None
    lock.release()
    path.write_bytes(b"12345 1700000000 unsaved 6100\n0\n")
    lock = RunLock(str(path), 3600)
    lock.acquire()
    assert lock.unsaved == 6100
    lock.release()


def test_a_shorter_marker_over_a_longer_line_leaves_no_tail(tmp_path: Path) -> None:
    """The line is cut to the marker: a size with fewer digits than the last one leaves no
    digits of the old one behind."""
    path = tmp_path / "lock"
    lock = RunLock(str(path), 3600)
    lock.acquire(now=1_700_000_000.0)
    lock.mark_unsaved(1_000_000)
    lock.mark_unsaved(42)
    assert path.read_bytes() == f"{os.getpid()} 1700000000 unsaved 42\n".encode("ascii")
    lock.release()


@pytest.mark.parametrize("line", [b"12345 1700000000 unsaved\n", b"12345 1700000000 other 6\n",
                                  b"12345 1700000000 unsaved x\n", b"12345 1700000000 unsaved -1\n",
                                  b"12345 1700000000 unsaved 99999999999999\n",  # no state file
                                  b"garbage\n", b""])
def test_other_words_after_the_holder_are_no_marker(tmp_path: Path, line: bytes) -> None:
    path = tmp_path / "lock"
    path.write_bytes(line)
    lock = RunLock(str(path), 3600)
    lock.acquire()
    assert lock.unsaved is None
    lock.release()


def test_the_stale_check_reads_past_the_marker(tmp_path: Path) -> None:
    """The busy path parses the first two words as before, marker or not."""
    path = tmp_path / "lock"
    lock = RunLock(str(path), 3600)
    lock.acquire(now=time.time() - 7200)
    lock.mark_unsaved(6100)
    try:
        rc, words = try_holder(path)
    finally:
        lock.release()
    assert rc == 3 and words[:2] == ["busy", str(os.getpid())] and words[3] == "True"


def test_marking_without_the_lock_is_a_no_op(tmp_path: Path) -> None:
    lock = RunLock(str(tmp_path / "lock"), 3600)
    lock.mark_unsaved(1)
    lock.clear_unsaved()
    assert lock.unsaved is None and not (tmp_path / "lock").exists()
