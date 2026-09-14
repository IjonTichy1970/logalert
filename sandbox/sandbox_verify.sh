#!/usr/bin/env bash
# Verify a distro is (a) usable and (b) genuinely unable -- or able -- to write to Windows.
#
# Usage:  bash sandbox_verify.sh --expect ro|rw <checkout-in-distro> <windows-home-in-distro>
#   e.g.  bash sandbox_verify.sh --expect ro /mnt/d/Projects/Logalert /mnt/c/Users/me
#
# THE PROOF IS A DISCRIMINATING PAIR, and one run cannot be it. Run this TWICE:
#   in the sandbox   with --expect ro   every write must be REFUSED with "Read-only file system"
#   in the owner's   with --expect rw   the identical write must SUCCEED
# A read-only mount that silently still allowed writes would look identical to
# a working one until the day it mattered -- and a "refusal" that is really
# ENOENT (the directory does not exist) looks identical to a real one. So every
# target's parent directory must exist, and the refusal's errno text is read.
set -u

# Positive control FIRST: three shell layers sit between the caller and this
# line, and a mangled harness has returned believable wrong numbers before.
bash -c 'exit 3'; rc=$?
if [ "$rc" -ne 3 ]; then
  echo "HARNESS BROKEN: positive control returned $rc, not 3 -- do not trust anything below"
  exit 2
fi

if [ "${1:-}" != "--expect" ] || [ $# -ne 4 ]; then
  echo "usage: $0 --expect ro|rw <checkout-in-distro> <windows-home-in-distro>" >&2
  exit 2
fi
expect="$2"; checkout="$3"; winhome="$4"
case "$expect" in ro|rw) ;; *) echo "usage: --expect ro|rw" >&2; exit 2 ;; esac

pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "===== identity and init"
echo "  user:  $(id -un) (uid $(id -u))"
echo "  pid1:  $(ps -p 1 -o comm= 2>/dev/null)"
# shellcheck disable=SC1091 # /etc/os-release is the distro's, not ours to lint
echo "  os:    $(. /etc/os-release && echo "$PRETTY_NAME")"
echo "  systemctl: $(systemctl is-system-running 2>&1 | head -1)"
echo "  expecting Windows mounts to be: $expect"

echo "===== mount options"
findmnt -t 9p,drvfs -no TARGET,OPTIONS 2>/dev/null | grep '^/mnt/' || echo "  (no /mnt entries)"

echo "===== 1. Windows must be READABLE"
if [ -d "$checkout" ] && ls "$checkout" >/dev/null 2>&1; then
  ok "can list the checkout over $checkout"
else
  bad "cannot list $checkout -- wrong path, or the drive is not mounted"
fi

echo "===== 2. Windows writes must be REFUSED (ro) / must SUCCEED (rw)"
for target in \
  "$checkout/.sandbox-write-probe" \
  "$winhome/.sandbox-write-probe" \
  "$winhome/.claude/.sandbox-write-probe"
do
  parent="$(dirname "$target")"
  if [ ! -d "$parent" ]; then
    # A refusal here would be ENOENT, which prints exactly like EROFS. Refuse to count it.
    bad "$parent does not exist -- a refusal would be ENOENT, not read-only; fix the path"
    continue
  fi
  err="$(touch "$target" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    rm -f "$target" 2>/dev/null
    if [ "$expect" = rw ]; then ok "wrote and removed: $target"; else bad "WROTE to $target -- the read-only mount is NOT holding"; fi
  else
    case "$err" in
      *"Read-only file system"*)
        if [ "$expect" = ro ]; then ok "write refused (Read-only file system): $target"; else bad "refused where it should succeed: $target ($err)"; fi ;;
      *)
        bad "refused for the WRONG reason: $target -- $err" ;;
    esac
  fi
done

echo "===== 3. the distro's own filesystem must stay writable"
for target in /tmp/.probe /opt/.probe /etc/.probe; do
  if touch "$target" 2>/dev/null; then
    ok "writable: $target"
    rm -f "$target"
  else
    bad "NOT writable: $target -- unusable for staging"
  fi
done

echo "===== 4. outbound network (needed for apt)"
if getent hosts archive.ubuntu.com >/dev/null 2>&1; then
  ok "DNS resolves archive.ubuntu.com"
else
  bad "no DNS -- apt will fail"
fi

echo "====="
echo "PASS=$pass FAIL=$fail (expected Windows mounts: $expect)"
[ "$fail" -eq 0 ] || exit 1
