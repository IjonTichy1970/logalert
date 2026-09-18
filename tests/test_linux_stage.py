"""Behavioural checks for tools/linux_stage.sh's WSL delegation, runnable on ANY host.

Runs the REAL script through a stub `wsl` binary, because the subject is which branch the
124 path takes -- decided by a probe issued AFTER the delegated run was killed -- and a textual
assertion that both messages exist in the file passes without proving either is reachable.

Two seams and one stateful stub, and why each is shaped as it is:

  * LOGALERT_WSL_CMD names the binary to ask. A PATH stub named `wsl.exe` does NOT work under
    Git Bash (it will not PATH-resolve a non-PE file by that name), so the seam takes a path.
    It selects WHICH binary is asked, never WHAT is asserted.
  * A `uname` SHIM on the child's PATH. The script branches on `[ "$(uname -s)" != "Linux" ]`,
    so on a Linux CI runner it would never enter the delegation and every case here would be
    Windows-only -- seven green locally, red on CI, same commit. The shim is a test-side file,
    deliberately NOT a second env var in the production script: an env lever that forces the
    non-Linux branch would live in the file CI trusts and could be used to make the stage pass.

The stub's health answer is STATEFUL, and has to be: `systemctl is-system-running` is asked
twice (pre-flight, then the post-hang re-probe). A stub that answered both the same could never
produce the case this file exists for -- a distro that was healthy when the run started and
degraded during it.

Every environment variable named here is also named in tools/linux_stage.sh. Rename the LOGALERT_
prefix in BOTH files or these cases stop reaching their seams and silently run the real wsl.exe.

    python -m pytest -q tests/test_linux_stage.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "tools" / "linux_stage.sh"
NL = chr(10)


def _bash() -> str:
    found = shutil.which("bash")
    if not found:
        pytest.skip("bash unavailable -- this check needs a real shell")
    if not shutil.which("timeout"):
        pytest.skip("timeout(1) unavailable -- `bounded` cannot produce a 124 without it")
    return found


def _write_exec(path: Path, text: str) -> None:
    # newline="\n": Python's write_text emits CRLF on Windows, and a CRLF shebang line
    # breaks bash with "bad interpreter".
    path.write_text(text, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def _write_uname_shim(tmp_path: Path) -> Path:
    """A `uname` that says Windows, so the script takes the delegation branch on any host."""
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _write_exec(
        shim_dir / "uname", "#!/usr/bin/env bash" + NL + 'echo "MINGW64_NT-10.0-HARNESS"' + NL,
    )
    return shim_dir


def _write_wsl_stub(
    tmp_path: Path, *, child: str, health_post: str, distro: str, reprobe_sleep: int,
) -> Path:
    """A `wsl` that lists one distro, answers health statefully, and runs the child as told."""
    counter = tmp_path / "health_calls"
    posix_repo = REPO.as_posix()
    if posix_repo[1:3] == ":/":  # D:/x -> /d/x, the form Git Bash `pwd` prints
        posix_repo = "/" + posix_repo[0].lower() + posix_repo[2:]
    stub = tmp_path / "wsl_stub.sh"
    _write_exec(
        stub,
        NL.join(
            [
                "#!/usr/bin/env bash",
                'args="$*"',
                "case \"$args\" in",
                '  *"-l -q"*) echo "' + distro + '" ;;',
                '  *"systemctl is-system-running"*)',
                '    if [ -e "' + counter.as_posix() + '" ]; then sleep ' + str(reprobe_sleep)
                + '; echo "' + health_post + '"; '
                'else touch "' + counter.as_posix() + '"; echo running; fi ;;',
                '  *"wslpath"*) echo "' + posix_repo + '" ;;',
                # `test -f` is matched BEFORE the script pattern: both mention the script name.
                '  *"test -f"*) exit 0 ;;',
                '  *"linux_stage.sh"*) ' + child + " ;;",
                "  *) exit 0 ;;",
                "esac",
                "",
            ]
        ),
    )
    return stub


def _run(
    tmp_path: Path, *, child: str = "exit 0", health_post: str = "running",
    distro: str = "stub-distro", mode: str = "auto", reprobe_sleep: int = 0,
    bound_health: str = "30",
) -> tuple[str, int]:
    bash = _bash()
    # A fresh directory per run: the health stub's counter file is what makes it stateful, and a
    # second run in the same directory would inherit the first run's "already asked" state.
    tmp_path = Path(tempfile.mkdtemp(prefix="run-", dir=tmp_path))
    shim_dir = _write_uname_shim(tmp_path)
    stub = _write_wsl_stub(
        tmp_path, child=child, health_post=health_post, distro=distro, reprobe_sleep=reprobe_sleep,
    )
    env = dict(os.environ)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env.get('PATH', '')}"
    env["LOGALERT_WSL_CMD"] = str(stub)
    env["LOGALERT_WSL_DISTRO"] = "stub-distro"
    env["LOGALERT_BOUND_DELEGATE"] = "1"  # a 1s bound against an 8s sleep manufactures a real 124
    env["LOGALERT_BOUND_HEALTH"] = bound_health
    env["LOGALERT_CHECK_MODE"] = mode
    # check=False on purpose: the exit code is part of what the cases read, never a reason to raise.
    # encoding= stated explicitly: text=True ALONE would decode with the locale codec (cp1252 on a
    # Windows box) and a non-ASCII byte in the output would kill the harness, not fail a check.
    proc = subprocess.run(
        [bash, str(SCRIPT)],
        capture_output=True, encoding="utf-8", errors="replace", timeout=90, cwd=REPO, env=env,
        check=False,
    )
    return proc.stdout + proc.stderr, proc.returncode


def test_the_stage_takes_the_delegation_path_at_all(tmp_path: Path) -> None:
    """ANTI-VACUITY FLOOR. If the script stopped entering wsl_delegate (a rename, a reordered
    platform block, a stub the seam no longer reaches) every other assertion in this file would
    pass by matching nothing and read as coverage. This is the case that says the harness reached
    its subject."""
    out, _ = _run(tmp_path)
    assert "delegating to WSL distro" in out or "delegated run" in out, (
        "the harness never reached the delegation path, so nothing below is meaningful:" + NL + out
    )


def test_a_green_delegated_run_is_green(tmp_path: Path) -> None:
    out, rc = _run(tmp_path, child="exit 0")
    assert rc == 0
    assert "LINUX STAGE GREEN (delegated)" in out


def test_a_RED_delegated_run_is_red_not_a_skip(tmp_path: Path) -> None:
    """A delegation verified only on the green path proves it runs, not that it can fail; the red
    case is the one that justifies the machinery. MUTATION: return 2 for any non-zero child exit
    (the "everything is could-not-check" defect); this reddens."""
    out, rc = _run(tmp_path, child="exit 3")
    assert rc == 1
    assert "delegated run FAILED on 'stub-distro' (exit 3)" in out
    assert "LINUX STAGE FAILED (delegated)" in out
    assert "SKIP" not in out


def test_a_hang_on_a_healthy_distro_is_blamed_on_the_run(tmp_path: Path) -> None:
    out, rc = _run(tmp_path, child="sleep 8", health_post="running")
    assert rc == 0, "a hang is could-not-check under auto mode, never a red"
    assert "HUNG" in out
    assert "STILL ANSWERS" in out
    assert "D state" in out
    assert "wsl --terminate" in out


def test_a_hang_on_a_degraded_distro_is_blamed_on_the_distro(tmp_path: Path) -> None:
    """Both hang cases are needed: 'always blame the run' satisfies the case above completely
    and would be exactly as wrong as 'always blame the bus' was -- the rule and its plausible
    alternative disagree only here."""
    out, _ = _run(tmp_path, child="sleep 8", health_post="")
    assert "HUNG" in out
    assert "STOPPED ANSWERING" in out
    assert "wsl --shutdown" in out


def test_the_health_reprobe_is_BOUNDED(tmp_path: Path) -> None:
    """A probe of a wedged machine can wedge in turn. Behavioural, not textual: the stub's SECOND
    health call sleeps 8 s while the bound is 1 s, so an unbounded re-probe both takes 8 s and
    then answers `running` (blaming the run). A textual `'bounded' in body` check stayed green
    with the bound moved into a comment; this one cannot."""
    started = time.monotonic()
    out, _ = _run(
        tmp_path, child="sleep 8", health_post="running", reprobe_sleep=8, bound_health="1",
    )
    elapsed = time.monotonic() - started
    assert "STOPPED ANSWERING (systemctl: no output)" in out, out
    assert elapsed < 7, f"the re-probe was not cut short: {elapsed:.1f}s"


def test_each_could_not_check_cause_has_its_own_skip_wording(tmp_path: Path) -> None:
    """The case on which the spellings must differ, so a fix that made every skip say 'hung'
    would be the same defect with a new string. The script sets EIGHT could-not-check notes; two
    are pinned here -- the hang on a healthy distro, and no distro of that name, the two that
    were wrong in the field. The other six (no wsl on PATH, wsl did not answer, cannot reach
    systemd, no path answer, cannot see the checkout, hung and the distro degraded) are not."""
    hung, _ = _run(tmp_path, child="sleep 8")
    assert "the delegated run hung" in hung
    assert "no usable WSL distro" not in hung
    absent, _ = _run(tmp_path, distro="some-other-distro")
    assert "no WSL distro named" in absent
    assert "the delegated run hung" not in absent


def test_required_mode_turns_a_skip_into_a_failure(tmp_path: Path) -> None:
    """The mode split is what keeps the stage from being vacuous: auto tolerates a laptop problem,
    required (CI) refuses to pass by not looking. MUTATION: make skip() always call note(); the
    required half reddens while the auto half stays green."""
    out_auto, rc_auto = _run(tmp_path, distro="some-other-distro", mode="auto")
    assert rc_auto == 0
    assert "LINUX STAGE SKIPPED" in out_auto
    out_req, rc_req = _run(tmp_path, distro="some-other-distro", mode="required")
    assert rc_req == 1
    assert "so this is not a pass" in out_req
    assert "LINUX STAGE FAILED" in out_req


def test_the_delegated_green_arm_consults_the_failure_list() -> None:
    """TEXTUAL, and named as such. The case it pins -- `required` mode, no `timeout`, a GREEN
    delegated run -- cannot be produced through the stub: `timeout` cannot be hidden from
    `command -v` without hiding every other coreutil the script needs. It was measured once on a
    scratch copy with the probe forced false: before the fix, a FAIL line and then
    `LINUX STAGE GREEN (delegated)`, rc 0; after it, the FAIL line and `LINUX STAGE FAILED -`,
    rc 1. This check keeps that arm from silently losing the test. MUTATION: drop the
    `[ -z "$FAILED" ]` test from the delegated `0)` arm; this reddens."""
    text = SCRIPT.read_text(encoding="utf-8")
    arm = re.search(r"^    0\)\n(.*?)^    1\)", text, re.M | re.S)
    assert arm, "the delegated case's 0) arm was not found"
    assert '[ -z "$FAILED" ]' in arm.group(1)


def _bounded_rc(case: str) -> tuple[int, float]:
    """Lift the real bounded() out of the script and run one call through it in bash."""
    bash = _bash()
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^bounded\(\) \{\n.*?^\}$", text, re.S | re.M)
    # Asserting the extraction found something is the point: a rename would otherwise yield an
    # empty harness and every case below would fail for the wrong reason.
    assert match and "timeout" in match.group(0), "bounded() not found in the script"
    harness = "_HAVE_TIMEOUT=1" + NL + match.group(0) + NL + case + NL + 'echo "rc=$?"' + NL
    started = time.monotonic()
    proc = subprocess.run(
        [bash, "-c", harness], capture_output=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    found = re.search(r"rc=(\d+)", proc.stdout)
    assert found, proc.stdout + proc.stderr
    return int(found.group(1)), time.monotonic() - started


def test_bounded_kills_a_hang_and_reports_124() -> None:
    """A hanging subprocess is the ONLY case where 'bounded' and 'unbounded' disagree, so the
    fixture must actually hang. MUTATION: drop `timeout` from bounded(); `sleep 20` then runs to
    completion with rc 0, so `rc == 124` reddens after 20 s (measured -- the first version of this
    docstring said the case hits the harness ceiling, which it does not). The harness's 30 s
    ceiling is the backstop that keeps a fixture longer than that from hanging the suite."""
    rc, elapsed = _bounded_rc("bounded 1 sleep 20")
    assert rc == 124
    assert elapsed < 15, "exit 124 alone does not prove the command was cut short"


def test_bounded_passes_success_and_failure_through_unchanged() -> None:
    """The load-bearing half of the contract. 'Bound everything and call any non-zero
    could-not-check' passes every other case in this file; `false` -> 1 is what reddens it."""
    assert _bounded_rc("bounded 5 true")[0] == 0
    assert _bounded_rc("bounded 5 false")[0] == 1


# -- the checks (issue #14): every binary that can hang is bounded --------------------------------

# The binaries run_native_checks() and its helpers call that talk to another process, the
# network or a mount that can stall: each must sit behind `bounded "$BOUND_..."`. Coreutils over
# files in the temp tree (cat, grep, stat, wc, find, sed, head, ls) are not listed: they cannot
# block there. `cp` IS listed because the one copy reads the checkout, which is a 9p mount in
# the sandbox -- exactly the operation that hangs. Any command under the temp tree ($T/...) or
# the versioned interpreter ($pyx) is bounded whatever its spelling (_TREE_BINARY); the names
# here are also the floor: each must be found at least once, so a rename cannot make the guard
# vacuous.
BOUNDED_BINARIES = ("runuser", "logrotate", "journalctl", "systemctl", "cp", "python3",
                    "mount", "umount", "dd",  # the full-disk check's tmpfs (issue #30)
                    "truncate",  # the band's one free page (issue #70)
                    "mkfs.ext4",  # the no-d_type check's image (issue #71)
                    "systemd-analyze",  # the documented unit's verify (issue #36)
                    '"$pyx"', '"$T/venv/bin/python"', '"$T/bin/logalert"')
_TREE_BINARY = re.compile(r'^"?\$\{?(?:T|pyx)\}?"?(?:/\S*)?$')
# where a command may begin: after these, the next word is at a command position
_SEPARATORS = re.compile(r"\|\||&&|\||;|&|\$\(|\(|(?<!\$)\{|`")
# what may precede a command without changing what runs: reserved words, `!`, nice/nohup/
# exec/command/time, bare VAR=x assignments, `env [-flags] [VAR=x ...]`
_PREFIX = re.compile(
    r"^(?:(?:if|elif|while|until|then|do|else|!|nice|nohup|exec|command|time)\s+"
    r"|[A-Za-z_][A-Za-z0-9_]*=\S*\s+"
    r"|env(?:\s+-\S+)*(?:\s+[A-Za-z_][A-Za-z0-9_]*=\S*)*\s+)+")
_BOUND = re.compile(r'^bounded\s+"\$BOUND_[A-Z]+"\s+')


def _command_positions() -> list[tuple[str, bool]]:
    """Every (binary, bounded?) occurrence at a command position anywhere in the script:
    continuation lines joined, comments and heredoc bodies dropped, each line split at the
    shell's separators, the prefixes above skipped on both sides of `bounded`."""
    text = SCRIPT.read_text(encoding="utf-8")
    text = text.replace("\\" + NL, " ")
    found: list[tuple[str, bool]] = []
    in_heredoc = False
    for raw in text.splitlines():
        line = raw.strip()
        if in_heredoc:
            in_heredoc = line != "EOF"  # stay in the body until the terminator
            continue
        if "<<'EOF'" in line or "<<EOF" in line:
            in_heredoc = True
            line = line.split("<<", 1)[0]
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"\s#.*$", "", line)
        for segment in _SEPARATORS.split(line):
            segment = _PREFIX.sub("", segment.strip())
            if not segment:
                continue
            bounded = _BOUND.match(segment) is not None
            if bounded:
                segment = _PREFIX.sub("", _BOUND.sub("", segment))
            if not segment:
                continue
            first = segment.split(maxsplit=1)[0]
            if first in BOUNDED_BINARIES or _TREE_BINARY.match(first):
                found.append((first, bounded))
    return found


def test_every_external_binary_in_the_checks_is_bounded() -> None:
    """TEXTUAL, and named as such -- the template's own rule ("a wiring guard in the tests is
    what keeps 'every binary is bounded' true after the next edit"). Enumerates rather than
    searches: every occurrence of every listed binary at a command position must be wrapped,
    and every listed binary must occur at least once, so a rename cannot make the guard
    vacuous. MUTATIONS (each reddens): drop `bounded "$BOUND_CMD"` from one runuser call; add
    `if ! runuser ...; then` or `FOO=1 runuser ...` unbounded; pipe journalctl into head; call
    "${T}/venv/bin/pip" unbounded. A binary hidden behind a variable is the documented limit."""
    found = _command_positions()
    unbounded = [name for name, bounded in found if not bounded]
    assert not unbounded, f"unbounded at a command position: {unbounded}"
    seen = {name for name, _ in found}
    missing = [name for name in BOUNDED_BINARIES if name not in seen]
    assert not missing, f"never found at a command position (renamed?): {missing}"
