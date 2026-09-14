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
  arrived as the literal character. Re-measured, the same escape arrived
  decoded in some calls and literal in others, while the other escapes probed
  (`\n`, `\t`, `\\`, `\r`, backticks, `$`) arrived literally every time -- so
  gated code spells code points as `chr(0x...)` and never writes the escape.
  ⚠️ mypy on the Windows dev host must
  be the pure-Python build (`pip install --no-binary mypy mypy`): the compiled
  wheel is blocked by an Application Control policy.

[Unreleased]: https://github.com/IjonTichy1970/logalert/commits/main
