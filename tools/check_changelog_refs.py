#!/usr/bin/env python3
"""Does every issue merged since the last release have a CHANGELOG entry? (#177)

## Why this exists

`CHANGELOG.md`'s header says "everything gets an entry", and `/ship` step 3 writes one per issue.
Both are conventions, and on 2026-08-21 -- cutting 0.6.6 -- **seven merged issues had no entry**:
#147, #149, #150, #161, #164, #169 and #174.

Four of the seven were WRITTEN AND THEN REVERTED by `/ship`'s own multi-commit split: the procedure
snapshots the tree before the entry is written, commits issue 1, then restores the rest of the tree
from that snapshot -- rolling `CHANGELOG.md` back and deleting the entry committed one step earlier.
Only the last commit's entry survived. `4aaa1e8` added #174's; `1bd458a` removed it.

## Why a missing entry is not a documentation problem

`/release` step 8 scans the entries for a `[contract]` tag and emits a worklist for the two client
repos. **#164 changed `wire/WIRE.sha256` and `wire/v1/wire_v1_contract.md`** -- a wire change all
three repos co-own -- and with its entry absent the scan reported "no contract changes, nothing for
the apps to mirror". This daemon and its clients deploy on completely independent schedules, so
nothing else would ever have said otherwise.

That is the failure this gate stage closes: not tidiness, but a silent false negative in the one
step that warns the apps.

## What it checks

Every `(#N)` in a NON-MERGE commit subject since the last `Release x.y.z` commit must appear
somewhere in `CHANGELOG.md`.

WARNING: SUBJECTS ONLY, matching `/close` and `list.py`. A body reference names an issue the commit
commit did not finish; requiring an entry would be a false alarm nobody could clear.

WARNING: MERGE COMMITS ARE EXCLUDED, and that is not tidiness either. `Merge pull request #176 from
...` carries a PULL REQUEST number, which never has a changelog entry -- treating it as an issue ref
would make this stage permanently and unfixably red.

Exit 0 = every ref has an entry. 1 = at least one does not. 2 = could not check.

WARNING: exit 2 is deliberately distinct from 0, per this repo's standing rule. A check that reports
"fine" when it could not establish the baseline manufactures exactly the confidence it exists to
withhold.
"""

import re
import shutil
import subprocess  # noqa: S404  # nosec - git is invoked with a fixed argv, never a shell string
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

EXIT_OK = 0
EXIT_MISSING = 1
EXIT_COULD_NOT_CHECK = 2

# `(#123)` or `(#123, #124)` in a commit subject -- the group, then every number inside it.
#
# WARNING: the single-ref form `\(#(\d+)\)` matched NOTHING in `(#42, #43)`, so both issues of a
# shared-seam commit were silently exempted from this check -- the exact false-negative direction
# the tool exists to close. Keep this shape identical to whatever the PR step uses to gather its
# `Closes #N` lines, or the two consumers of the convention drift and one of them lies.
SUBJECT_GROUP = re.compile(r"\((#\d+(?:,\s*#\d+)*)\)")
SUBJECT_NUMBER = re.compile(r"#(\d+)")
# The commit `/release` writes. Anchored, so an issue titled "Release the ..." cannot be mistaken
# for one.
RELEASE_SUBJECT = re.compile(r"^Release \d+\.\d+\.\d+\b")


def _git(*args: str) -> tuple[bool, str]:
    """Run git in the repo root, with the encoding stated: without it a Windows subprocess decodes
    with cp1252, so the subject a failure message quotes arrives as mojibake (an em dash becomes
    three characters), and a byte cp1252 does not define would raise under strict decoding. The
    `(#N)` match itself is ASCII and unaffected either way.

    The executable is RESOLVED with `shutil.which` rather than passed as a bare name, which is how
    `check_issue_titles.py` closes S607/B607 by construction rather than by suppression.
    """
    git = shutil.which("git")
    if git is None:
        return False, "the `git` executable is not on PATH"
    try:
        proc = subprocess.run(  # noqa: S603  # nosec - fixed argv, no shell, no caller input
            [git, "-C", str(REPO_ROOT), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return False, f"could not run git: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()
    return True, proc.stdout


# How far back the baseline search looks. Named because TWO places reason about it: the search, and
# the "the window was FULL" case below -- where a baseline that exists just out of reach must not be
# reported as a baseline that does not exist.
_LOG_LIMIT = 500

# What `baseline()` found. Three-valued on purpose, see the docstring.
FOUND = "found"
NONE_YET = "none-yet"
UNREACHABLE = "unreachable"


def baseline() -> tuple[str, str | None, str]:
    """The most recent `Release x.y.z` commit, as `(state, sha, detail)`.

    WARNING: THE STATE IS THREE-VALUED, AND THAT IS THE POINT (#302). This used to return
    `(None, reason)` for four different situations, and `main` treated all four as EXIT_OK: a repo
    before its first release, a SHALLOW CLONE, a full window, and a git failure were one answer.
    Only the first of those is "nothing to compare against". The other three are could-not-check --
    which the module docstring above has always said is exit 2, distinct from 0, for exactly this
    reason.

    WARNING: THE SHALLOW CASE IS NOT HYPOTHETICAL. `.github/workflows/ci.yml` carried no
    `fetch-depth`, so every CI run searched a one-commit clone, found no release commit, and
    reported a skip. The stage measured nothing from the day it was added until #302, while working
    correctly on every developer clone -- the combination that keeps a vacuous check alive, because
    local runs keep confirming it.
    """
    ok, shallow = _git("rev-parse", "--is-shallow-repository")
    if not ok:
        return UNREACHABLE, None, shallow
    if shallow.strip() == "true":
        return UNREACHABLE, None, (
            "the clone is SHALLOW, so the history holding the baseline was never fetched "
            "(a CI checkout needs `fetch-depth: 0`)"
        )
    ok, out = _git("log", "--no-merges", "--format=%H%x1f%s", "-n", str(_LOG_LIMIT))
    if not ok:
        return UNREACHABLE, None, out
    lines = [line for line in out.splitlines() if "\x1f" in line]
    for line in lines:
        sha, subject = line.split("\x1f", 1)
        if RELEASE_SUBJECT.match(subject):
            return FOUND, sha, subject
    # A full window means the search was TRUNCATED, not that the history was exhausted. Reporting
    # "no release commit" here would be the same conflation the shallow case makes.
    if len(lines) >= _LOG_LIMIT:
        return UNREACHABLE, None, (
            f"no `Release x.y.z` commit in the last {_LOG_LIMIT}, and the window is FULL -- the "
            f"baseline is out of reach rather than absent"
        )
    return NONE_YET, None, "no release commit yet -- nothing to compare against"


def refs_since(sha: str) -> tuple[bool, dict[int, str]]:
    """`{issue: subject}` for every non-merge commit after `sha`."""
    ok, out = _git("log", "--no-merges", "--format=%s", f"{sha}..HEAD")
    if not ok:
        return False, {}
    found: dict[int, str] = {}
    for subject in out.splitlines():
        for group in SUBJECT_GROUP.findall(subject):
            for ref in SUBJECT_NUMBER.findall(group):
                found.setdefault(int(ref), subject)
    return True, found


def main() -> int:
    if not CHANGELOG.is_file():
        print(f"could not check: {CHANGELOG} is missing", file=sys.stderr)
        return EXIT_COULD_NOT_CHECK

    state, sha, detail = baseline()
    if state == UNREACHABLE:
        # The baseline could not be established. Reporting a skip here -- which is what this did
        # until #302 -- manufactures the confidence the module docstring says to withhold.
        print(f"COULD NOT CHECK -- {detail}", file=sys.stderr)
        return EXIT_COULD_NOT_CHECK
    if sha is None:
        # Not a failure: a repo before its first release has nothing to compare against, and saying
        # so is more useful than inventing a baseline at the root commit. `sha is None` rather than
        # `state == NONE_YET` so the narrowing is visible to mypy as well as to the reader.
        print(f"skipped: {detail}")
        return EXIT_OK

    ok, found = refs_since(sha)
    if not ok:
        print(f"could not check: {found}", file=sys.stderr)
        return EXIT_COULD_NOT_CHECK

    text = CHANGELOG.read_text(encoding="utf-8")
    # Substring, not a parse. An entry may cite its issue anywhere in its prose, and several do --
    # requiring the ref in the first line would fail on entries that are correct.
    missing = {n: s for n, s in found.items() if f"#{n}" not in text}

    if missing:
        print(
            f"{len(missing)} issue(s) merged since {detail.strip()!r} have NO CHANGELOG entry:",
            file=sys.stderr,
        )
        for n in sorted(missing):
            print(f"  #{n}  {missing[n][:88]}", file=sys.stderr)
        print(
            "\nEvery merged issue gets an entry (CHANGELOG.md's header says so). A missing one "
            "also hides its tag from the release step's worklist scan, which is how a change "
            "other repos must mirror reaches them without anyone being told.",
            file=sys.stderr,
        )
        return EXIT_MISSING

    print(f"{len(found)} issue(s) merged since the last release; all have CHANGELOG entries.")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
