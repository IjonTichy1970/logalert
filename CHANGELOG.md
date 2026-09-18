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

- `[internal]` **`tools/check_changelog_refs.py` matches refs on digit
  boundaries rather than by bare substring** (#47). A presence test of
  `f"#{n}" not in text` accepted `#1` on the strength of a cited `#10`.
  A regex boundary `(?<!\d)#N(?!\d)` prevents false negatives.

- `[internal]` **A test pins STARTTLS to a verifying SSL context** (#40).
  `starttls()` without the context passed the whole suite, and the stdlib's
  default there verifies nothing.

- `[contract]` **The run lock is `0600`; a stale-lock line says when the
  recorded holder is gone** (#27). `flock` needs no write access, so a lock any
  local user could open read-only was a lock any local user could hold. A lock
  left `0644` by 0.1.0 is tightened by the next run; the gone-holder line points
  at `fuser` instead of a dead PID.

- `[contract]` **A `state_file` that is a symbolic link is refused** (#39). A
  link never worked as a redirect: the first save replaced the link itself with
  a regular file, silently, and the root-refusal rule judged the link's target.

- `[contract]` **A listed symbolic link is followed only when its owner is
  root, the running user or the file's owner** (#26). In a directory another
  user owns, what a listed name resolves to is that user's choice; a link they
  plant towards a file that is not theirs is now a failed item, not a mail.
  Root's `/var/log/foo -> /data/foo` and an application's own `current ->
  today.log` keep working.

- `[internal]` **Logs, archives and the `file:` log are opened by descriptor,
  never by name after a check** (#29). A FIFO swapped in between the check and
  the open blocked `open(2)` for good, with the lock held; a non-blocking open
  and `fstat` on what was opened close that window.

- `[contract]` **A report is cut at 1 MiB of text, and the trailer says so**
  (#25). A message a relay refused for its size was rebuilt from the same
  position and refused every run, and the section stopped alerting; 1 MiB is
  1.4 MB on the wire as base64, up to 3.2 MB as quoted-printable, under a
  default limit either way. `docs/USAGE.md` names the escapes from a message
  refused for any other reason every run.

- `[contract]` **`scan_timeout` bounds one file's scan (300 s; `0` is off), and
  `--check-config` warns about a regex with a nested quantifier** (#28). Such a
  regex runs for hours on one long line of ordinary words, holding the lock;
  past the bound the file is a failed item naming the line and the pattern,
  its position kept. Enforced on POSIX; Windows reports the key and cannot
  enforce it.

- `[contract]` **A listed compressed file is decompressed once at first sight
  and not at all while unchanged** (#44). The cursor records the archive's size
  and mtime; the example's second watch no longer lists a rotated copy beside
  its log, and `--check-config` warns when a listed entry is one (it was read
  as a live log and mailed whole after every rotation).

- `[contract]` **A glob's file with nothing new is a DEBUG record, and each glob
  gets one INFO summary per run** (#50). A daily directory or a host tree wrote
  a `0 line(s) read` line per file per run -- thousands of identical records
  burying the ones the section exists for. A listed file, and a glob's file
  with new lines, keep their own record.

- `[internal]` **The rotation catch-up decompresses the matched archive once,
  not three times** (#46). The handle the content stage verified travels to
  the read, and the context window before the saved position is kept from the
  confirming seek. Measured: 2.9 passes over the archive down to 1.

- `[contract]` **A rotation that lands during the catch-up stops the run at the
  copy it moved, and the next run carries on from the last copy read** (#32).
  The old answer skipped the moved copy and went on to the live file, and its
  lines were never mailed. A copy renamed or gone between the directory listing
  and its open makes the plan list the directory again, once.

- `[contract]` **The documents say SMTP AUTH is not supported and name the way
  through: `msmtp-mta` or `dma` as the sendmail transport** (#58). A relay's
  `530 Authentication required` and `554 Relay access denied` get a
  troubleshooting row; Exim, Debian's default MTA, is named beside Postfix,
  `dma` and `msmtp-mta`.

- `[contract]` **Lines are decoded as UTF-8, the pattern and exclude rows say
  so, and `--check-config` points out a pattern with non-ASCII text, once per
  key** (#57). A latin-1 log never matched an umlaut pattern (and an umlaut
  exclude let the line through), no ordinary pattern matched a UTF-16 export,
  and a UTF-8 BOM defeated `^` -- all silently. A troubleshooting row names
  the three shapes.

- `[contract]` **The service user's files are documented: the `file:` log is
  pre-created (`install -o logalert -m 640 /dev/null`), the venv is made under
  `umask 022` on a hardened host, and the state directory is owned by the
  user, not merely writable** (#37). Three troubleshooting rows quote the
  messages and give the remedies, including a root-owned lock.

- `[contract]` **`INSTALL.md` gains "Python on an older distribution" -- a
  versioned CPython built from source with `make altinstall` -- and "Moving to
  a new host"** (#23). The four libraries the build needs at import time are
  named with the check that proves them; a test pins that check to the
  package's module-level imports. Rehearsed in the sandbox, both with and
  without the libraries.

- `[contract]` **SIGTERM ends a run the way Ctrl-C does: the sendmail child
  killed with its process group, the temp state file unlinked, the lock
  released, `logalert: terminated` and exit 143** (#33). The default
  disposition bypassed every cleanup: an orphaned child queued what it had
  read and a temp file stayed beside the state. A signalled run leaves one
  WARNING record and no end line.

- `[contract]` **A syslog socket nobody drains costs a run two 2 s waits and
  the loud stderr fallback, not a hang before the lock** (#35). journald
  stopped or wedged under its socket unit blocked the first record for good;
  the timeout lives in the stdlib's reconnect seam, where one set after
  construction was lost. The probe's stream `connect` is bounded the same way,
  and Python 3.14's `SysLogHandler(timeout=)` covers INET sockets only.

- `[contract]` **A full disk or quota is refused before any mail: the run
  saves the state once, as loaded, before its first section** (#30). The
  writable-directory probe is a 0-byte file that needs no block, so a full
  filesystem passed it and the alert was mailed again on every run until space
  was freed. A first lock on a full disk names the errno, not the ownership.

- `[contract]` 🚨 **A file read for the first time in a section whose
  delivery failed keeps its place too: an entry where that read began** (#31).
  The #12 entry's "a failed delivery keeps the cursors of the files whose lines
  the message carried" held for files with a saved position only; a
  `--from-start` file got no entry, was first-sighted at its end next run,
  and its lines were never sent.

- `[contract]` **A rotated copy a permission keeps out of reach is a failed
  item (exit 1), the position moving on all the same; a copy or directory the
  user cannot read is one warning, and the no-copy line names the permission
  first** (#38). The read failure was warned about twice, the unsearchable
  directory dropped without a word, and the causes line named four causes the
  run had just ruled out. A corrupt copy stays a warning.

- `[contract]` **logrotate's `extension` form is a rotated copy:
  `router.1.log.gz` is read after a rotation and left out of a glob beside
  `router.log`** (#51). The interval's lines were lost under it and the plain
  copy was mailed whole. The chain keeps to the matched copy's naming form, so
  a numbered live sibling is never chained as a copy; the epoch suffix no
  longer goes through `fromtimestamp`, which overflows on a 32-bit `time_t`.

- `[contract]` **A position parked on a rotated copy records that copy's
  modification time, and the next run takes the copy with that time and first
  line before any guess by content** (#65). A banner log's newer copy fit the
  content check and the run resumed inside the wrong file, silently; a reused
  inode with the same first line was taken too. The state gains two optional
  fields on such an entry.

- `[contract]` **When no rotated copy holds the saved position, the copies
  written since are read before the live file instead of being skipped** (#64).
  After a rotation gap deeper than `rotate` keeps -- routine after a stop under
  a tight `rotate` -- every line of those copies was lost with the copies in
  the directory. A refused delivery now keeps the entry's sighting too (touched
  once halfway to `state_ttl`), so the gap's bound stays at the position.

- `[contract]` **A live file rotated and compressed while the plan was made
  is read once, from the copy** (#67). gzip finishing inside that window made
  the `.gz` a chain member beside the handle it was made from, and the file's
  lines were mailed twice. The handle is skipped when the last copy read has
  its first line, its modification time and at least its length; any of the
  three failing keeps the read.

- `[contract]` **A `copytruncate` that lands under the open handle is caught
  by the reader, chunk by chunk: what it read from the truncated file is
  dropped and the position kept is from before** (#66). The old cursor paired
  the old first line with an offset into the new content: lines lost, then a
  fragment and a duplicate (measured with real logrotate). One WARNING line;
  the next run finds the copy by content.

- `[contract]` **A run whose mail went out and whose state then could not be
  saved marks the lock file, and the next run sends nothing until a save that
  reaches that size plus a block proves the room** (#70). The #30 refusal left
  a band -- room for the positions as they were, none for what a run adds --
  where the same lines went out on every run; now they go out once more, when
  the room is back. `cat` of the lock shows `unsaved <bytes> <state file>`.

- `[contract]` **A file truncated and refilled past the saved position with
  the same first line is caught as a truncation, not read on from the stale
  offset** (#34). The old rules -- inode, first line, size -- read a fragment
  and lost the refill's lines before it, silently. The entry gains an optional
  `anchor` (up to 4 KiB before the position, hashed as read), which #66's
  in-run check compares too; a 0.1.0 entry is trusted once.

- `[internal]` **The test helpers every module copied live once, in
  `tests/conftest.py`** (#43). The `Site` fixture, the scandir denial, the
  `-m logalert` runner, the tried symlink, the fence scanner and the rotation
  helpers; the fake MTA's knob list had already drifted by one between two
  copies. Nine symlink tests now try the link instead of skipping on Windows
  outright, and no check-config test reaches `getfqdn()` by accident.

- `[internal]` **A 3.14 build without `compression.zstd` fails the one `.zst`
  read test instead of skipping it** (#42). The skip was announced only, and
  the gate's verdict counts an announced skip as green by design; the read
  path would have been measured nowhere. Below 3.14 nothing changes.

- `[internal]` **Seven behaviours the audit's mutation run found unpinned are
  pinned, each red under its mutant on the platform where it is real** (#41).
  The glob record's moment is the run's start (a frozen clock could not tell);
  a configured section's record survives an outage longer than `state_ttl`;
  root's own state is not foreign (faked uids, so CI sees it); the dry run's
  console guard; an empty state file is corrupt; three boundaries.

- `[contract]` **A glob's wildcard directory component over a directory the
  user can list but not search is a failed item (exit 1) where the listing
  carries no `d_type`, not silence** (#71). Every candidate's `is_dir` is an
  `lstat` the parent refuses (ext4 without `filetype`, XFS `ftype=0`, some
  FUSE and network mounts), and each was dropped without a word, the glob's
  moment moving on. The line is #38's: `cannot examine N of the M entries`.

- `[contract]` **The documented systemd unit carries `TimeoutStartSec=3600`, and
  the docs say which hardening lines break the mail path** (#36). A oneshot
  has no start timeout of its own and the timer never starts an activating
  unit: a wedged run silenced the schedule for good, and the two lock verdicts
  are cron's. `NoNewPrivileges=` and its kin make a setgid `sendmail` keep the
  caller's group (measured); the Linux stage verifies the documented text.

- `[internal]` **The PreToolUse guard is launched through `python`, then
  `python3`, and refuses every Bash command when neither is on PATH; the gate
  runs the command as spelled** (#55). On a stock Debian or Ubuntu the bare
  `python` exited 127 and Claude Code let every command through, announced but
  inert, with the guard's gate stage green. Three checks now run the string
  itself: the venv off PATH, a `python3`-only PATH, an empty one.

- `[internal]` **The gate refuses a stale or unknown `# noqa` (RUF100, RUF102),
  and the two ported tools tell this repository's story** (#48). Eight
  directives suppressed nothing (`S603` is ignored tree-wide) and a planted
  `# noqa: XYZ999` passed in silence; the docstrings cited another project's
  issues, commits and files. `reflow_md.py`'s four fence walks are one
  generator, byte-identical over every document in the tree.

- `[internal]` **The workflows use `checkout@v7` and `setup-python@v7`, and the
  gate prints its tools' versions once at the top** (#52). The v4 and v5
  majors declare Node 20, which every job was annotated as forced onto Node
  24; nothing in the newer majors' notes reaches these workflows. The extras
  let ruff, mypy, pytest and markdown float, so a drift that reddens an
  unchanged tree is now diagnosable from one line of the log.

- `[internal]` **The Pages build runs the document pins and the markdown width
  check on every PR, so a docs-only PR is no longer unchecked** (#45).
  `ci.yml`'s `paths-ignore` keeps its cycle; the 51 pure-text tests over
  `USAGE.md`, `INSTALL.md`, `README.md` and the real changelog need only
  `pytest` and the `docs` extra, and run there in seconds. Everything else
  stays could-not-check for such a PR, as documented.

- `[internal]` **The Linux stage skips, named, when it is not root and has no
  passwordless sudo, before it builds anything** (#59). Measured as an
  ordinary user: the wheel built and nine checks FAILED naming mail, the
  state, the rotation and the documented unit, for faults no non-root caller
  can avoid. The native path also finds `runuser` and `logrotate` under a PATH
  without `/usr/sbin`, and the header is this stage's, not the template's.

- `[contract]` **A glob's new file that could not be opened this run keeps the
  glob's moment, so it is read whole once it can be** (#69). The record moved
  past a file a permission kept shut, and the next run first-sighted it at its
  end -- everything written before the fix never mailed. The outage rule now
  covers a permission; a glob with no moment yet takes the run's all the same,
  so one file that never opens cannot switch the new-file rule off.

- `[internal]` **The four values that lived twice live once, `State.dirty` is
  gone, and the test-only file opener left the package** (#49). The exit
  codes, the `Transport` literal, the lock path and the compression suffixes
  are imported where they were copied (a fifth suffix would have split the
  last pair); `dirty` was set in seven places and read in none; `open_log_file`
  was a door around the rotation catch-up that only the tests used.

- `[internal]` **The release artifacts are built on Linux at the tag, and a
  check refuses a pair with a world-writable member, CRLF metadata or a
  missing file before it is attached** (#56). Built on the Windows host, the
  sdist's members were 0666 and root's `tar xzf` left a world-writable tree
  (35 of 36 entries, measured); `release.yml` builds, runs
  `tools/check_artifacts.py` and attaches the pair to the release.

- `[contract]` **The full-disk marker in the lock file is keyed to the state
  file: `unsaved <bytes> <state file>`, one per configuration sharing the
  directory** (#73). The lock is the directory's, so configuration B's run
  read A's marker as its own: refused with A's message, proved the room
  against A's size and cleared A's marker. Each run now honours, sets and
  clears only its own and carries the others along; `--reset-state` likewise.

- `[contract]` **The rotation catch-up compares the anchor: a copy with other
  bytes before the position is never the file, whatever its stamp or inode,
  and a parked entry records the copy's anchor** (#74). An older copy sharing
  a banner log's first line, longer than the position, was taken as a guess
  at every restart that truncated the log -- 166 old lines and a fragment
  mailed before the new file. A cursor without an anchor is trusted as before.

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
