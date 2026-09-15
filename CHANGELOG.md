# Changelog

All notable changes to this project are recorded here. The `[Unreleased]`
section accumulates changes as they merge; the release step rolls it under a
version, and the GitHub Release for that version carries the rolled section as
its notes.

**Everything gets an entry.** Internal work (CI, tooling, refactors, tests,
hardening) is recorded too, because later corrections, guards and post-mortems
cite it. All of it goes under **Nitty Gritty**, the only category, last in each
version.

⚠️ **Every entry is tagged `[contract]` or `[internal]`, and the tag is
load-bearing, not decorative:**

- **`[contract]`** -- touches anything an operator's deployment depends on: the
  command-line surface, the configuration format, on-disk state and log layout,
  the service unit, the install procedure. The release step scans for these and
  writes the upgrade notes from them.
- **`[internal]`** -- no operator-visible surface. Everything else.

⚠️ **An untagged `[contract]` change is worse than an unrecorded one.**
Operators upgrade on their own schedule, so the release notes are the only
warning they get: the release step would report "no contract changes -- nothing
to do on upgrade", and be believed. **If you are unsure which a change is, it is
`[contract]`. An untagged entry stops the release.**

Format: `- ` + the tag in backticks + an optional marker + a **bold headline
sentence** + `(#N)` + a period + the body, wrapped at 80 display columns with
two-space continuation (`tools/reflow_md.py --check` enforces; reflow only the
new block, never the whole file). Markers: 🚨 a wrong claim or a check that
measured nothing; ⭐ the insight or the control that proved it; ⚠️ a caveat or
standing rule; 🔶 an owner's call, with its reasoning. The prose separator is
`--`; version headings use the em dash. A false claim being corrected is quoted
*"in italics inside double quotes"*, left in place, and cross-referenced from a
new entry.

## [Unreleased]

### Nitty Gritty

- `[contract]` **Project bootstrap: packaging, CI, and the release workflow**
  (#1). `pyproject.toml` is the single source of the version (`0.1.0`), with
  `logalert.__version__` derived from installed metadata; MIT as a PEP 639 SPDX
  expression. A `logalert` console entry point with `--version` and smoke tests.
  CI on a 3.12 / 3.13 / 3.14 matrix mirrored by the classifiers, with a docs
  `paths-ignore`. `MANIFEST.in`, `README.md`, `INSTALL.md`, LF line endings, and
  the project `CLAUDE.md`. Distribution is GitHub Releases, not PyPI; the first
  release waits until the watcher does something useful.

- `[contract]` **Install guide and design constraints from the venv deployment
  pattern** (#3). `INSTALL.md` documents the dedicated `/opt/logalert-venv`
  pattern end to end: a venv created with a versioned interpreter and why (a
  venv is bound to the Python minor that created it, and a bare `python3 -m
  venv` breaks silently when an OS upgrade re-points the alias), the wheel from
  GitHub Releases, the `/usr/local/bin` symlink that is what makes `sudo
  logalert` resolve under `secure_path`, verification, upgrading logalert versus
  upgrading Python, uninstall, and a troubleshooting table. ⭐ Four claims
  inherited from the reference pattern were corrected after being reproduced on
  Ubuntu 24.04: the console-script shebang mirrors the interpreter name the venv
  was created with, so it is `bin/python3.12` with the documented commands and
  not *"`bin/python`"*; a *"recently modified `pyvenv.cfg`"* cannot occur when
  only the alias moved and instead marks a `venv --upgrade` attempt (the
  `command =` line in that file is the exact fingerprint); `venv --upgrade`
  bootstraps a new `site-packages` rather than *"re-linking `bin/python`"*; and
  bash 5.2+ reports a dangling interpreter as `cannot execute: required file not
  found`. `CLAUDE.md` gains the deployment-model constraints the watcher must
  satisfy: console scripts over `python -m`, config and state outside the venv,
  foreground mode and `SIGHUP` under a service manager.

- `[internal]` **Adopt the Windows-plus-WSL handoff kit: sandbox, shell-text
  hook, changelog doctrine, and the gate** (#4). `tools/gate.sh` is now the only
  definition of green: `ascii`, `shell guard tests`, `markdown width`,
  `changelog refs`, `ruff` (a `warning:` line is red), `mypy` (strict),
  `pytest`, and a Linux-only stage delegated into the `rlyeh-sandbox` WSL distro
  on Windows and run natively on CI under `LOGALERT_CHECK_MODE=required`, where
  a skip is a failure; CI invokes the gate with `fetch-depth: 0` so the refs
  stage can read history. A `PreToolUse` hook
  (`.claude/hooks/guard_shell_text.py`, committed, with its test as a gate
  stage) refuses the four Bash command shapes that mangle escape-bearing text or
  truncate a file. This changelog moves to the doctrine in its header:
  everything gets an entry, one category, load-bearing tags, 80 columns. Gated
  Python is ASCII, enforced by `tools/check_ascii.py`, which caught its own
  first draft: a backslash-u escape written through the tool-call transport
  arrived as the literal character. Re-measured, the same escape arrived decoded
  in some calls and literal in others, while the other escapes probed (`\n`,
  `\t`, `\\`, `\r`, backticks, `$`) arrived literally every time -- so gated
  code spells code points as `chr(0x...)` and never writes the escape. ⚠️ mypy
  on the Windows dev host must be the pure-Python build (`pip install
  --no-binary mypy mypy`): the compiled wheel is blocked by an Application
  Control policy.

- `[contract]` **The configuration file: INI sections, a loader that validates
  everything, `--check-config` and `--example-config`** (#6). One file, any
  number of watch sections plus a reserved `[logalert]` section; every key of
  the 0.1.0 schema is parsed in `logalert/config.py` and nowhere else. Patterns
  are case-sensitive by default (`ipatterns` / `iregex` opt in); noise exclusion
  in the same four shapes; priority tags `[high] `, `[medium] `, `[low] ` with
  the priority system off unless configured; `report`, `start`, `archive_dir`,
  `max_lines`, `context`. Paths must be absolute and globs are refused so a glob
  is never silently a literal path; addresses are bare `local@domain`; an empty
  list entry is an error rather than a match-everything pattern; `%` is literal;
  a duplicate section or key is an error with its line number; `[DEFAULT]` is
  refused because `configparser` would merge it into every section. ⚠️ A
  continuation line beginning with `#` or `;` is dropped by `configparser` as a
  comment -- documented and pinned, not fixable inside INI. `--check-config`
  prints the effective settings -- including whether the sendmail binary exists
  AND is executable, and the effective From address with a warning when it would
  not travel beyond this host -- and exits 0 or 2 without touching state;
  `--example-config` prints the commented example that ships inside the package,
  so an installed host has it and the docs quote it. ⭐ The adversarial review
  reproduced a `--check-config` that called a non-executable sendmail "found",
  `re.compile` raising `OverflowError` past the `re.error` handler, and a path
  with an embedded newline passing every check -- all fixed and pinned.

- `[contract]` **Cursor and state: file identity, truncation, first sight at the
  end, atomic per-section state, the run lock** (#7). `logalert/state.py` owns
  the state file (`state_file`, default `/var/lib/logalert/state.json`): JSON
  with a schema version, one cursor per (section, configured path) -- a file two
  sections watch has two cursors, so the section whose mail failed keeps its
  place while the other advances -- written atomically (a temp file in the same
  directory, `fsync`, `os.replace`; `mkstemp` makes it 0600, so the state
  directory belongs to the user cron runs logalert as) after each section's mail
  is accepted; `check_state_dir` proves the directory is writable BEFORE
  anything is sent and never creates it. A file that cannot be parsed is a hard
  error naming the file and `--reset-state`; entries unseen for `state_ttl` days
  expire. `logalert/cursor.py` opens each file, `fstat`s the descriptor it
  reads, and applies the identity rules: same inode with the same first line
  continues; a different first line on the same inode is a rotation (ext4 hands
  a freed inode number straight back); a smaller size is `copytruncate`; a
  different inode is a rotation; a device id change alone continues with one log
  line. First sight starts at the end, on a line boundary, and logs the bytes
  skipped; `start = beginning` (and `--from-start`, wired in #12) read from 0.
  Lines are read in binary and decoded with replacement; the cursor advances
  only past complete lines; a line longer than 2000 bytes is cut there, and the
  cut is a hard boundary so the boundaries from a given offset are a function of
  the bytes alone. A run of NUL bytes at a line start -- the hole `copytruncate`
  leaves under a writer without `O_APPEND` -- is consumed before the cap is
  measured, counted, and logged once per file. `.gz`, `.bz2` and `.xz` (and
  `.zst` on 3.14+) are read through the decompressor with the offset in the
  uncompressed stream, `tell()` after the seek detecting a shorter stream where
  `seek` is silent; a half-written archive is an `OSError` like any other
  unreadable file, and a FIFO, directory or device is refused before `open(2)`
  could block on it. `logalert/lock.py` is a real OS lock on both platforms
  (`flock`; `msvcrt.locking` on a byte beyond the holder info, so the info stays
  readable); the holder writes its PID and start time, a holder older than
  `lock_stale` is reported as stale, and a stale verdict is confirmed by a
  second read so a run refused in the microseconds before the holder's write
  cannot mistake the previous line for a stuck run. `--reset-state [PATH]`
  forgets the cursors for one file or all of them, under the lock, replaces a
  state file nothing can read, and refuses to run as root against a state file
  another user owns (naming `sudo -u <owner>`) -- `mkstemp` plus `os.replace`
  would hand the file to root and lock the cron user out; the three exit modes
  exclude one another. In this issue a rotation or truncation reads the live
  file from the top; reading the rotated copy first is #8. ⭐ The adversarial
  review (five lenses, twelve refuters) reproduced a first-sight boundary one
  byte short of the cap, `--reset-state` reporting a permission problem as "no
  state file" (exit 0), a NUL hole splitting the line after it at the cap, and
  the lock leaking when its info write failed -- all fixed and pinned; the
  refuters replaced a `chown`-on-root fix with the refusal above and dropped an
  `os.access` probe that was inert on Windows and blocked the `--reset-state`
  escape hatch on Linux.

[Unreleased]: https://github.com/IjonTichy1970/logalert/commits/main
