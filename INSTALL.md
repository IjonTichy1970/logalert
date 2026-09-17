# Installing logalert

logalert is published as a Python wheel on the project's [GitHub
Releases](https://github.com/IjonTichy1970/logalert/releases) page. It is **not
on PyPI**: `pip install logalert` fails with `No matching distribution found` —
install from the release wheel as described here.

The pattern is a dedicated virtual environment at a fixed path,
`/opt/logalert-venv`, that holds **only** the venv. It is isolated from the
system Python, reproducible, and disposable: rebuilding it is remove + recreate
+ reinstall, with nothing else to reason about.

## Requirements

- Python **3.11 or newer**, available as a **versioned** binary (`python3.11`,
  `python3.12`, …). The venv must be created with that versioned name — step 1
  explains why.
- `pip` and the `venv` module. Debian/Ubuntu ship `venv` separately: `apt
  install python3.12-venv` (match your version).
- Outbound access to PyPI during install if the release has runtime dependencies
  — those are fetched from PyPI even though logalert itself is not (or pre-fetch
  their wheels on an air-gapped host).

Commands that touch `/opt` or `/usr/local` need root; they are shown with
`sudo`.

## Install

### 1. Create the venv with a versioned interpreter

```bash
sudo python3.12 -m venv /opt/logalert-venv
```

`python3.12` is an **example** — use whichever 3.11-or-newer version you have
(`python3.11 -m venv …` on Debian 12, `python3.13 -m venv …` on a 3.13 box).
The rule is to name *a* version, never bare `python3`.

Why: a venv is bound to the Python minor version that created it. Its packages
live in `lib/python3.X/site-packages`, and its `bin/python` resolves (via
`bin/python3.12`) to the interpreter that created it. `python3 -m venv` links to
the unversioned `python3` alias, so when an OS upgrade re-points that alias at a
newer minor, the venv silently starts running under a Python that looks in a
`site-packages` directory that does not exist — and every command fails with
`ModuleNotFoundError: No module named 'logalert'` although nothing on disk
changed. A versioned binary is not silently re-pointed; the venv keeps running
on 3.12 until you *choose* to rebuild it.

If your system ships only an unversioned `python3` with no versioned binary
beside it, `python3` is the only option — then plan to rebuild the venv after
every Python upgrade ([Upgrading Python](#upgrading-python)).

On a host whose default umask is `027` or `077` (`UMASK` in `/etc/login.defs`,
a hardening baseline), run every command that writes into the venv -- this
one, step 2's `pip install`, and every upgrade -- under `umask 022`:

```bash
sudo sh -c 'umask 022; python3.12 -m venv /opt/logalert-venv'
```

`pam_umask` applies that default to every `sudo` session whatever your own
shell's umask is (Debian and Ubuntu; measured on Ubuntu 24.04: a `077` shell
gets `0027` under `UMASK 027`, and a `077` shell gets `0022` under `UMASK
022`). A venv made `750` is one the service user cannot enter, and a package
pip installed `750`/`640` into a readable venv is one it cannot import: step
4's checks pass in a root shell (a non-root administrator sees an empty
`readlink` and `Permission denied` on the rest), and the first command run as
the service user fails. The service-user check at the end of step 5 catches
both; `sudo chmod -R o+rX /opt/logalert-venv` repairs a venv already made that
way -- until the next `pip install` made under that umask.

### 2. Download the wheel and install it

From the [Releases](https://github.com/IjonTichy1970/logalert/releases) page, or
on the command line (`X.Y.Z` = the release you want):

```bash
gh release download vX.Y.Z --repo IjonTichy1970/logalert --pattern '*.whl'
# without the gh CLI (FreeBSD: fetch instead of curl -LO):
curl -LO https://github.com/IjonTichy1970/logalert/releases/download/vX.Y.Z/logalert-X.Y.Z-py3-none-any.whl
```

Then:

```bash
sudo /opt/logalert-venv/bin/pip install ./logalert-X.Y.Z-py3-none-any.whl
```

On the hardened host of step 1, pip creates the package's files under the
umask it runs with, so this too goes under `umask 022`:

```bash
sudo sh -c 'umask 022; /opt/logalert-venv/bin/pip install ./logalert-X.Y.Z-py3-none-any.whl'
```

The `py3-none-any` wheel is pure Python — one file covers every supported Python
version and operating system.

### 3. Put the command on `PATH` — the `/usr/local/bin` symlink

```bash
sudo ln -s /opt/logalert-venv/bin/logalert /usr/local/bin/logalert
```

This is more than convenience. `sudo` replaces the caller's `PATH` with the
`secure_path` from `sudoers` (typically
`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`), which never
includes a venv's `bin/` — so `sudo logalert` works **only** because of this
symlink. The right Python still runs: pip writes the console script with an
absolute shebang into the venv's `bin/` (`#!/opt/logalert-venv/bin/python3.12`
with the commands above — the name mirrors the interpreter the venv was created
with), so following the symlink executes the venv's own interpreter regardless
of `PATH`.

### 4. Verify

```bash
readlink /opt/logalert-venv/bin/python     # → python3.12 — a versioned name; "python3" means the alias trap
readlink /opt/logalert-venv/bin/python3.12 # → /usr/bin/python3.12 (FreeBSD: /usr/local/bin/python3.12)
grep ^command /opt/logalert-venv/pyvenv.cfg # → the venv command that made it — versioned here too
head -1 /opt/logalert-venv/bin/logalert    # → starts with #!/opt/logalert-venv/bin/
sudo which logalert                        # → /usr/local/bin/logalert
logalert --version
ls /opt/logalert-venv                      # bin include lib [lib64] pyvenv.cfg — nothing else
```

Run these once after install and again after any Python upgrade. The first line
alone catches the alias problem before it bites. Once a configuration and a mail
transport exist (steps 5 and 6), `logalert --check-config` and `logalert
--test-mail SECTION` complete the check: the settings as logalert reads them,
and one real message through the transport.

### 5. Configuration and state

logalert reads `/etc/logalert.conf` and keeps its state under
`/var/lib/logalert` -- both **outside** the venv, which the upgrade and
uninstall procedures below wipe whole. The state directory must be **owned** by
the user the scheduled run executes as -- owned, not merely writable: the run
writes the state file (`0600`) and the run lock there, and a run as root against
another user's state directory is refused so that it cannot leave root-owned
files the real user then cannot touch. That refusal keys on the directory's
owner; a `root:logalert 2770` directory lets a root run through, and the
root-owned lock it leaves stops every later run of the service user
([Troubleshooting](#troubleshooting)). A dedicated system user is the usual
choice:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin logalert
sudo install -d -o logalert -g logalert -m 750 /var/lib/logalert
sudo sh -c 'logalert --example-config > /etc/logalert.conf'
sudo -u logalert /opt/logalert-venv/bin/logalert --version   # the venv is readable and executable by the service user
```

The last line is the service-user check step 4 could not make before the user
existed: `Permission denied` or `No module named 'logalert.__main__'` there is
the umask trap of steps 1 and 2.

Edit `/etc/logalert.conf`: at least one watch section with `subject`, `to`,
`files` and a pattern, and `from = ` in `[logalert]` unless the default (the
running user at this host's name) travels on your mail system. The file must be
readable by the service user and the watched logs must be too (the `adm` group
reads the syslog files under `/var/log` on Debian and Ubuntu: `sudo usermod -aG
adm logalert`). The check that reads the file back is at the end of step 6: it
needs the mail transport to exist first.

### 6. Mail

The default transport hands each message to the local `/usr/sbin/sendmail`; a
fresh server has none, and logalert refuses to start rather than run without
one (`transport = auto but /usr/sbin/sendmail does not exist: install an MTA
...`). Install one and point it at your mail server -- Postfix, `dma` or
`msmtp-mta` on Debian and Ubuntu (`dma` is in FreeBSD's base); on Debian, Exim
(`exim4-daemon-light`) is the default MTA and, where the standard install put
it, already provides `/usr/sbin/sendmail`: configure it rather than install a
second one beside it -- or set `transport = smtp` and `smtp_host` in
`[logalert]` to hand messages to a relay directly. That relay must accept mail
from this host without a login: SMTP AUTH is not supported, and a relay that
wants one (a `530`, `Authentication required`) is reached through an MTA that
can log in as the sendmail transport instead -- Postfix or Exim in their
smarthost configuration, `msmtp-mta` (`auth on`) or `dma`
([docs/USAGE.md](docs/USAGE.md#mail)). Then, as the service user:

```bash
sudo -u logalert logalert --check-config
sudo -u logalert logalert --test-mail router-disk
```

The first prints the effective settings and every file each watch will read,
exit 0 when the configuration is usable and 2 when it is not (before this step,
on a host without an MTA, it is exit 2 with `transport = auto but
/usr/sbin/sendmail does not exist ...`, and so is every run: nothing is read
until the transport exists). It touches neither the state nor the log
destination -- nor does it look for the state directory; the first run does. The
second sends one real one-line message to that section's recipients and prints
the transport's answer (exit 0 accepted, 1 not delivered, 2 configuration).
"Accepted for queueing" means the MTA took it; `mailq` shows whether it left. A
root `logalert -n` (dry run) is safe too: it reads, prints what it would send,
and creates nothing. [docs/USAGE.md](docs/USAGE.md#mail) covers the transports,
the From address and what an alert looks like.

### 7. Schedule it

logalert has no daemon mode: every run reads, mails, saves and exits. **cron**,
in the service user's crontab (`sudo crontab -u logalert -e`):

```
MAILTO=noc@example.net
*/5 * * * * /usr/local/bin/logalert
```

The absolute path is what carries the venv (cron's `PATH` is a short fixed one
that need not include `/usr/local/bin`, and the symlink's shebang picks the
venv's interpreter); `MAILTO` is where the one line of a failed run goes. A run
that finds nothing prints nothing, so cron mails nothing.

**systemd**, as a oneshot service and a timer -- an absolute `ExecStart`, the
service user, no daemonising:

```
# /etc/systemd/system/logalert.service
[Unit]
Description=logalert run

[Service]
Type=oneshot
User=logalert
ExecStart=/usr/local/bin/logalert

# /etc/systemd/system/logalert.timer
[Unit]
Description=Run logalert every five minutes

[Timer]
OnCalendar=*:0/5
AccuracySec=1m

[Install]
WantedBy=timers.target
```

```bash
sudo systemd-analyze verify /etc/systemd/system/logalert.service /etc/systemd/system/logalert.timer
sudo systemctl daemon-reload
sudo systemctl enable --now logalert.timer
```

`systemd-analyze verify` prints nothing when the units are sound. Under a timer
nothing mails the one line of a failed run; it is in the journal (next section)
at info, and `journalctl -p err -t logalert` finds each failed item's ERROR
record, the same text. Keep the default `log = syslog`
under a timer -- [docs/USAGE.md](docs/USAGE.md#running-it) explains why.

### 8. Where the log is

Every run records what it did -- the files read, the lines matched, each message
sent with its recipients and `Message-ID`, every failure -- through syslog by
default, tagged `logalert`. On a systemd host:

```bash
journalctl -t logalert
journalctl -p warning -t logalert --since today
```

Without `journalctl`, the syslog daemon's file for `user`-facility messages
holds the same lines: `/var/log/syslog` on Debian and Ubuntu (readable by root
and the `adm` group), `/var/log/messages` on Red Hat. For a file of logalert's
own, set `log = file:/var/log/logalert.log` in `[logalert]` and pre-create the
file owned by the service user -- `/var/log` is root's, so the user cannot
create it there (every run would then say `cannot open the activity log ...;
logging to stderr` and mail its records to cron), and a root run that is
refused still creates the file root-owned first; a pre-created file is
appended to by both without changing owner:

```bash
sudo install -o logalert -g logalert -m 640 /dev/null /var/log/logalert.log
```

Give it a logrotate stanza of its own, since nothing else rotates it:

```
# /etc/logrotate.d/logalert
/var/log/logalert.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 640 logalert logalert
}
```

Or `log = stderr` to see the records on the terminal. `--log DEST` picks any of
these for one run.

## Keep the venv directory disposable

Store **nothing of your own** in `/opt/logalert-venv` — no configuration, state,
or launcher scripts. The rebuild below empties that directory (`--clear`) and
the uninstall removes it; anything you keep inside goes with it.

## Upgrading logalert

Install the new wheel into the same venv; pip replaces the old version. Then
restart any running logalert process.

```bash
sudo /opt/logalert-venv/bin/pip install --upgrade ./logalert-X.Y.Z-py3-none-any.whl
```

On a host with a hardened default umask, under `umask 022` as in steps 1 and 2
(`sudo sh -c 'umask 022; ...'`), or the upgraded package is one the service
user cannot read.

## Upgrading Python

A **different operation**. The venv does not survive a Python minor-version
change (step 1). Expect it after a distribution release upgrade, which removes
the previous minor's packages; where interpreters are installed side by side
(FreeBSD `pkg`, Fedora) the old one usually stays and the venv keeps running
until you choose to rebuild. Either way, rebuild against the new interpreter,
then reinstall the wheel (download it again per step 2 if you no longer have
it):

```bash
sudo python3.13 -m venv --clear /opt/logalert-venv     # the NEW version
sudo /opt/logalert-venv/bin/pip install ./logalert-X.Y.Z-py3-none-any.whl
```

Two traps:

- `--clear` **wipes the directory first** — which is why it must hold nothing
  but the venv.
- `python3 -m venv --upgrade` is **not** the fix. It rewrites `pyvenv.cfg` and
  bootstraps a fresh `lib/python3.Y/site-packages` for the new version (holding
  only pip) but leaves the old tree — and your packages — behind, so the venv
  stays broken behind a plausible-looking repair.

## Uninstall

Stop the schedule first: `sudo crontab -u logalert -r`, or `sudo systemctl
disable --now logalert.timer` and remove the two unit files. Then:

```bash
sudo rm /usr/local/bin/logalert
ls /opt/logalert-venv           # look inside first — anything of yours in here is about to go
sudo rm -rf /opt/logalert-venv
```

`/etc/logalert.conf`, `/var/lib/logalert` (the state and the lock) and the
`logalert` user are left for you to remove deliberately.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'logalert'` from `/usr/local/bin/logalert` or the venv (as every user, root included; `No module named 'logalert.__main__'` as the service user alone, with root's `--version` fine, is the umask row below instead) | The venv's interpreter was upgraded out from under it. Confirm: `readlink /opt/logalert-venv/bin/python` prints the unversioned `python3`, and `/opt/logalert-venv/bin/python --version` reports a minor whose `python3.X/` directory under `/opt/logalert-venv/lib` is missing — or exists but holds only pip, meaning someone already ran `venv --upgrade`. `grep ^command /opt/logalert-venv/pyvenv.cfg` shows the last `venv` command that touched the directory: `python3 -m venv` is the alias trap, `--upgrade` the wrong repair. Same fix either way. (A traceback naming `/usr/local/bin/logalert` is still a venv problem — the symlink only resolves into it.) | Rebuild the venv against the new interpreter ([Upgrading Python](#upgrading-python)). Not `venv --upgrade`. |
| `cannot execute: required file not found` (bash 5.2+), `bad interpreter: No such file or directory` (older bash), or `sudo` / a service manager reporting `No such file or directory` for `/usr/local/bin/logalert` although the file exists — typically right after a **distribution release upgrade** | The venv's `bin/python3.X` symlink dangles — the old Python was **removed**, not just re-pointed. A release upgrade (Debian 12 → 13, Ubuntu LTS → LTS) drops the previous minor's packages as obsolete, so `/usr/bin/python3.X` is gone. The versioned-interpreter rule cannot prevent this; it only prevents the *silent* alias case above. Confirm: `readlink /opt/logalert-venv/bin/python3.X` names a path that no longer exists. | Rebuild against the new interpreter ([Upgrading Python](#upgrading-python)) and reinstall the same wheel — it is pure Python, so no new download is needed unless you no longer have it. Plan this step into every release upgrade. |
| `python3.12: command not found` | That versioned binary is not installed, or the example does not match your version | Use the version you have (`ls /usr/bin/python3.* /usr/local/bin/python3.*`); on a minimal box, bare `python3` plus rebuild-on-upgrade |
| `ensurepip is not available` (Debian/Ubuntu) | The `venv` module ships separately | `apt install python3.12-venv` (match your version) |
| `sudo: logalert: command not found` | No symlink in a `secure_path` directory | Create the `/usr/local/bin` symlink (step 3) or use the absolute venv path |
| `pip install logalert` fails or installs the wrong thing | logalert is not published to PyPI | Install from the release wheel (step 2) |
| A rebuild (`--clear`) or uninstall removed my config / launcher | They were stored inside the venv directory | Restore from backup; keep them outside ([Keep the venv directory disposable](#keep-the-venv-directory-disposable)) |
| `state directory /var/lib/logalert does not exist -- create it, owned by the user logalert runs as` (exit 1) | Step 5 was skipped, or the run is under a different user's `state_file` | Create it, owned by the service user (step 5); a root run never creates it for you |
| `state directory ... belongs to <user>; a run as root would leave the state and the lock root-owned ...` (exit 1) | `sudo logalert` against the service user's state | Run as that user: `sudo -u logalert logalert ...` |
| `transport = auto but /usr/sbin/sendmail does not exist: install an MTA ...` (exit 2) | No mail transfer agent on the host | Install one, or `transport = smtp` with `smtp_host` (step 6) |
| `not delivered via smtp (...): SMTPSenderRefused: 530 ...` from `--test-mail`, or the same `530` as `failed: [section] ...` in cron's mail every run (exit 1) | The relay wants a login (`Authentication required`); `transport = smtp` cannot log in | Reach it through an MTA that can, as the sendmail transport (`transport = auto`): Postfix or Exim in their smarthost configuration, `msmtp-mta` (`auth on`) or `dma` (step 6) |
| `[section] /var/log/x.log: Permission denied` (exit 1) | The service user cannot read that log | `sudo usermod -aG adm logalert` on Debian/Ubuntu, or a group/ACL of your own (step 5) |
| `sudo: unable to execute ...: Permission denied` at step 5 or 6, `/bin/sh: 1: /usr/local/bin/logalert: Permission denied` in cron's mail, or `status=203/EXEC` for the unit -- while the same commands work as root; or, with the venv itself readable, `ModuleNotFoundError: No module named 'logalert.__main__'` as the service user | The venv, or the package inside it, was made under a hardened default umask (`UMASK 027` or `077` in `/etc/login.defs`, applied to every `sudo` session by `pam_umask`): directories `750` or `700` the service user cannot enter, or files it cannot read | `sudo chmod -R o+rX /opt/logalert-venv` -- again after every `pip install` made under that umask -- or make the venv and its installs under `umask 022` (steps 1 and 2) |
| `warning: cannot open the activity log /var/log/logalert.log (Permission denied); logging to stderr` in cron's mail every run -- every record of a quiet run, four or five lines | `log = file:` names a file the service user cannot create (`/var/log` is root's), or a root run created it root-owned | Pre-create it owned by the service user (step 8); an existing one: `sudo chown logalert:logalert /var/log/logalert.log` |
| `state directory: Permission denied (/var/lib/logalert/lock) -- the lock file must belong to the user logalert runs as` (exit 1), `cannot take the run lock ... (Permission denied)` from `--reset-state`, or `state file ...: cannot read (Permission denied) -- is it owned by another user?` | A root run happened before the service user's first one, in a state directory root OWNS (`root:logalert 2770`): the lock and the state are root-owned `0600`. The refusal of root runs keys on the owner (of the state file once it exists, of the directory before), so a directory that is merely writable by the service user does not protect it | `sudo chown logalert:logalert /var/lib/logalert /var/lib/logalert/lock /var/lib/logalert/state.json` -- every position kept, and root's runs refused from then on. With no run alive the `lock` file may be removed instead, and a root-owned state replaced by `--reset-state` with no path (the lock it then takes is the user's), at the cost of every position |

## Platform notes

- **Linux** is what CI tests. **FreeBSD** follows the same steps: Python from
  `pkg` lands in `/usr/local/bin` (so `python3.12` is there), and `fetch`
  replaces `curl -LO`.
- **Windows**: the same pattern with `py -3.12 -m venv C:\logalert-venv` and the
  `Scripts\` layout (`C:\logalert-venv\Scripts\pip.exe`,
  `C:\logalert-venv\Scripts\logalert.exe`); there is no symlink step.

## Development install

Contributors use a separate, **editable** venv inside the source tree — see
[README.md](README.md). Never deploy that pattern, and never develop in the
deployment one. If `logalert --version` in the dev venv shows an old number
after a version bump, the editable install's metadata snapshot is stale — run
`.venv/bin/python -m pip install -e ".[dev,docs]"` again.
