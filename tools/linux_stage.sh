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
  :  # undo every mutation run_native_checks() makes -- unconditionally
}
trap cleanup EXIT

run_native_checks() {
  echo "-- example check"
  # Replace this block. It exists so the template can be run end to end as-is.
  local out rc pid1
  out="$(bounded "$BOUND_CMD" uname -sr 2>&1)"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "uname did not finish within ${BOUND_CMD}s -- the kernel was not asked"
  elif [ "$rc" -ne 0 ]; then
    fail "uname exited $rc: $out" "uname"
  else
    ok "running natively on: $out"
  fi

  echo "-- systemd is PID 1"
  # Bounded like the check above, and 124 read FIRST: a killed `ps` prints
  # nothing, and "not systemd" would be the wrong reading of no answer.
  pid1="$(bounded "$BOUND_CMD" ps -p 1 -o comm= 2>/dev/null)"; rc=$?
  if [ "$rc" -eq 124 ]; then
    skip "ps did not finish within ${BOUND_CMD}s -- PID 1 was not asked"
  elif [ "$pid1" = "systemd" ]; then
    ok "systemd is PID 1 -- unit checks are possible here"
  else
    skip "systemd is not PID 1 (${pid1:-no answer}) -- cannot start a unit (wsl.conf needs [boot] systemd=true)"
  fi
}

run_native_checks
echo ""
[ -z "$FAILED" ] && { echo "LINUX STAGE GREEN"; exit 0; }
echo "LINUX STAGE FAILED -$FAILED"; exit 1
