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

- `[contract]` **Rotation catch-up: the rotated copies are read first, across
  every naming style, compressed or not** (#8). When the cursor says a file
  rotated or was truncated, `logalert/rotation.py` finds the copy that holds the
  saved position and reads its tail, then every newer archive in full, then the
  live file from 0 -- nothing written between two runs is missed, however many
  rotations happened (the owner's criterion, pinned against real `logrotate` for
  `create`, `compress`, three rounds of `delaycompress`, `copytruncate` with and
  without `compress`, `dateext`, `rotate 0`, `nocreate` and `olddir`). Archives
  are recognised by name in the file's directory, in the directory a re-pointed
  link used to lead to, and in the section's `archive_dir` (now live): numeric
  `.N` (lower is newer, so savelog's and newsyslog's `.0` and logrotate's `.1`
  both work), the dated forms of logrotate `dateext`, TimedRotatingFileHandler
  and newsyslog, hand-rolled `.YYYYMMDD` and `.<epoch>`, each with an optional
  `.gz` / `.bz2` / `.xz` / `.zst`; anything else sharing the stem orders by
  mtime and the log says so. The copy is identified by content -- the saved
  first line, read through the decompressor, and a length of at least the saved
  offset -- among the archives written after the last run, oldest first; then by
  the saved inode among all regular files whatever their name; last among the
  older archives, newest first, with a WARNING when that is a guess. ⭐ The
  inode comes second because ext4 hands a freed number straight back: measured
  natively, after a rotation that compressed the file, the NEW live file got the
  old inode and the next rotation left it under `.1` with the same banner first
  line -- an inode-first search took the wrong archive. A `nocreate` rotation
  (the live file absent) is read at once and the cursor parks on the archive; a
  consumer that stops part-way through the chain gets a cursor on the archive it
  stopped in. A half-written or corrupt archive yields what it had with a
  WARNING and the chain continues; nothing matching is one WARNING naming the
  file, the saved identity, the directories searched and the likely causes, and
  the live file is read from the top. `open_source()` is what the run loop calls
  per file (wired in #12); `LogFile` alone still reads a rotated file from 0.
  Two cursor rules changed with it: a file that HAD a complete first line and
  now has none is TRUNCATED whatever its size (`copytruncate` under a writer
  without `O_APPEND` leaves a NUL hole where the first line was, and a size that
  passes the offset check), and the fingerprint skips a NUL hole of any size
  (bounded at 64 MiB) rather than 4 KiB of it, so a file copytruncated once can
  still be matched to its copies after the next time. `zlib.error` (a `.gz` with
  a valid header over a corrupt body) and, on 3.14, `ZstdError` are read errors
  like the others -- before, one such file anywhere among the archives was a
  crash on every run -- and the reader uses `read1`, so a cut archive hands over
  what it did decompress before failing. ⭐ Four review lenses reproduced,
  besides those, a `None` saved fingerprint letting the inode stage take another
  log's archive, `.YYYYMMDD` and `.<epoch>` parsed as `.N` and ordered
  backwards, a `.bak` joining the chain behind an oddly named match, a stale
  `router.log.1` shadowing `old/router.log.1.gz`, and hard links knocking a
  chain member out -- all fixed and pinned by the tests their surviving
  mutations named.

- `[contract]` **Matching and context: whole lines against literal and regex
  patterns, noise exclusion, priority tags, and `grep -C` context that never
  crosses a file** (#9). `logalert/match.py` turns the `Line` stream a source
  yields into a bounded report per file. Every pattern is tried in config order
  and the first to match is recorded: a literal as a substring, a regex with
  `search`, case-sensitive unless the section opted in (`ipatterns` fold with
  `lower()`, the same simple folding `re.IGNORECASE` applies, so `ipatterns` and
  `iregex` agree). A `[high]` / `[medium]` / `[low]` tag sets the priority of
  the lines ITS pattern matches, an untagged pattern carries the section's
  `priority`, and a line matching several takes the highest; with nothing
  configured a match has no priority -- the system is off by default, as decided
  in #9. A matching line that also matches an exclude is dropped and counted
  (one DEBUG line per file) but stays in the stream as context for a
  neighbouring match: an exclude means "do not alert on this", not "never show
  this". Context follows `grep -n -C` as measured natively: `n` lines either
  side, windows that touch or overlap merge, a stretch of omitted lines is a
  `--` gap (even under `-c 0`), and the lines before a run's first match may lie
  before the saved position -- the source is asked for exactly the missing ones,
  once, through a second handle that is checked against the identity the run saw
  and answers nothing, with one WARNING, when the file is gone or unreadable (a
  rotation between the open and the first match would otherwise hand the NEW
  file's lines over as the old one's context). Line numbers are the file's, as
  `grep -n` shows them: the reader counts them, the count rides in the state
  file as an optional `line` field (the one addition to #7's layout; a state
  file without it is counted once at first sight, at DEBUG, and never again),
  and the fragments of a line the 2000-byte cap split share the number. ⭐ A
  physical line is matched WHOLE, its fragments joined up to `MAX_FRAGMENTS` (8,
  16 KB of a line): per-fragment matching had an anchored `^kernel: .*oom`
  firing on a tail fragment and a literal the cap split never matching. ⭐
  Context never crosses files, now pinned rather than asserted: after a rotation
  the stream spans the archive's tail and the live file, and the before-window
  was carrying the archive's last lines into the live file's first match -- a
  change of physical file empties both windows and is a gap, and the pre-offset
  hook serves only the file the stream started in. A report is bounded (`cap`):
  past it nothing more is stored, only counted, so a first run from the top of a
  large log cannot hold the whole log in memory, and the priority is tracked as
  the matches arrive. The section's `context` (else the command line's `-c`) is
  wired by the run loop in #12 and `max_lines` applied by the report in #10;
  this entry is the matching alone. ⭐ Three review lenses (the byte arithmetic
  against `grep`, spec fidelity of the line count, mutation of the new tests)
  also reproduced the priority rule demoting a `high` section line matched by a
  `[low]` pattern, a vanished archive turning a match into a traceback, and the
  hook answering from an archive that had yielded nothing -- fixed and pinned,
  with the reviewer's tests that named their mutations merged into the suite.

[Unreleased]: https://github.com/IjonTichy1970/logalert/commits/main
