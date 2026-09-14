#!/usr/bin/env bash
# Sandbox preflight -- run FIRST in any session that uses the sandbox distro.
#
# WSL can come back wonky after the laptop sleeps. The failure modes are
# quiet: a skewed clock, stale 9p mounts, dead DNS, or units left in a failed
# state from last time. Each of those makes a test result mean something other
# than what it appears to mean.
#
# The clock check matters most for anything time-sensitive (TLS, apt, any code
# that validates timestamps) -- and an agent cannot perceive elapsed time at
# all, so skew is exactly the fault it has no intuition for. It gets measured,
# never assumed.
#
# Usage:  bash sandbox_preflight.sh <host-utc-epoch-seconds>
#
# From Git Bash, in one line (GNU date's %s is a UTC epoch, correctly):
#     MSYS_NO_PATHCONV=1 timeout 90 wsl.exe -d rlyeh-sandbox -u root -- bash <path>/sandbox_preflight.sh "$(date -u +%s)"
# From PowerShell, get the epoch with:
#     [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
# NOT with `Get-Date -UFormat %s`, which in Windows PowerShell 5.1 returns an
# epoch computed from LOCAL time. On a UTC-6 box that reports a phantom 21600s
# skew against a perfectly correct clock -- the first run of this script
# "found" exactly that, and the clock was fine.
set -u

# Positive control FIRST. Three shell layers sit between the caller and this
# line (Git Bash -> wsl.exe -> bash); a mangled harness has returned believable
# wrong numbers before. If this does not read 3, nothing below is evidence.
bash -c 'exit 3'; rc=$?
if [ "$rc" -ne 3 ]; then
  echo "HARNESS BROKEN: positive control returned $rc, not 3 -- do not trust anything below"
  exit 2
fi

DISTRO_NAME="${LOGALERT_WSL_DISTRO:-rlyeh-sandbox}"   # the template's variable; only used in the printed remedies
host_epoch="${1:-}"
warn=0

echo "===== clock"
guest_epoch="$(date -u +%s)"
echo "  guest UTC: $(date -u '+%Y-%m-%d %H:%M:%S')"
if [ -z "$host_epoch" ]; then
  echo "  SKIP  no host epoch passed -- cannot detect skew"
  warn=$((warn+1))
else
  skew=$(( guest_epoch - host_epoch ))
  abs=${skew#-}
  echo "  host UTC epoch: $host_epoch / guest: $guest_epoch / skew: ${skew}s"
  if [ "$abs" -gt 5 ]; then
    echo "  WARN  clock skew ${skew}s -- apt/TLS and any time-sensitive test are unreliable."
    echo "        fix: hwclock -s   (or terminate and restart the distro)"
    echo "        if the skew equals your timezone offset, the INSTRUMENT is wrong, not the clock"
    warn=$((warn+1))
  else
    echo "  OK    within 5s"
  fi
fi

echo "===== Windows mounts (must be present AND read-only)"
# Derived from the mount table, not a hardcoded drive list: a box with only C:
# would otherwise warn forever about a D: it never had. 9p is WSL2's DrvFs
# transport; `drvfs` covers older configurations.
mounts="$(findmnt -t 9p,drvfs -no TARGET 2>/dev/null | grep '^/mnt/' || true)"
if [ -z "$mounts" ]; then
  echo "  WARN  no Windows drive is mounted under /mnt -- stale after resume, or automount is off"
  warn=$((warn+1))
fi
for m in $mounts; do
  opts="$(findmnt -no OPTIONS "$m" 2>/dev/null || true)"
  if [ "${opts%%,*}" = "ro" ]; then
    echo "  OK    $m ro"
  else
    echo "  WARN  $m is ${opts%%,*} -- expected ro; the sandbox can write to Windows"
    warn=$((warn+1))
  fi
done

echo "===== dns / network"
if getent hosts archive.ubuntu.com >/dev/null 2>&1; then
  echo "  OK    resolves archive.ubuntu.com"
else
  echo "  WARN  DNS broken -- classic post-resume symptom; apt will fail"
  warn=$((warn+1))
fi

echo "===== systemd"
state="$(systemctl is-system-running 2>&1 | head -1)"
echo "  is-system-running: ${state:-no output}"
# ONLY the three healthy answers pass; anything else -- an error, an unknown
# state, NO OUTPUT -- is "systemd is not reachable". The first version of this
# check counted failed units instead, and `systemctl --failed` returns nothing
# when the bus is down, which is byte-identical to "no failed units": it printed
# PREFLIGHT CLEAN while nothing systemd-related could work.
#
# `degraded` is accepted: it means some unit failed (the getty units fail on
# every WSL boot), and the failed-units check below says which.
#
# The unhealthy arms name a cause ONLY where the answer's text supports one. A
# bus error is the lost-dbus-socket fault; `offline` or "not been booted" means
# systemd is not PID 1 at all (a wsl.conf problem, not a WSL problem); the
# transitional states may need nothing but a few seconds. Everything else is
# reported as measured, with no diagnosis: naming a cause the evidence cannot
# support sends the reader to the wrong subsystem with confidence.
case "$state" in
  running|degraded|starting) ;;
  *"Failed to connect to bus"*|*"Failed to get D-Bus"*)
    echo "  WARN  systemd is NOT REACHABLE -- every systemd result below is meaningless."
    echo "        This is the lost-dbus-socket fault. wsl --terminate does NOT clear it;"
    echo "        only wsl --shutdown does -- and that restarts EVERY distro on the machine,"
    echo "        so it is the owner's call. Ask."
    warn=$((warn+1))
    ;;
  offline|*"not been booted"*)
    echo "  WARN  systemd is not PID 1 -- every systemd result below is meaningless."
    echo "        Check [boot] systemd=true in /etc/wsl.conf, then wsl --terminate $DISTRO_NAME."
    warn=$((warn+1))
    ;;
  initializing|maintenance|stopping)
    echo "  WARN  systemd is '$state' -- still booting or shutting down; wait and re-run."
    echo "        If it persists: wsl --terminate $DISTRO_NAME."
    warn=$((warn+1))
    ;;
  *)
    echo "  WARN  systemd answered '${state:-no output}' -- not reachable; cause NOT determined."
    echo "        Probe before repairing: ls -l /run/dbus/system_bus_socket; systemctl status dbus."
    warn=$((warn+1))
    ;;
esac
# console-getty and getty@tty1 fail on EVERY WSL boot -- there is no real
# console to attach to. They are excluded by name rather than by silencing the
# check, because a check that always warns is one you learn to ignore, and then
# it does not report the failure that mattered. Listed explicitly so a change in
# the exclusion set stays visible.
benign='console-getty\.service|getty@tty1\.service'
all_failed="$(systemctl --failed --no-legend --plain 2>/dev/null | awk '{print $1}')"
real_failed="$(echo "$all_failed" | grep -vE "^($benign)$" | grep -v '^$' || true)"

echo "  excluded as always-failing under WSL: console-getty.service, getty@tty1.service"
if [ -n "$real_failed" ]; then
  echo "  WARN  failed unit(s) beyond the known-benign set:"
  while IFS= read -r unit; do echo "        $unit"; done <<< "$real_failed"
  warn=$((warn+1))
else
  echo "  OK    no unexpected failed units"
fi

echo "====="
if [ "$warn" -eq 0 ]; then
  echo "PREFLIGHT CLEAN"
else
  echo "PREFLIGHT: $warn warning(s) -- do NOT trust results until resolved."
  echo "Clock, mount and DNS warnings: wsl --terminate $DISTRO_NAME, then reconnect."
  echo "A bus-failure warning: wsl --shutdown (ask the owner); terminate does not clear it."
  echo "Any other systemd warning carries its own remedy above; do not shut down on a guess."
  exit 1
fi
