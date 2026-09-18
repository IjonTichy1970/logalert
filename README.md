# logalert

logalert reads the log files you name, from where it left off last time, and
emails the lines that match your patterns -- one message per watch, with the
context you ask for, and nothing at all when nothing matched. It runs from cron
or a systemd timer, follows a log through rotation and compression without
mailing a line twice, and refuses to start rather than fail silently when it
cannot save its place or hand a message to the mail system. It is a Python
program with no runtime dependencies, installed from a release wheel into a
dedicated virtual environment on the Linux host that holds the logs.

A watch is a section of one INI file:

```ini
[logalert]
from = logalert@example.net

[router-disk]
subject = Router disk failure
to = noc@example.net
files = /var/log/router.log
patterns =
    disk failure
    Requesting reboot
```

and one cron line runs it, as the user that owns its state directory:

```
MAILTO=noc@example.net
*/5 * * * * /usr/local/bin/logalert
```

`logalert --example-config` prints the complete, commented configuration;
[docs/USAGE.md](docs/USAGE.md) is the operator's reference (every option, every
key, rotation, mail, logging, troubleshooting).

## Requirements

- **Linux.** That is what CI tests and the release is gated on. FreeBSD follows
  the same steps and is expected to work, but nothing gates it yet.
- **Python 3.11 or newer, as a versioned binary** (`python3.11`, `python3.12`,
  ...): the deployment venv must be created with the versioned name, for a
  reason [INSTALL.md](INSTALL.md) explains.
- **A mail transfer agent** providing `/usr/sbin/sendmail` (Postfix, Exim, dma,
  msmtp-mta), configured to relay to your mail server -- or an SMTP relay
  logalert can hand messages to directly (`transport = smtp`, no login: a relay
  that wants one is reached through msmtp or dma). A fresh server has neither;
  logalert says so and stops instead of running without one.

## Installation

logalert is distributed through **[GitHub
Releases](https://github.com/IjonTichy1970/logalert/releases)**, not PyPI --
`pip install logalert` will not work. The supported procedure is a dedicated
venv at `/opt/logalert-venv` and a symlink on `PATH`:

```bash
sudo python3.12 -m venv /opt/logalert-venv
sudo /opt/logalert-venv/bin/pip install ./logalert-X.Y.Z-py3-none-any.whl
sudo ln -s /opt/logalert-venv/bin/logalert /usr/local/bin/logalert
logalert --version
```

[INSTALL.md](INSTALL.md) has the whole of it: why the interpreter is versioned,
the configuration and state directory, the service user, the mail transport,
the cron line or the systemd timer, where the log is, upgrading, uninstalling
and a troubleshooting table.

## Documentation

- [docs/USAGE.md](docs/USAGE.md) -- options, configuration reference, position
  tracking and rotation, running it, mail, logging, troubleshooting.
- [INSTALL.md](INSTALL.md) -- installing, configuring, scheduling, upgrading.
- [CHANGELOG.md](CHANGELOG.md) -- every change, each linked to the issue that
  holds the decisions behind it; rendered at
  <https://ijontichy1970.github.io/logalert/>.

## Development

Developed on Windows, run on Linux. Create the venv with a **versioned**
interpreter, then install the package in editable mode with the dev and docs
extras:

```bash
python3.12 -m venv .venv            # Windows: py -3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,docs]"   # Windows: .venv/Scripts/python.exe
```

The floor is 3.11, and CI's 3.11 leg is the authority for it: develop against
3.11 where you have it; where you do not, a native run on 3.11 before the push
predicts what that leg will say.

The gate is one file, `tools/gate.sh`, and it is the only definition of green.
Run it with the venv's interpreter first on PATH and read its exit code directly
(never through a pipe):

```bash
export PATH="$PWD/.venv/bin:$PATH"   # Windows Git Bash: $PWD/.venv/Scripts
bash tools/gate.sh > gate.out 2>&1; rc=$?
```

On the Windows dev host the Linux-only stage runs inside a WSL sandbox; see
`CLAUDE.md` for the sandbox rules. The Claude Code hook in
`.claude/settings.json` runs the shell-text guard through `python`, then
`python3`, and refuses every Bash command when neither is on the PATH of the
shell Claude Code runs in (a stock Debian or Ubuntu has `python3` only);
the gate's `shell guard tests` stage runs that command as spelled, with the
venv off PATH, so an inert guard is a red gate. Changes are tracked in
[CHANGELOG.md](CHANGELOG.md).

## License

MIT -- see [LICENSE](LICENSE).
