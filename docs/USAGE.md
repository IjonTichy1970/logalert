# Using logalert

logalert is a log file watcher for cron. Every run it reads each configured log
file from where the previous run left off, matches the new lines against the
patterns of each section, and sends one email per section that matched. It
remembers its position per file, follows rotations (rotated copies are read
first, compressed or not), takes a lock so two runs never overlap, and logs its
own activity to syslog. On success it prints nothing; when something needs
attention it prints one line to stderr, which cron mails to `MAILTO`.

This document covers the command, the configuration file, position tracking and
rotation, running it from cron or a systemd timer, mail, logging and
troubleshooting. Installation (the venv, the `/usr/local/bin` symlink, upgrades)
is in [INSTALL.md](../INSTALL.md); every change to the behaviour described here
is in [CHANGELOG.md](../CHANGELOG.md), and the issue each entry links to holds
the decision behind it.

Linux is what CI tests. The BSDs are expected to work (the syslog socket paths
and the `newsyslog` naming styles are handled) but are not gated.

## Synopsis

```
usage: logalert [-h] [--version] [-f PATH] [--from ADDR] [-c N] [--attach]
                [--from-start] [-n] [-d] [--state-file PATH] [--log DEST]
                [--check-config | --example-config | --reset-state [PATH] |
                --test-mail SECTION]
```

A bare `logalert` is the run. The first time, on a new host:

```
sudo -u logalert logalert --check-config
sudo -u logalert logalert --test-mail router-disk
sudo -u logalert logalert -n
```

`--check-config` prints the effective settings and exits 2 if the file cannot be
used; `--test-mail` sends one real message through the configured transport and
reports its answer; `-n` reads and matches, prints the messages it would send,
and changes nothing. Then the cron line (see [Running it](#running-it)).

Run it as the user that will run it from cron, and never as root against that
user's state: a root run would leave the state file root-owned and the next cron
run unable to read it, so logalert refuses with `state file ... belongs to
<user>; ... run as that user instead: sudo -u <user> logalert ...`.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `-f PATH`, `--config PATH` | `/etc/logalert.conf` | The configuration file. |
| `--from ADDR` | `from =` in the config, else the running user at this host | The From address for this run, a bare `local@domain`. |
| `-c N`, `--context N` | `0` | Lines of context before and after each match, like `grep -C`, for sections that set no `context` of their own. |
| `--attach` | off | Send every report as an attachment this run, whatever the sections' `report` says. |
| `--from-start` | off | Read a file seen for the first time from its beginning instead of its end. Only a first sight is affected; a file with a saved position continues from it. |
| `-n`, `--dry-run` | off | Read and match, print to stdout the messages that would be sent, send nothing, write nothing (no lock, no state). The activity log goes to stderr instead of the configured destination (unless `--log` is given). Exit 0 or 1 by the same rule as a run. |
| `-d`, `--debug` | off | Write the activity log at DEBUG level to stderr as well (every cursor decision, the lock, the excludes); the configured destination still receives INFO and above. |
| `--state-file PATH` | `state_file =` in the config | Use this state file (absolute) instead of the configured one; honoured by the run, `--reset-state` and `--check-config`. |
| `--log DEST` | `log =` in the config | Write the activity log to `syslog`, `stderr`, `file:/absolute/path` or `udp:host:port` for this run, in every mode. |
| `--check-config` | | Validate the configuration, print the effective settings, and exit: 0 usable, 2 not. Never touches state or the log destination. |
| `--example-config` | | Print the complete, commented example configuration that ships inside the package, and exit. |
| `--reset-state [PATH]` | | Forget the saved position in `PATH` (spelled as in the config, or as `--check-config` lists a glob's match) for every section that watches it, or in every file when `PATH` is omitted; the next run treats those files as first sight, at the end (or the beginning per `start`) -- a section's new-file rule for globs is forgotten with them and resumes after its next run. |
| `--test-mail SECTION` | | Send a one-line test message to that section's recipients through the configured transport, print the transport's answer, and exit: 0 accepted, 1 not delivered, 2 configuration. |
| `--version` | | Print the version and exit. |
| `-h`, `--help` | | Print the options and exit. |

The four modes (`--check-config`, `--example-config`, `--reset-state`,
`--test-mail`) exclude one another. `--check-config`, `--reset-state` and
`--test-mail` refuse the run-only flags (`-n`, `--attach`, `--from-start`, a
nonzero `-c`) with exit 2: `-n --reset-state` would otherwise reset the state
under a flag that promised to leave it alone. `--example-config` prints the
example and exits before the run-only flags are examined.

## Exit codes and "cron-quiet"

| Exit | Meaning | What is printed |
| --- | --- | --- |
| 0 | The run completed; every section that had something to send was sent. | Nothing, on either stream. A match is the normal outcome, not an event. |
| 1 | Something needs attention: a file that could not be read, a delivery that failed, a recipient the server refused, a state file that could not be saved, a stale lock, a corrupt state file. | Exactly one line on stderr naming every failed item, and the detail in the activity log. |
| 2 | A usage or configuration error; nothing ran. | argparse's own message, or `logalert: <what is wrong>`. |
| 130 | Interrupted (Ctrl-C). The lock is released. | `logalert: interrupted`. |

The one line of exit 1 has a fixed shape:

```
logalert: 2 of 3 sections sent; failed: [router-disk] sendmail exit 75 (EX_TEMPFAIL): ...; see the log
```

The count clause appears only when some section had something to send (`section`
for one, `would be sent` under `-n`); each failed item is one clause between the
semicolons; `see the log` points at the activity log, where every failed item is
also recorded at ERROR with its context. The line is one line: control
characters and line breaks from a file name or a mail server's reply are folded,
so nothing can forge a second one.

"Cron-quiet" is the whole design: cron mails every byte a job writes to `MAILTO`
(or discards it with a note when no MTA is installed), so an exit 0 writes
nothing, and a problem is one line the operator reads in that mail. One
exception is deliberate: when the activity log's own destination cannot be used
(no syslog socket, a log file that cannot be opened), the run says so once on
stderr and logs there for the rest of the run, exit code unchanged -- a log that
went nowhere would be worse than a mailed line. See [Logging](#logging).

## Configuration reference

One INI file, `/etc/logalert.conf` by default. A reserved `[logalert]` section
holds the global settings; every other section is a watch: which files to read,
what to look for, whom to email. `logalert --check-config` after every edit.

The file's rules:

- Values that are lists (`to`, `files`, `patterns`, the excludes) go one per
  line, indented under the key. A blank entry is an error: an empty pattern
  would match every line. A list line beginning with `#` or `;` is dropped by
  the parser as a comment -- to match text that starts with one of those, use a
  regex with an escape (`\#`, `\;`).
- Paths are absolute. A `files` entry may be a glob (`*`, `?`, `[...]`; see
  [Globs](#globs)); every other path is one path, and a glob character in it
  is refused. `**` is refused everywhere, and a `files` entry may not end in
  a separator.
- Addresses are bare `local@domain`: no display name, no `<>`, no leading `-`
  (every sendmail implementation would read it as an option). Duplicates in `to`
  are sent once.
- Keys are case-insensitive; section names are case-sensitive and appear as
  written in the `X-Logalert-Section` header and in the log. A duplicate section
  or key is an error with its line number. `[DEFAULT]` is refused. `%` is
  literal. An unknown key is a warning from `--check-config`, then ignored.
- The two files logalert writes are checked, in every mode, exit 2: `state_file`
  may not be the configuration file (`state_file: /etc/logalert.conf is the
  config file itself`); a `file:` log may not be the state file, the `lock`
  beside it or the configuration file (`[logalert] log:
  /var/lib/logalert/state.json is the state file`); and neither may lie inside
  the venv (`... is inside the venv (/opt/logalert-venv), which the upgrade
  procedure wipes`). `--log` is checked the same way; `--state-file` against the
  configuration file and the log.

### The `[logalert]` section

| Key | Default | Meaning |
| --- | --- | --- |
| `from` | the running user at this host | The From address of the alerts, a bare `local@domain`. The default is `<user>@<hostname>`; when that has no domain part, or a local-only one (`.local`, `.localdomain`, `.localhost`), every run logs a warning: such mail may not travel beyond the host. Set it explicitly on such hosts. |
| `state_file` | `/var/lib/logalert/state.json` | Where the position in each file is kept between runs. The directory must exist and be writable by the user cron runs logalert as; logalert never creates it (a root run would leave it root-owned). The file is written `0600`, and so is the run lock, the file `lock` beside it (a lock any local user could open would be a lock any local user could hold). A `state_file` that is a symbolic link is refused (`is a symbolic link -- name the real path`): a link never worked as a redirect, since the first save would replace the link itself. Never under the venv. |
| `log` | `syslog` | Where logalert writes its own activity: `syslog`, `stderr`, `file:/absolute/path` (outside the venv), or `udp:host:port` (`udp:[2001:db8::1]:514` for an IPv6 address; explicit only; never a fallback). See [Logging](#logging). |
| `transport` | `auto` | How alerts leave the box: `auto` uses the local sendmail binary when it exists and otherwise refuses to start, saying what to install; `sendmail`; `smtp` to a relay. |
| `sendmail_path` | `/usr/sbin/sendmail` | The sendmail binary (postfix's, dma's, msmtp's `sendmail` wrapper). Invoked as `sendmail -i -f <From> <recipients>`. |
| `smtp_host` | | The relay, required when `transport = smtp`. A name, so the certificate can be verified under `smtp_starttls`. |
| `smtp_port` | `25` | The relay's port. |
| `smtp_starttls` | `no` | Ask for STARTTLS with the platform's default certificate verification; a relay that cannot is a delivery failure, never a silent downgrade. |
| `mail_timeout` | `60` | Seconds to wait for the mail system. For sendmail the whole child; for SMTP each socket operation (a slow relay can hold a run for a few multiples of it and still succeed). At most 3600. |
| `lock_stale` | `3600` | A run that finds another run holding the lock exits 0 quietly -- cron overlap is normal -- unless the holder is older than this many seconds, which is exit 1: `stale lock: another run (PID N) has held the lock <path> for Ns`. |
| `scan_timeout` | `300` | Seconds one file's scan may take before it is given up as a failed item naming the line and the pattern being tried (`scanning exceeded scan_timeout (300 s) at line N while trying regex '...'`); its position stays, the section's other files are processed. `0` turns the bound off. A scan that is legitimately long -- a first read of a multi-GB file on a slow machine -- needs a higher value, or `0` for that run; the message names the key. Enforced on Linux and the BSDs (an interval timer); on Windows `--check-config` says `not enforced on this platform`. |
| `state_ttl` | `30` | Days after which the saved position of a file that has not been seen is forgotten (logged as `forgotten`). A file that is back after that is a first sight again. |
| `subject_suffix` | `yes` | Append ` -- N match(es)` to every Subject, a literal a mail filter can key on. `no` for the bare subject. |

### A watch section

| Key | Default | Meaning |
| --- | --- | --- |
| `subject` | required | The Subject of the alert, verbatim (plus the suffix above). |
| `to` | required | The recipients, one per line or comma-separated. |
| `files` | required | The log files to read, absolute, one per line; an entry with `*`, `?` or `[` is a glob, expanded at every run ([Globs](#globs)). Compressed files (`.gz`, `.bz2`, `.xz`; `.zst` on Python 3.14+) may be listed directly and are read as a live log -- right for a one-off `start = beginning` read of a static archive, never for a rotated copy of a log the section already lists: the catch-up reads the copies itself, and a listed copy is mailed whole after every rotation (`--check-config` warns). An archive that has not changed since it was last read (same inode, size and mtime) is not decompressed again. A file the list names more than once, or a glob matches after it was named, is read once. A listed symbolic link is followed only when its owner is root, the running user or the file's owner ([Globs](#globs)). |
| `patterns` | | Literal text, matched case-SENSITIVELY against the whole line. A line matches when any pattern of any of the four shapes is found in it. |
| `ipatterns` | | Literal text, case-insensitive. |
| `regex` | | Python regular expressions (`re.search`), case-sensitive. `re` has no backtracking limit: a nested or ambiguous quantifier (`(x+)+`, `(x*)*`, `(\w+\s?)+`) can run for hours on one long line of ordinary words, holding the lock -- `--check-config` warns about that shape, and `scan_timeout` bounds the damage. An anchor or a delimiter class (`[^ ]+`) usually stops it. |
| `iregex` | | Regular expressions, case-insensitive. |
| `exclude`, `iexclude`, `exclude_regex`, `iexclude_regex` | | Noise: a line that matched a pattern but also matches one of these is dropped, and the count of dropped lines is in the mail's summary. The same four shapes. |
| `priority` | off | `high`, `medium` or `low`. The priority system is OFF unless a section sets this or a pattern carries a tag; when on, the mail carries `X-Logalert-Priority` plus the conventional `X-Priority` (`1`/`3`/`5`) and `Importance` (`high`/`normal`/`low`), the highest across the mail's matches. A pattern can carry its own tag: `[high] Requesting reboot` (lowercase, one space); any other bracketed prefix is part of the literal. |
| `report` | `inline` | `inline` puts the report in the body; `attachment` attaches it as `<section>-<YYYYmmddTHHMM>.txt`, the section name reduced to ASCII letters, digits, `.`, `_` and `-` and cut to 40 characters (see [Mail](#mail)). The body opens with a summary either way. `--attach` forces the attachment for one run. |
| `context` | the command line's `-c` | Lines of context before and after each match, like `grep -C`. Context never crosses from one file into another. |
| `max_lines` | `200` | At most this many MATCHING lines per email, each with its context, then one trailer `... and N more matching line(s)` with the exact remainder. The report is also cut at 1 MiB of text, whatever `max_lines` says (`; the report was cut at 1 MiB` in the trailer): 1.4 MB on the wire as base64, up to 3.2 MB as quoted-printable, under a relay's default size limit either way. |
| `start` | `end` | Where the first run of a file begins: `end` (no flood of old news) or `beginning`. |
| `archive_dir` | the log's own directory | Where rotated copies live when they are not beside the log: logrotate's `olddir`, newsyslog's `-a`. |
| `include_archives` | `no` | Whether a glob reads rotated copies as files of their own ([Globs](#globs)). `yes` is for directories where the dated names ARE the live files (Apache's `rotatelogs`), never for a rotating log. |

A physical line is matched and shown whole up to 16000 bytes (eight 2000-byte
fragments); what lies beyond is neither matched nor shown, and the report line
ends in ` [cut]`. A context line from before the saved position is shown as its
first 2000 bytes, with the same marker.

### The example

This is `logalert --example-config`, verbatim (a test keeps the two identical):

```ini
# logalert configuration -- /etc/logalert.conf
#
# One file, any number of sections. The [logalert] section holds the global
# settings; every other section is a WATCH: which files to read, what to look
# for, and whom to email. Run `logalert --check-config` after editing.
#
# Values that are lists (to, files, patterns, ...) go one per line, indented:
#
#     patterns =
#         disk failure
#         Requesting reboot
#
# A blank line inside such a list is an error. A list line that begins with
# `#` or `;` is treated as a comment by the parser and dropped -- to match text
# that starts with one of those, use a regex with an escape: `\#` or `\;`
# (the `regex` block below shows one).

[logalert]
# Sender address of the alert emails. Default: the user logalert runs as, at
# this host's name (`root@host.example.net`). Set it explicitly on hosts with
# odd DNS, or to make replies land somewhere useful.
#from = logalert@example.net

# Where logalert remembers its position in each file between runs. The
# directory must exist and be writable by the user cron runs logalert as; the
# file is written 0600 and the run lock (`lock`) sits next to it. To forget a
# position: `logalert --reset-state /path/to/file` (every file: no path).
#state_file = /var/lib/logalert/state.json

# Where logalert writes its own activity: syslog (default: the first socket
# of /dev/log, /var/run/log, /var/run/syslog, tagged `logalert` so that
# `journalctl -t logalert` finds the runs), stderr, file:/absolute/path
# (outside the venv), or udp:host:port (explicit only; never a fallback).
# When the destination cannot be used the run says so once on stderr and
# logs there. `--log DEST` overrides this for one run.
#log = syslog

# How alert emails leave the box: auto (default) uses the local sendmail
# binary when it exists and otherwise refuses to start with a message saying
# what to install; sendmail; or smtp to a relay.
#transport = auto
#sendmail_path = /usr/sbin/sendmail
#smtp_host = mail.example.net
#smtp_port = 25
#smtp_starttls = no

# Seconds to wait for the mail system before treating a message as not
# delivered. Nothing in a cron job may hang forever.
#mail_timeout = 60

# A run that finds another run still holding the lock exits quietly -- unless
# that run is older than this many seconds, which is reported as a problem.
#lock_stale = 3600

# Seconds a single file's scan may take before the run gives it up as a failed
# item naming the line and the pattern (a regex with a nested quantifier can
# run for hours on one long line, holding the lock). 0 turns the bound off.
# Enforced on Linux and the BSDs; on Windows it is reported and not enforced.
#scan_timeout = 300

# Days after which the position of a file that has not been seen is forgotten.
#state_ttl = 30

# Append " -- N match(es)" to the Subject. Set to no for the bare subject.
#subject_suffix = yes

# ---------------------------------------------------------------------------
# A watch. The section name is yours; it appears in the X-Logalert-Section
# header and in logalert's own log.

[router-disk]
# Subject of the alert email (verbatim; see subject_suffix above).
subject = Router disk failure

# Recipients: one per line, or comma-separated. Bare addresses only.
to =
    noc@example.net

# Log files to read: absolute paths, one per line. Compressed files (.gz,
# .bz2, .xz) may be listed directly. An entry may be a glob (* ? [...], the
# shell's rules: a wildcard stays within one directory level, names starting
# with a dot need a pattern that starts with one, [[] is a literal bracket; **
# is refused), expanded at every run: /var/log/hosts/*/messages. A glob reads
# regular files only, never through a symbolic link (list a link by name),
# and leaves rotated copies out (see include_archives).
files =
    /var/log/router.log

# What to look for. Literal text is matched case-SENSITIVELY:
patterns =
    disk failure
    Requesting reboot

# Case-insensitive literals, and regular expressions (Python `re` syntax),
# case-sensitive and case-insensitive:
#ipatterns =
#    link down
#regex =
#    ^\S+ \d+ \d\d:\d\d:\d\d router1 kernel: .*fail
#    \#\d+ .*failed
#iregex =
#    error|panic

# Noise: a line that matches a pattern but also matches one of these is
# dropped. The same four shapes as the patterns.
#exclude =
#    disk failure on da9 (known bad, ticket open)
#iexclude =
#    test message
#exclude_regex =
#    ^.* kernel: \[debug\]
#iexclude_regex =
#    heartbeat

# Priority (optional; OFF unless set here or by a tag). high, medium or low.
# When set, the email carries X-Logalert-Priority plus the standard
# X-Priority / Importance headers, so a mail client can route high-priority
# alerts to the inbox and the rest to a folder. A single pattern can carry its
# own priority with a leading tag (lowercase, then one space), which also
# works when priority is unset:
#
#     patterns =
#         [high] Requesting reboot
#         [low] link flap
#
#priority = medium

# Where the findings go: inline (default) as a plain-text report in the body,
# or attachment as a text file, with a brief summary in the body either way.
#report = inline

# Lines of context before and after each match (like grep -C). Default: the
# command line's -c, which defaults to 0.
#context = 0

# Flood cap: at most this many matching lines per email, each with its
# context; the rest is one trailer, "... and N more matching line(s)".
#max_lines = 200

# On the first run, where to begin in each file: end (default -- no flood of
# old news) or beginning.
#start = end

# Where rotated archives live when they are not beside the log (logrotate's
# olddir, newsyslog's -a). Default: the log's own directory.
#archive_dir = /var/log/archive

# Whether a glob in files reads rotated copies (router.log.1,
# router.log-20260915.gz, router.log.bak, messages.gz) as files of their
# own. Default no: a rotation would otherwise mail every alert twice. yes
# is for directories where the dated names ARE the live files (Apache's
# rotatelogs), never for a rotating log. A file a glob first matches is read
# from its beginning when it is newer than the section's last run over that
# glob (a new daily file is all new content); otherwise its first sight
# starts at the end, like a listed file's. logalert --check-config lists
# what a glob matches and what it leaves out.
#include_archives = no

# ---------------------------------------------------------------------------
# A second watch, with case-insensitive matching. The rotated copies
# (firewall.log.0.gz, ...) are read by the catch-up when the file rotates;
# listing one beside the live file would mail it whole after every rotation.

#[firewall-denies]
#subject = Firewall denies
#to = noc@example.net, security@example.net
#files =
#    /var/log/firewall.log
#ipatterns =
#    deny
#exclude_regex =
#    from 192\.0\.2\.
#report = attachment
#max_lines = 1000
```

## Position tracking and rotation

**The first run starts at the end of each file.** Nothing that was already in
the file is mailed; the activity log records it as a warning, `[router-disk]
/var/log/router.log: first sight; starting at the end, 48211 bytes skipped`,
because it is the one time lines are deliberately never sent. `start =
beginning` in the section, or `--from-start` for one run, reads a first sight
from the beginning instead.

**The state file** (`state_file`, JSON) holds one entry per section and file:
the byte offset just after the last complete line read, the file's identity
(inode and device, plus a hash of its first line as a fingerprint), and when it
was last seen. Two sections watching the same file have two entries, so the
section whose mail failed keeps its place while the other advances. The state is
saved after each section, atomically; a section whose delivery failed keeps the
positions of the files that contributed to the message (the next run re-sends
them) and marks them seen, so a file whose mail keeps failing never expires; a
file of that section that matched nothing moves on, since none of its lines is
in the message.

**Rotation.** When a run finds the live file rotated (a different inode) or
truncated (smaller than the saved offset, as `copytruncate` leaves it), it looks
for the rotated copy that holds the saved position and reads it first -- the
copy's tail from the saved offset, then every newer copy in full, then the live
file from the beginning -- so nothing written between two runs is missed,
however many rotations happened in between. It recognises, beside the log or in
`archive_dir`:

- numeric suffixes `.N` (lower is newer: `.0` for savelog and newsyslog, `.1`
  for logrotate);
- date stamps: logrotate's `dateext` in its spellings (`-YYYYMMDD`,
  `-YYYYMMDDHH`, `-YYYYMMDD-<epoch>`, `-YYYY-MM-DD`, `-YYYYMMDDHHMMSS`,
  `-<epoch>`), Python's `TimedRotatingFileHandler` (`.YYYY-MM-DD`,
  `.YYYY-MM-DD_HH-MM-SS`), newsyslog's `-t` (`.YYYYMMDDTHHMMSS`), and the
  hand-rolled `.YYYYMMDD` and `.<epoch>`;
- each of those with an optional `.gz`, `.bz2` or `.xz` after it, read through
  the decompressor (`.zst` too, on Python 3.14+; on an older Python a `.zst`
  archive is skipped with a warning and its lines are lost -- keep logrotate on
  gzip there);
- anything else sharing the log's name as a stem, ordered by modification time,
  with a log line saying the order was a guess.

Symbolic links are never candidates. A `.gz` still being written yields what it
has, with a warning, and the run continues.

**A rotation during the run.** When a rename rotation (logrotate's default;
not `copytruncate`) lands while the copies are being read, a copy the run was
about to open is no longer the file it planned to read (renamed on, compressed
away, removed). The run stops there and says so:

```
[router-disk] /var/log/router.log: router.log.1 was renamed or removed under us (a rotation during the run); stopping here, the next run resumes after router.log.2 (the names are from before the rotation)
```

Its position stays at the end of the last copy it read, and the next run carries
on from there under the new names, so nothing is mailed twice or lost --
provided that copy is still there at the next run: keep one rotation more than
the interval between runs needs. A rotation landing between the directory
listing and the copies' opens makes the run list the directory again, once; a
second rotation inside one run reaches the warning below, whose first cause is
then `a second rotation during this run (a copy was renamed twice)`.

**The "no rotated copy" warning.** When no copy holds the saved position the log
says so:

```
[router-disk] /var/log/router.log: no rotated copy holds the saved position (...) in /var/log -- likely causes: ...; reading the live file from the beginning, and lines written between the last run and the last rotation are lost
```

The `likely causes` the line names, in the log's own words: `rotate 0` (the
rotating tool keeps no copies), `an olddir or -a elsewhere (set archive_dir)`
(the archives live somewhere else), `unsupported compression` (a compressor
logalert does not read: `.zst` before Python 3.14, or anything but gzip, bzip2
and xz), `the archive aged out` (rotation ran more times than there are archives
kept: raise `rotate`, or run logalert more often than the rotation), and, only
when the file had no complete first line at the last run, `the file had no
complete first line at the last run` (nothing to match a copy by but its inode,
which compression replaces). After a rotation during the run (above) the first
cause named is `a second rotation during this run (a copy was renamed twice)`.
A file replaced by one that is not a continuation
of it -- a different first line, or a new inode with no archive behind it --
says the same thing. The run does not fail on it; the live file is read from the
beginning, so anything in it is mailed once.

**Forgetting a position.** `logalert --reset-state /var/log/router.log` forgets
every section's position in that file; `logalert --reset-state` forgets them
all. The next run is a first sight (the end of the file, or the beginning per
`start`) -- it prints what it did and records it in the activity log. A state
file nothing can read is refused by the run with `fix it or start over with
--reset-state`; `--reset-state` without a path replaces it with an empty one.
`state_ttl` forgets the position of a file not seen for that many days on its
own.

## Globs

A `files` entry containing `*`, `?` or `[` is expanded at every run, with the
shell's rules: a wildcard stays within one directory level (`/var/log/*.log`,
`/var/log/hosts/*/messages`), a name starting with `.` is matched only by a
pattern component that starts with one, `[[]` is a literal bracket, and the
platform decides case. `**` is refused, and so is an entry ending in `/`. A
glob matches regular files only, and never through a symbolic link: a link it
matches, and a link where a wildcard directory component would descend, is
passed over (a link is a name you never wrote, and a run with more privilege
than whoever planted it would otherwise mail a file outside the directory);
list a link by name, which is your own choice -- but what a listed name
resolves to at each run is the choice of whoever owns its directory, so a
listed link is followed only when its owner is root, the user logalert runs
as, or the owner of the file it points to (root's `/var/log/foo -> /data/foo`
and an application's own `current -> today.log` work; a link another user
planted towards a file that is not theirs is a failed item, `is a symbolic
link owned by <user> to a file owned by <other>; not followed`). A link to
another link is refused (`points at another symbolic link`); name the file. A
directory, FIFO or device the glob names is passed over too (`/var/log/*`
names directories nobody wants read), where a listed entry naming one is an
error; hard links to one file are read once, under the first name in sort
order. The matches are read in name
order, so a daily directory reads in date order, and each match is one file
with its own position in the state, spelled as `--check-config` lists it --
that spelling is what `--reset-state` takes (given the glob itself,
`--reset-state` says so and takes nothing). A file the list names and a glob
also matches is read once, under the listed spelling.

**Rotated copies are left out** unless the section sets `include_archives =
yes`. Three shapes of name are copies wherever they stand, live file present or
not: a rotation suffix the rotation rules recognise (see
[above](#position-tracking-and-rotation)), numeric or dated, compressed or not,
when what precedes it does not end in a digit (`router.log.1`,
`router.log-20260915.gz`, `access_log.1726358400`, `app.2024` -- but
`192.0.2.1`, `2026-09-15` and `python3.12` are files); a copy's suffix
(`.bak`, `.old`, `.orig`, `.save`, `-old`, `-bak`, with or without a
compression extension); and a bare compression extension (`messages.gz`, a
hand-made `gzip` of a live log). A fourth is judged against the other matches
in the same directory: a numeric or dated rotation of another matched name
whose base ends in a digit (`router1.2` beside `router1`). Nothing else is:
`fw-dmz` beside `fw`, `router1.example.net` beside `router1`, `syslog.log`
beside `syslog` are hosts and logs of their own. Otherwise `/var/log/router*`
would read `router.log.1.gz` as a file of its own and mail every alert twice
after each rotation. `--check-config` lists what a glob matched, what it left
out and what it passed over, by name; a file left out that is wanted is listed
by name. `include_archives = yes` reads every match: for directories where the
dated names are the live files, as Apache's `rotatelogs` writes them; on a
rotating log it mails the rotated content twice, because the renamed copy gets
a position of its own and the next rotation replaces it.

**A new file is read from its beginning.** A file a glob matches for the first
time is read from 0, not from its end, when the section has run before with
that glob in its `files`, that run listed the glob's directories, and the file's
modification time is not older than the start of that run: a new daily file is
all new content, and starting at its end would lose what was written before
the run that found it. The activity log says `new since the last run (<glob>);
reading from the beginning`. Everything else is a plain first sight at the
end: a glob just added to the configuration (so widening `error.log` to
`*.log` never mails a long-lived file whole), a glob whose directory has never
yet been listed (a share mounted or a permission granted after the first run
does not mail every live log in it whole), a file older than the section's last
run (a copy restored with its timestamps, a rotated copy under
`include_archives = yes` -- `rename`, `gzip`, `xz` and `bzip2` keep the
original's modification time), and every listed path, whose first sight is
unchanged. A glob's moment is recorded when the section's positions are saved
-- never when its delivery failed, so the re-run reads the same new file from
0 again, and never for a glob whose directory was away or could not be listed
that run, so a file created during the outage is read whole once the directory
is back. `--reset-state` forgets the section's moments with the positions: a
file that appears between the reset and the next run is a plain first sight
at the end, so reset right before a run, or preview with `-n`. One consequence
to know: a file the glob matched but could not open for a while (a `Permission
denied` failed item each run) is read from 0 once it can be, since it was never
matched before; a fresh modification time on old content (`cp` without `-p`)
reads the same way, at most `max_lines` matching lines in one mail; `-n` first
shows what that would send.

A glob that matches nothing is nothing to do (a DEBUG line; the directory may
not exist yet). A directory the glob needs to list but cannot is a failed item
(`[section] /var/log/hosts/*/messages: cannot list /var/log/hosts (Permission
denied)`, exit 1): a watch that went quiet because of a permission must not
look like a watch with nothing to say.

## Running it

**cron.** As the user that owns the state directory (here a user named
`logalert`, with `/var/lib/logalert` created for it, mode `750`), in that user's
crontab (`crontab -u logalert -e`):

```
MAILTO=noc@example.net
*/5 * * * * /usr/local/bin/logalert
```

The absolute path matters: cron's `PATH` is a short fixed one that may not
include `/usr/local/bin`, and the symlink is what carries the venv's interpreter
(see [INSTALL.md](../INSTALL.md)). `MAILTO` is where the one line of a failed
run goes; a run that finds nothing prints nothing and cron sends nothing. A run
that starts while the previous one is still delivering finds the lock held and
exits 0 quietly, so a slow relay never produces a pile-up; a holder older than
`lock_stale` is reported as exit 1 instead.

**systemd timer.** The same as a oneshot service and a timer, with an absolute
`ExecStart` and the service user:

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

Keep the default `log = syslog` under a timer: the records reach the journal
tagged `logalert` with their own priority, so `journalctl -t logalert` and
`journalctl -p warning -t logalert` work. Stderr under a unit reaches the
journal too, under the same `logalert` tag (the executable's name) but every
line at one priority (info), so `journalctl -p warning` would not find a warning
written there, and the one line of a failed run would appear there twice (as the
ERROR record and as the stderr line); nothing mails it unless `OnFailure=` or a
journal watcher is set up -- which is the trade-off against cron's `MAILTO`.

## Mail

**An MTA is required for the default transport.** `transport = auto` uses the
local `sendmail` binary; a fresh Ubuntu server has none, and the run refuses to
start rather than fail silently: `transport = auto but /usr/sbin/sendmail does
not exist: install an MTA (Ubuntu: apt install postfix, dma or msmtp-mta;
FreeBSD 14+: dma is in base) or set transport = smtp and smtp_host`. Any of
those provides `/usr/sbin/sendmail`; configure it to relay to your mail server.
`--check-config` reports whether the binary exists and is executable.

**SMTP.** `transport = smtp` with `smtp_host` (a name), `smtp_port` and
`smtp_starttls` hands the message to a relay directly. One refused recipient is
reported (`refused: <recipient> -- <reply>`) with the others delivered, exit 1
-- the message counts as delivered and the section's position moves, so the
refused recipient is not re-sent those lines (the others must not get them
twice); every recipient refused, a rejected message, a dropped connection or no
reply within `mail_timeout` is a failed delivery, and the section's position is
kept so the next run re-sends it. A message refused for the same reason every
run -- a relay's size limit below the 1 MiB report cap, a content filter -- is
rebuilt from the same position and refused again: exit 1 every run, and
everything written after the block waits behind it. The escapes: lower
`max_lines` for one run (the message shrinks, is accepted, and the position
moves past the whole block), an `exclude_regex` for the offending shape, or
`--reset-state <file>`, which forgets what is pending.

**`--test-mail SECTION`** sends one real one-line message to that section's
recipients and prints what happened:

```
sent via sendmail (/usr/sbin/sendmail): accepted for queueing (exit 0)
to: noc@example.net
size: 704 bytes; Message-ID: <...@example.net>
```

or `logalert: not delivered via smtp (relay.example.net:25): <answer>` on stderr
with exit 1, or the configuration error with exit 2.

**"Accepted for queueing" is not "delivered".** A sendmail exit 0 means the MTA
took the message; it may still sit in the queue if the relay is unreachable.
`mailq` (and the MTA's own log: `journalctl -u 'postfix*'`, `journalctl -t dma`,
`/var/log/mail.log`) is where a message that never arrived is found. The
activity log records every message with its recipients, size and `Message-ID`
(also in the mail's summary), the key to correlate the two.

**The From address.** By default the user logalert runs as at this host's name.
When that has no domain part or ends in a local-only domain, every run logs a
warning (`From address 'logalert@router1' has no domain part; set from = in
[logalert]`) because such mail may be refused or dropped along the way: set
`from = logalert@example.net`. `--from ADDR` overrides it for one run.

**What an alert looks like.** `Subject: Router disk failure -- 3 match(es)`;
`From`, `To`, `Date`, `Message-ID`, `Auto-Submitted: auto-generated` (no
vacation replies), `X-Prepared-By: logalert <version>`, `X-Logalert-Section`,
and the priority headers only when a priority is set. The body opens with a
summary: the subject, the count, the host and the time of the run on one line,
then the section, the priority when set, one `Files:` row per file read with its
matches and lines read (and the lines an exclude dropped), and the `Message-ID`.
Then the report, in `grep -n -C`'s vocabulary: a `==> file <==` header per
physical file (a rotated copy's tail before the live file after a rotation), `N:
text` for a match, `N- text` for a context line, `--` between separated windows,
` [cut]` where the cap dropped the rest of a line, and the `max_lines` trailer.
With `report = attachment` the report is a `text/plain` attachment named
`<section>-<YYYYmmddTHHMM>.txt`, the section name reduced to ASCII letters,
digits, `.`, `_` and `-` (a run of anything else becomes one `_`), never
starting with `-` or `.`, and cut to 40 characters, so the name never folds into
the form some mail clients show as `ATT00001.txt`. The body is always 7-bit
clean, so no MTA's line limit rejects it whatever the log holds: when every line
is ASCII and at most 78 characters it travels as is, otherwise quoted-printable
or base64 (a typical syslog line is longer, so the raw message shows `=3D=3D>
file <=3D=3D`); an attachment is encoded on its own. A mail client decodes it;
`grep` over a mailbox file or a message in the MTA's queue shows the encoded
form; `-n` prints it decoded. A byte that is not UTF-8, and any control
character in a log line other than a TAB -- an ESC, a NUL, a form feed, DEL, the
C1 range -- is shown as the replacement character U+FFFD, and a CR is dropped: a
coloured log line arrives with the mark standing where each ESC was and the rest
of the sequence as text. In a Subject or a section name a TAB or a line boundary
becomes a space.

## Logging

Every run records its activity: the start (config path, section count), per file
the lines read and matched and how the position was resolved when there is
something to say (first sight, rotated, truncated; a plain continue is a DEBUG
record, seen under `--debug`) -- a file a glob matched with nothing new is a
DEBUG record too, and each glob gets one INFO summary per run (`<glob>: N
file(s), M with new lines, K matched`), so a daily directory does not write a
line per file per run -- per section the message sent with its recipients,
size and `Message-ID` -- or the failure with the transport's answer -- every
failed item at ERROR, every expired position, and the exit code at the end. The
lines are one line each, the level shown as a word for `warning:`, `error:` and
`debug:` (INFO carries none).

**Where.** With the default `log = syslog`, on a systemd host:

```
journalctl -t logalert
journalctl -p warning -t logalert --since today
```

Elsewhere, wherever the syslog daemon puts `user`-facility messages tagged
`logalert[<pid>]` -- the run's records at `user.info`, its warnings at
`user.warning`, so a daemon that files only `*.notice` and up keeps the warnings
and drops the rest: `/var/log/syslog` (Debian/Ubuntu with rsyslog),
`/var/log/messages` (Red Hat). logalert probes `/dev/log`, `/var/run/log` and
`/var/run/syslog` for the socket.

**The other destinations.** `log = file:/var/log/logalert.log` appends one
stamped line per record (`2026-09-15T14:11:02-0600 logalert[<pid>]: ...`) to a
file the running user can write; `log = stderr` prints them (`logalert: ...`);
`log = udp:host:514` sends them to a syslog collector -- explicit only, and be
aware that a datagram to a port nobody listens on is silent forever. `--log
DEST` picks any of these for one run, in every mode.

**When the destination cannot be used** -- no syslog socket, a log file that
cannot be opened, a relay host that does not resolve -- the run does not stop
and does not go silent: it writes one warning line to stderr naming what was
tried, logs the rest of the run to stderr, and exits with the run's own code.
The line is `no usable syslog socket (/dev/log: connection refused;
/var/run/log: not a socket; /var/run/syslog: missing); logging to stderr`,
`cannot open the activity log /var/log/logalert.log (Permission denied); logging
to stderr`, `udp:relay.example.net:514: cannot resolve the host (...); logging
to stderr`, or `udp:[2001:db8::1]:514: cannot open (...); logging to stderr`
when the host resolves but the socket cannot be made. Under cron that mail is
the signal to fix the logging. A destination that fails mid-run (the syslog
daemon stopped, a full disk) is reported once the same way: `the activity log at
syslog (/dev/log) failed (...); logging to stderr from here on`.

**`-n` logs to stderr** rather than to the configured destination (unless
`--log` is given): a dry run writes nothing, and its records (`start: ... (dry
run)`, `would be forgotten`, the first-sight line) belong in front of the
operator, not in syslog as if they were a run's. **`--debug`** adds every DEBUG
record (cursor decisions, offsets, the lock, the excludes) on stderr while the
configured destination still gets INFO and above.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Nothing arrives, cron is quiet | The first run of a file starts at its end, so lines already there are never mailed; or the message is queued in the MTA | Append a matching line and wait for the next run, or run `logalert -n` and see what it would send; `mailq` for a queued message; `journalctl -t logalert` for what each run did |
| `transport = auto but /usr/sbin/sendmail does not exist: ...` (exit 2) | No MTA on the host | Install one (`postfix`, `dma`, `msmtp-mta`; `dma` is in FreeBSD's base) or set `transport = smtp` and `smtp_host` |
| The same `sendmail exit N` / `552` refusal in cron's mail every run, and nothing new from that section | The relay refuses the message for what it is (its size below the 1 MiB report cap, a content filter), and the run re-sends the same message each time -- everything behind it waits | Lower `max_lines` for one run so the block is accepted and the position moves past it; an `exclude_regex` for the shape; or `--reset-state <file>` (what is pending is forgotten). The MTA's log names the reason |
| `... failed: [router-disk] sendmail exit 75 (EX_TEMPFAIL): ...; see the log` in cron's mail (exit 1), or `logalert: not delivered via sendmail (...): sendmail exit 75 (EX_TEMPFAIL): ...` from `--test-mail` | The MTA could not accept the message (its queue, its configuration) | The MTA's log; the run kept the section's position, so the next run re-sends (a `--test-mail` failure keeps nothing: there is no position) |
| The same lines arrive twice | A delivery whose state could not be saved afterwards (`state not saved` in the log) -- the mail went out, then the next run re-sent it | Make the state directory writable by the running user (`state directory ... is not writable`); the run refuses to send when it cannot save, once it knows |
| `[section] /var/log/x.log: Permission denied` (exit 1) | The running user cannot read that file; the other files of the section were processed | Grant read access (a group, ACLs) or run as a user that has it |
| `state file ... belongs to <user>; a run as root would leave it root-owned ...` -- or, before the first run, `state directory ... belongs to <user>; ...` (exit 1) | A root run against a cron user's state | Run as that user: `sudo -u <user> logalert ...` |
| `stale lock: another run (PID N) has held the lock ... for Ns` (exit 1) | A run still alive and holding the lock for longer than `lock_stale` -- a stuck run, or one that genuinely takes that long. Not a leftover file: the lock is an OS lock, released the moment its holder exits or is killed; the last holder's line stays in the `lock` file by design and turns nobody away | Look at PID N (`ps -o pid,etime,cmd -p N`; `cat` the `lock` file beside the state file shows the holder's PID and start time) and end it if it is stuck -- the lock is released with it; raise `lock_stale` if runs really take that long. If `ps` shows PID N is not logalert, the PID was reused: `fuser <lock>` as root names the holder. Do not remove the `lock` file: while the holder lives, the next run would lock a fresh file and overlap it |
| `stale lock: the recorded holder PID N is gone; another process holds the lock ... (fuser ... names it)` (exit 1) | The last run that wrote the file has exited (or PID N now belongs to another user's process, which cannot hold a `0600` lock), and something else holds the lock: a run stalled between taking the lock and writing its line, or another process that opened the file and locked it | `fuser <lock>` (or `lsof <lock>`), run as root -- as the running user it cannot see another user's process -- names the holder; end it if it should not be there. The lock is `0600` (one left `0644` by an earlier version is tightened by the next run that takes it), so only the running user and root can open it |
| `warning: no usable syslog socket (...); logging to stderr` in cron's mail every run | No syslog daemon, or the socket is somewhere else | Start rsyslog / journald, or set `log = file:/var/log/logalert.log` |
| `warning: From address '...' has no domain part; set from = in [logalert]` | The default From is `<user>@<short hostname>` | Set `from = logalert@example.net` |
| `'...' is not a bare local@domain address` (exit 2; as `[logalert] from:`, `[<section>] to:` or `--from:`) | A display name, angle brackets or spaces in an address; a leading `-` is refused separately as `'...' starts with '-'` | Bare addresses only |
| `scanning exceeded scan_timeout (300 s) at line N while trying regex '...'` (exit 1), or `stale lock` every run with PID N alive at 100 % CPU | A regex with a nested quantifier on a long line (`(x+)+`, `(\w+\s?)+`): `re` backtracks without bound; on Windows nothing bounds it | Simplify the regex (an anchor, a delimiter class instead of `\w+\s?`); the file is re-read next run, so `--reset-state <file>` skips the line if the log must keep it; `--check-config` names the shape |
| A pattern starting with `#` or `;` never matches | The parser drops such a continuation line as a comment | A regex with the escape: `\#`, `\;` |
| `no rotated copy holds the saved position ...` in the log after every rotation | The archives are elsewhere, or fewer are kept than rotations happen between runs | Set `archive_dir`, or run logalert more often than the rotation |
| `--reset-state /var/log/x.log` says `no entry for ...` | The path is not spelled as in the config (or as `--check-config` lists a glob's match), or the file was never seen; given the glob itself it says `is a glob` | Use the exact path; `--reset-state` with no path forgets everything |
| A glob mails nothing, or a file it should read is missing from `--check-config` | The glob matches nothing where it looks (`matches nothing`), or the file is left out as a rotated copy (`left out:` -- a name ending in `.N` or a date such as `app.2024`, a `.bak` or `.gz` twin), or it is not a regular file or is a symbolic link (`passed over:`) | `logalert --check-config` names every match and everything left out; list a wanted file by name, or set `include_archives = yes` for a directory of dated live files |
| `[section] /var/log/app/current: is a symbolic link owned by app to a file owned by root; not followed (...)` (exit 1) | A listed path is a link whose owner is neither root, nor the running user, nor the owner of the file it points to -- in a directory another user owns, what the name resolves to is that user's choice | List the file itself, or make the link root's (`chown -h root <link>`); a link another user planted is the reason the rule exists |
| `[section] /var/log/hosts/*/messages: cannot list /var/log/hosts (Permission denied)` (exit 1) | The running user cannot list a directory the glob needs to look into; the files of the section that were reachable were processed | Grant read and search permission on the directory, or run as a user that has it |
