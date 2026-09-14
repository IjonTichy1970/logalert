# logalert — project instructions

Generalized log file watcher. Python, **public** repo `IjonTichy1970/logalert`, distributed via
GitHub Releases (not PyPI). Global `~/.claude/CLAUDE.md` applies; this file adds the specifics.

## Build & Test (the gate)

From the repo root, using the project venv (Windows dev host — adjust the path on POSIX):

```
./.venv/Scripts/python.exe -m ruff check .
./.venv/Scripts/python.exe -m pytest -q
```

Both must be green before any commit. Expected skips: **none yet** — record them here as they
appear so "N passed, M skipped" reads as green instead of triggering an investigation.

## Workflow

- Batch workflow: `/ship #N` (gate → ONE commit per issue → push `claude/pending`) → `/close`
  (ONE PR with a `Closes #N` line per issue → CI → history-preserving merge) → `/release x.y.z`.
- `/release`: the full logalert procedure lives in `.claude/skills/release/SKILL.md` (local,
  gitignored) — bump `pyproject.toml`, roll the changelog, push `main`, tag `vX.Y.Z`, build
  sdist + wheel, publish a GitHub Release. **Personal skills shadow project skills of the same
  name**, so a bare `/release` resolves to the global skill, whose first step is to defer to
  that project file when it exists. Follow the project file; invoking `/release` here is the
  owner's approval for exactly the three mutations it lists.
- Never commit to `main` directly (the `/release` commit is the one exception).
- Commit subject ends in `(#N)`; body explains why; trailer is the session's attribution line.
  Write the message to a file and `git commit -F <file>` (PowerShell here-strings mangle them).
- Keep `CHANGELOG.md` `[Unreleased]` current with every user-facing `/ship`; omit CI/test/internal
  work.
- Before committing non-trivial work, run an adversarial review pass (subagent) that hunts for
  real defects and adds missing tests. Give it the lenses the change actually needs.

## Versioning

- `pyproject.toml` is the ONLY place the version lives; `logalert.__version__` derives from
  installed metadata. Never add a second copy.
- An editable install snapshots `pyproject.toml` metadata at install time — run
  `pip install -e ".[dev]"` again after a version bump (refreshes the dev `--version`), a
  `[project.scripts]` change (the new console script is otherwise missing from `.venv/Scripts/`),
  or a `dependencies` change.
- `requires-python`, the `Programming Language :: Python :: 3.x` classifiers, and the CI matrix
  in `.github/workflows/ci.yml` must stay in sync (currently 3.12 / 3.13 / 3.14).
- MINOR per feature (bundling several per minor is fine pre-1.0), PATCH for bug/doc fixes.

## Deployment model (design constraints)

Two venvs with two jobs — never mix them:
- **Dev:** `.venv/` in the source tree, editable (`pip install -e ".[dev]"`); always invoke its
  interpreter by explicit path, never bare `python`/`pytest`.
- **Deployment:** `/opt/logalert-venv`, installed from the release wheel, created with a
  **versioned** interpreter, holding nothing but the venv, with a `/usr/local/bin/logalert`
  symlink (that symlink is what lets `sudo logalert` resolve under `secure_path`; the console
  script's absolute shebang picks the interpreter). `INSTALL.md` documents it — keep every
  section of that doc when editing it (versioned-venv rule + "it's an example", disposable venv
  dir, symlink rationale, upgrade-logalert vs upgrade-Python split, `--upgrade`/`--clear`
  traps, uninstall look-inside warning, `ModuleNotFoundError` fingerprint, not-on-PyPI).

Constraints the watcher's design must satisfy to fit that pattern:
- Every user-facing entry point is a console script in `[project.scripts]`; document the
  script, not `python -m …` (`python -m` under the wrong `PATH` Python gives
  `No module named 'logalert'`; a console script carries the venv interpreter in its shebang).
- Runtime `dependencies` stay honest; build/dev-only tooling lives in extras.
- Configuration and state live **outside** the venv (`/etc/…`, `/var/lib/logalert/`-style
  paths), never under `/opt/logalert-venv` — the rebuild and uninstall procedures wipe it.
- A long-running mode must be able to stay in the **foreground** (systemd `Type=simple`, stderr
  → journald; let the service manager daemonize) and reload configuration on `SIGHUP`
  separately from restart. If it ever self-daemonizes and logs to syslog, probe the platform
  socket paths (`/dev/log`, `/var/run/log`, `/var/run/syslog`) and warn loudly on UDP fallback —
  UDP `connect()` succeeds with nobody listening, and the daemon logs nowhere.
- Service units use an absolute `ExecStart` (the symlink or the venv binary).
- Claim in classifiers only what CI tests: `Operating System :: POSIX :: Linux` once the watcher
  lands; "BSD known to work but ungated" belongs in prose, not metadata.
- Once the CLI exists, `INSTALL.md` still needs: the service unit (absolute `ExecStart`,
  foreground flag), the config/state paths, and a non-`journalctl` way to find logs.

Owner's reference docs (`PROJECT-HANDOFF.md`, `PYTHON-VENV-DEPLOYMENT.md`) sit in `_review/` on
the dev host — gitignored. Consult them before designing install or service behavior.

## Public-repo hygiene

No real hostnames, IPs, emails, credentials, customer/org names — in source, tests, comments,
commit messages, PR bodies, or GitHub issues (issue edit history is world-visible; redacting is
not enough). Use RFC 5737 IPs (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`), RFC 2606
names (`*.example.net`), `noc@example.net`-style contacts. Genericize any support paste
(tracebacks, listings, launcher scripts) before it reaches an issue or commit.

## Windows dev-host notes

- Author files with the Write tool (no BOM, LF); `.gitattributes` normalizes to LF in the repo.
- Use the Bash tool for `gh ... --jq` (PowerShell garbles the jq expression).
- Pass `C:/...`-style paths (not `/c/...`) to anything the venv Python opens.
- `gh pr checks --watch` may say "no checks" for minutes after PR creation — poll
  `gh run list --branch <b>` before concluding anything is broken.
