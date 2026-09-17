"""Guards over `tools/check_changelog_refs.py` (#177).

WARNING: the load-bearing case is `test_a_MERGE_commit_ref_is_not_treated_as_an_issue`. Every other
test here passes on a version that scans all commits including merges -- and that version is
permanently, unfixably red on this repo, because `Merge pull request #176 from ...` carries a PULL
REQUEST number that will never have a changelog entry.

NOTE: comments here stay ASCII. See CLAUDE.md on bandit and cp1252.
"""

import shutil
import subprocess
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_changelog_refs.py"
# RESOLVED, not a bare name -- the same reason the tool does it (S607/B607).
GIT = shutil.which("git") or "git"

EXIT_OK = 0
EXIT_MISSING = 1
EXIT_COULD_NOT_CHECK = 2


def _repo(
    root: Path,
    changelog: str,
    subjects: list[str],
    release: str | None = "Release 0.1.0 -- x",
) -> Path:
    """A throwaway git repo with a release commit, then one commit per subject.

    Real commits rather than a stubbed `git log`: the tool's whole job is to read history, and a
    stub would be testing the stub. `--no-gpg-sign` and a local identity so this cannot depend on
    the machine's git configuration.
    """
    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603  # nosec - fixed argv, no shell
            [GIT, "-C", str(root), *args],
            check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    root.mkdir(parents=True, exist_ok=True)
    (root / "tools").mkdir(exist_ok=True)
    run("init", "-q", "-b", "main")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "T")
    run("config", "commit.gpgsign", "false")
    (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8", newline="\n")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")
    if release is not None:
        (root / "seed").write_text("1", encoding="utf-8", newline="\n")
        run("add", "-A")
        run("commit", "-q", "-m", release)
    for i, subject in enumerate(subjects):
        (root / f"f{i}").write_text("x", encoding="utf-8", newline="\n")
        run("add", "-A")
        run("commit", "-q", "-m", subject)
    # The tool locates the repo from its OWN path, so it has to live inside the fixture.
    (root / "tools" / "check_changelog_refs.py").write_bytes(TOOL.read_bytes())
    return root


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603  # nosec - our interpreter, our script
        [sys.executable, str(root / "tools" / "check_changelog_refs.py")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )


def test_a_missing_entry_is_caught(tmp_path: Path) -> None:
    """The case this stage exists for: an issue merged since the release, with no entry.

    MUTATION: have the tool return EXIT_OK unconditionally; this reddens.
    """
    root = _repo(tmp_path / "r", "# Changelog\n\n- nothing here\n", ["Do a thing (#42)"])
    result = _run(root)
    assert result.returncode == EXIT_MISSING
    assert "#42" in result.stderr


def test_BOTH_refs_of_a_shared_seam_commit_are_demanded(tmp_path: Path) -> None:
    """`(#42, #43)` is the subject form for two issues sharing one commit.

    The single-ref pattern `\\(#(\\d+)\\)` matched NOTHING in that form, so both issues were
    silently exempted -- measured on a fixture exactly like this one. The subject below carries a
    third, single-ref commit as the control: it proves the tool is looking at all.

    MUTATION: revert SUBJECT_GROUP to the single-ref pattern; this reddens (only #44 is named).
    """
    root = _repo(
        tmp_path / "r", "# Changelog\n\n- nothing here\n",
        ["Two issues in one commit (#42, #43)", "Single (#44)"],
    )
    result = _run(root)
    assert result.returncode == EXIT_MISSING
    assert "#42" in result.stderr and "#43" in result.stderr, result.stderr
    assert "#44" in result.stderr, "the control ref was not demanded, so nothing above is evidence"


def test_a_PRESENT_entry_passes(tmp_path: Path) -> None:
    """ANTI-VACUITY, and it comes first: without it, "always fail" passes the test above."""
    root = _repo(tmp_path / "r", "# Changelog\n\n- `[server]` **Did a thing** (#42).\n",
                 ["Do a thing (#42)"])
    result = _run(root)
    assert result.returncode == EXIT_OK, result.stderr


def test_a_prefix_match_in_a_cited_number_does_not_satisfy_a_smaller_ref(
    tmp_path: Path,
) -> None:
    """#47. `(#10)` in the changelog must NOT satisfy a commit referencing `(#1)`.

    A bare `f"#{n}" not in text` substring check accepted `#1` because `"#1"` is a prefix of
    `"#10"`, manufacturing a silent false negative.

    MUTATION: revert to `f"#{n}" not in text`; this reddens (exits EXIT_OK instead of EXIT_MISSING).
    """
    root = _repo(
        tmp_path / "r",
        "# Changelog\n\n- `[internal]` **Issue ten** (#10).\n",
        ["Fix issue one (#1)"],
    )
    result = _run(root)
    assert result.returncode == EXIT_MISSING
    assert "#1" in result.stderr

    # Positive control: citing #1 satisfies #1
    root_ok = _repo(
        tmp_path / "r_ok",
        "# Changelog\n\n- `[internal]` **Issue one** (#1).\n- `[internal]` **Issue ten** (#10).\n",
        ["Fix issue one (#1)"],
    )
    result_ok = _run(root_ok)
    assert result_ok.returncode == EXIT_OK, result_ok.stderr


def test_a_MERGE_commit_ref_is_not_treated_as_an_issue(tmp_path: Path) -> None:
    r"""THE test. `Merge pull request #176 from ...` is a PR number, not an issue.

    A version that demanded an entry per PR would be red forever and get switched off.

    WARNING: `--no-merges` IS NOT WHAT SAVES THIS, and a mutation run proved it -- dropping the flag
    leaves this test GREEN. What actually excludes a PR merge is `SUBJECT_GROUP` requiring
    PARENTHESES: GitHub writes `Merge pull request #176 from x/y`, and the parenthesised pattern
    does not match a bare `#176`. `--no-merges` is belt to that brace, for a squash subject with a
    parenthesised ref. This docstring credited the wrong mechanism until the mutation corrected it.

    So the real discriminating case is a BARE ref, which the sibling test below covers. This one
    keeps the realistic merge subject, because that is the shape the repo actually produces.
    """
    root = _repo(tmp_path / "r", "# Changelog\n\n- nothing\n", [], release="Release 0.1.0 -- x")

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603  # nosec - fixed argv, no shell
            [GIT, "-C", str(root), *args], check=True, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")

    run("checkout", "-q", "-b", "side")
    (root / "side").write_text("1", encoding="utf-8", newline="\n")
    run("add", "-A")
    run("commit", "-q", "-m", "side work")
    run("checkout", "-q", "main")
    run("merge", "--no-ff", "-q", "-m", "Merge pull request #176 from x/y", "side")

    result = _run(root)
    assert result.returncode == EXIT_OK, (
        "a pull-request number in a merge subject was demanded as an issue entry -- this stage "
        "would be permanently red and would be turned off\n" + result.stderr
    )


def test_a_BARE_ref_without_parentheses_is_not_demanded(tmp_path: Path) -> None:
    r"""THE real protection against PR-merge subjects, isolated.

    `SUBJECT_GROUP` requires the parentheses. A bare `#176` -- which is exactly how GitHub writes
    `Merge pull request #176 from x/y` -- must not be read as an issue that owes an entry.

    MUTATION: relax `SUBJECT_GROUP` to `(#\d+(?:,\s*#\d+)*)` -- the same group with no
    surrounding parentheses; this reddens.
    """
    root = _repo(
        tmp_path / "r", "# Changelog\n\n- nothing\n", ["Mentions #176 with no parens"]
    )
    assert _run(root).returncode == EXIT_OK


def test_a_BODY_reference_is_not_demanded(tmp_path: Path) -> None:
    """Subjects only, matching `/close` and `list.py`.

    A body reference names an issue the commit did NOT finish. Demanding an entry for it is a false
    alarm nobody can clear -- and `/close` already refuses to write a `Closes` line for one.

    MUTATION: scan `%B` instead of `%s`; this reddens.
    """
    root = _repo(tmp_path / "r", "# Changelog\n\n- nothing\n", [])

    def run(*args: str) -> None:
        subprocess.run(  # noqa: S603  # nosec - fixed argv, no shell
            [GIT, "-C", str(root), *args], check=True, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")

    (root / "f").write_text("x", encoding="utf-8", newline="\n")
    run("add", "-A")
    run("commit", "-q", "-m", "A subject with no ref\n\nbut the body mentions (#99) in passing")

    assert _run(root).returncode == EXIT_OK


def test_a_SHALLOW_clone_is_COULD_NOT_CHECK_not_a_skip(tmp_path: Path) -> None:
    """#302. THE DEFECT THIS STAGE SHIPPED WITH, and it was live in CI from the day it was added.

    `.github/workflows/ci.yml` had no `fetch-depth`, so `actions/checkout` made a ONE-COMMIT clone.
    `baseline()` then found no `Release x.y.z` subject -- correctly, there was only one commit --
    and reported `skipped: no Release x.y.z commit in the last 500`, exit 0. Confirmed against the
    real CI log for run 33931929159 before this was written.

    WARNING: THE MUTATION IS THE CLONE, NOT A CODE EDIT. A test on a full clone passes under the old
    code and the new one alike, because the old code was correct whenever the history was actually
    present. Only a truncated history separates them, which is why this test does the expensive
    thing and makes one.

    WARNING: assert on the EXIT CODE. Both the old and the new code can print the word "skipped"
    somewhere, so asserting on the message does not discriminate -- the same lesson #301 recorded
    about a check that only read stdout.
    """
    source = _repo(tmp_path / "origin", "# Changelog\n\n- #7\n", ["work (#7)"])
    shallow = tmp_path / "shallow"
    subprocess.run(  # noqa: S603  # nosec - fixed argv, no shell
        [GIT, "clone", "-q", "--depth", "1", source.as_uri(), str(shallow)],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    (shallow / "tools").mkdir(exist_ok=True)
    (shallow / "tools" / "check_changelog_refs.py").write_bytes(TOOL.read_bytes())

    result = _run(shallow)

    assert result.returncode == EXIT_COULD_NOT_CHECK, (
        "a truncated history was reported as a clean skip:\n" + result.stdout + result.stderr
    )
    assert "SHALLOW" in result.stderr, result.stderr


def test_a_FULL_clone_of_the_same_repo_still_checks(tmp_path: Path) -> None:
    """Detection leg for the test above, and it is load-bearing rather than decorative.

    Without it, "exits 2" is equally satisfied by a tool that has simply stopped working -- and the
    fix for #302 is one `git rev-parse` away from refusing every run.
    """
    source = _repo(tmp_path / "origin", "# Changelog\n\n- #7\n", ["work (#7)"])
    full = tmp_path / "full"
    subprocess.run(  # noqa: S603  # nosec - fixed argv, no shell
        [GIT, "clone", "-q", source.as_uri(), str(full)],
        check=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    (full / "tools").mkdir(exist_ok=True)
    (full / "tools" / "check_changelog_refs.py").write_bytes(TOOL.read_bytes())

    result = _run(full)

    assert result.returncode == EXIT_OK, result.stdout + result.stderr
    assert "SHALLOW" not in result.stderr


def test_a_FULL_search_window_is_COULD_NOT_CHECK_not_a_skip(tmp_path: Path) -> None:
    """The fourth cause, and the subtlest: the release commit exists but sits beyond `_LOG_LIMIT`.

    A full window means the search was TRUNCATED, not that the history was exhausted -- the same
    conflation the shallow case makes, one layer along. Before #302 this reported a skip.

    WARNING: the limit is shrunk IN THE FIXTURE'S COPY of the tool, not in the tool. The fixture
    already copies the file, so this is a fixture technique rather than a production seam added for
    testability -- 500 real commits would be a disproportionate way to reach one branch.
    """
    source = _repo(
        tmp_path / "origin",
        "# Changelog\n\n- #7\n",
        [f"work {i} (#7)" for i in range(4)],
    )
    patched = TOOL.read_text(encoding="utf-8").replace("_LOG_LIMIT = 500", "_LOG_LIMIT = 3", 1)
    assert "_LOG_LIMIT = 3" in patched, "the limit constant was renamed; this fixture is stale"
    (source / "tools" / "check_changelog_refs.py").write_text(
        patched, encoding="utf-8", newline="\n"
    )

    result = _run(source)

    assert result.returncode == EXIT_COULD_NOT_CHECK, (
        "a truncated search was reported as a clean skip:\n" + result.stdout + result.stderr
    )
    assert "window is FULL" in result.stderr, result.stderr


def test_a_GIT_FAILURE_is_COULD_NOT_CHECK_not_a_skip(tmp_path: Path) -> None:
    """The third cause that used to collapse into EXIT_OK: `_git` returning `(False, ...)`.

    A directory that is not a git repository at all stands in for "git could not answer" -- no
    `.git`, so `rev-parse` exits non-zero. Before #302 this printed a skip and exited 0.
    """
    root = tmp_path / "not-a-repo"
    (root / "tools").mkdir(parents=True)
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8", newline="\n")
    (root / "tools" / "check_changelog_refs.py").write_bytes(TOOL.read_bytes())

    result = _run(root)

    assert result.returncode == EXIT_COULD_NOT_CHECK, result.stdout + result.stderr
    assert "COULD NOT CHECK" in result.stderr, result.stderr


def test_NO_release_commit_is_a_SKIP_not_a_failure(tmp_path: Path) -> None:
    """A repo before its first release has nothing to compare against.

    Inventing a baseline at the root commit would demand entries for the whole of history on a fresh
    clone, which is a stage nobody would keep.
    """
    root = _repo(tmp_path / "r", "# Changelog\n\n- nothing\n", ["Do a thing (#42)"], release=None)
    result = _run(root)
    assert result.returncode == EXIT_OK
    assert "skipped" in result.stdout


def test_the_baseline_is_the_RELEASE_commit_not_merely_a_subject_containing_Release(
    tmp_path: Path,
) -> None:
    r"""The anchor is `^Release x.y.z`, so an issue about releasing something cannot become one.

    The decoy STARTS with "Release", so `.match()` alone cannot reject it -- only the
    `\d+\.\d+\.\d+` version pattern can. Without it the decoy becomes the baseline and every
    issue before it silently stops being checked: a guard quietly narrowing its own scope.

    WARNING: an earlier fixture used "Rework the Release notes page", which `.match()` rejects on
    its own -- so the test passed under the mutation and proved nothing. The decoy must start with
    the word.

    MUTATION: drop the version pattern, leaving `^Release`; this reddens, because #42 predates the
    decoy and falls outside the window.
    """
    root = _repo(
        tmp_path / "r",
        "# Changelog\n\n- nothing\n",
        ["Do a thing (#42)", "Release notes for the beta, drafted (#7)"],
    )
    result = _run(root)
    assert result.returncode == EXIT_MISSING
    assert "#42" in result.stderr, (
        "an issue merged BEFORE a decoy 'Release' subject stopped being checked -- the baseline "
        "matched prose rather than the release commit"
    )
