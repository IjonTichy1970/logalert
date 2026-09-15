"""A fake sendmail for the transport tests: run as ``[sys.executable, "fake_sendmail.py", ...]``
or through the platform wrapper ``fake_mta()`` in test_transport.py builds.

It records what a real sendmail would see and then behaves as told:

- ``LOGALERT_FAKE_DIR`` (required): the directory that receives the records --
  ``pid.txt`` (its pid, written before stdin is read), ``argv.json`` (``sys.argv`` plus the
  parsed ``-i`` / ``-f`` / recipients), ``stdin.bin`` (the bytes the message consists of:
  everything with ``-i``; without it, as dma does, the message ends at the first line that
  is a single ``.`` -- the review showed a fake that reads it all cannot see a dropped flag)
- ``LOGALERT_FAKE_STDERR_BYTES``: write that many bytes of noise to stderr first
- ``LOGALERT_FAKE_SLEEP``: sleep that many seconds before exiting
- ``LOGALERT_FAKE_EXIT``: write dma's ``sendmail: bad mail input format`` line to stderr and
  exit with that code; otherwise exit 0

Pure ASCII, stdlib only; nothing here needs root (measured under ``runuser -u nobody``).
"""

import json
import os
import sys
import time


def parse_args(args: list[str]) -> dict[str, object]:
    dash_i = False
    envelope_from: str | None = None
    recipients: list[str] = []
    other_options: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "-i":
            dash_i = True
        elif arg == "-f":
            i += 1
            envelope_from = args[i] if i < len(args) else None
        elif arg.startswith("-f") and len(arg) > 2:
            envelope_from = arg[2:]
        elif arg.startswith("-"):
            other_options.append(arg)
        else:
            recipients.append(arg)
        i += 1
    return {
        "dash_i": dash_i,
        "envelope_from": envelope_from,
        "recipients": recipients,
        "other_options": other_options,
    }


def _until_dot(data: bytes) -> bytes:
    """What a sendmail without -i keeps: the lines before the first lone ``.`` line."""
    lf = chr(10).encode()
    kept: list[bytes] = []
    for line in data.split(lf):
        if line.rstrip(chr(13).encode()) == b".":
            return lf.join(kept) + (lf if kept else b"")
        kept.append(line)
    return data


def main() -> int:
    fake_dir = os.environ.get("LOGALERT_FAKE_DIR")
    if not fake_dir:
        sys.stderr.write("fake_sendmail: LOGALERT_FAKE_DIR is not set" + chr(10))
        return 78  # EX_CONFIG
    os.makedirs(fake_dir, exist_ok=True)

    with open(os.path.join(fake_dir, "pid.txt"), "w", encoding="ascii") as fh:
        fh.write(f"{os.getpid()}" + chr(10))

    data = sys.stdin.buffer.read()

    record = parse_args(sys.argv[1:])
    if not record["dash_i"]:  # dma: `if (!nodot && linelen == 2 && line[0] == '.') break;`
        data = _until_dot(data)
    record["argv"] = list(sys.argv)
    record["stdin_size"] = len(data)
    record["cwd"] = os.getcwd()
    if sys.platform != "win32":  # mypy narrows on sys.platform, not on hasattr
        record["uid"] = os.getuid()
        record["euid"] = os.geteuid()
    with open(os.path.join(fake_dir, "argv.json"), "w", encoding="ascii") as fh:
        json.dump(record, fh, indent=1, sort_keys=True)  # ensure_ascii keeps it ASCII
    with open(os.path.join(fake_dir, "stdin.bin"), "wb") as fh:
        fh.write(data)

    noise = os.environ.get("LOGALERT_FAKE_STDERR_BYTES")
    if noise:
        remaining = int(noise)
        line = "e" * 79 + chr(10)
        while remaining > 0:
            chunk = line[:remaining]
            sys.stderr.write(chunk)
            remaining -= len(chunk)
        sys.stderr.flush()

    sleep = os.environ.get("LOGALERT_FAKE_SLEEP")
    if sleep:
        time.sleep(float(sleep))

    code = os.environ.get("LOGALERT_FAKE_EXIT")
    if code:
        sys.stderr.write("sendmail: bad mail input format" + chr(10))
        sys.stderr.flush()
        return int(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
