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

- `[contract]` **Alert email composition: one 7-bit-clean message per section, a
  summary in both modes, the report inline or attached, the priority headers
  only when configured** (#10). `logalert/mail.py` is the single home of the
  message's shape: `compose()` turns a section's reports into a `Mail`, and the
  transports (#11) and the run loop (#12) consume it. The message is built under
  `email.policy.default.clone(cte_type="7bit")`, so `set_content` picks `7bit`
  for ASCII lines of at most 78 characters and quoted-printable or base64
  otherwise -- no wire line ever exceeds 78 bytes whatever a log line holds (dma
  rejects an unencoded line over 998 bytes, and nothing folds one);
  `max_line_length` stays at 78 because at 998 a non-ASCII subject folded into
  one 280-character encoded-word (RFC 2047 caps them at 75). Headers, in this
  order and all before the content: `From`, `To`, `Subject`, `Date` (the local
  time with its offset), `Message-ID` (the From's domain, no lookup),
  `Auto-Submitted: auto-generated`, `X-Prepared-By: logalert <version>`,
  `X-Logalert-Section`; then ONLY when the section or a matched tag set a
  priority -- the system is off by default -- `X-Logalert-Priority:
  high|medium|low` with the conventional `X-Priority: 1|3|5` and `Importance:
  high|normal|low`, carrying the highest across the mail's matches, stored or
  not. The subject is the section's `subject` then ` -- N match(es)` (the
  literal a filter can key on) unless `subject_suffix = no`. The body opens with
  a summary in both modes -- the subject, the count, the host and the run time
  on one line, then `Section:`, `Priority:` (when set), one `Files:` row per
  file read with its match count, the lines read and any dropped by an exclude,
  and the `Message-ID:` the activity log will carry, so an alert can be traced
  to its run. `report = inline` continues with the report; `report = attachment`
  (or `--attach`, wired in #12) attaches it as `text/plain` named
  `<section>-<YYYYmmddTHHMM>.txt`, the section name reduced to `[A-Za-z0-9._-]`,
  never starting with `-` (an option to a shell tool in the download directory)
  or `.` (hidden), at most 40 characters -- measured, from 48 the stdlib folds
  the name into RFC 2231 continuations some clients show as `ATT00001.txt`. The
  report is `grep -n -C`'s vocabulary as measured for #9: a `tail`-style `==>
  file <==` header per physical file (the archive's tail before the live file
  after a rotation), `N: text` for a match, `N- text` for context, `--` for
  omitted lines, the fragments of a cut line as one line ending in ` [cut]` when
  the cap dropped the rest. `max_lines` is applied here, in its documented unit:
  at most that many MATCHING lines per email across the section's files in
  order, each with its context, then one trailer `... and N more matching
  line(s)` with the exact remainder; after the last budgeted match only its own
  after-window follows -- a NUL-only line the reader skipped inside that window
  does not end it, a change of physical file does, as it does the scan's (the
  verification of this entry reproduced the live file's header and two of its
  lines printed before the trailer, and it is pinned). For that, #9's
  `scan(cap=)` changes unit -- it stored the first `cap` report ENTRIES and now
  stores the first `cap` matching lines with the context of each (an entry cap
  of 200 cut the window of match 26 at `-c 3`); `FileReport` records its
  `context` width and loses `omitted` (`matched - len(matches)` says it).
  Sanitised before composition: a lone surrogate becomes `?` (`set_content`
  raises on one), CR is removed (a bare CR is a line break to the encoder),
  every other C0 control except TAB and LF, and DEL, become U+FFFD (measured: a
  NUL and a form feed ride through `7bit` to the relay untouched); in a header
  value TAB and the line boundaries that pass that -- LF, and NEL, LS and PS --
  become a space. ⭐ The review reproduced why that last rule exists: U+0085,
  U+2028 and U+2029 are one line to `configparser` and to the loader's
  single-line rule, survive a C0-only sanitiser, and are exactly what
  `EmailMessage` refuses in a header -- a `ValueError` on every run for a
  section whose subject carried one, before any transport. The From must be the
  bare `local@domain` the loader accepts, now a public `config.is_address()`
  that `--from` (#11/#12) applies too; anything else is refused with a
  `ValueError` naming the fix (`set from = in [logalert]`). ⭐ The native run
  decided that: Python 3.12.10 on the dev host renders a From of `x@` as `<>`,
  and 3.12.3 in the sandbox -- Ubuntu 24.04's -- raises `IndexError` from the
  header parser on the same input; a tolerant path would have depended on the
  patch level. `Mail.flatten("sendmail"|"smtp")` produces the exact bytes once
  per transport and caches them -- LF for the sendmail pipe, CRLF
  (`email.policy.SMTP`) for SMTP DATA, one byte per line apart -- and `len()` of
  the one handed over is the size the log reports; a header added after a
  transport's `flatten` never reaches that transport's wire. `Mail.preview()` is
  what `--dry-run` (#12) prints: the headers decoded, the body as a reader sees
  it, each attachment's text under its name -- never the quoted-printable wire.
  Three review lenses (wire fidelity against `BytesParser` round trips, 300
  random streams against `grep -n -C` and 400 against an independent budget
  reference; specification fidelity and the #11/#12/#13 consumers; mutation
  testing and hostile input) also reproduced the Message-ID domain cut from the
  raw sender, the RFC 2231 threshold and the after-window gap above -- all
  fixed, 18 deliberate mutations reddened by the merged tests. Recorded for the
  issues that own them: the loader accepts a duplicate entry in `files` and has
  no address length cap (a 1000-character local part puts a 1013-byte `To` line
  on the wire, over RFC 5322's 998), an unreadable file is absent from the
  summary, and nothing but `max_lines` x the context x `MAX_FRAGMENTS` x
  `LINE_CAP` bounds a mail's size (about 23 MB worst case at the defaults).

- `[contract]` **Mail delivery: the sendmail pipe and SMTP with a timeout on
  everything that can hang, the effective From, `--test-mail`** (#11).
  `logalert/transport.py` is the single home of the transport rules; `deliver()`
  hands a composed mail over and returns a `Delivery` (the recipients accepted,
  those the server refused with its answer, the bytes handed over, the
  transport's one-line answer) or raises `DeliveryError` whose message IS the
  transport's answer -- a returned `Delivery` always means the message left the
  box. `transport = auto` (the default) is sendmail when `sendmail_path` is a
  regular, executable file, else a configuration error that says what to do:
  `transport = auto but /usr/sbin/sendmail does not exist: install an MTA
  (Ubuntu: apt install postfix, dma or msmtp-mta; FreeBSD 14+: dma is in base)
  or set transport = smtp and smtp_host` (a fresh Ubuntu server image has no
  sendmail at all; a directory or a non-executable file gets the same shape);
  `--check-config` now exits 2 on it, and on a From the run would refuse, where
  it used to print `NOT FOUND` and exit 0. The sendmail argv is `[sendmail_path,
  "-i", "-f", <From>, <recipient>...]` with the message piped in as the exact LF
  bytes, the child bounded by `mail_timeout` and, on POSIX, leading its own
  process group so a `sudo`- or `runuser`-style wrapper dies with its children
  at the deadline. ⭐ Measured against dma 0.13 in the sandbox (installed for
  the measurement and purged): `-oi`, the classic spelling its own man page
  calls a synonym, is a no-op in dma -- only `-i` keeps a body line that is a
  single `.` from ending the message silently with exit 0; its line limit is 997
  bytes, headers included, and one longer line (unless it is the last, without a
  newline) is the whole message refused with exit 65 and `sendmail: bad mail
  input format: ...` (#10's quoted-printable bodies are what keeps every alert
  under it); a recipient beginning with `-` is an option wherever it sits in
  argv, and a lone `-bp` prints the queue, discards the alert and exits 0 (the
  loader's rule on addresses is the guard; no `--`, not every sendmail honours
  it); exit 0 arrives in 20 ms and means ACCEPTED FOR QUEUEING, never delivered
  -- dma never returns 75, its temporary failures are exit 0 plus a queue entry,
  `mailq` is the tell. A non-zero exit is reported as `sendmail exit 65
  (EX_DATAERR): <its first stderr line>` (the sysexits names are a table of our
  own: `os.EX_*` is absent on Windows), a timeout as `sendmail did not finish
  within 60 s` with what the child said before hanging, a binary that cannot
  start as `could not execute <path>: <reason>` -- naming the path ourselves,
  since Windows leaves `exc.filename` empty, and saying when the file exists but
  its `#!` interpreter does not; what an MTA writes to stderr on exit 0
  (Postfix's `postdrop` with the daemon stopped) is kept in the answer and
  logged as a WARNING. SMTP: `smtplib.SMTP(host, port, local_hostname=<the
  From's host>, timeout=mail_timeout)` -- without `local_hostname` smtplib runs
  its own `getfqdn()`, which stalled 20 s on a host with dead DNS --
  `smtp_starttls = yes` requests STARTTLS with a default SSL context (the
  certificate is verified against `smtp_host`, so it must be a name) and lets
  `SMTPNotSupportedError` be the loud failure; the exact CRLF bytes go through
  the low-level `sendmail()`, which adds one byte per line starting with `.` and
  nothing else. One refused recipient is a per-recipient failure logged as a
  WARNING with the others delivered; every recipient refused, a session the
  server closed at RCPT (the recipients it never tried are named as such), a
  refused sender, a rejected DATA, a dropped connection, a silent server and a
  refused connection are `DeliveryError`, each naming what happened -- the
  exception type where there is one, the server's reply where it gave one. ⚠️
  smtplib's `timeout` bounds each socket operation, not the session: a slow
  relay can hold a run for about eight times `mail_timeout` and still succeed,
  and resolving `smtp_host` is the system resolver's bound -- bounded, not
  forever; a timeout after DATA says the server may still deliver what it read.
  `mail_timeout` is capped at 3600 s by the loader: above about 25 days the
  platform's own timeouts overflow into a traceback. The effective From is
  `--from ADDR`, else `from =`, else the user logalert runs as at this host (the
  only branch that may consult a resolver), and must be the bare `local@domain`
  the loader accepts -- `config.is_address()`, from #10 -- because dma accepts
  anything without a newline as `-f` and smtplib puts a space or a `>` on the
  wire in one line; a From with no domain or a local-only one gets the standing
  warning. `--test-mail SECTION` sends one real message to that section's
  recipients through the configured transport and prints `sent via sendmail
  (/usr/sbin/sendmail): accepted for queueing (exit 0)`, `to: ...`, `size: N
  bytes; Message-ID: <...>` (0), or `logalert: not delivered via smtp
  (host:port): <answer>` on stderr (1), or the configuration error (2); a
  partial refusal prints the accepted lines, `refused: <recipient> -- <reply>`
  on stderr, and exits 1. Two test doubles ship in `tests/`: an ESMTP stub on
  127.0.0.1 port 0 (banner, EHLO with SIZE / 8BITMIME / optional STARTTLS, a
  configurable 550 per recipient, 552 on DATA, drops, silence, per-verb replies,
  every command and DATA payload recorded -- verbs matched case-insensitively,
  since 3.12 already sends them in lowercase while `STARTTLS` and the context
  manager's `QUIT` are uppercase) and a fake sendmail that records its argv and
  its stdin bytes, honours `-i` as dma does, sleeps or exits as told; the
  end-to-end tests reach the fake through `sendmail_path` behind a `.cmd`
  wrapper on Windows and a shebang copy on POSIX. ⭐ Four review lenses and a
  refute pass reproduced nine defects the tests had not: a QUIT answered with
  anything but 221 after the DATA 250 raised out of smtplib's context manager --
  the message was queued, and the run loop would have re-sent it every run (the
  farewell is ours now; the outcome is decided at DATA); a recipient listed
  twice let DATA go out to nobody, since smtplib decides "all refused" by
  counting (the loader deduplicates `to`, and nothing accepted raises); a
  relay's multi-line or control-laden reply reached the log and the console as
  forged extra lines (`mail.clean_header` folds every reply and stderr line); a
  `mail_timeout` of 35 days was an uncaught `OverflowError`; a directory as
  `sendmail_path` passed the transport while `--check-config` printed NOT FOUND
  and exited 0 (one shared judgement now, `config.sendmail_problem()`);
  `--check-config` never resolved the From it printed; from a real console a
  refused recipient printed twice through `logging.lastResort`, which pytest's
  own handler had hidden (the library `NullHandler` is in place); and a forking
  wrapper's grandchild survived the timeout and queued the mail `deliver()` had
  reported undelivered; and a 421 mid-RCPT named only the recipients smtplib had
  tried (the untried ones are named as such now). Eight judgment-level findings
  were refuted by two skeptics each; the three that would recur are recorded on
  the issue: a 4xx per recipient stays a per-recipient failure (greylisting
  refuses every recipient on first contact, which is a `DeliveryError` and the
  next run's retry), and the SMTP answer stays `accepted by host:port` because
  `sendmail()` discards the relay's DATA reply -- the Message-ID is the
  correlation key. Noted for #16: `mailq`, `journalctl -t dma`,
  `/var/log/mail.log`, a wrapper that `setsid`s itself, the per-operation SMTP
  timeout.

- `[contract]` **The run: every section read, one message per section that
  matched, the state moved after each section, nothing said on exit 0** (#12).
  `logalert/run.py` is the single home of the run's rules;
  `logalert/__main__.py` parses the command line and dispatches there. A bare
  `logalert` IS the run now (the #1 test that a bare invocation prints the help
  is retired -- the help is `--help` -- and a missing config is exit 2 with
  `logalert: <path>: config file not found or not readable`). The order, each
  step before anything irreversible: the transport and the effective From are
  settled first (a configuration error is exit 2 with its own line before a file
  is read); then, unless `--dry-run`, the state directory (#7's
  `check_state_dir`) BEFORE the lock -- ⭐ the map had the lock first, and the
  review reproduced as `nobody` and as root what that costs: a root run refused
  for another user's state had already created a root-owned `lock`, so the
  remedy the refusal names (`sudo -u <user> logalert ...`) then failed forever
  on that lock, and a missing directory arrived as the lock's raw ENOENT instead
  of `does not exist -- create it, owned by the user logalert runs as`; then the
  lock (a holder younger than `lock_stale` is exit 0 with an INFO line, `another
  run (PID N) has held the lock <path> for Ns; this run exits quietly` -- cron
  overlap is normal -- and an older one exit 1, named as `stale lock: ...`; a
  lock file that cannot be opened is `state directory: <reason> (<lock>) -- the
  lock file must belong to the user logalert runs as`); then the state file (a
  corrupt one is exit 1 naming `--reset-state`, nothing read) and the expiry of
  entries unseen for `state_ttl` days, each logged as `forgotten` (`would be
  forgotten` under `-n`). Then per section, in file order: a file that cannot be
  opened is a failed item (`[section] <path>: <reason>`), its cursor stays and
  the section continues; a file absent this run is nothing to do (cursor and
  `last_seen` stay, so it expires in time; a configured file never seen is exit
  0 with a DEBUG line); each source read is scanned with the section's `context`
  (else `-c N`) and its `max_lines`, its cursor taken after the read, and logged
  as `[section] <path>: N line(s) read, M matched`; a first sight starts at the
  end unless `--from-start` or `start = beginning`. Nothing matched: the cursors
  move. Anything matched: ONE message composed (#10) and delivered (#11).
  Accepted: the cursors move (🔶 a partial refusal is accepted -- the recipient
  who got it must not get it twice -- with each refused recipient a failed item,
  `[section] refused: <recipient> -- <reply>`). Refused: `[section] <the
  transport's answer>` is the failed item, logged at ERROR with the Message-ID,
  and the files that contributed to the message keep their place so the re-run
  re-sends them -- ⭐ while a file that was read and matched nothing moves
  anyway: the map's rule was "touch every file read", `touch` is a no-op for a
  file with no entry, so a file at its first sight in a section whose delivery
  failed (any file added to the section that run, every file under
  `--from-start`) was first-sighted AGAIN next run and skipped to its new end,
  every line written in between silently lost, against #8's owner criterion;
  none of a zero-match file's lines is in the message (the summary still names
  it, `<file> -- no match in N line(s) read`), and context never crosses files,
  so its cursor is safe to move. Every file read is `touch`ed so it never
  expires while its mail keeps failing. The state is saved after EACH section,
  never once at the end; a save that fails is `[section] state not saved:
  <reason>` and, when the mail went out, an ERROR line saying the next run
  re-sends it unless a later save in this run succeeds. Exit codes: 0 ran and
  wrote NOTHING to stdout or stderr -- cron mails every byte -- matches
  included, a match being the normal outcome; 1 with exactly one stderr line
  naming every failed item, `logalert: 2 of 3 sections sent; failed:
  [router-disk] sendmail exit 75 (EX_TEMPFAIL); see the log` (the count clause
  only when some section had something to send, `section` singular for one,
  `would be sent` under `-n`; header-clean, so a relay's or a file name's
  control characters cannot forge a second line); 2 a usage or configuration
  error, argparse's own included, nothing ran; 130 on Ctrl-C, one line
  `logalert: interrupted` instead of a traceback, the lock released. The
  options, all of them the run's: `-c/--context N` (like `grep -C`, for sections
  that set no `context` of their own; a negative N is exit 2), `--attach` (every
  report as an attachment this run, whatever the sections say), `--from-start`,
  `-n/--dry-run` (reads and matches, prints each message that would be sent to
  stdout as `Mail.preview()` renders it, takes no lock, needs no writable
  directory, writes nothing; exit 0 or 1 by the same rule), `-d/--debug` (the
  activity log at DEBUG on stderr for this call only, every record one
  header-clean line -- breaking cron-quiet on purpose), `--state-file PATH`
  (absolute; wins over `state_file =` in every mode: the run, `--reset-state`,
  and `--check-config`, which prints `state_file: PATH (--state-file)`), and
  `--from ADDR` from #11. ⭐ The run-only flags refuse the modes: `-n
  --reset-state` wiped the state under a flag whose help promised to leave it
  untouched, and `-n --test-mail` sent a real message; `-n`, `--attach`,
  `--from-start` and `-c` with `--check-config`, `--reset-state` or
  `--test-mail` are argparse's exit 2 now; and `--reset-state` reset the
  configured file while `--state-file` kept its cursors (it takes the override
  now). A `state_file` that is the config file itself is refused for every mode,
  exit 2 (`state_file: <path> is the config file itself` -- `--reset-state`'s
  replace-an-unparseable-file escape hatch would have overwritten the config); a
  directory or any other non-regular file is exit 1 (`state file <path> is not a
  regular file`); nothing is created either way. What root may not do, one step
  earlier than #7 drew it: the lock is opened with `O_NOFOLLOW` (⭐ a `lock`
  symlink planted in a directory another user owns was truncated and written
  through by a root run -- reproduced), and when the state file does not exist
  yet #7's foreign-owner rule applies to the state DIRECTORY, so a root first
  run into the cron user's empty directory is refused (`state directory <dir>
  belongs to <user>; a run as root would leave the state and the lock root-owned
  and unusable by that user -- run as that user instead: sudo -u <user> logalert
  ...`) instead of leaving root-owned state and lock there; on Windows a
  read-only state file is refused up front (`state file <path> is read-only --
  nothing was sent`: the directory probe passed, the mail went out, the save
  failed, the next run double-sent). The activity log, on the `logalert.*`
  loggers (the library's `NullHandler` swallows it until #13 wires `log =` and
  `--log DEST`, which move there): the start line (config path, section count,
  `(dry run)`) first of all and `end: exit N` last on every exit the run returns
  (0, 1 and 2 -- the transport or From configuration error and a fresh lock
  holder included; a killed run, Ctrl-C's 130 among them, leaves no end line) --
  every failed item once at ERROR where it is collected, every expired entry,
  per file the lines read and matched, per section the delivery or the failure
  with the transport's answer and the Message-ID; ⭐ before the review a failed
  delivery, a stale lock, an unwritable directory and a corrupt state left no
  WARNING-or-above record at all. Also from the review's 27 reproduced findings
  (four lenses, 39 shapes across both hosts, two real processes racing on the
  lock and a run killed mid-delivery among them; #14's service-user rehearsal
  run in the sandbox as `nobody` -- the state file lands `0600` from `mkstemp`,
  the lock `0644`, exit 0 with 0 bytes on both streams): `clean_text` now folds
  the C1 range too (a raw CSI is live on a terminal that honours C1; NEL stays
  the boundary `clean_header` folds), and an OSError from our own `open_log` no
  longer repeats the path. Five judgment-level findings were refuted by two
  skeptics each and stand as designed: a configured file absent and never seen
  is exit 0 (nothing to do); the loader's unknown-key warnings are
  `--check-config`'s, not the run's; a file listed twice in one section is read
  twice (the loader's rule, noted since #10); an unexpected exception inside a
  section propagates with its traceback (a bug must be loud; the `finally`
  releases the lock). `tests/test_run.py` (52 tests, the reviewers' 17 merged;
  three POSIX-only, two of those also skipped in the root sandbox, one
  Windows-only) drives the run end to end through a two-section config and the
  fake sendmail, which now records every call in order (`call-NNNN-argv.json`,
  `call-NNNN-stdin.bin`) and can fail for one recipient
  (`LOGALERT_FAKE_EXIT_IF_RCPT`) so that one section of a run fails; 37
  deliberate mutations, 36 reddened on Windows and the 37th (the directory-owner
  rule) pinned by a POSIX-only test. Noted for #13: the record set above is what
  its destinations carry, and `__main__`'s one-line formatter is the shape every
  destination should use; for #15/#16: the exit-code table, the line shape, `-n`
  semantics, `--from-start` on first sight only, `--state-file` absolute, the
  lock's two verdicts, expiry.

- `[contract]` **The activity log: syslog with the identifier, stderr, a file or
  UDP, `--log`, and a fallback that is never silent** (#13).
  `logalert/activity.py` is the single home of where the records go, in what
  shape, and what happens when the destination fails; the records themselves are
  the ones #7-#12 emit on the `logalert.*` loggers (the start and end lines, per
  file the lines read and matched and the identity line, per section `sent via
  ...` with the recipients, the byte count and the Message-ID, or the failure
  with the transport's answer, every failed item at ERROR, expiry, the lock at
  DEBUG). `log = syslog` (the default) | `stderr` | `file:/absolute/path` |
  `udp:host:port` in the config, `--log DEST` on the command line winning over
  it in every mode, like `--state-file`. `syslog` probes `/dev/log`,
  `/var/run/log` and `/var/run/syslog` in that order for a path that IS a socket
  (`stat.S_ISSOCK`, never `exists()`: `/var/run/log` is a directory on Ubuntu
  and a socket on FreeBSD) and that accepts a datagram, else a stream,
  `connect()` of our own -- ⭐ measured, `SysLogHandler` constructs without
  raising on a missing path or on a directory, holding a closed socket, and then
  prints three chained tracebacks per record; a server that has gone with its
  socket file left behind (journald stopped) is two tracebacks per record. The
  handler's `ident` is `logalert[<pid>]: `, so journald records
  `SYSLOG_IDENTIFIER=logalert` and `SYSLOG_PID`, `journalctl -t logalert` and
  `journalctl -p warning -t logalert` find the runs (without the ident the
  journal shows `_COMM=python` and no identifier; with the console script
  `_COMM=logalert` too) and rsyslog's `/var/log/syslog` line reads `<time>
  <host> logalert[<pid>]: <message>`; the facility is user. `--check-config`
  prints what `syslog` resolves to here: `log: syslog (/dev/log)`, or `log:
  syslog -- no usable socket (/dev/log: connection refused; /var/run/log: not a
  socket; /var/run/syslog: missing); the run would log to stderr` (on Windows
  `-- not available on this platform; ...`), with ` (--log)` when the command
  line chose it; a check never touches the destination and creates nothing. The
  fallback is ALWAYS stderr and ALWAYS loud: when the destination cannot be used
  the first record on stderr is one WARNING in the log's own shape -- `no usable
  syslog socket (...); logging to stderr`, `syslog is not available on this
  platform; logging to stderr`, `cannot open the activity log
  /var/log/logalert.log (Permission denied); logging to stderr`,
  `udp:relay.example.net:514: cannot resolve the host (...); logging to stderr`,
  or `udp:[2001:db8::1]:514: cannot open (...); logging to stderr` when the host
  resolves but the socket cannot be created (an address family the host lacks)
  -- and the run goes on with its exit code unchanged (cron mails that line,
  which is the point; it is the one thing an exit 0 may now write to stderr). ⚠️
  Never UDP as a fallback: measured on both platforms, three datagrams to a port
  nobody listens on produce nothing anywhere, the "logs nowhere" fault; `udp:`
  is built only when configured, an unresolvable host raises at construction and
  takes the fallback, and `udp:[2001:db8::1]:514` is the IPv6 spelling (⭐ the
  review found `[::1]` accepted by the loader and then unresolvable at run
  time). A destination that fails later -- the syslog server gone, a full disk
  -- is reported ONCE, `the activity log at syslog (/dev/log) failed
  (ConnectionRefusedError: [Errno 111] Connection refused); logging to stderr
  from here on`, the failed record and the rest of the call go to stderr, and
  teardown stays quiet: ⭐ the review filled a 64 KiB tmpfs and found the run
  mailing, saving its state and logging `end: exit 0` -- then `close()` flushing
  the buffered bytes again, the `OSError` escaping `main()`, a 30-line traceback
  in cron's mail and exit 1; the teardown now unwires every handler, suppresses
  that second failure and resets the logger's level in its own `finally`.
  `file:` is opened at setup, never lazily (⭐ measured,
  `FileHandler(delay=True)` raises the open failure out of `logger.info()` into
  the caller, which would have aborted a run at its first record with a
  traceback), and by us, like the lock: ⭐ the review had `nobody` plant a
  `logalert.log` symlink in a shared directory and a root run appended the
  activity log into the file that user chose, and a FIFO as the destination
  blocked `open(2)` for good before the lock so cron runs would pile up while
  `--check-config` said nothing; now `lstat` first -- `is a symbolic link --
  name the real path`, `is a directory`, `not a regular file` (a FIFO, a device:
  `/dev/full` included) each take the loud fallback -- then `os.open` with
  `O_NOFOLLOW` and an `fstat` behind it. The file is appended with `0644` under
  the umask (it names files, sections, recipients and Message-IDs, never a log
  line). ⭐ The state file, the run lock and the config file are refused as the
  log, by the loader (`[logalert] log: <path> is the state file` / `the run
  lock` / `the config file`, exit 2) and again in `main` for the effective paths
  under `--log` and `--state-file`: measured, `log = file:<state_file>` wrote
  `start: ...` into the state before every run read it (exit 1 forever, and
  `--reset-state` replaced the state only for the next record to corrupt it
  again, nothing naming `log =` as the cause), and `log = file:<config>` grew
  the config by one stamped line per record until a doubled key made every mode
  exit 2. A `file:` path or a `state_file` inside the running interpreter's venv
  (`sys.prefix` when it differs from `sys.base_prefix`, by real path) is `is
  inside the venv (/opt/logalert-venv), which the upgrade procedure wipes` --
  the deployment constraint, applied to the state file too, which #7 had not; a
  NUL in any path value is `contains a NUL character` instead of a `ValueError`
  traceback from `realpath`. One line per record, whatever the destination: the
  message, prefixed `warning: ` / `error: ` / `debug: ` for those levels and
  nothing for INFO (journald and rsyslog carry the priority anyway; a file or a
  cron mail needs the word), header-clean through `clean_header` (rsyslog writes
  `#012` for a newline, a terminal honours a raw CSI); the destination's own
  prefix is the ident for syslog (journald adds the time),
  `2026-09-15T14:11:02-0600 logalert[<pid>]: ` for the file, `logalert: ` for
  stderr -- which retires #12's `logalert: INFO logalert.run: ...` shape for
  `--debug`. `--debug` adds a stderr handler at DEBUG and leaves the configured
  destination at INFO (a debug session does not flood syslog); when the
  destination IS stderr there is one handler, at DEBUG. 🔶 `--dry-run` logs to
  stderr, not to the configured destination, unless `--log` is explicit: `-n`
  promised to write nothing (#12), a `file:` log would be appended, and a dry
  run's records (`start: ... (dry run)`, `would be forgotten`, the first-sight
  line) belong in front of the operator, not in syslog as if they were a run's.
  🔶 The first-sight skip (`first sight; starting at the end, N bytes skipped`)
  is a WARNING now, as this issue lists it, where #7 logged it at INFO: the one
  time lines are deliberately never mailed, and `journalctl -p warning` should
  show it; `reading from the beginning` stays INFO. `--reset-state` writes its
  own record (`forgot N cursor(s) for every file (--reset-state)`, and the
  escape hatch's `...; replaced it with an empty state (--reset-state)` at
  WARNING), `--test-mail`'s `sent via ...` is part of the trail, and both honour
  `--log`. Two corrections to the issue's text, both settled during the #12
  review: "bytes read" per file is not a well-defined number across a rotation
  catch-up, so the per-file line stays `N line(s) read, M matched`; the identity
  line is INFO when it says something and DEBUG for a plain continue. Rehearsed
  end to end in the sandbox with the installed console script: as root and as
  `nobody`, a run with the default lands in the journal under the identifier
  with `PRIORITY` 4 for the first-sight warning and 6 for the rest, exit 0 with
  0 bytes on both streams; `--check-config` prints `log: syslog (/dev/log)`; a
  `file:` path in a missing directory is one warning line and the log on stderr,
  no traceback. Four review lenses (correctness by reproduction on both hosts,
  specification fidelity and the #14/#15/#16 consumers, 42 mutants against the
  tests, operational safety as the cron user) reported 15 reproduced findings --
  the ones above and: `--check-config` never touching the destination (a mutant
  that made the check attach -- and create a `file:` log -- survived every test
  on Linux; the map had the check attaching to an explicit `--log`, which would
  have doubled the fallback line, corrected before the review), the
  stream-socket branch, the dying-destination path under `--debug`, `--log` for
  `--reset-state` and `--test-mail`, the teardown `close()` and the escape
  hatch's record were unpinned -- all fixed and pinned; six judgment-level
  findings were refuted by two skeptics each and stand (a `--test-mail` record
  carries no marker; `--check-config` does not stat a `file:` destination's
  directory; a record over rsyslog's 8 KiB default needs no cap: only a `sent
  via` line with 160 recipients gets there, and the journal and the file keep it
  whole). `tests/test_activity.py` (32 tests, the reviewers' 12 merged; seven
  POSIX-only -- a datagram socket the test binds stands in for `/dev/log`, the
  dead daemon, a stream socket, `/dev/full`, a FIFO, a planted symlink, the
  socket a check names -- each saying so; the journal itself is #14's check, as
  the issue says); the fixtures of the run, transport and reset-state tests set
  `log = file:<tmp>/activity.log`, since the default falls back to stderr on
  Windows. 22 mutations of the fix round, 20 reddened; the two race guards
  (`O_NOFOLLOW` behind the `lstat` check, the pre-open regular-file check behind
  the post-open one) are equivalent by design, and the map's claim that
  `S_ISSOCK -> exists()` is a killable mutation was wrong for the same reason --
  the connect probe is the guard. Noted for #14: the journal side is assertable
  with `SYSLOG_IDENTIFIER`, `SYSLOG_PID`, `_UID` of the service user, `PRIORITY`
  and `_COMM`; for #15/#16: the five fallback lines (a `udp:` destination has
  two: `cannot resolve the host` and `cannot open`), the `--check-config`
  resolutions, the level words, the file line shape, `--log`, `-n` to stderr,
  and that a text syslog file may cut a very long `sent via` line where the
  journal keeps it whole.

- `[internal]` **Linux stage: the installed script proven on a real host -- as a
  service user, through a real logrotate, into the journal, against the lock and
  the mode bits** (#14). `tools/linux_stage.sh`'s `run_native_checks()` replaces
  the kit template's two example checks (`uname`, `systemd is PID 1`) with eight
  that assert properties of logalert, keyed on a `mktemp -d` tree its trap
  removes, nothing on the host outside it touched (pip's cache is pointed into
  it), no user created, no unit installed, nothing mailed anywhere (the "MTA" is
  `tests/fake_sendmail.py` under the venv's shebang); the residue is this run's
  records in the host's logs (the journal, the syslog file rsyslog mirrors it
  into, `runuser`'s session lines in `auth.log`). Every host binary that can
  stall (`python3`, `cp` over the 9p mount, the venv and its pip, the installed
  script, `runuser`, `logrotate`, `journalctl`) sits behind `bounded
  "$BOUND_..."` with its exit code read first, 124 a skip; the stage's own
  helpers (`body.py`, `offset.py`, `journal.py` under the temp venv's
  interpreter) are bounded too, but a 124 there is a red naming the mail, the
  state or the filter -- never a pass -- and a `systemctl is-active` that does
  not answer is treated as journald absent (the `/var/log/syslog` check runs
  instead); every skip names its cause and is red under
  `LOGALERT_CHECK_MODE=required` (CI), tolerated in auto mode (the Windows gate,
  which delegates into the sandbox). The checks: **preconditions** -- `python3`
  names its minor and that `python3.X` must be on PATH (a VERSIONED interpreter,
  `INSTALL.md`'s rule), `runuser`, `logrotate`, `gzip`, the service user
  (`nobody`, `LOGALERT_STAGE_USER` to choose another); **install** -- the source
  copied out of the checkout (read-only over 9p in the sandbox; pip builds
  in-tree) and built into a wheel by the temp venv's own pip (an isolated build
  that fetches `setuptools>=77` once, so the network is needed; its absence is
  could-not-check, not a red naming the wheel), installed with `--no-deps`, the
  console script symlinked into a bin directory the way `INSTALL.md` prescribes:
  `logalert --version` through the symlink equals `version = "..."` in
  `pyproject.toml`, and the script's shebang is the venv's interpreter;
  **service user** -- as `nobody`, over a fixture log that holds a MATCHING line
  before the first run: first sight exits 0 with 0 bytes on both streams and
  mails nothing (a run from the beginning would mail that line), a new matching
  line is one mail from `alerts@example.net` to `noc@example.net` carrying it,
  exit 0 and quiet, the state's offset for the file equal to its size;
  **rotation** -- a matching line, a REAL `logrotate -f` with `compress`, a
  matching line in the fresh file: one mail, the report opening with the rotated
  copy's `==> ... <==` header, the pre- and post-rotation lines once each and
  the line mailed BEFORE the rotation never (mail bodies are read decoded -- a
  temp path makes a summary line longer than 78 characters, and the body then
  travels quoted-printable where `==>` is `=3D=3D>`); **lock** -- the holder's
  fake sendmail sleeps 3 s, the second run is launched once the fake has
  recorded the call and runs with `--log file:` under the fake's directory, and
  the turn-away must be OBSERVED there (`this run exits quietly`, #12's line) --
  ⭐ the review found the first version's assertions (one mail, two quiet exits)
  satisfied by two runs that never overlapped, and the mutation it named
  reddening only by timing; an unobserved overlap is could-not-check now;
  **syslog** -- after `journalctl --sync`, the records naming the temp tree
  carry `SYSLOG_IDENTIFIER=logalert` and the service user's `_UID`, and a run
  with `--log stderr` leaves its records on stderr and none in the journal
  (`/var/log/syslog` when journald is absent; a skip when both are); ⭐ every
  `journalctl` is read through a file with its exit code first -- through a
  pipe, a query that hung or refused printed `the journal holds 0 record(s)`,
  byte for byte the red the `ident dropped` mutation prints, so a journal-side
  fault would have sent the reader to `activity.py`; **permission** -- a `0600`
  root-owned log beside a readable one in a section: exit 1, exactly one stderr
  line naming it with `Permission denied`, the sibling's match mailed and its
  state advanced (the EACCES case pytest cannot establish on Windows or in the
  root sandbox); **modes** -- the state file `600`, the lock `644`, the
  directory still `750`, all the service user's. ⭐ Also from the review: the
  tree inherited the caller's umask, so `sudo` from a dev user with 027 or 077
  turned every service-user check red with `Permission denied` lines blaming the
  product (`umask 022` first); an unchecked `mktemp -d` under an unwritable
  `TMPDIR` left the tree variable empty and laid `/logs`, `/fake`, `/conf2` and
  the two helper scripts out at the filesystem root as root (measured on a copy
  stopped before the install; the real script's next lines head for `/venv`,
  `/bin/sendmail` and `/state`), which the trap correctly refused to remove
  (checked; a skip); `cleanup()` killed the lock check's subshell and not the
  run beneath it, which logged `state not saved` under the removed tree (it
  signals the `timeout` child, which forwards down the chain); the interpreter
  probe discarded `python3`'s stderr, reporting a loader failure as a PATH
  problem. Every check that asserts logalert (seven of the eight; preconditions
  asserts the host) reddens under a mutation of logalert of one line (two for
  the cursors never saved), applied to the checkout and measured through the
  stage in the sandbox (the wheel is built from the checkout) -- `__version__`
  hardcoded; the summary printed on exit 0; the cursors never saved; a rotated
  file read as a plain continue; the archive re-read from offset 0; every first
  sight from the beginning; a fresh lock holder as a failed item; the syslog
  ident dropped; an unreadable file ending its section; the state file `0644`;
  the lock `0600` -- eleven, every one naming its check in the verdict line.
  Re-measured after the fixes: green under `required` in 8 s, green under `umask
  077`, an unreachable index a skip in auto mode, a refusing `journalctl` named,
  a SIGTERM mid-check leaving no process, no tree and no record.
  `tests/test_linux_stage.py` gains the wiring guard the template asked for and
  the kit never wrote: every listed binary (`runuser`, `logrotate`,
  `journalctl`, `systemctl`, `cp` -- the one copy reads the 9p mount --
  `python3`, and any command under the temp tree or the versioned interpreter,
  however quoted) at a command position must be wrapped, prefixes (`if`, `!`,
  `then`, `while`, a bare `VAR=x`, `env`) stripped on both sides of the bound,
  heredoc bodies and comments dropped, the whole file scanned, and every listed
  name found at least once so a rename cannot make the guard vacuous; the
  delegation tests are unchanged. CLAUDE.md's expected-skips paragraph is
  unchanged (the stage's own skips are announced in its output, not pytest's).
  The stage is what CI runs natively on the three legs under `required` from
  this commit on; the journal side is the check #13 deferred here.

[Unreleased]: https://github.com/IjonTichy1970/logalert/commits/main
