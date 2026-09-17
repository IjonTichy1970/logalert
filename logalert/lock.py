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

The lock file is ``0600`` (issue #27): ``flock`` needs no write access, so a lock any local
user could open read-only was a lock any local user could hold, silently, for ``lock_stale``
and then with a stale-lock line blaming the last real run's PID. A lock left at ``0644`` by
an earlier version is tightened by the next holder that owns it. On POSIX a stale verdict
also says when the recorded holder is no longer a process (``os.kill(pid, 0)``: ``ESRCH``),
so the line points at ``fuser`` instead of a dead PID; Windows has no harmless probe
(``os.kill(pid, 0)`` terminates there), so the line is unchanged.

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
        # only a stale verdict is worth the probe: a fresh one is normal cron overlap
        self.holder_gone: bool | None = holder_gone(pid) if self.stale else None
        super().__init__(self.describe())

    def describe(self) -> str:
        who = f"PID {self.pid}" if self.pid is not None else "an unknown process"
        age = f"{self.age:.0f}s" if self.age is not None else "an unknown time"
        if self.holder_gone:
            return (f"the recorded holder PID {self.pid} is gone; another process holds the "
                    f"lock {self.path} (fuser {self.path} names it)")
        return f"another run ({who}) has held the lock {self.path} for {age}"


class RunLock:
    """Acquire with ``acquire()``; release on exit (``__exit__`` or the process ending)."""

    def __init__(self, path: str, stale_after: int) -> None:
        self.path = path
        self.stale_after = stale_after
        self.fd: int | None = None

    def acquire(self, now: float | None = None) -> None:
        """Take the lock or raise ``LockBusy`` describing the holder. Never blocks."""
        # O_NOFOLLOW: a planted symlink named lock in a directory another user owns would
        # otherwise be truncated and written through by a root run
        flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(self.path, flags, 0o600)
        try:
            _tighten(fd)  # before the lock: a held legacy 0644 lock would otherwise stay so
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
        try:
            moment = time.time() if now is None else now
            _write_holder(fd, f"{os.getpid()} {moment:.0f}\n".encode("ascii"))
            log.debug("lock %s taken (PID %d)", self.path, os.getpid())
        except BaseException:  # an OSError, or a signal landing here (issue #33)
            self.release()  # the lock was taken; do not keep it with no info behind it
            raise

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


def holder_gone(pid: int | None) -> bool | None:
    """Whether the process the lock file names is gone: True for ``ESRCH``, and for ``EPERM``
    too -- the lock is ``0600``, so a live process of another user with that PID cannot be
    the holder; the PID was reused. False when it exists as ours. None when nothing can be
    said: no PID, a number no kernel could have handed out (a hand-edited line), or Windows,
    where ``os.kill(pid, 0)`` would terminate it."""
    if sys.platform == "win32" or pid is None or pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return True
    except OverflowError:  # a PID that does not fit a C int
        return None
    return False


def _tighten(fd: int) -> None:
    """A lock we own that is readable by others (left by an earlier version) becomes
    ``0600``; a failure here is not the lock's business."""
    if sys.platform == "win32":
        return
    try:
        st = os.fstat(fd)
        if st.st_mode & 0o077 and st.st_uid == os.geteuid():
            os.fchmod(fd, 0o600)
            log.info("the lock was mode %04o; now 0600", st.st_mode & 0o777)
    except OSError:
        pass


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
