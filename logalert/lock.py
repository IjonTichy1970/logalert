"""The run lock: one logalert run per state directory at a time.

Cron does not wait for the previous run; two runs reading the same cursors would both mail the
same lines and race on the state file. The lock is a real OS lock on both platforms
(``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows) so the overlap test runs on the gate.
The holder writes its PID and start time into the file for the run it turns away; the line
stays after release, so ``cat <state dir>/lock`` shows the last run. A run refused in the
few microseconds between a holder's lock and its write reads the PREVIOUS line, so a stale
verdict -- the only consequential one -- is confirmed by a second read after that window.
What a turned-away run does with ``LockBusy`` is the run loop's business (issue #12): the
design is that a blocked run logs the holder's PID and age and exits 0 quietly -- cron
overlap is normal -- while a holder older than ``lock_stale`` seconds is a stuck run, which
must not silence the watcher forever: one stderr line and exit 1.

The descriptor is held until exit and is never inherited by children (``os.open`` descriptors
are non-inheritable). ``flock`` locks travel with the open file description, so the lock
outlives a fork of the holder; on Windows ``msvcrt.locking`` is per handle. The locked byte
sits beyond the holder info so that info stays readable while the lock is held: Windows
refuses a read whose REQUESTED range overlaps a locked byte, which is why ``_INFO_CAP`` must
stay below ``_LOCK_BYTE`` and ``_read_holder`` must never ``readline()``.
"""

import errno
import logging
import math
import os
import sys
import time

log = logging.getLogger("logalert.lock")

_LOCK_BYTE = 4096  # beyond any holder info; Windows lets a byte past EOF be locked
_INFO_CAP = 200  # bytes of holder info a blocked run reads; below _LOCK_BYTE, see above
_RECHECK_AFTER = 0.2  # seconds; a stale verdict is confirmed with a second read

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class LockBusy(Exception):
    """Another run holds the lock. ``stale`` decides whether that is news."""

    def __init__(self, path: str, pid: int | None, started: float | None, stale_after: int,
                 now: float | None = None) -> None:
        self.path = path
        self.pid = pid
        self.started = started
        moment = time.time() if now is None else now
        self.age: float | None = None if started is None else max(0.0, moment - started)
        self.stale = self.age is not None and self.age > stale_after
        super().__init__(self.describe())

    def describe(self) -> str:
        who = f"PID {self.pid}" if self.pid is not None else "an unknown process"
        age = f"{self.age:.0f}s" if self.age is not None else "an unknown time"
        return f"another run ({who}) has held the lock {self.path} for {age}"


class RunLock:
    """Acquire with ``acquire()``; release on exit (``__exit__`` or the process ending)."""

    def __init__(self, path: str, stale_after: int) -> None:
        self.path = path
        self.stale_after = stale_after
        self.fd: int | None = None

    def acquire(self, now: float | None = None) -> None:
        """Take the lock or raise ``LockBusy`` describing the holder. Never blocks."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            _lock(fd)
        except OSError as exc:
            if exc.errno not in _BUSY_ERRNOS:
                os.close(fd)
                raise
            busy = LockBusy(self.path, *_read_holder(fd), self.stale_after, now)
            if busy.stale:
                # The holder writes its line a few statements after taking the lock; a run
                # that lands in that window reads the previous holder's line. A stale
                # verdict costs an alarm, so confirm it once the window has passed.
                time.sleep(_RECHECK_AFTER)
                busy = LockBusy(self.path, *_read_holder(fd), self.stale_after, now)
            os.close(fd)
            raise busy from None
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        moment = time.time() if now is None else now
        try:
            _write_holder(fd, f"{os.getpid()} {moment:.0f}\n".encode("ascii"))
        except OSError:
            self.release()  # the lock was taken; do not keep it with no info behind it
            raise
        log.debug("lock %s taken (PID %d)", self.path, os.getpid())

    def release(self) -> None:
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def _write_holder(fd: int, info: bytes) -> None:
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, info)


def _read_holder(fd: int) -> tuple[int | None, float | None]:
    """The PID and start time the holder wrote, if the file says anything usable."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        parts = os.read(fd, _INFO_CAP).decode("ascii", errors="replace").split()
        pid, started = int(parts[0]), float(parts[1])
    except (OSError, ValueError, IndexError):
        return None, None
    if not math.isfinite(started):
        return None, None
    return pid, started


if sys.platform == "win32":
    _BUSY_ERRNOS = frozenset({errno.EACCES, errno.EDEADLOCK})

    def _lock(fd: int) -> None:
        os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    _BUSY_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK})

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
