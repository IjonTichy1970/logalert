#!/usr/bin/env python3
"""Offline behavioural checks for `guard_shell_text.py`.

Run as a gate stage -- `.claude/` is excluded from ruff, mypy, bandit and pytest discovery
(pyproject.toml says why), so this tooling is gated by BEHAVIOUR: a script that exits 0 or 1,
wired as its own named stage of the gate.

    python .claude/hooks/test_guard_shell_text.py     # exit 0 = pass

⚠️ Every BLOCK case is a command shape that actually ran and actually caused damage. Every ALLOW
case is a shape used routinely in THIS repo and must keep working -- a guard that blocks ordinary
work gets switched off, and the project this hook came from carries the sharper form of that rule: *a gate is
never weakened to make it quiet*, which only holds if the gate is not firing on honest work.

⭐ **THE MUTATION THAT MUST REDDEN:** delete the `[ \\t]*$` end-of-line anchor from
`HEREDOC_START`. The grep-search-string case below must then be BLOCKED and this file must fail.
That is the case on which the anchored and unanchored rules disagree -- and it is a live false
positive that hit the upstream repo within two commands of their hook going in.

⚠️ The inert version to avoid: asserting only that "a heredoc with a backslash is blocked". That
stays green with the anchor removed, so it tests nothing about the fix.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard_shell_text.py")

FAILURES = []
CHECKS = 0

# ⚠️ Built from code points. A literal backslash in this file would be the very bug under test
# reaching the test that exists to catch it.
BS = chr(92)
NL = chr(10)


def verdict_for_tool(tool_name, command):
    """2 = blocked, 0 = allowed. `tool_name` is a parameter so the Bash-only gate is testable."""
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})
    # ⚠️ EXPLICIT encoding, per CLAUDE.md's Windows quirks. `text=True` decodes with the locale
    # codec (cp1252 on this box), so a single non-ASCII byte in a future block message would take
    # the whole harness down with a UnicodeDecodeError rather than failing a check.
    proc = subprocess.run(
        [sys.executable, GUARD], input=payload, capture_output=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode


def verdict(command):
    """The common case: the guard's verdict on a Bash command."""
    return verdict_for_tool("Bash", command)


def check(label, got, want):
    global CHECKS
    CHECKS += 1
    if got != want:
        FAILURES.append("%s%s      got:  %r%s      want: %r" % (label, NL, got, NL, want))


# ---- BLOCK: the real failures ---------------------------------------------------------------
check(
    "⭐ a heredoc carrying a backslash is blocked (the mangled-regex failure, twice)",
    verdict("python - <<'PY'" + NL + "p = r'[^\"" + BS + BS + "]'" + NL + "PY"),
    2,
)
check(
    "⭐⭐ a truncating open(...,'w') is blocked (this is how CHANGELOG.md hit zero bytes)",
    verdict("python - <<'PY'" + NL + "open(p,'w').write(s)" + NL + "PY"),
    2,
)
check(
    "⭐ a backtick inside --body \"...\" is blocked (it substitutes INTO the filed issue)",
    verdict('gh issue create --title x --body "see `example.com` for more"'),
    2,
)
check(
    "a python script piped through a shell is blocked on its own",
    verdict("python - <<'PY'" + NL + "print(1)" + NL + "PY"),
    2,
)
check(
    "an ENV-PREFIXED piped script is blocked too (the prefix the MSYS rule prescribes)",
    verdict("MSYS_NO_PATHCONV=1 python - <<'PY'" + NL + "print(1)" + NL + "PY"),
    2,
)
check(
    "a piped script on a SECOND LINE is blocked too (re.M on the command-position anchor)",
    verdict("echo hi" + NL + "python - <<'PY'" + NL + "print(1)" + NL + "PY"),
    2,
)
check(
    "a piped script with a REDIRECTION after the delimiter is blocked too (the anchor allows it)",
    verdict("python - <<'PY' 2>&1" + NL + "print(1)" + NL + "PY"),
    2,
)
check(
    "Path.write_text's truncating sibling is caught too (open(p,'wb'))",
    verdict("python - <<'PY'" + NL + "open(p,'wb').write(b)" + NL + "PY"),
    2,
)

# ---- ALLOW: what a project does constantly. Blocking these would get the hook deleted. --------
check("the gate is allowed", verdict("bash tools/gate.sh"), 0)
check(
    "the gate as this repo actually runs it is allowed (PATH export, redirect, rc capture)",
    verdict('export PATH="$PWD/.venv/Scripts:$PATH"; bash tools/gate.sh > out 2>&1; rc=$?'),
    0,
)
check("pytest is allowed", verdict("python -m pytest -q"), 0)
check("pytest through the venv interpreter is allowed", verdict("./.venv/Scripts/python.exe -m pytest -q"), 0)
check("plain git is allowed", verdict("git -C D:/Projects/Logalert log --oneline -5"), 0)
check(
    "running a script FROM DISK is allowed -- the remedy must never be blocked",
    verdict("python tools/check_ascii.py logalert/__init__.py"),
    0,
)
check(
    "--body-file is allowed -- the remedy for the --body failure",
    verdict("MSYS_NO_PATHCONV=1 gh issue comment 12 --body-file C:/Users/me/scratch/map.md"),
    0,
)
check(
    "git commit -F is allowed -- the remedy for a mangled commit message",
    verdict("git commit -F C:/Users/me/scratch/commit.txt"),
    0,
)
check(
    "the sandbox preflight one-liner is allowed (env prefix, timeout, -u root, $(...))",
    verdict(
        "MSYS_NO_PATHCONV=1 timeout 90 wsl.exe -d rlyeh-sandbox -u root -- "
        'bash /mnt/d/Projects/Logalert/sandbox/sandbox_preflight.sh "$(date -u +%s)"'
    ),
    0,
)
check(
    "grep with a regex ARGUMENT is allowed (searching, not authoring)",
    verdict("grep -nE 'TODO|FIXME' tools/gate.sh"),
    0,
)
check(
    "appending prose with a quoted heredoc is allowed -- it cannot truncate",
    verdict("cat >> CHANGELOG.md <<'EOF'" + NL + "just prose" + NL + "EOF"),
    0,
)
check("plain git is allowed (the everyday case)", verdict("git status"), 0)
check(
    "a grep whose SEARCH STRING is a piped-script shape is NOT blocked (reading, not authoring)",
    verdict('grep -rn "python - <<" .claude/'),
    0,
)
check(
    "a grep whose search string ALTERNATES on that shape is NOT blocked either (the introducer anchor)",
    verdict('grep -n "CHECKS' + BS + '|python - <<' + BS + '|piped" tools/x.py'),
    0,
)
# ⚠️ This must send text the guard WOULD block under Bash, or it proves nothing about the
# tool_name gate. Upstream's version was `check("a non-Bash tool is never blocked", 0, 0)` --
# comparing a literal to itself, so it never invoked the guard at all and could not fail.
# MUTATION: delete the `tool_name != "Bash"` early return in main(). This reddens; the 0-vs-0
# form stays green.
check(
    "⭐ a NON-Bash tool carrying blockable text is not blocked -- the guard is Bash-scoped",
    verdict_for_tool("Write", "open(p,'w').write(s)"),
    0,
)

# ---- ⚠️ THE STATED COVERAGE BOUNDARY, pinned rather than left implicit -----------------------
#
# This command loses a backslash level in exactly the same way a heredoc body does -- it is the
# same transport defect -- and it is DELIBERATELY not blocked. A rule broad enough to catch it
# fires on every Windows path and every `sed` expression, and gets the hook switched off.
#
# ⭐ Pinning it makes the limit a DECISION rather than an oversight: whoever widens the guard has
# to change this line and say why.
check(
    "⭐⭐ an inline backslash OUTSIDE a heredoc is NOT blocked -- the honest coverage boundary",
    verdict("printf '%s' 'a" + BS + BS + "b'"),
    0,
)
check(
    "a Windows path is not blocked (the reason the general rule is refused)",
    verdict("echo C:" + BS + "Users" + BS + "me" + BS + "AppData"),
    0,
)

# ---- ⚠️ THE LIVE FALSE POSITIVE, now a permanent regression test ------------------------------
#
# Contains no heredoc at all: `<<'EOF'` is a grep SEARCH STRING. The upstream repo's first pattern
# looked for the token anywhere followed by a backslash anywhere, and blocked an ordinary read
# within two commands of going in. The fix was to anchor the introducer to END OF LINE and scan
# only the BODY. This is the case the two designs disagree on.
check(
    "a grep whose SEARCH STRING mentions a heredoc is NOT blocked (the live false positive)",
    verdict("grep -n \"one shell use|escape-bearing|<<'EOF'\" CLAUDE.md | head -8"),
    0,
)
# ⚠️ THE CASE ABOVE CANNOT REDDEN THE ANCHOR MUTATION, and that was measured, not assumed: it
# carries no backslash, so with the anchor dropped the fake heredoc's "body" is still clean and the
# guard still allows it. It pins a token-only rule and nothing more.
#
# ⭐ THIS is the discriminating case -- a search string carrying BOTH a heredoc token AND a
# backslash, which is exactly what grepping for these rules looks like. With the end-of-line anchor
# the introducer does not match (the line continues), so it is allowed; without the anchor it
# matches, the rest of the command becomes the "body", the backslash is found, and an ordinary read
# is BLOCKED.
check(
    "⭐⭐ a grep mentioning a heredoc AND carrying a backslash is NOT blocked (pins the anchor)",
    verdict('grep -nE "<<' + "'EOF'" + '|[0-9]' + BS + BS + '+" CLAUDE.md'),
    0,
)
check(
    "a backslash ELSEWHERE in a command with a heredoc is not the body's business",
    verdict("grep -c '[0-9]" + BS + "+' f.txt && cat >> n.md <<'EOF'" + NL + "prose" + NL + "EOF"),
    0,
)
check(
    "a Windows path alongside an unrelated heredoc is allowed",
    verdict(
        "cat >> log.md <<'EOF'" + NL + "note" + NL + "EOF" + NL +
        "echo C:" + BS + "Users" + BS + "me"
    ),
    0,
)

# ---- THE PAYLOAD IS UTF-8 BYTES, and text-mode stdin decodes with the LOCALE codec -----------
#
# Measured on the sibling ASCII hook, not reasoned about: a character whose UTF-8 contains a byte
# cp1252 does not define (a star, U+2B50, is E2 AD 90 and 0x90 is undefined) made a text-mode
# `json.load(sys.stdin)` RAISE, and the fail-open `except` then ALLOWED the command. So the guard
# reads `sys.stdin.buffer`. The fixture must send RAW UTF-8 -- `json.dumps` defaults to
# `ensure_ascii=True`, which escapes non-ASCII and leaves a text-mode read nothing to mangle -- under
# a cp1252 child (PYTHONIOENCODING), so it discriminates on Linux CI too, where stdio is UTF-8.
# MUTATION: revert `json.loads(sys.stdin.buffer.read())` to `json.load(sys.stdin)`. This reddens.
STAR = chr(0x2B50)


def verdict_raw_utf8(command):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}, ensure_ascii=False)
    proc = subprocess.run(
        [sys.executable, GUARD], input=payload.encode("utf-8"), capture_output=True,
        env=dict(os.environ, PYTHONIOENCODING="cp1252"),
    )
    return proc.returncode


check(
    "a blockable command carrying a non-cp1252 character is STILL blocked (bytes, not locale)",
    verdict_raw_utf8("python - <<'PY'" + NL + "print('" + STAR + "')" + NL + "PY"),
    2,
)

# ---- the string as spelled (issue #55): settings.json's command through the shell that runs it --
#
# Claude Code passes a `command` hook to `sh -c` on Linux and macOS and to Git Bash on Windows,
# with ITS environment -- not the gate's, where the venv's Scripts directory is first on PATH
# and `python` resolves trivially. So the command string is run as spelled, three times, a
# BLOCK payload on stdin: (a) with the repo's .venv entries stripped from PATH -- the
# interpreter the hook actually finds on this host -- expecting the block (exit 2); (b) with
# PATH reduced to a directory holding only a `python3` shim -- the Debian/Ubuntu host without
# python-is-python3, where the bare `python` made the hook exit 127 and every command
# proceed; (c) with an empty PATH, where the launcher must fail CLOSED with its own message,
# never proceed. No `sh` is could-not-check: a FAIL, never a pass. MUTATIONS: the python3
# fallback dropped from the launcher -> (b) exits 127; its `exit 2` dropped -> (c) exits 127.
import shutil
import tempfile

SETTINGS = os.path.join(HERE, os.pardir, "settings.json")
REPO_ROOT = os.path.abspath(os.path.join(HERE, os.pardir, os.pardir))


def hook_command():
    with open(SETTINGS, encoding="utf-8") as fh:
        data = json.load(fh)
    for entry in data["hooks"]["PreToolUse"]:
        if entry.get("matcher") == "Bash":
            return entry["hooks"][0]["command"]
    raise SystemExit("settings.json: no PreToolUse hook for Bash")


def hook_verdict(path_env):
    """(exit code, stderr) of the command string as spelled, under `sh -c` with PATH set."""
    sh = shutil.which("sh")
    if sh is None:
        FAILURES.append("no `sh` on this host: the hook command as spelled could not be run "
                        "(could-not-check is never a pass)")
        return None, ""
    payload = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "python - <<'PY'" + NL + "open(p,'w')" + NL
                                         + "PY"}})
    env = dict(os.environ, PATH=path_env, CLAUDE_PROJECT_DIR=REPO_ROOT)
    proc = subprocess.run([sh, "-c", hook_command()], input=payload, capture_output=True,
                          encoding="utf-8", errors="replace", env=env, cwd=REPO_ROOT)
    return proc.returncode, proc.stderr


_stripped = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                            if ".venv" not in p)
_rc, _err = hook_verdict(_stripped)
check("⭐ the hook command as spelled blocks with the venv off PATH (the interpreter the hook "
      "finds)", (_rc, "BLOCKED" in _err), (2, True))
with tempfile.TemporaryDirectory() as _shims:
    _shim = os.path.join(_shims, "python3")
    with open(_shim, "w", encoding="utf-8", newline=NL) as fh:  # a fresh file in a temp dir
        fh.write("#!/bin/sh" + NL + "exec \"" + sys.executable.replace(BS, "/") + "\" \"$@\""
                 + NL)
    os.chmod(_shim, 0o755)
    _rc, _err = hook_verdict(_shims)
    check("⭐ a host with python3 and no python still blocks (the launcher's fallback)",
          (_rc, "BLOCKED" in _err), (2, True))
    _empty = os.path.join(_shims, "nothing")
    os.mkdir(_empty)
    _rc, _err = hook_verdict(_empty)
    check("⭐ a host with neither interpreter is refused, never proceeded (fail closed)",
          (_rc, "neither python nor python3" in _err), (2, True))

# ---- anti-vacuity: the guard must be capable of BOTH answers ----------------------------------
#
# ⚠️ Without this, a guard stuck on "allow" passes every ALLOW case above and reads as coverage.
_answers = {
    verdict("git status"),
    verdict("python - <<'PY'" + NL + "open(p,'w')" + NL + "PY"),
}
check("⭐ the guard returns BOTH verdicts -- it is not stuck on one", _answers, {0, 2})

if FAILURES:
    print("FAIL - %d of %d checks:" % (len(FAILURES), CHECKS), file=sys.stderr)
    for failure in FAILURES:
        print("  - %s" % failure, file=sys.stderr)
    sys.exit(1)
print("OK - the real failures are blocked and ordinary work is not (%d checks)." % CHECKS)
