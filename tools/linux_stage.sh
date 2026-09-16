#!/usr/bin/env bash
# TEMPLATE: a gate stage that only makes sense on Linux, delegated into a WSL
# sandbox when the gate runs on Windows. Copy it, keep it in tools/, and fill in
# run_native_checks(). Everything else is the delegation machinery, which was
# learned the expensive way (a 20-minute silent hang, a misattributed fault, a
# stub that was silently bypassed) and should be copied rather than re-derived.
#
# Modes:
#   auto      (default) skip what this host cannot run, and SAY so
#   required  a skip is a FAILURE -- could-not-check is never a pass
#
# The `required` mode is what keeps this from being vacuous. A Windows dev box
# genuinely cannot host systemd, so a hard requirement everywhere would make the
# local gate permanently red; requiring it on the platform that gates the PR is
# the same guarantee without that cost. NOTHING SETS IT FOR YOU: put
# `LOGALERT_CHECK_MODE: required` under the CI job's `env:`; until then CI runs
# this stage in auto mode and passes by not looking.
#
# Exit vocabulary, everywhere in this file: 0 = green, 1 = red, 2 = could not
# check. 2 is NEVER a pass; under `required` it becomes a failure.
#
# `set -u`, deliberately NOT `set -e` or `pipefail`: nearly every check below
# captures a command that is EXPECTED to fail (`out="$(cmd 2>&1)"; rc=$?`), and
# under `set -e` that assignment kills the script before it can report or roll
# back -- the raw error text alone then reads like a clean refusal, and the
# rollback never runs. Capture first, read `rc` DIRECTLY (never after an `if`:
# an `if` whose condition fails with no `else` returns 0), and `|| true` any
# diagnostic pipeline whose first stage may legitimately exit non-zero.
set -u

MODE="${LOGALERT_CHECK_MODE:-auto}"
# shellcheck disable=SC1007 # `CDPATH= cd --` is a deliberate one-shot ENV PREFIX so a stray
# CDPATH in the caller's environment cannot silently send us to a different directory.
REPO_ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
# This script's path relative to the repo root, as the distro will run it. The
# template assumes it lives in tools/; change this line if you move it.
SELF_REL="tools/$(basename -- "$0")"

FAILED=""
note()  { echo "  $*"; }
# fail <message> [<tag>]: the tag is what the closing summary line names; it
# defaults to the whole message.
fail()  { echo "  FAIL  $1"; FAILED="${FAILED} ${2:-$1}"; }
ok()    { echo "  ok    $*"; }

# A skip is tolerated in auto mode and fatal in required mode. Nothing else may
# decide whether an unrun check counts as a pass.
skip() {
  if [ "$MODE" = "required" ]; then
    fail "$1 (LOGALERT_CHECK_MODE=required, so this is not a pass)"
  else
    note "SKIP  $1"
  fi
}

# -- bounding external commands ------------------------------------------------
#
# Every external command gets a bound. An unbounded one once hung a whole gate
# for 20 minutes with no output after the stage header; a point-in-time health
# probe cannot prevent that (it passed, then the distro degraded DURING the run).
# Only a bound answers about the command that is currently executing.
#
# THE CONTRACT, and the middle line is the one that matters:
#   0   -- the command succeeded
#   124 -- it did not FINISH: could-not-check, never a pass. The caller reports
#          it, and skip() already turns that into a failure under `required`.
#   *   -- ANY other code passes through UNCHANGED.
# A bound must never convert a real rejection into "could not check": that would
# swap a red for a skip. "Did not finish" and "finished and said no" differ.
#
# Bounds are generous on purpose: a bound tight enough to catch a rare hang at
# the cost of routine false failures gets the stage switched off. Set each one
# well above an honest measured run and well below the hang you have seen (the
# source values: a ~90 s delegated run got 420 s; sub-second binaries got 60 s).
BOUND_DELEGATE="${LOGALERT_BOUND_DELEGATE:-420}"   # the whole delegated run
BOUND_CMD="${LOGALERT_BOUND_CMD:-60}"              # each external binary
BOUND_HEALTH="${LOGALERT_BOUND_HEALTH:-30}"        # every probe of the distro itself; short, one is asked AFTER a hang

echo "linux stage: mode=$MODE"

# Probed ONCE, at top level -- not inside bounded(). A skip issued from inside
# `$(bounded ...)` runs in a subshell: the parent's FAILED never changes (so
# `required` mode reports GREEN over an unbounded run) and the warning text is
# captured INTO the value being read, which turned one health probe's answer
# into "SKIP ... running" and a healthy distro into "cannot reach systemd".
if command -v timeout >/dev/null 2>&1; then
  _HAVE_TIMEOUT=1
else
  _HAVE_TIMEOUT=""
  skip "timeout(1) unavailable -- external commands run UNBOUNDED in this run"
fi
bounded() {
  local limit="$1"; shift
  if [ -z "$_HAVE_TIMEOUT" ]; then
    # Already said so above. Run unbounded so the run yields its other information.
    "$@"
    return $?
  fi
  # timeout(1) returns 124 when it kills the command and the command's own code
  # otherwise -- exactly the contract above, so nothing is remapped here.
  timeout "$limit" "$@"
}

# -- delegation to WSL ---------------------------------------------------------
#
# THE DELEGATED RUN IS PREDICTIVE, NOT AUTHORITATIVE. The distro is long-lived
# and mutable, so a result can depend on what an earlier session left behind.
# CI runs this natively on a clean machine every time and stays the authority.
#
# Returns 0 (delegated run green), 1 (delegated run RED), or 2 (could not
# delegate, and said why). NEVER 0 for could-not-check.
#
# WHY the delegation could not be checked, in the caller's words. A string
# rather than a new return code 3: 0/1/2 is the spelled-out vocabulary, and
# splitting `2` would invite a caller to read the new value as a failure.
# Without it, "there is no distro", "I cannot see the checkout" and "it ran for
# seven minutes and hung" all print the same skip line.
DELEGATE_NOTE=""

# The distro's own verdict on itself, or empty if it could not be asked.
# MSYS_NO_PATHCONV=1 on EVERY wsl.exe call: Git Bash rewrites arguments that
# look like absolute POSIX paths before a native binary sees them.
# BOUNDED, and not as belt-and-braces: this is also called AFTER a hang, and a
# probe of a wedged machine can wedge in turn (four `ss` probes once joined the
# D-state pile they were sent to measure).
wsl_health() {
  bounded "$BOUND_HEALTH" env MSYS_NO_PATHCONV=1 "$1" -d "$2" -u root -- \
    systemctl is-system-running 2>&1 | tr -d '\r'
}

wsl_delegate() {
  local distro wsl health winroot wslroot rc
  distro="${LOGALERT_WSL_DISTRO:-rlyeh-sandbox}"
  # A TEST SEAM, and the only reason it exists: the could-not-check branch cannot
  # be exercised otherwise -- reproducing it for real means breaking the sandbox,
  # and a PATH stub does not work (Git Bash will not PATH-resolve a non-PE file
  # named wsl.exe, while running one by full path works). It selects WHICH binary
  # is asked, never WHAT is asserted. It must never be used to make this pass.
  wsl="${LOGALERT_WSL_CMD:-wsl.exe}"

  if ! command -v "$wsl" >/dev/null 2>&1; then
    note "no $wsl on PATH -- cannot delegate"
    DELEGATE_NOTE="no $wsl on PATH"
    return 2
  fi

  # Bounded like every other wsl.exe call, and read through a file so the exit
  # code is wsl's rather than the pipeline's. `wsl -l -q` is UTF-16 with CRs on
  # some builds; strip both before matching.
  local listing
  listing="$(mktemp)"
  bounded "$BOUND_HEALTH" env MSYS_NO_PATHCONV=1 "$wsl" -l -q > "$listing" 2>/dev/null
  rc=$?
  if [ "$rc" -eq 124 ]; then
    rm -f "$listing"
    note "$wsl did not list distros within ${BOUND_HEALTH}s"
    DELEGATE_NOTE="$wsl did not answer"
    return 2
  fi
  if ! tr -d '\0\r' < "$listing" | grep -qx -- "$distro"; then
    rm -f "$listing"
    note "no WSL distro named '$distro' (set LOGALERT_WSL_DISTRO to choose another)"
    DELEGATE_NOTE="no WSL distro named '$distro'"
    return 2
  fi
  rm -f "$listing"

  # `degraded` is accepted deliberately: a distro with one failed unrelated unit
  # (the getty units fail on every WSL boot) is still usable.
  health="$(wsl_health "$wsl" "$distro")"
  case "$health" in
    running|degraded|starting) ;;
    *)
      # TWO DISTINCT FAULTS, and this arm is only one of them:
      #   lost dbus socket      systemd is PID 1 but every systemctl call fails   --shutdown ONLY
      #   D-state accumulation  the delegated run hangs; systemctl still answers  --terminate clears
      # `--shutdown` fixes both; `--terminate` is a half-fix and has PRODUCED the
      # dbus fault when run against the hang. Reported rather than done, either
      # way: shutdown restarts every distro on the machine, including the owner's.
      note "WSL distro '$distro' cannot reach systemd (systemctl: ${health:-no output})"
      note "      fix: wsl --shutdown, then re-run -- note that restarts ALL distros"
      DELEGATE_NOTE="the distro cannot reach systemd"
      return 2 ;;
  esac

  # Translate the checkout path. `wslpath` inside the distro is authoritative (it
  # honours a custom automount root); the sed is a fallback for a shell without
  # `pwd -W`.
  winroot="$(cd "$REPO_ROOT" 2>/dev/null && pwd -W 2>/dev/null)" || winroot=""
  wslroot=""
  if [ -n "$winroot" ]; then
    # Bounded; a 124 here yields an empty answer, falls to the sed guess, and is
    # caught by the checked `test -f` below rather than trusted.
    wslroot="$(bounded "$BOUND_HEALTH" env MSYS_NO_PATHCONV=1 "$wsl" -d "$distro" -u root -- wslpath -a "$winroot" 2>/dev/null | tr -d '\r')"
  fi
  [ -n "$wslroot" ] || wslroot="$(printf '%s' "$REPO_ROOT" | sed -E 's#^/([a-zA-Z])/#/mnt/\L\1/#')"

  # CHECKED rather than trusted: a wrong translation says so here instead of
  # surfacing as "No such file or directory" from inside the distro, which reads
  # like a missing script. Bounded, because `test -f` over a stale 9p mount is
  # exactly the operation that hangs, on a distro whose systemd still answers.
  bounded "$BOUND_HEALTH" env MSYS_NO_PATHCONV=1 "$wsl" -d "$distro" -u root -- test -f "$wslroot/$SELF_REL"
  rc=$?
  if [ "$rc" -eq 124 ]; then
    note "the distro did not answer a path probe within ${BOUND_HEALTH}s (stale mount?)"
    DELEGATE_NOTE="the distro did not answer a path probe"
    return 2
  fi
  if [ "$rc" -ne 0 ]; then
    note "cannot see this checkout from '$distro' at $wslroot"
    DELEGATE_NOTE="the distro cannot see this checkout"
    return 2
  fi

  note "delegating to WSL distro '$distro' at $wslroot"
  echo ""
  # The child is Linux, so it takes the native path below and never re-delegates.
  # MODE is passed through so `required` stays required on the far side.
  MSYS_NO_PATHCONV=1 bounded "$BOUND_DELEGATE" "$wsl" -d "$distro" -u root -- \
    env LOGALERT_CHECK_MODE="$MODE" bash "$wslroot/$SELF_REL"
  rc=$?
  echo ""
  # 124 is COULD-NOT-CHECK, not "the delegated run failed". Returning 1 here
  # would report a RED stage for a laptop problem; returning 2 falls through to
  # the ordinary skip, which `required` mode still turns into a failure.
  if [ "$rc" -eq 124 ]; then
    note "delegated run HUNG on '$distro' after ${BOUND_DELEGATE}s and was killed"
    # ASK THE DISTRO; DO NOT ASSUME. This branch once asserted a cause without
    # measuring it and was wrong five runs running. Naming a cause the evidence
    # cannot support sends the reader to the wrong subsystem with confidence.
    health="$(wsl_health "$wsl" "$distro")"
    case "$health" in
      running|degraded|starting)
        note "      the distro STILL ANSWERS (systemctl: $health) -- so the hang is INSIDE the run"
        note "      most likely processes stuck in D state, which SIGKILL cannot clear"
        note "      fix: wsl --terminate $distro (targeted), or wsl --shutdown (clears both faults)"
        DELEGATE_NOTE="the delegated run hung and was killed, on a distro that is still healthy" ;;
      *)
        note "      the distro STOPPED ANSWERING (systemctl: ${health:-no output}) -- it degraded during the run"
        note "      fix: wsl --shutdown, then re-run -- note that restarts ALL distros"
        DELEGATE_NOTE="the delegated run hung and the distro degraded during it" ;;
    esac
    return 2
  fi
  [ "$rc" -eq 0 ] && { note "delegated run GREEN on '$distro'"; return 0; }
  note "delegated run FAILED on '$distro' (exit $rc)"
  return 1
}

# -- platform ------------------------------------------------------------------
if [ "$(uname -s)" != "Linux" ]; then
  wsl_delegate
  case $? in
    0)
      # GREEN only if nothing earlier failed: under `required`, the top-level
      # `timeout` skip has already set FAILED, and a green run over unbounded
      # commands must not outrank it. Measured: without this test the stage
      # printed a FAIL line and then GREEN, exit 0.
      [ -z "$FAILED" ] && { echo "LINUX STAGE GREEN (delegated)"; exit 0; }
      echo "LINUX STAGE FAILED -$FAILED"; exit 1 ;;
    1) echo "LINUX STAGE FAILED (delegated)${FAILED:+ -$FAILED}"; exit 1 ;;
    *) ;;  # 2: could not delegate. Fall through to the ordinary skip.
  esac
  # The reason comes from wsl_delegate, the only thing that knows it. The
  # fallback is for the case where it never set one -- itself a bug worth seeing.
  skip "not Linux ($(uname -s)) and ${DELEGATE_NOTE:-no usable WSL distro} -- CI is the authority"
  echo ""
  [ -z "$FAILED" ] && { echo "LINUX STAGE SKIPPED"; exit 0; }
  echo "LINUX STAGE FAILED -$FAILED"; exit 1
fi

# -- privilege -----------------------------------------------------------------
# Installing a unit or a vhost needs root, but a gate is deliberately NOT run as
# root. Inside the sandbox the child already IS root (`-u root` above). On CI
# runners and a dev box configured for it, re-exec once under passwordless
# sudo; where it does not exist, the privileged checks skip and say why.
# `sudo -n` is non-interactive: a plain `sudo` would sit waiting for a password
# with no tty and look exactly like a hung gate. The env var is the recursion
# guard. Delete this block if run_native_checks() needs no privilege.
# `sudo -E` keeps the environment but NOT PATH: Ubuntu's `secure_path` replaces
# it even with -E (measured), so the venv-first PATH the gate insists on would
# be gone in here and any `python` below would be the system one. PATH is
# re-set through `env`; the LOGALERT_* variables survive on their own.
if [ "$(id -u)" -ne 0 ] && [ "${LOGALERT_SUDO_REEXEC:-}" != "1" ] && sudo -n true 2>/dev/null; then
  export LOGALERT_SUDO_REEXEC=1
  echo "  (re-executing under sudo for the privileged checks)"
  exec sudo -E env PATH="$PATH" bash "$0" "$@"
fi

# -- the native Linux path -----------------------------------------------------
# This runs on CI natively and inside the sandbox when delegated. Fill it in.
# Rules that earned their place:
#   * wrap every external binary in `bounded "$BOUND_CMD" ...`
#   * check `$? -eq 124` FIRST, before interpreting output: a killed command
#     produces no output, and "no output means clean" turns a hang into a pass
#   * a check that reads a file over /mnt/<drive> sees 0777 modes (9p); copy the
#     file into the distro's own filesystem (mktemp -d) before asking about modes
#   * assert the PROPERTY, not the environment: a long-lived sandbox may have a
#     leftover service on a port, so "503 or 200 proves proxying, 404 refutes it"
#     beats "expect 503"; and a standing skip must SAY it is expected, or it
#     sends a leftover-process hunt every run
#   * restore EVERYTHING you install or mutate, on EVERY exit path, via the trap
#     below -- on a long-lived sandbox a restore that runs only on the happy
#     path leaves the next run measuring this run's wreckage (two mutation
#     results once became false confirmations that way); a second
#     `trap ... EXIT` REPLACES the first, so extend cleanup() rather than
#     adding a trap; and never let a production installer call this stage --
#     a cleanup that is correct here takes a live site down there
#   * a wiring guard in the tests is what keeps "every binary is bounded" true
#     after the next edit: strip comments, match each binary only at a command
#     position, and require EVERY occurrence to be wrapped -- enumerate, never
#     `re.search`, because a search cannot answer an "all" question
# shellcheck disable=SC2329 # invoked by the EXIT trap, not by name
cleanup() {
  # Everything the checks create lives under $T; a run they left in the background is
  # killed first so the tree is not removed under it. Unconditional: on a long-lived
  # sandbox a restore that runs only on the happy path leaves the next run measuring
  # this run's wreckage.
  if [ -n "${BG_PID:-}" ]; then
    # the subshell's child is `timeout`, which forwards the signal down to the run (measured:
    # killing the subshell alone left the run to log 'state not saved' under the removed tree)
    pkill -TERM -P "$BG_PID" 2>/dev/null
    wait "$BG_PID" 2>/dev/null
  fi
  [ -n "${T:-}" ] && rm -rf "$T"
}
trap cleanup EXIT

# The checks (issue #14): properties of the INSTALLED script on a real Linux host, as the
# service user, over a fixture nobody else can see. Every assertion is keyed on $T so a
# leftover from an earlier session can neither satisfy nor break one; nothing on the host
# outside $T is touched (pip's cache included: PIP_CACHE_DIR points into $T), and the only
# residue is this run's records in the host's logs: the journal, the syslog file rsyslog
# mirrors it into, and runuser's session lines in auth.log.
T=""
BG_PID=""
SVC="${LOGALERT_STAGE_USER:-nobody}"   # the service user: exists everywhere, owns nothing
BOUND_BUILD="${LOGALERT_BOUND_BUILD:-240}"   # the wheel build and install: a cold pip cache fetches setuptools

# as_svc <tag> <args...>: the installed script as the service user, the fake's directory
# in its environment; stdout and stderr captured to $T/<tag>.out and $T/<tag>.err (the
# lock check runs two at once), the exit code returned.
as_svc() {
  local tag="$1"; shift
  bounded "$BOUND_CMD" runuser -u "$SVC" -- env LOGALERT_FAKE_DIR="$T/fake" "${SVC_ENV[@]}" \
    "$T/bin/logalert" "$@" > "$T/$tag.out" 2> "$T/$tag.err"
}
SVC_ENV=()
fake_calls() { find "$T/fake" -name 'call-*-argv.json' 2>/dev/null | wc -l; }
last_argv()  { find "$T/fake" -name 'call-*-argv.json' | sort | tail -1; }
# last_body: the latest mail's text, DECODED (a body with any line over 78 characters
# travels quoted-printable, where `==>` is `=3D=3D>`), into $T/body.txt
last_body() {
  bounded "$BOUND_CMD" "$T/venv/bin/python" "$T/body.py" "$(find "$T/fake" -name 'call-*-stdin.bin' | sort | tail -1)" > "$T/body.txt" 2>&1
}
# quiet <tag>: nothing on either stream
quiet() { [ ! -s "$T/$1.out" ] && [ ! -s "$T/$1.err" ]; }
# first_err <tag>: the first stderr line, for a failure message
first_err() { head -1 "$T/$1.err"; }

# check_journal: the journal side of the syslog check, in its own function so a skip
# (journalctl gone, hung or refusing) ends this check and not the ones after it.
check_journal() {
  local out rc journald=""
  if command -v journalctl >/dev/null 2>&1; then
    journald="$(bounded "$BOUND_CMD" systemctl is-active systemd-journald 2>/dev/null)"; rc=$?
    [ "$rc" -eq 124 ] && journald="no answer from systemctl within ${BOUND_CMD}s"
  fi
  if [ "$journald" = "active" ]; then
    # Every journalctl call is read through a file with its exit code FIRST: a query that
    # hangs or refuses must not be read as 'the journal holds 0 records' (a red against the
    # syslog handler) -- measured, a pipe into the filter did exactly that.
    bounded "$BOUND_CMD" journalctl --sync >/dev/null 2>&1; rc=$?
    if [ "$rc" -eq 124 ]; then
      skip "journalctl --sync did not finish within ${BOUND_CMD}s -- the journal side was not checked"; return
    fi
    bounded "$BOUND_CMD" journalctl -t logalert --no-pager -o json --since "-30min" > "$T/journal.jsonl" 2> "$T/journal.err"; rc=$?
    if [ "$rc" -eq 124 ]; then
      skip "journalctl did not answer within ${BOUND_CMD}s -- the journal side was not checked"; return
    elif [ "$rc" -ne 0 ]; then
      skip "journalctl exited $rc ($(head -1 "$T/journal.err")) -- the journal side was not checked"; return
    fi
    out="$(bounded "$BOUND_CMD" "$T/venv/bin/python" "$T/journal.py" "$T" "$(id -u "$SVC")" < "$T/journal.jsonl" 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ] || [ -z "$out" ]; then
      fail "the journal filter exited $rc: $out" "journal-filter"
    else
      set -- $out
      if [ "$1" -lt 4 ]; then
        fail "the journal holds $1 record(s) naming this run; the runs above should have left at least 4" "journal-records"
      elif [ "$2" -ne "$1" ]; then
        fail "$(( $1 - $2 )) of $1 journal records lack SYSLOG_IDENTIFIER=logalert or the service user's _UID" "journal-ident"
      elif [ "$3" -ne 0 ]; then
        fail "the --log stderr run left $3 record(s) in the journal" "journal-stderr"
      else
        ok "$1 journal records name this run, every one tagged logalert with _UID $(id -u "$SVC"); the --log stderr run left none"
      fi
    fi
  elif [ -r /var/log/syslog ]; then
    n="$(grep -c "logalert\[[0-9]*\]: .*$T/logalert.conf" /var/log/syslog)"
    if [ "$n" -lt 1 ]; then
      fail "/var/log/syslog holds no 'logalert[pid]:' line naming this run" "syslog-file"
    elif grep -q "logalert\[[0-9]*\]: .*$T/conf2/" /var/log/syslog; then
      fail "the --log stderr run left lines in /var/log/syslog" "syslog-stderr"
    else
      ok "$n /var/log/syslog lines carry logalert[pid] for this run; none from the --log stderr run"
    fi
  else
    skip "neither journald nor /var/log/syslog here -- the syslog side was not checked"
  fi
}

run_native_checks() {
  local out rc pyx version want shebang before after body argv n lines
  T="$(mktemp -d)"; rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$T" ] || [ ! -d "$T" ]; then
    # every path below is "$T/..."; an empty T would lay the tree out at / as root
    T=""
    skip "mktemp -d failed (TMPDIR=${TMPDIR:-unset}) -- no private tree to work in"; return
  fi
  # The caller's umask is not ours: sudo carries a hardened 027 or 077 into the re-exec, and
  # everything below must be readable (the venv, the confs, the fixture) and the console
  # script executable by $SVC. mktemp gives 0700 whatever the umask.
  umask 022
  chmod 755 "$T"
  mkdir -p "$T/bin" "$T/logs" "$T/fake" "$T/conf2"
  # Two helpers the checks run through the temp venv's interpreter: the latest mail's
  # text decoded, and the offset the state file holds for one file.
  cat > "$T/body.py" <<'EOF'
import email, email.policy, sys
msg = email.message_from_binary_file(open(sys.argv[1], "rb"), policy=email.policy.default)
for part in msg.walk():
    if part.get_content_type() == "text/plain":
        sys.stdout.write(part.get_content())
EOF
  cat > "$T/offset.py" <<'EOF'
import json, sys
state = json.load(open(sys.argv[1], encoding="utf-8"))
entry = state["entries"].get(sys.argv[2], {}).get(sys.argv[3])
print(entry["offset"] if entry else -1)
EOF

  echo "-- preconditions"
  # A VERSIONED interpreter is INSTALL.md's rule for the venv; python3 names its own minor.
  pyx="$(bounded "$BOUND_CMD" python3 -c 'import sys; print("python3.%d" % sys.version_info[1])' 2> "$T/python3.err")"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "python3 did not answer within ${BOUND_CMD}s -- no interpreter to build with"; return
  fi
  if [ -z "$pyx" ]; then
    # an interpreter that starts and dies (a loader error once sudo has dropped LD_LIBRARY_PATH)
    # is not a PATH problem; say what it said
    skip "python3 exited $rc without naming its minor: $(head -1 "$T/python3.err")"; return
  fi
  if ! command -v "$pyx" >/dev/null 2>&1; then
    skip "no versioned interpreter $pyx on PATH -- cannot create the venv INSTALL.md prescribes"; return
  fi
  local missing=""
  for tool in runuser logrotate gzip; do
    command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
  done
  if [ -n "$missing" ]; then
    skip "missing on this host:$missing -- the service-user, rotation and lock checks need them"; return
  fi
  if ! id "$SVC" >/dev/null 2>&1; then
    skip "no user '$SVC' on this host (set LOGALERT_STAGE_USER)"; return
  fi
  ok "$pyx, runuser, logrotate, gzip and user $SVC are here"

  echo "-- install: the wheel from the checkout into a versioned temp venv, the symlink"
  # The checkout is read-only in the sandbox (9p) and pip builds in-tree: copy the source
  # first. The copy is the one operation that reads /mnt, so it is bounded like a probe.
  mkdir -p "$T/src"
  bounded "$BOUND_CMD" cp -r "$REPO_ROOT/logalert" "$REPO_ROOT/pyproject.toml" "$REPO_ROOT/README.md" \
    "$REPO_ROOT/INSTALL.md" "$REPO_ROOT/CHANGELOG.md" "$REPO_ROOT/LICENSE" "$REPO_ROOT/MANIFEST.in" \
    "$REPO_ROOT/tests/fake_sendmail.py" "$T/src/" 2>&1; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "copying the checkout did not finish within ${BOUND_CMD}s (stale mount?)"; return
  elif [ "$rc" -ne 0 ]; then
    fail "copying the checkout exited $rc" "copy"; return
  fi
  find "$T/src" -name __pycache__ -type d -prune -exec rm -rf {} +
  out="$(bounded "$BOUND_BUILD" "$pyx" -m venv "$T/venv" 2>&1)"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "$pyx -m venv did not finish within ${BOUND_BUILD}s"; return
  elif [ "$rc" -ne 0 ]; then
    fail "$pyx -m venv exited $rc: $out" "venv"; return
  fi
  # The venv's own pip builds the wheel with what the checkout says (setuptools>=77, an
  # isolated build, so this needs the network once) -- the way pip install would on the box.
  out="$(bounded "$BOUND_BUILD" env PIP_CACHE_DIR="$T/pipcache" "$T/venv/bin/python" -m pip wheel -q --no-deps \
    -w "$T/dist" "$T/src" 2>&1)"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "the wheel build did not finish within ${BOUND_BUILD}s (no network for setuptools?)"; return
  elif [ "$rc" -ne 0 ] && printf '%s' "$out" | grep -q -e "Could not find a version that satisfies the requirement setuptools" \
      -e "Failed to establish a new connection" -e "NewConnectionError" -e "Temporary failure in name resolution"; then
    # pip said no about the network, not about the checkout: could-not-check, never a pass
    skip "the isolated build could not fetch setuptools (no network?): $(printf '%s' "$out" | tail -1 | head -c 120)"; return
  elif [ "$rc" -ne 0 ]; then
    fail "the wheel build exited $rc: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')" "wheel"; return
  fi
  out="$(bounded "$BOUND_BUILD" env PIP_CACHE_DIR="$T/pipcache" "$T/venv/bin/python" -m pip install -q --no-deps \
    "$T"/dist/logalert-*.whl 2>&1)"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "pip install did not finish within ${BOUND_BUILD}s"; return
  elif [ "$rc" -ne 0 ]; then
    fail "pip install exited $rc: $(printf '%s' "$out" | tail -3 | tr '\n' ' ')" "install"; return
  fi
  ln -s "$T/venv/bin/logalert" "$T/bin/logalert"
  want="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$T/src/pyproject.toml")"
  version="$(bounded "$BOUND_CMD" "$T/bin/logalert" --version 2>&1)"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "logalert --version did not answer within ${BOUND_CMD}s"; return
  elif [ "$version" != "logalert $want" ]; then
    fail "--version through the symlink printed '$version', pyproject.toml says '$want'" "version"
  else
    ok "logalert --version == pyproject.toml ($want) through the symlink"
  fi
  shebang="$(head -1 "$T/venv/bin/logalert")"
  case "$shebang" in
    "#!$T/venv/bin/python"*) ok "the console script's shebang is the venv interpreter ($shebang)" ;;
    *) fail "the console script's shebang is '$shebang', not the venv's interpreter" "shebang" ;;
  esac

  echo "-- service user: a run as $SVC over a fixture log, quiet on exit 0, one mail per match"
  # The fake MTA (tests/fake_sendmail.py) under the venv's shebang; a config the service
  # user can read over a log it can read; a state directory it owns.
  { printf '#!%s\n' "$T/venv/bin/python"; cat "$T/src/fake_sendmail.py"; } > "$T/bin/sendmail"
  chmod 755 "$T/bin/sendmail"
  chown "$SVC" "$T/fake"
  install -d -m 750 -o "$SVC" "$T/state"
  # a line that MATCHES sits in the file before the first run: first sight starts at the
  # end, so it must never be mailed (a run from the beginning would mail it)
  printf 'boot\ndisk failure zero\nquiet\n' > "$T/logs/router.log"
  chmod 644 "$T/logs/router.log"
  cat > "$T/logalert.conf" <<EOF
[logalert]
sendmail_path = $T/bin/sendmail
state_file = $T/state/state.json
from = alerts@example.net
[router-disk]
subject = Router disk failure
to = noc@example.net
files = $T/logs/router.log
patterns =
    disk failure
EOF
  chmod 644 "$T/logalert.conf"
  as_svc r1 -f "$T/logalert.conf"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "the first run did not finish within ${BOUND_CMD}s"; return
  elif [ "$rc" -ne 0 ] || ! quiet r1; then
    fail "the first run (first sight) exited $rc with $(wc -c < "$T/r1.out")/$(wc -c < "$T/r1.err") bytes on stdout/stderr: $(first_err r1)" "first-sight"
  elif [ "$(fake_calls)" -ne 0 ]; then
    fail "the first run mailed $(fake_calls) time(s); first sight starts at the end" "first-sight"
  else
    ok "first sight: exit 0, nothing on either stream, no mail"
  fi
  echo "disk failure one" >> "$T/logs/router.log"
  as_svc r2 -f "$T/logalert.conf"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "the second run did not finish within ${BOUND_CMD}s"; return
  elif [ "$rc" -ne 0 ] || ! quiet r2; then
    fail "the run with a match exited $rc with $(wc -c < "$T/r2.out")/$(wc -c < "$T/r2.err") bytes on stdout/stderr: $(first_err r2)" "match-run"
  elif [ "$(fake_calls)" -ne 1 ]; then
    fail "the run with a match made $(fake_calls) sendmail call(s), not 1" "match-run"
  else
    argv="$(last_argv)"
    if ! grep -q '"alerts@example.net"' "$argv" || ! grep -q '"noc@example.net"' "$argv"; then
      fail "the envelope is not from alerts@example.net to noc@example.net: $(tr -d '\n' < "$argv" | head -c 300)" "envelope"
    elif ! last_body || ! grep -q 'disk failure one' "$T/body.txt"; then
      fail "the mail does not carry the matching line: $(head -c 200 "$T/body.txt" | tr '\n' '|')" "mail-body"
    else
      ok "a match: exit 0, nothing on either stream, one mail from alerts@example.net to noc@example.net with the line"
    fi
  fi
  out="$(bounded "$BOUND_CMD" "$T/venv/bin/python" "$T/offset.py" "$T/state/state.json" router-disk "$T/logs/router.log" 2>&1)"; rc=$?
  if [ "$rc" -ne 0 ]; then
    fail "the state file could not be read: $out" "state-read"
  elif [ "$out" != "$(wc -c < "$T/logs/router.log")" ]; then
    fail "the state's offset for router.log is $out, the file is $(wc -c < "$T/logs/router.log") bytes" "state-advance"
  else
    ok "the state advanced to the end of the file ($out bytes)"
  fi

  echo "-- rotation: a real logrotate -f with compress between two matching lines"
  cat > "$T/logrotate.conf" <<EOF
$T/logs/router.log {
    rotate 3
    compress
    missingok
}
EOF
  chmod 644 "$T/logrotate.conf"
  echo "disk failure before rotation" >> "$T/logs/router.log"
  out="$(bounded "$BOUND_CMD" logrotate -f -s "$T/logrotate.state" "$T/logrotate.conf" 2>&1)"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "logrotate did not finish within ${BOUND_CMD}s"; return
  elif [ "$rc" -ne 0 ] || [ ! -f "$T/logs/router.log.1.gz" ]; then
    fail "logrotate exited $rc and left: $(ls "$T/logs" | tr '\n' ' ') -- $out" "logrotate"; return
  fi
  echo "disk failure after rotation" >> "$T/logs/router.log"
  chmod 644 "$T/logs/router.log"   # logrotate recreated it under our umask; be explicit
  before="$(fake_calls)"
  as_svc r3 -f "$T/logalert.conf"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "the run after the rotation did not finish within ${BOUND_CMD}s"; return
  fi
  last_body
  body="$T/body.txt"
  if [ "$rc" -ne 0 ] || ! quiet r3; then
    fail "the run after the rotation exited $rc with output: $(first_err r3)" "rotation-run"
  elif [ "$(( $(fake_calls) - before ))" -ne 1 ]; then
    fail "the run after the rotation made $(( $(fake_calls) - before )) mail(s), not 1" "rotation-run"
  elif [ ! -s "$body" ]; then
    fail "the mail after the rotation could not be decoded" "rotation-body"
  elif [ "$(grep -c 'disk failure before rotation' "$body")" -ne 1 ] || [ "$(grep -c 'disk failure after rotation' "$body")" -ne 1 ] \
      || [ "$(grep -c 'disk failure one' "$body")" -ne 0 ]; then
    fail "the mail after the rotation carries the pre-rotation line $(grep -c 'disk failure before rotation' "$body") time(s), the post-rotation line $(grep -c 'disk failure after rotation' "$body") time(s) and the line mailed BEFORE the rotation $(grep -c 'disk failure one' "$body") time(s); once, once and never is the contract" "rotation-lines"
  elif [ "$(grep -n '^==> ' "$body" | head -1 | grep -c 'router.log.1.gz')" -ne 1 ]; then
    fail "the report does not open with the rotated copy: $(grep '^==> ' "$body" | tr '\n' ' ')" "rotation-order"
  else
    ok "both lines mailed once, the rotated copy read before the live file"
  fi

  echo "-- lock: two runs at once, the second turned away quietly"
  echo "disk failure race" >> "$T/logs/router.log"
  before="$(fake_calls)"
  # A holds the lock while its fake sendmail sleeps; B is launched once the fake has recorded
  # the call (so A is inside its delivery, not a fixed sleep's guess), with its own log so
  # the turn-away is OBSERVED rather than inferred from one mail and two quiet exits, which
  # an uncontended pair would also produce
  SVC_ENV=(LOGALERT_FAKE_SLEEP=3)
  ( as_svc lockA -f "$T/logalert.conf"; echo "$?" > "$T/rcA" ) &
  BG_PID=$!
  SVC_ENV=()
  n=0
  while [ "$(fake_calls)" -le "$before" ] && [ "$n" -lt 100 ]; do sleep 0.1; n=$((n + 1)); done
  as_svc lockB -f "$T/logalert.conf" --log "file:$T/fake/lockB.log"; rc=$?
  wait "$BG_PID"
  BG_PID=""
  if [ "$n" -ge 100 ]; then
    skip "the lock holder never reached its delivery within 10 s -- the lock was not exercised"
  elif [ "$rc" -eq 124 ] || [ "$(cat "$T/rcA")" = "124" ]; then
    skip "a run in the lock check did not finish within ${BOUND_CMD}s"
  elif [ "$rc" -ne 0 ] || ! quiet lockB; then
    fail "the second run exited $rc with output: $(first_err lockB)" "lock-second"
  elif ! grep -q 'this run exits quietly' "$T/fake/lockB.log" 2>/dev/null; then
    skip "the two runs did not overlap (no turn-away in the second run's log) -- the lock was not exercised"
  elif [ "$(cat "$T/rcA")" -ne 0 ] || ! quiet lockA; then
    fail "the first run (the lock holder) exited $(cat "$T/rcA"): $(first_err lockA)" "lock-first"
  elif [ "$(( $(fake_calls) - before ))" -ne 1 ]; then
    fail "the two runs together mailed $(( $(fake_calls) - before )) time(s), not 1" "lock-mail"
  else
    ok "the holder mailed once; the second run, launched during that delivery, was turned away: exit 0, nothing on either stream, the holder named in its log"
  fi

  echo "-- syslog: the identifier in the journal, nothing there under --log stderr"
  cat "$T/logalert.conf" > "$T/conf2/logalert.conf"
  as_svc stderr -f "$T/conf2/logalert.conf" --log stderr; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "the --log stderr run did not finish within ${BOUND_CMD}s"; return
  elif [ "$rc" -ne 0 ] || ! grep -q "^logalert: start: $T/conf2/logalert.conf" "$T/stderr.err"; then
    fail "the --log stderr run exited $rc; stderr: $(first_err stderr)" "log-stderr"
  else
    ok "--log stderr: the records are on stderr"
  fi
  cat > "$T/journal.py" <<'EOF'
import json, sys
key, uid, tag = sys.argv[1], sys.argv[2], 0
named = other = 0
for line in sys.stdin:
    r = json.loads(line)
    m = r.get("MESSAGE")
    if not isinstance(m, str) or key not in m:
        continue
    if "/conf2/" in m:
        other += 1
        continue
    named += 1
    if r.get("SYSLOG_IDENTIFIER") == "logalert" and str(r.get("_UID")) == uid:
        tag += 1
print(named, tag, other)
EOF
  check_journal

  echo "-- permission: a 0600 root-owned log in a section, the readable sibling still processed"
  printf 'up\n' > "$T/logs/fw.log"
  chmod 644 "$T/logs/fw.log"
  printf 'secret DENY\n' > "$T/logs/root-only.log"
  chmod 600 "$T/logs/root-only.log"
  cat >> "$T/logalert.conf" <<EOF
[firewall]
subject = Firewall denies
to = noc@example.net
files =
    $T/logs/fw.log
    $T/logs/root-only.log
patterns =
    DENY
EOF
  as_svc p1 -f "$T/logalert.conf"; rc=$?   # first sight of fw.log; the root-only file fails
  if [ "$rc" -eq 124 ]; then
    skip "the permission run did not finish within ${BOUND_CMD}s"; return
  fi
  echo "DENY 192.0.2.9" >> "$T/logs/fw.log"
  before="$(fake_calls)"
  as_svc p2 -f "$T/logalert.conf"; rc=$?
  lines="$(wc -l < "$T/p2.err")"
  if [ "$rc" -eq 124 ]; then
    skip "the permission run did not finish within ${BOUND_CMD}s"; return
  elif [ "$rc" -ne 1 ]; then
    fail "a section with an unreadable file exited $rc, not 1: $(first_err p2)" "permission-exit"
  elif [ "$lines" -ne 1 ] || [ -s "$T/p2.out" ] || ! grep -q "root-only.log: Permission denied" "$T/p2.err"; then
    fail "expected exactly one stderr line naming root-only.log: got $lines line(s): $(head -2 "$T/p2.err" | tr '\n' '|')" "permission-line"
  elif [ "$(( $(fake_calls) - before ))" -ne 1 ] || ! last_body || ! grep -q 'DENY 192.0.2.9' "$T/body.txt"; then
    fail "the readable sibling's match was not mailed ($(( $(fake_calls) - before )) mail(s))" "permission-sibling"
  else
    out="$(bounded "$BOUND_CMD" "$T/venv/bin/python" "$T/offset.py" "$T/state/state.json" firewall "$T/logs/fw.log" 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ]; then
      fail "the state file could not be read: $out" "permission-state"
    elif [ "$out" != "$(wc -c < "$T/logs/fw.log")" ]; then
      fail "the readable sibling's state did not advance (offset $out)" "permission-state"
    else
      ok "exit 1, one stderr line naming the unreadable file, the sibling mailed and its state advanced"
    fi
  fi

  echo "-- modes: the state file and the lock the service user left behind"
  local st lk dr
  st="$(stat -c '%a %U' "$T/state/state.json" 2>/dev/null)"
  lk="$(stat -c '%a %U' "$T/state/lock" 2>/dev/null)"
  dr="$(stat -c '%a %U' "$T/state" 2>/dev/null)"
  if [ "$st" != "600 $SVC" ]; then
    fail "the state file is '$st', not '600 $SVC'" "mode-state"
  elif [ "$lk" != "644 $SVC" ]; then
    fail "the lock is '$lk', not '644 $SVC'" "mode-lock"
  elif [ "$dr" != "750 $SVC" ]; then
    fail "the state directory is '$dr' after the runs, not the '750 $SVC' it was created with" "mode-dir"
  else
    ok "state file 600, lock 644, directory 750, all owned by $SVC"
  fi
}

run_native_checks
echo ""
[ -z "$FAILED" ] && { echo "LINUX STAGE GREEN"; exit 0; }
echo "LINUX STAGE FAILED -$FAILED"; exit 1
