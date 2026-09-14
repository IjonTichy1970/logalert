#!/usr/bin/env python3
r"""Refuse Bash commands that author escape-bearing text or truncate a file through a shell.

Ported from an earlier project, where it was built after the tenth failure of the prose rule it
enforces. The ATTRIBUTION in "What actually happens" was re-measured with a control before
porting, because the first write-up blamed the wrong layer and a wrong attribution teaches the
wrong avoidance.

⚠️ This docstring is a RAW string on purpose. It shows backslash sequences literally, and a plain
one would both mis-render them and raise SyntaxWarning on the invalid escapes. That is this file's
own subject arriving one layer down.

## Why this exists

The project this hook came from carried "never construct escape-bearing text through a shell"
as prose for months. It was broken **ten times** there, three of them in the session that was
writing the rule down. Prose does not prevent it: by the time you remember the rule, the mangled text is already in a
file, in a filed issue, or -- twice now -- the file is at zero bytes.

⭐ The project's own doctrine applied to itself: **prefer a construction that PREVENTS over a habit
that must be maintained.**

## What actually happens (measured here 2026-08-24, with a control)

⚠️ **The heredoc is NOT the culprit, and the upstream issue said it was.** A quoted `<<'EOF'` is
literal exactly as POSIX specifies. What is not literal is the **Bash-tool transport**: every
INLINE command loses **exactly one backslash level before the shell parses anything**.

    control file written by `Write`, never crossed a shell : a \ \ b
    quoted <<'EOF' from a script ON DISK                   : a \ \ b   <- identical
    unquoted <<EOF from a script on disk                   : a \ b
    the SAME heredoc typed INLINE                          : a \ b
    inline  printf '%s' 'a\\b'   -- no heredoc at all      : a \ b     <- the disproof

The last line is why "avoid heredocs" is the wrong lesson: a plain single-quoted argument loses the
same level. Blocking a heredoc body is still worth doing -- it is the highest-frequency AUTHORING
shape -- but it is one instance of a transport-wide problem, not the problem. ⚠️ The construct that
genuinely interprets escapes is the UNQUOTED `<<EOF`, which nobody was worried about.

## Coverage, stated honestly

⚠️ **This guard covers the three highest-cost shapes. It does NOT cover the general transport
loss.** `printf '%s' 'a\\b'` is the same defect and passes deliberately. A rule broad enough to
catch it would fire on `C:\Users\...`, on `grep '\.md$'` and on every `sed` expression -- and a
guard that blocks ordinary work gets switched off. Narrow guard plus accurate prose beats a broad
guard that gets deleted. That boundary is pinned by an ALLOW case in the test, so it is a stated
limit rather than an accident.

## What it blocks, and why each one is here

Every pattern is a REAL failure, not a hypothetical:

1. **A heredoc carrying a backslash.** Typed inline, the body arrives one backslash level short.
   Cost: a regex that would not compile, twice.
2. **A truncating open() in a piped script.** `open(p, "w")` truncates *before* the write; an error
   after that leaves nothing. Cost: a `MEMORY.md` at zero bytes in one repo, and 415KB of
   `CHANGELOG.md` at zero bytes in another.
3. **A backtick inside a `--body "..."` argument.** The shell command-substitutes it into the text.
   Cost: `command not found` printed into the middle of a filed GitHub issue.

## What it deliberately does NOT block

Reading, searching, `git`, `bash tools/gate.sh`, `pytest`, `--body-file`, running a script FROM
DISK, and `cat >> file <<'EOF'` for prose -- the one shell form that cannot truncate. The point is
to stop *authoring* through a shell, not to make the shell unusable.

⚠️ It reads the command text only. It cannot see what a script does once it is on disk, which is
fine: a script on disk was written by `Write` and never crossed a shell.

Exit 2 blocks the call and shows the reason to the model.
"""
import json
import re
import sys

BACKSLASH = chr(92)  # ⚠️ a code point, not a literal -- see the docstring's note.

# (compiled pattern, what it is, what to do instead)
RULES = [
    (
        re.compile(r"open\s*\([^)]*['\"][wW]b?['\"]"),
        "a truncating open(..., 'w') inside a shell command",
        "open(path,'w') and Path.write_text TRUNCATE before they write; an error after that leaves "
        "nothing. This is how a MEMORY.md and 415KB of CHANGELOG.md were lost. Use the Write tool, "
        "or write-temp-then-rename.",
    ),
    (
        re.compile(r"--body\s+\"[^\"]*`"),
        'a backtick inside a --body "..." argument',
        "The shell will command-substitute it INTO the issue text. Use --body-file with a file "
        "written by the Write tool.",
    ),
    (
        # A COMMAND position: the start of the command, the start of any LINE (re.M), or after
        # `|`, `&`, `;` -- with `NAME=value` env prefixes allowed in between. The port measured
        # `PYTHONIOENCODING=utf-8 python - <<` and a second-line `python - <<` both sailing
        # through the unanchored, single-line form. And a REAL heredoc introducer after it --
        # the delimiter, optional redirections, END OF LINE -- the same anchor HEREDOC_START
        # uses, for the same reason: a `grep` whose search string is `CHECKS\|python - <<\|x`
        # continues past the token and is reading, not authoring. Both halves pinned in the test.
        re.compile(
            r"(?:^|[|&;])\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:python|python3)\s+-\s*"
            r"<<-?\s*(['\"]?)\w+\1(?:[ \t]*\d*[<>]{1,2}&?\S*)*[ \t]*$",
            re.M,
        ),
        "a Python script piped through a shell",
        "Write the script to a file and run the file: nothing parses the content, and it is "
        "re-runnable and diffable. This is the construction that PREVENTS.",
    ),
]


# A REAL heredoc introducer: `<<` + optional `-`, an optionally-quoted delimiter, and then END OF
# LINE.
# ⚠️ The end-of-line anchor is the whole point, and its absence was a live false positive in the
# upstream repo within two commands of the hook going in: a `grep -n "...<<'EOF'..."` search STRING
# matched a pattern that only looked for the token anywhere. A guard that blocks ordinary reading
# gets switched off, so the introducer must be structural. Pinned by a regression test.
HEREDOC_START = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[ \t]*$", re.M)


def heredoc_body_has_backslash(command):
    """The delimiter of the first heredoc whose BODY contains a backslash, or None.

    Scans only between the introducer line and its terminator, so a backslash elsewhere in the
    command -- a Windows path, a regex argument, an escaped quote -- is none of this rule's
    business.
    """
    for match in HEREDOC_START.finditer(command):
        delimiter = match.group(2)
        rest = command[match.end():]
        end = re.search(r"^[ \t]*" + re.escape(delimiter) + r"[ \t]*$", rest, re.M)
        body = rest[: end.start()] if end else rest
        if BACKSLASH in body:
            return delimiter
    return None


def main():
    try:
        # sys.stdin.buffer, NOT sys.stdin. Text-mode stdin decodes with the LOCALE codec (cp1252
        # on a Windows console); the sibling ASCII hook measured a command carrying a character
        # whose UTF-8 has a byte cp1252 does not define (a star, U+2B50, is E2 AD 90) making the
        # decode RAISE -- and the fail-open `except` below then ALLOWED it. JSON is UTF-8 by
        # RFC 8259, and json.loads accepts bytes. Pinned by the raw-UTF-8 case in the test.
        payload = json.loads(sys.stdin.buffer.read())
    except Exception:
        return 0  # never block on a payload we cannot read
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""

    delimiter = heredoc_body_has_backslash(command)
    if delimiter:
        print(
            "BLOCKED by .claude/hooks/guard_shell_text.py -- the <<" + delimiter +
            " heredoc body contains a backslash.\n\n"
            "The heredoc itself is literal, but the Bash-tool transport eats exactly one backslash "
            "level from every INLINE command before the shell parses anything -- measured, "
            r"r'a\\b' arrives 3 characters, not 4. "
            "Write the script to a file with the Write tool and run the file, where nothing is "
            "consumed.",
            file=sys.stderr,
        )
        return 2

    for pattern, what, remedy in RULES:
        if pattern.search(command):
            print(
                "BLOCKED by .claude/hooks/guard_shell_text.py -- " + what + ".\n\n" + remedy +
                "\n\nThe project this hook came from carried this rule as prose and broke it 10 "
                "times, three of them while the rule was being written down. That is why it is a "
                "hook and not a paragraph.",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
