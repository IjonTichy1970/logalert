"""A fake sendmail for the transport and run tests: run as ``[sys.executable,
"fake_sendmail.py", ...]`` or through the platform wrapper ``install()`` builds.

It records what a real sendmail would see and then behaves as told:

- ``LOGALERT_FAKE_DIR`` (required): the directory that receives the records --
  ``pid.txt`` (its pid, written before stdin is read), ``argv.json`` (``sys.argv`` plus the
  parsed ``-i`` / ``-f`` / recipients), ``stdin.bin`` (the bytes the message consists of:
  everything with ``-i``; without it, as dma does, the message ends at the first line that
  is a single ``.`` -- the review showed a fake that reads it all cannot see a dropped flag).
  Those two are the LATEST call's; every call also leaves ``call-NNNN-argv.json`` and
  ``call-NNNN-stdin.bin``, so a run that mails several sections can be read back in order
- ``LOGALERT_FAKE_STDERR_BYTES``: write that many bytes of noise to stderr first
- ``LOGALERT_FAKE_SLEEP``: sleep that many seconds before exiting
- ``LOGALERT_FAKE_EXIT``: write dma's ``sendmail: bad mail input format`` line to stderr and
  exit with that code; otherwise exit 0. ``LOGALERT_FAKE_EXIT_IF_RCPT``: do so only when
  that address is among the recipients, so one section of a run can fail

Pure ASCII, stdlib only; nothing here needs root (measured under ``runuser -u nobody``).
"""

import json
import os
import sys
import time
from pathlib import Path


def install(directory: Path) -> Path:
    """This fake as a binary ``sendmail_path`` can name: a ``.cmd`` wrapper on Windows, a
    copy with the interpreter's shebang and mode 0755 on POSIX (both measured to pass argv
    and stdin through unchanged). Keep paths and addresses plain: the wrapper is cmd.exe
    text, so a caret, an ampersand or a percent in either breaks it there."""
    me = Path(__file__)
    if sys.platform == "win32":
        wrapper = directory / "sendmail.cmd"
        wrapper.write_text(f'@"{sys.executable}" "{me}" %*' + chr(10), encoding="ascii",
                           newline=chr(13) + chr(10))
    else:
        wrapper = directory / "sendmail"
        wrapper.write_text(f"#!{sys.executable}" + chr(10) + me.read_text(encoding="utf-8"),
                           encoding="utf-8", newline=chr(10))
        wrapper.chmod(0o755)
    return wrapper


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
    number = 1 + len([n for n in os.listdir(fake_dir) if n.endswith("-argv.json")])
    for name in ("argv.json", f"call-{number:04d}-argv.json"):
        with open(os.path.join(fake_dir, name), "w", encoding="ascii") as fh:
            json.dump(record, fh, indent=1, sort_keys=True)  # ensure_ascii keeps it ASCII
    for name in ("stdin.bin", f"call-{number:04d}-stdin.bin"):
        with open(os.path.join(fake_dir, name), "wb") as fh:
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
    only_for = os.environ.get("LOGALERT_FAKE_EXIT_IF_RCPT")
    if code and only_for and only_for not in record["recipients"]:  # type: ignore[operator]
        code = None
    if code:
        sys.stderr.write("sendmail: bad mail input format" + chr(10))
        sys.stderr.flush()
        return int(code)
    return 0


if __name__ == "__main__":
    sys.exit(main())
