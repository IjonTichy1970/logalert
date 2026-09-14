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
  name**, so a bare `/release` resolves to the generic global skill (bump + roll + push only).
  When `/release` is invoked in this project, read and follow that file instead; invoking
  `/release` here is the owner's approval for exactly the three mutations it lists.
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
- An editable install snapshots the version at install time — `pip install -e ".[dev]"` again
  after a bump to refresh the dev `--version`.
- `requires-python`, the `Programming Language :: Python :: 3.x` classifiers, and the CI matrix
  in `.github/workflows/ci.yml` must stay in sync (currently 3.12 / 3.13 / 3.14).
- MINOR per feature (bundling several per minor is fine pre-1.0), PATCH for bug/doc fixes.

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
