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

- Python **3.12 or newer**, available as a **versioned** binary (`python3.12`,
  `python3.13`, …). The venv must be created with that versioned name — step 1
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

`python3.12` is an **example** — use whichever 3.12-or-newer version you have
(`python3.13 -m venv …` on a 3.13 box). The rule is to name *a* version, never
bare `python3`.

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
alone catches the alias problem before it bites.

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

Stop any running logalert process (and disable its service unit, if you created
one) first.

```bash
sudo rm /usr/local/bin/logalert
ls /opt/logalert-venv           # look inside first — anything of yours in here is about to go
sudo rm -rf /opt/logalert-venv
```

Configuration and state files are left for you to remove deliberately.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'logalert'` from `/usr/local/bin/logalert` or the venv | The venv's interpreter was upgraded out from under it. Confirm: `readlink /opt/logalert-venv/bin/python` prints the unversioned `python3`, and `/opt/logalert-venv/bin/python --version` reports a minor whose `python3.X/` directory under `/opt/logalert-venv/lib` is missing — or exists but holds only pip, meaning someone already ran `venv --upgrade`. `grep ^command /opt/logalert-venv/pyvenv.cfg` shows the last `venv` command that touched the directory: `python3 -m venv` is the alias trap, `--upgrade` the wrong repair. Same fix either way. (A traceback naming `/usr/local/bin/logalert` is still a venv problem — the symlink only resolves into it.) | Rebuild the venv against the new interpreter ([Upgrading Python](#upgrading-python)). Not `venv --upgrade`. |
| `cannot execute: required file not found` (bash 5.2+), `bad interpreter: No such file or directory` (older bash), or `sudo` / a service manager reporting `No such file or directory` for `/usr/local/bin/logalert` although the file exists — typically right after a **distribution release upgrade** | The venv's `bin/python3.X` symlink dangles — the old Python was **removed**, not just re-pointed. A release upgrade (Debian 12 → 13, Ubuntu LTS → LTS) drops the previous minor's packages as obsolete, so `/usr/bin/python3.X` is gone. The versioned-interpreter rule cannot prevent this; it only prevents the *silent* alias case above. Confirm: `readlink /opt/logalert-venv/bin/python3.X` names a path that no longer exists. | Rebuild against the new interpreter ([Upgrading Python](#upgrading-python)) and reinstall the same wheel — it is pure Python, so no new download is needed unless you no longer have it. Plan this step into every release upgrade. |
| `python3.12: command not found` | That versioned binary is not installed, or the example does not match your version | Use the version you have (`ls /usr/bin/python3.* /usr/local/bin/python3.*`); on a minimal box, bare `python3` plus rebuild-on-upgrade |
| `ensurepip is not available` (Debian/Ubuntu) | The `venv` module ships separately | `apt install python3.12-venv` (match your version) |
| `sudo: logalert: command not found` | No symlink in a `secure_path` directory | Create the `/usr/local/bin` symlink (step 3) or use the absolute venv path |
| `pip install logalert` fails or installs the wrong thing | logalert is not published to PyPI | Install from the release wheel (step 2) |
| A rebuild (`--clear`) or uninstall removed my config / launcher | They were stored inside the venv directory | Restore from backup; keep them outside ([Keep the venv directory disposable](#keep-the-venv-directory-disposable)) |

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
