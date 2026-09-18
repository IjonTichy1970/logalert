# logalert -- project instructions

Generalized log file watcher. Python, **public** repo `IjonTichy1970/logalert`,
developed on Windows, run on Linux, distributed via GitHub Releases (not PyPI).
Global `~/.claude/CLAUDE.md` applies; this file adds the specifics. The owner's
reference documents sit in `_review/` on the dev host (gitignored):
`PROJECT-HANDOFF.md` (the workflow), `PYTHON-VENV-DEPLOYMENT.md` (the deployment
pattern), `HANDOFF.md` (WSL sandbox, shell-text hook, changelog doctrine, the
gate). Consult them before designing install, service, gate or review behaviour.

## Build & Test (the gate)

`tools/gate.sh` is the ONLY definition of "green". Every workflow step and CI
invoke it rather than listing commands. From the repo root in Git Bash, with the
venv's interpreter first on PATH, and the exit code read directly -- never
through a pipe (`$?` after `| tail` is tail's):

```
export PATH="$PWD/.venv/Scripts:$PATH"
bash tools/gate.sh > "<scratchpad>/gate.out" 2>&1; rc=$?
```

Stages, in order: `ascii` (gated Python stays ASCII), `shell guard tests`,
`markdown width` (80 display columns), `changelog refs`, `changelog structure`
(`tools/render_changelog.py --check`: one `### Nitty Gritty`, every entry
tagged, the heading shapes, the About section last), `ruff` (a `warning:`
line is red), `mypy` (strict), `pytest`, `linux stage` (delegated into the
`rlyeh-sandbox` WSL distro here; native on CI under
`LOGALERT_CHECK_MODE=required`, where a skip is a failure). The verdict line
names every stage that announced a skip. **Read the stage lines, not the
verdict** -- an announced skip reads as green at a glance.

Expected announced skips on the Windows host: `pytest` only (the POSIX-only
tests -- file modes, a FIFO, symlinks, the real-`logrotate` module, the
POSIX-only lock probe, a rename under an open handle, a signal delivered to a
handler, the faked uids of the root-owner pin -- all but the lock probe saying
"runs in the sandbox and on CI"; 71 as of #69); `changelog refs` skipped ("no
release commit yet") only until 0.1.0 was cut. In the root sandbox the
non-root tests skip instead, saying "expected in the root sandbox; CI runs it"
(12 as of #69), plus one Windows-only test ("the read-only attribute is a
Windows shape"); as `nobody` the one root-only test skips ("needs root to
plant another user's link"), and the Linux stage carries that scenario on CI.
Any other announced skip is worth reading. mypy on this host is the
pure-Python build (`pip install --no-binary mypy mypy`): the compiled wheel's
DLL is blocked by an Application Control policy. **mypy here checks nothing
under a `sys.platform != "win32"` branch** (it narrows the platform and skips
the block): a `/ship` that touches one runs mypy natively in the sandbox first
(`/srv/logalert-py312/bin/python -m mypy --strict logalert tools tests` over a
copy of the tree in `/tmp`; that venv carries `markdown` and `types-Markdown`,
the `docs` extra, since #17), or CI's Linux mypy is the first to see it -- PR
#21 was red on exactly that.

Run the sandbox preflight FIRST in any session that runs the gate (see WSL
sandbox). Never run two gates against the sandbox at once.

## Workflow

- Batch workflow: `/ship #N` (gate -> ONE commit per issue -> push
  `claude/pending`) -> `/close` (ONE PR with a `Closes #N` line per issue -> CI
  -> history-preserving merge) -> `/release x.y.z`.
- `/release`: the full logalert procedure lives in
  `.claude/skills/release/SKILL.md` (local, gitignored). Personal skills shadow
  project skills of the same name, so a bare `/release` resolves to the global
  skill, whose first step is to defer to that file. Invoking `/release` here is
  the owner's approval for exactly the three mutations it lists. The tag push
  starts `.github/workflows/release.yml`, which builds the sdist and the wheel
  on Linux, refuses a bad pair (`tools/check_artifacts.py`: a world-writable
  member, CRLF metadata, a missing file) and attaches them to the release
  (#56); nothing is built on this host.
- Never commit to `main` directly (the `/release` commit is the one exception).
  The commit subject ends in `(#N)`: the PR step gathers `Closes #N` from
  subjects only, and the `changelog refs` stage keys on the same convention;
  issues that share a commit are `(#N, #M)`. The body says why; the trailer is
  the session's attribution line. Write the message to a file and `git commit -F
  <file>`.
- **The changelog, per `/ship`, in this order:** write the entry under `##
  [Unreleased]` / `### Nitty Gritty` (append to the heading that is there --
  never a second one; the release rolls `[Unreleased]` verbatim), tag it, reflow
  ONLY the new block to 80 columns, THEN run the gate on the tree that contains
  the entry. Entry-then-gate: the other order shipped red commits in the source
  project. The `## About this changelog` section at the END of `CHANGELOG.md`
  is the only definition of the policy and the tags (the owner's call: a reader
  of a changelog knows what one is; the versions come first); never restate it
  in a skill or script. A new entry goes at the end of `[Unreleased]`, before
  the next version heading or that section.
- A docs-only PR gets the Pages build only (`pages.yml` renders the changelog
  and refuses a structural defect, runs the document pins and the markdown
  width, #45); `ci.yml` skips it (`paths-ignore`). That is could-not-check
  for everything else, not green: the local gate stands in, and `git diff
  --name-only main..HEAD` must be docs-only before the merge.
- Before committing non-trivial work, run an adversarial review pass (subagents)
  that hunts for real defects and adds missing tests, with the lenses the change
  needs. After ANY workflow that could have touched the tree, run `git status`:
  a review subagent once built a Linux venv into this repo root.

## Versioning

- `pyproject.toml` is the ONLY place the version lives; `logalert.__version__`
  derives from installed metadata. Never add a second copy.
- An editable install snapshots `pyproject.toml` metadata at install time -- run
  `pip install -e ".[dev,docs]"` again after a version bump (refreshes the dev
  `--version`), a `[project.scripts]` change (the new console script is
  otherwise missing from `.venv/Scripts/`), or a `dependencies` change.
- `requires-python`, the `Programming Language :: Python :: 3.x` classifiers and
  the CI matrix in `.github/workflows/ci.yml` stay in sync (currently 3.11 /
  3.12 / 3.13 / 3.14; `tests/test_install_doc.py` pins the three together and
  the documents' "3.11 or newer" to them). The floor is 3.11 (#53): mypy
  reads the 3.11 typeshed on every host (`python_version` in `pyproject.toml`
  -- the 3.12 dev venv would otherwise pass a 3.12-only API), the sandbox's
  `/srv/logalert-py311` venv runs the suite on it natively, and CI's 3.11 leg
  is the authority. 3.10 is excluded outright (`datetime.UTC`).
- MINOR when the operator-facing set gains a member or a member changes
  substantively; PATCH for corrections, tooling, docs, gate work. The
  `[contract]` tag does NOT drive a minor -- it is a release-notes signal.
- The release commit subject `Release x.y.z -- ...` is a protocol with
  `tools/check_changelog_refs.py` (`^Release \d+\.\d+\.\d+\b`). The release date
  is the LOCAL date (`date '+%Y-%m-%d'`), never UTC or a session header.

## Code register

- **Gated Python (`logalert/`, `tools/`, `tests/`) is pure ASCII** -- no emoji,
  no em dashes: markdown emphasis instead, `--` for a dash, `WARNING:` for the
  sign. The `ascii` stage enforces it, reporting code points and never printing
  the character (printing it IS the crash on a cp1252 console). Deliberate
  non-ASCII test data is built from the code point (`chr(0x2014)`) with a
  comment saying so.
- **A `\uXXXX` escape in Write/Edit content CAN arrive already decoded** --
  measured on this host as the character in some calls and the six literal
  characters in others, in one session. `\n`, `\t`, `\\`, `\r`, backticks and
  `$` arrived literally in every probe. Never rely on either outcome: spell a
  code point as `chr(0x...)` in Python, and type the real character when a
  doc needs it.
- Docs (`*.md`) keep the rich style and wrap at 80 display columns. In the
  changelog the prose separator is `--` and version headings use the em dash.
- `.claude/hooks/` carries the rich style and shell-shaped strings by design; it
  is excluded from ruff, mypy, bandit and pytest discovery and gated by
  behaviour (its test is a stage). `testpaths = ["tests"]` is a guard, not a
  convenience -- keep it if `norecursedirs` is ever overridden.
- `subprocess` calls state `encoding="utf-8", errors="replace"`; `text=True`
  alone decodes with the locale codec. `Path.write_text` needs `newline="\n"` on
  Windows. A POSIX file-mode, ownership or traversal test is `pytest.skip`ped
  when `os.name != "posix"` and run natively in the sandbox, where the mode bits
  are real; a mode test that is green on Windows is decoration.

## Shell rule (enforced by the PreToolUse hook)

**Never construct ESCAPE-BEARING TEXT through a shell.** Not file content, not
test data, not a sample string, not a search pattern -- if it contains `\n`,
`\t`, a `\` continuation, `$`, or a backtick, it must not cross a shell on its
way to being written **or compared**. Use `Write`/`Edit`, or build the
characters from code points inside a real script file (`chr(10)`, `chr(92)`).
The safe path is: write the content to a file and run the file, where nothing is
consumed. `git commit -F <file>` and `gh --body-file <file>` are the same move.
A backtick inside `--body "..."` is command-substituted INTO the text. And
**never truncate a file you cannot regenerate**: `open(path, "w")` and
`Path.write_text` truncate before they write; use `Write`, or
write-temp-then-rename.

The hook (`.claude/hooks/guard_shell_text.py`, wired in `.claude/settings.json`)
blocks the four shapes that actually caused damage and deliberately allows
reading, searching, git, the gate, and running a script from disk. `printf '%s'
'a\\b'` is the same transport defect and passes -- a stated boundary, pinned by
an ALLOW case. GitHub parses closing keywords anywhere, including inside
backticks: never write `Closes #N` in a body unless it should close that issue.

## Windows dev host

- `MSYS_NO_PATHCONV=1` on every native binary whose arguments could begin with
  `/` (`gh`, `wsl.exe`): Git Bash rewrites `/mnt/d/...` to `C:/Program
  Files/Git/mnt/d/...`, and `gh` once filed an issue titled `C:/Program
  Files/Git/ship ...` and exited 0.
- `timeout <n>` on every `wsl.exe` call; every hang in this history was silent.
- Use the Bash tool for `gh ... --jq` (PowerShell garbles jq); `jq` itself is
  absent from Git Bash -- parse `gh --json` with `python -c` or use `--jq`.
- `$?` after a pipe is the last command's. Redirect to a file and capture
  directly. Redirect targets must be Windows-resolvable: the scratchpad, never
  `/tmp`.
- Pass `C:/...`-style paths (not `/c/...`) to anything the venv Python opens.
- The console is cp1252: printing a non-ASCII character crashes the reader and
  looks like a truncated result. Set `PYTHONIOENCODING=utf-8` and write to a
  file when output may carry one.
- `gh pr checks --watch` may say "no checks" for minutes after PR creation; poll
  `gh run list --branch <b>` before concluding anything is broken.
- A `.pyc` whose (mtime, size) match the source is served even under
  `PYTHONDONTWRITEBYTECODE=1`; purge `__pycache__` before trusting a mutation
  test. Restore a mutated file from its `Write`-authored source, never with `git
  checkout` on an uncommitted file.

## WSL sandbox

Two distros. `Ubuntu-24.04` is the owner's and the DEFAULT, with Windows drives
mounted **rw**. `rlyeh-sandbox` is the sandbox: drives mounted **ro** (a VFS
flag, so it stops root too), systemd as PID 1, Python 3.10-3.12, and the agent's
to install packages in and leave services and state in, as root. It is shared
with the owner's other project; a fresh `logalert-sandbox` is the fallback if it
ever will not serve.

- **Always `-d rlyeh-sandbox -u root`.** A bare `wsl.exe` hits the rw owner
  distro and returns plausible wrong answers -- measured here: a review subagent
  built a venv INTO this checkout that way.
- Every call has the shape `MSYS_NO_PATHCONV=1 timeout <n> wsl.exe -d
  rlyeh-sandbox -u root -- bash /mnt/d/Projects/Logalert/<script>` -- a script
  file written with the Write tool, never an inline `bash -lc '...'` (three
  shell layers eat quoting twice; `$?` came back empty and a positive control
  returned 1). Strip `\r` from output (`tr -d '\r'`) before comparing.
- **Preflight first, every session:** `sandbox/sandbox_preflight.sh "$(date -u
  +%s)"` via the line above. Exit 0 = clean; 1 = do not trust any result until
  repaired; 2 = the harness itself is broken. Clock, mount and DNS warnings:
  `wsl --terminate rlyeh-sandbox`, then reconnect. A bus failure (`Failed to
  connect to bus`): only `wsl --shutdown` clears it, and that restarts EVERY
  distro including the owner's -- **ask, every time**; never run it from a
  script, never under a run in flight.
- The delegated run is PREDICTIVE, NOT AUTHORITATIVE: the sandbox is long-lived
  and mutable, and a result can depend on what an earlier session left behind;
  CI on a clean machine is the authority. Assert the property, not the
  environment; a standing skip must say it is expected.
- Nothing of record lives only in the sandbox: author in the checkout, run from
  `/mnt/d/...`, capture stdout, write from Windows. `pip install` from `/mnt/d`
  fails on the 9p mount -- copy to `/srv` first; files read over `/mnt` look
  `0777`, so copy into `mktemp -d` before asking about modes. Tests run natively
  there with `/srv/logalert-py312/bin/python -m pytest -q -p no:cacheprovider
  --basetemp=/tmp/logalert-pytest tests` (the checkout is read-only there);
  `/srv/logalert-py311` is the same venv on the floor. Install the wheel built
  from the tree into the venv first (`--no-index --find-links`): the tests
  that spawn a second process (`lock_holder.py`, `-m logalert`, the console
  script) import the INSTALLED package, not the tree.
- Never run two gates against the sandbox at once. End the session with `wsl
  --terminate rlyeh-sandbox`; it is the polite exit, not a repair.
- Subagents: give each its own uniquely named scratchpad subdirectory, hand it
  the exact `wsl.exe` line above, and diff the tree afterwards.

## Deployment model (design constraints)

Two venvs with two jobs -- never mix them:

- **Dev:** `.venv/` in the source tree, editable (`pip install -e ".[dev,docs]"`
  -- the gate's mypy and the render test need the `docs` extra too);
  always invoke its interpreter by explicit path, never bare `python`/`pytest`.
- **Deployment:** `/opt/logalert-venv`, installed from the release wheel,
  created with a **versioned** interpreter, holding nothing but the venv, with a
  `/usr/local/bin/logalert` symlink (that symlink is what lets `sudo logalert`
  resolve under `secure_path`; the console script's absolute shebang picks the
  interpreter). `INSTALL.md` documents it -- keep every section of that doc when
  editing it (versioned-venv rule + "it's an example", disposable venv dir,
  symlink rationale, upgrade-logalert vs upgrade-Python split,
  `--upgrade`/`--clear` traps, uninstall look-inside warning,
  `ModuleNotFoundError` fingerprint, not-on-PyPI).

Constraints the watcher's design must satisfy to fit that pattern:

- Every user-facing entry point is a console script in `[project.scripts]`;
  document the script, not `python -m ...` (`python -m` under the wrong `PATH`
  Python gives `No module named 'logalert'`; a console script carries the venv
  interpreter in its shebang).
- Runtime `dependencies` stay honest; build/dev-only tooling lives in extras.
- Configuration and state live **outside** the venv (`/etc/...`,
  `/var/lib/logalert/`-style paths), never under `/opt/logalert-venv` -- the
  rebuild and uninstall procedures wipe it.
- A long-running mode must be able to stay in the **foreground** (systemd
  `Type=simple`, stderr to journald; let the service manager daemonize) and
  reload configuration on `SIGHUP` separately from restart. If it ever
  self-daemonizes and logs to syslog, probe the platform socket paths
  (`/dev/log`, `/var/run/log`, `/var/run/syslog`) and warn loudly on UDP
  fallback -- UDP `connect()` succeeds with nobody listening, and the daemon
  logs nowhere.
- Service units use an absolute `ExecStart` (the symlink or the venv binary).
  Rehearse the installer and the unit in the sandbox (run the installer twice
  for idempotence, AND once from a clean room -- remove `/opt/logalert-venv`,
  the config and state paths and the unit, or use a fresh distro -- as a
  named first-install rehearsal: a sandbox installed into once only ever
  rehearses the upgrade path; probe as the service user with
  `runuser -u <svc> -- <venv>/bin/python -c 'import logalert'`;
  `systemd-analyze verify`'s exit code cannot gate a unit -- parse its text,
  scoped to the unit's basename).
- Claim in classifiers only what CI tests: `Operating System :: POSIX :: Linux`
  once the watcher lands; "BSD known to work but ungated" belongs in prose, not
  metadata.
- `INSTALL.md` carries, after the venv steps: the config and state paths with
  the service user, the mail transport, the schedule (the cron line and the
  `Type=oneshot` unit + timer with an absolute `ExecStart` -- there is no daemon
  mode, so no foreground flag), and a non-`journalctl` way to find the log.
  `tests/test_install_doc.py` pins the unit and the cron line to `USAGE.md`'s.

## Public-repo hygiene

No real hostnames, IPs, emails, credentials, customer/org names -- in source,
tests, comments, commit messages, PR bodies, or GitHub issues (issue edit
history is world-visible; redacting is not enough). Use RFC 5737 IPs
(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`), RFC 2606 names
(`*.example.net`), `noc@example.net`-style contacts. Genericize any support
paste (tracebacks, listings, launcher scripts) before it reaches an issue or
commit.
