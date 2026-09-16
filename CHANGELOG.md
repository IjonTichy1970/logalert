# Changelog

All notable changes to this project are recorded here. The `[Unreleased]`
section accumulates changes as they merge; the release step rolls it under a
version, and the GitHub Release for that version carries the rolled section as
its notes.

## [Unreleased]

### Nitty Gritty

- `[internal]` **Changelog entries are one bold sentence, then at most three
  short sentences** (#60). The issue behind `(#N)` carries the reasoning; the
  gate refuses an entry over six lines. Every earlier entry is rewritten to the
  shape.

- `[contract]` **Python 3.11 is the floor: the wheel installs on Debian 12's
  only Python** (#53). The code's measured floor was 3.11 all along; 3.12 was
  the metadata's claim. CI gains a 3.11 leg, mypy reads the 3.11 typeshed on
  every host, and a test pins the floor, the classifiers, the CI matrix and
  the documents together.

## [0.1.0] — 2026-09-16 — the watcher, its mail and the guide to install it

### Nitty Gritty

- `[contract]` **Packaging, CI and the release workflow: `pyproject.toml` holds
  the version and a `logalert` console script answers `--version`** (#1). CI
  runs on Python 3.12, 3.13 and 3.14, mirrored by the classifiers. Distribution
  is GitHub Releases, not PyPI.

- `[contract]` **`INSTALL.md` documents the `/opt/logalert-venv` pattern: a venv
  from a versioned interpreter, the wheel from GitHub Releases, the
  `/usr/local/bin` symlink** (#3). Four claims inherited from the reference
  pattern were reproduced as wrong and corrected there. `CLAUDE.md` gains the
  deployment constraints the watcher must satisfy.

- `[internal]` **`tools/gate.sh` is the only definition of green, with the WSL
  sandbox, the shell-text hook and the changelog doctrine adopted from the
  handoff kit** (#4). Gated Python is ASCII, enforced by `tools/check_ascii.py`;
  code points are spelled `chr(0x...)` because a written escape can arrive
  decoded. A Linux-only stage runs in the sandbox here and natively on CI.

- `[contract]` **The configuration file: INI watch sections plus `[logalert]`,
  every key validated by `logalert/config.py`, `--check-config` and
  `--example-config`** (#6). Patterns are case-sensitive unless `ipatterns` or
  `iregex` opts in; paths must be absolute; a duplicate section or key is an
  error with its line number and `[DEFAULT]` is refused. `configparser` drops a
  continuation line beginning with `#` or `;`.

- `[contract]` **Cursor, state and the run lock: file identity by inode and
  first line, truncation detected, first sight at the end, atomic per-section
  state** (#7). The state file (`/var/lib/logalert/state.json`) holds one cursor
  per section and path, written after each section's mail is accepted; a line
  is read in 2000-byte fragments. `--reset-state [PATH]` forgets cursors and
  refuses to run as root against another user's state file.

- `[contract]` **Rotation catch-up: after a rotation or truncation the rotated
  copies are read first, then the live file from the top** (#8). Copies are
  found by name in the file's directory and `archive_dir` -- numeric, dated and
  compressed forms -- and matched by content before inode, because ext4 reuses
  inode numbers. A corrupt archive yields what it had with a WARNING.

- `[contract]` **Matching: whole lines against literal and regex patterns, noise
  excludes, priority tags, and `grep -C` context that never crosses a file**
  (#9). The first pattern to match is recorded; an excluded line is dropped but
  stays as context. Line numbers are the file's and ride in the state as an
  optional `line` field. A physical line is matched whole, up to eight
  2000-byte fragments.

- `[contract]` **Alert mail: one 7-bit-clean message per section with a summary,
  the report inline or attached, priority headers only when configured** (#10).
  The subject ends ` -- N match(es)` unless `subject_suffix = no`; `max_lines`
  caps the matching lines per email; an attachment is named
  `<section>-<YYYYmmddTHHMM>.txt`. Control characters in a log line are
  sanitised before composition.

- `[contract]` **Mail delivery: the sendmail pipe (`-i -f <From>`) or SMTP, with
  `mail_timeout` on everything that can hang, and `--test-mail SECTION`** (#11).
  `transport = auto` is sendmail when `sendmail_path` is a regular, executable
  file, else a configuration error naming an MTA to install. Exit 0 from
  sendmail means accepted for queueing, never delivered. A refused recipient is
  a per-recipient failure; every recipient refused is a failed delivery.

- `[contract]` **The run: every section read, one message per section that
  matched, the state saved after each section, nothing printed on exit 0**
  (#12). Exit 1 is one stderr line naming every failed item; 2 a usage or
  configuration error; 130 Ctrl-C. A root run into the service user's state
  directory is refused, and a failed delivery keeps the cursors of the files
  whose lines the message carried.

- `[contract]` **The activity log: `log = syslog` (the default) | `stderr` |
  `file:PATH` | `udp:host:port`, `--log DEST`, and a fallback to stderr that is
  never silent** (#13). Syslog records carry the identifier `logalert`, so
  `journalctl -t logalert` finds the runs. `--dry-run` logs to stderr unless
  `--log` says otherwise; the first-sight skip is a WARNING. The state file,
  the lock and the config are refused as the log.

- `[internal]` **The Linux stage proves the installed script on a real host: as
  a service user, through a real `logrotate`, into the journal, against the
  lock and the mode bits** (#14). Eight checks in a `mktemp -d` tree, every
  host binary bounded; a skip is red under `LOGALERT_CHECK_MODE=required`.
  Every check but preconditions reddens under a one- or two-line mutation of
  logalert.

- `[contract]` **`docs/USAGE.md` is the operator's reference, from the options
  and exit codes through configuration, rotation, mail, logging and
  troubleshooting** (#15). A test pins the document to the package: the example
  config verbatim, every option and key in its table, the four exit codes.
  Under a systemd timer the default `log = syslog` stays.

- `[contract]` **Globs in `files` are expanded at every run; rotated copies are
  left out unless `include_archives = yes`** (#18). A file a glob matches for
  the first time is read from the beginning when it appeared since the last
  run. Links are never followed and a directory that cannot be listed is a
  failed item. `--check-config` names what a glob matched and left out.

- `[internal]` **A typing slip in the syslog probe, caught by CI's mypy where
  the host's could not see it** (#13). mypy on Windows checks nothing under a
  `sys.platform != "win32"` branch, so it runs natively in the sandbox before a
  `/ship` that touches one.

- `[internal]` **The changelog as a page: `tools/render_changelog.py` renders
  this file to a GitHub Pages site and is the gate's `changelog structure`
  stage** (#17). A structural defect is exit 1 naming the line; a file it cannot
  read is exit 2. The explanatory text moves from the top of this file to the
  trailing About section. A `docs` extra carries `markdown`; dev venvs are
  `.[dev,docs]`.

- `[contract]` **`README.md` is rewritten for the operator and `INSTALL.md`
  carries on through configuration, mail, the schedule and the log** (#16).
  The state directory belongs to the service user; the cron line and the
  oneshot unit are one text across the documents, pinned by a test.
  `pyproject.toml` claims Linux only; the sdist gains `docs/` and drops
  `tests/`.

## About this changelog

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
new block, never the whole file). **The headline is one short sentence saying
what the change is; the body is at most three short sentences, and only when
the change needs them.** The issue behind `(#N)` carries the reasoning, the
measurements, the review findings and the test counts -- never the changelog.
An entry is at most six lines, and `tools/render_changelog.py --check` refuses
a longer one. Markers, rare: 🚨 a wrong claim or a check that measured nothing;
⭐ the insight or the control that proved it; ⚠️ a caveat or standing rule;
🔶 an owner's call. A corrected claim is one sentence naming the entry it
corrects; the quote lives on the issue. The prose separator is `--`; version
headings use the em dash.


[Unreleased]: https://github.com/IjonTichy1970/logalert/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/IjonTichy1970/logalert/releases/tag/v0.1.0
