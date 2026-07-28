#!/bin/bash
# ============================================================================
# backup-offsite.sh -- copy the newest local backups to encrypted cloud storage.
#
# WHY THIS EXISTS: until 2026-07-28 every backup lived on D:\Backups, a second
# disk in the SAME machine as the data it protects. That defends against one
# disk dying and nothing else -- fire, theft, ransomware, a failed PSU or a bad
# Windows update takes both copies at once. The 105 Immich photos lost earlier
# that day were unrecoverable for exactly this class of reason: there was no
# older, separate copy to go back to.
#
# This is the "1" in 3-2-1: three copies, two media, ONE offsite.
#
# ENCRYPTION IS NOT OPTIONAL HERE. bind-Vaultwarden contains every password in
# the household and bind-Immich contains the family photo library. Both go to
# a provider that must never be able to read them, so the remote is an rclone
# "crypt" remote layered over the storage remote -- filenames and contents are
# encrypted locally before a single byte leaves the machine.
#
# ---------------------------------------------------------------------------
# ONE-TIME SETUP (needs your cloud account; cannot be scripted unattended)
#
#   1. rclone config
#        n) New remote -> name: b2raw   (or gdriveraw)
#           Backblaze B2  : needs Key ID + Application Key from the B2 console
#           Google Drive  : opens a browser to authorise
#
#   2. rclone config
#        n) New remote -> name: offsite
#           Storage: crypt
#           remote: b2raw:homelab-backups     <- bucket/folder in step 1
#           Encrypt filenames: standard
#           Set a STRONG password. WRITE IT DOWN SOMEWHERE THAT IS NOT THIS
#           MACHINE. Losing it means the offsite copy is unreadable, which
#           makes it worth nothing precisely when you need it.
#
#   3. bash backup-offsite.sh --test
#
# Free tiers as of 2026-07: Backblaze B2 10GB, Google Drive 15GB, OneDrive 5GB.
# One full local set is ~741MB, so KEEP_DAYS=7 lands near 5GB and fits any of
# them without paying.
# ============================================================================

set -uo pipefail

REMOTE="${OFFSITE_REMOTE:-offsite:}"
SRC="${OFFSITE_SRC:-/d/Backups}"
KEEP_DAYS="${OFFSITE_KEEP_DAYS:-7}"
LOG="${OFFSITE_LOG:-/c/ServerData/Stacks/backup-offsite.log}"
STAMP="${OFFSITE_STAMP:-/c/ServerData/Stacks/.offsite-last-success}"

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG"; }

# winget installs rclone to a shim under LOCALAPPDATA and adds it to PATH, but
# a shell started before the install (and Task Scheduler, which does not
# inherit an interactive PATH) will not see it. Check the known locations
# explicitly rather than relying on PATH.
RCLONE=""
for cand in \
  "rclone" \
  "/c/Users/$USERNAME/AppData/Local/Microsoft/WinGet/Links/rclone.exe" \
  "/c/Program Files/Rclone/rclone.exe" ; do
  if command -v "$cand" >/dev/null 2>&1 || [ -x "$cand" ]; then RCLONE="$cand"; break; fi
done
if [ -z "$RCLONE" ]; then
  log "FATAL rclone not found - install it with: winget install Rclone.Rclone"
  exit 2
fi
log "using rclone: $RCLONE"

# A misconfigured remote must fail loudly here rather than silently uploading
# nothing and leaving a green log behind.
if ! "$RCLONE" lsd "$REMOTE" >/dev/null 2>&1; then
  log "FATAL cannot reach remote '$REMOTE' - run 'rclone config' first (see header)"
  exit 2
fi

if [ "${1:-}" = "--test" ]; then
  log "TEST remote '$REMOTE' is reachable"
  "$RCLONE" about "$REMOTE" 2>/dev/null | sed 's/^/  /' | tee -a "$LOG"
  exit 0
fi

log "=========================================================="
log "offsite backup START  remote=$REMOTE  keep=${KEEP_DAYS}d"

# Only the NEWEST archive of each kind is uploaded. The local store keeps 14
# days of every generation; shipping all 127 of them offsite would burn the
# free tier for versions that already exist locally. Offsite exists to survive
# losing this machine, not to be a second full history.
sent=0; failed=0; bytes=0
for prefix in $(ls "$SRC"/*.tar.gz 2>/dev/null | xargs -n1 basename 2>/dev/null \
                | sed 's/_[0-9]\{8\}-[0-9]\{6\}\.tar\.gz$//' | sort -u); do
  newest=$(ls -t "$SRC"/${prefix}_*.tar.gz 2>/dev/null | head -1)
  [ -z "$newest" ] && continue
  sz=$(stat -c %s "$newest" 2>/dev/null || echo 0)
  if "$RCLONE" copy "$newest" "$REMOTE/current/" --no-traverse 2>>"$LOG"; then
    log "  ok    $(basename "$newest") ($((sz/1024/1024))MB)"
    sent=$((sent+1)); bytes=$((bytes+sz))
  else
    log "  FAIL  $(basename "$newest")"
    failed=$((failed+1))
  fi
done

# Dated snapshot so a corruption discovered days later can still be rolled
# back. Server-side copy where the backend supports it, so this costs no
# second upload.
today=$(date -u +'%Y%m%d')
"$RCLONE" copy "$REMOTE/current/" "$REMOTE/daily/$today/" --no-traverse 2>>"$LOG" \
  && log "  snapshot -> daily/$today"

# Prune old snapshots. Uses rclone's own age filter rather than parsing dates
# out of directory names, which breaks the moment the format changes.
"$RCLONE" delete "$REMOTE/daily/" --min-age "${KEEP_DAYS}d" --rmdirs 2>>"$LOG" \
  && log "  pruned snapshots older than ${KEEP_DAYS}d"

if [ "$failed" -eq 0 ] && [ "$sent" -gt 0 ]; then
  date -u +'%Y-%m-%dT%H:%M:%SZ' > "$STAMP"
  log "SUMMARY  uploaded=$sent  failed=0  size=$((bytes/1024/1024))MB"
  log "=========================================================="
  exit 0
fi

log "SUMMARY  uploaded=$sent  FAILED=$failed"
log "=========================================================="
[ "$failed" -gt 0 ] && exit 2 || exit 1
