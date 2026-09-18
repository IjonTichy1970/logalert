"""A second process for the run-lock tests: hold the lock until stdin closes, or report busy.

usage: lock_holder.py LOCK_PATH STALE_AFTER [STARTED_EPOCH]

Prints one line and flushes: ``held <pid>`` (then waits for stdin to close, releases, exits
0), ``busy <pid> <age> <stale>`` (exit 3), or ``error <text>`` (exit 4). STARTED_EPOCH
fabricates the holder's start time so the stale rule can be tested without waiting.
"""

import os
import sys

from logalert.lock import LockBusy, RunLock


def main() -> int:
    path, stale_after = sys.argv[1], int(sys.argv[2])
    started = float(sys.argv[3]) if len(sys.argv) > 3 else None
    lock = RunLock(path, stale_after, key="state.json")
    try:
        lock.acquire(now=started)
    except LockBusy as exc:
        age = "?" if exc.age is None else f"{exc.age:.0f}"
        print(f"busy {exc.pid} {age} {exc.stale}", flush=True)
        return 3
    except OSError as exc:
        print(f"error {exc}", flush=True)
        return 4
    print(f"held {os.getpid()}", flush=True)
    sys.stdin.read()  # the test closes our stdin when it is done with us
    lock.release()
    return 0


if __name__ == "__main__":
    sys.exit(main())
