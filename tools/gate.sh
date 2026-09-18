#!/usr/bin/env bash
# The project gate -- the single definition of "is this green?".
#
# THIS FILE IS THE ONLY COPY OF THE GATE. Every workflow step and CI invoke it rather than
# listing the commands themselves: in the source project the command list lived in four places
# for exactly one afternoon and was already wrong in all four. A file that is executed cannot
# drift from itself.
#
# Usage:  bash tools/gate.sh
# Exit 0 = GATE GREEN. 1 = GATE RED, and every failed stage is named. All stages run; the gate
# does not stop at the first red, because a gate that stops hides the second failure.
#
# Run it with the venv's interpreter first on PATH (Git Bash: export PATH="$PWD/.venv/Scripts:$PATH"
# on Windows, "$PWD/.venv/bin:$PATH" on Linux), with ruff, mypy and pytest installed into that
# venv (`python -m pip install -e ".[dev,docs]"` -- the `dev` extra in pyproject.toml is that
# list, `docs` what the changelog renderer and its stubs need for mypy and the render test; on
# this Windows host mypy must be the `--no-binary mypy` build, see pyproject.toml). Every
# third-party tool runs as `python -m <tool>` and the project's own scripts
# as `python <path>`, never a bare console script: a bare `ruff` resolves off PATH and can be a
# DIFFERENT interpreter's install than the `python` running the tests, so the gate could check
# one environment and test another without saying so. Every tool stage failing at once (ruff,
# mypy, pytest) is the tell that PATH is wrong, not the change; `No module named` on those three
# means the venv is missing them.
#
# Never pipe this script through `tail` or `head`: `$?` after a pipe reports the last command in
# the pipe, which succeeds on anything, so a RED gate prints rc=0. Redirect to a file and capture
# the exit code directly: `bash tools/gate.sh > out 2>&1; rc=$?`.
set -u
export PYTHONIOENCODING=utf-8   # a cp1252 console kills any tool that prints a non-ASCII finding
export PYTHONDONTWRITEBYTECODE=1

# shellcheck disable=SC1007 # `CDPATH= cd --` is a deliberate one-shot ENV PREFIX so a stray
# CDPATH in the caller's environment cannot silently send us to a different directory.
cd -- "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)" || exit 1

# The tools' versions, once, before the first stage: the extras let them float (`ruff>=0.5`,
# `mypy>=1.10`, `pytest>=8`, `markdown>=3.5`), so a tool release can redden an unchanged tree once
# or twice a year, and this line is what makes that diagnosable from the log (issue #52). A
# distributions() scan rather than four version() calls: a missing extra reads MISSING here
# instead of failing three stages further down without a name.
python -c 'import sys, importlib.metadata as m; have = {str(d.metadata.get("Name", "")).lower(): str(d.version) for d in m.distributions()}; print("tools: python %d.%d.%d, " % sys.version_info[:3] + ", ".join(n + " " + have.get(n, "MISSING") for n in ("ruff", "mypy", "pytest", "markdown")))'

FAILED=""
SKIPPED=""
stage() {
  local name="$1"; shift
  printf '\n-- %s --\n' "$name"
  local log rc
  log="$(mktemp)"
  "$@" 2>&1 | tee "$log"
  # The STAGE's exit code, not tee's: `$?` after a pipe is the last command's. Captured
  # DIRECTLY, never after an `if`: an `if` whose condition fails with no `else` returns 0
  # (POSIX), so `if "$@"; then ...; fi; rc=$?` reads 0 for every failure and reports
  # "FAILED: ruff (exit 0)" -- red, but sending you to debug the harness.
  rc=${PIPESTATUS[0]}
  # An announced skip still reads as green at a glance: the source project read GATE GREEN five
  # times in one session while a stage had never run. Record the STAGES that announced one -- a
  # `SKIP`/`skipped` marker at the start of a line (this gate's helpers, the changelog-refs tool,
  # pytest's `-rs` lines) or pytest's `N skipped` summary -- and name them beside the verdict.
  # Per stage rather than per line: the first version counted lines, case-sensitively, and
  # reported one linux-stage skip as two (marker plus `SKIPPED` verdict) while missing the refs
  # tool's lowercase `skipped:` entirely -- wrong in both directions.
  if grep -qiE '^[[:space:]]*skip(ped)?\b|[0-9]+ skipped\b' "$log"; then
    SKIPPED="$SKIPPED $name"
  fi
  rm -f "$log"
  if [ "$rc" -ne 0 ]; then
    echo "FAILED: $name (exit $rc)"
    FAILED="$FAILED $name"
  fi
}
# A stage the gate itself decides not to run goes through here, so it is counted like any other.
skip_stage() {
  printf '\n-- %s --\n  SKIP  %s\n' "$1" "$2"
  SKIPPED="$SKIPPED $1"
}

# ruff prints `warning: Invalid # noqa directive ...` for a malformed suppression and still
# EXITS 0 (measured) -- and prints nothing at all for a blanket `# noqa <prose>` unless RUF100
# is selected, which pyproject.toml does (with RUF102 for a code ruff does not know: a planted
# `# noqa: XYZ999` passed here before, issue #48). A warning is not a pass. `--no-cache` is
# deliberate: a cached run does not re-emit the warning, and a guard that only fires on a cold
# cache is not a guard.
ruff_strict() {
  local out rc
  out="$(python -m ruff check --no-cache . 2>&1)"; rc=$?
  printf '%s\n' "$out"
  [ "$rc" -ne 0 ] && return "$rc"
  if printf '%s\n' "$out" | grep -q '^warning:'; then
    echo "ruff emitted the warning(s) above and still exited 0. A warning is NOT a pass."
    return 1
  fi
  return 0
}

# File lists come from git with `--others --exclude-standard`, never bare `git ls-files`: the
# local gate runs BEFORE `git add`, so a tracked-only list never sees the file you just wrote,
# and a stage that covers nothing prints the same skip line as an empty repo. Under a batch
# workflow that window is days. `--exclude-standard` keeps `.venv/` and caches out.
LS_FILES="git ls-files --cached --others --exclude-standard"
MARKDOWN="$($LS_FILES '*.md' || true)"
# Gated Python: the package, the tools and the tests. NOT `.claude/hooks/`, which carries the
# rich style on purpose and is gated by behaviour below.
GATED_PY="$($LS_FILES 'logalert/*.py' 'tools/*.py' 'tests/*.py' || true)"

# -- ASCII first: it is what keeps every later stage able to PRINT its findings on a cp1252
#    console (bandit dies with UnicodeEncodeError before reporting; so does any script that
#    prints the offending character). Bytes in, code points out; an empty list is exit 2.
if [ -n "$GATED_PY" ]; then
  # shellcheck disable=SC2086 # word-splitting is the point: one path per argument
  stage "ascii" python tools/check_ascii.py $GATED_PY
else
  skip_stage "ascii" "no gated Python yet"
fi

# -- agent tooling, gated by BEHAVIOUR (it is excluded from every style tool by pyproject.toml)
stage "shell guard tests" python .claude/hooks/test_guard_shell_text.py

# -- prose
if [ -n "$MARKDOWN" ]; then
  # shellcheck disable=SC2086 # word-splitting is the point: one path per argument
  stage "markdown width" python tools/reflow_md.py --check $MARKDOWN
else
  skip_stage "markdown width" "no markdown yet"
fi
stage "changelog refs" python tools/check_changelog_refs.py
# The structure the file's About section defines (one category, every entry tagged, the
# heading shapes): the same parse the Pages build runs, here before the entry is committed.
stage "changelog structure" python tools/render_changelog.py --check

# -- code
stage "ruff"   ruff_strict
stage "mypy"   python -m mypy --strict logalert tools tests
# A `.pyc` already on disk is still READ when its (mtime, size) match the source, even under
# PYTHONDONTWRITEBYTECODE=1 -- that variable stops WRITING bytecode, not reading it (measured).
# A directory copy of this kit can carry one. Purge before the tests so they import the source
# that is there; the export above then keeps the caches absent for the rest of the run.
find logalert tools tests .claude/hooks -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
# `-rs` lists every skipped test with its reason, so a skip inside the suite is announced.
stage "pytest" python -m pytest -q -rs -p no:cacheprovider
# Enable once the project has dependencies to audit and code for bandit to scan:
# stage "bandit"    python -m bandit -q -c pyproject.toml -r .     # `-c` or it ignores its excludes
# stage "pip-audit" python -m pip_audit -r requirements.txt

# -- Linux-only checks, delegated into the WSL sandbox on Windows. Auto mode skips and SAYS so;
#    set LOGALERT_CHECK_MODE=required in the CI job's `env:` so a skip THERE is a failure. Nothing
#    sets it for you: until it is set, CI runs this stage in auto mode and passes by not looking.
stage "linux stage" bash tools/linux_stage.sh

echo ""
if [ -n "$FAILED" ]; then
  echo "GATE RED -$FAILED"
  exit 1
fi
if [ -n "$SKIPPED" ]; then
  echo "GATE GREEN -- but these stage(s) announced a skip:$SKIPPED. A skip is not coverage; read them."
else
  echo "GATE GREEN"
fi
