#!/bin/bash
# ============================================================================
# mc-backup.sh -- back up the Minecraft WORLD only.
#
# WHY THIS IS SEPARATE FROM backup-volumes.sh: that script discovers volumes
# rather than hardcoding them (correctly -- a hardcoded list once passed green
# on the exact bug it was written to catch). But minecraft_data is ~15 GB, most
# of which is a 600 MB modpack unpacked into hundreds of jars that CurseForge
# will hand back on demand. At 14-day retention that is ~210 GB of re-downloadable
# files on a 180 GB drive. minecraft_data is therefore in that script's SKIP_RE,
# and this script backs up the part that genuinely cannot be recovered: the world.
#
# Usage:  bash mc-backup.sh
# Restore: stop the stack, then
#   MSYS_NO_PATHCONV=1 docker run --rm -v minecraft_data:/data \
#     -v /d/Backups/minecraft:/backup valkey/valkey:9 \
#     tar -xzf /backup/world-<stamp>.tgz -C /data
# ============================================================================
set -uo pipefail

VOLUME="${MC_VOLUME:-minecraft_data}"
DEST="${MC_BACKUP_DIR:-/d/Backups/minecraft}"
RETENTION_DAYS="${MC_RETENTION_DAYS:-7}"
LOG_FILE="${MC_LOG_FILE:-/c/ServerData/Stacks/minecraft/mc-backup.log}"
# An image already present locally. The Docker credential helper is broken on
# this host, so anything requiring a registry pull would fail; see docker-pull.sh.
HELPER_IMAGE="${MC_HELPER_IMAGE:-valkey/valkey:9}"
STAMP=$(date +%Y%m%d-%H%M%S)

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"; }

if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE" 2>/dev/null | tr -d ' ')" -gt 1048576 ]; then
  mv -f "$LOG_FILE" "${LOG_FILE}.1" 2>/dev/null
fi

log "=== mc-backup START  volume=$VOLUME dest=$DEST retention=${RETENTION_DAYS}d ==="

if ! docker volume inspect "$VOLUME" >/dev/null 2>&1; then
  log "SKIP  volume $VOLUME does not exist - the server has never been started"
  exit 0
fi

mkdir -p "$DEST" 2>/dev/null
if [ ! -d "$DEST" ]; then
  log "FATAL  $DEST missing and could not be created"
  exit 2
fi

# Flush the world to disk before tarring it. A live server writes chunks
# continuously, so a tar taken mid-save can capture a torn region file.
#
# rcon-cli lives inside the image and reads RCON_PASSWORD from the container's
# own environment, so no secret is handled, passed or logged here.
#
# The timeout matters: ENABLE_AUTOPAUSE SIGSTOPs the JVM when idle, and a
# stopped process cannot answer RCON. That is not an error -- a paused server
# has no in-flight writes, so a plain tar of it is already consistent. Fall
# through rather than failing.
FLUSHED=no
if [ "$(docker inspect -f '{{.State.Status}}' minecraft 2>/dev/null)" = "running" ]; then
  if timeout 25 docker exec minecraft rcon-cli save-off  >/dev/null 2>&1 \
  && timeout 25 docker exec minecraft rcon-cli save-all flush >/dev/null 2>&1; then
    FLUSHED=yes
    log "  world flushed and saving paused"
  else
    log "  note: RCON did not answer within 25s (server paused by autopause, or RCON off) - taking a direct copy instead"
  fi
fi

# Same invocation shape as backup-volumes.sh, including MSYS_NO_PATHCONV, which
# stops Git Bash rewriting the container-side paths into Windows paths.
ARCHIVE="$DEST/world-${STAMP}.tgz"
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "${VOLUME}:/source:ro" -v "${DEST}:/backup" \
  "$HELPER_IMAGE" \
  tar -czf "/backup/world-${STAMP}.tgz" -C /source world 2>/dev/null
RC=$?

# Re-enable saving no matter how the tar went. Skipping this on failure would
# leave the live server never writing to disk, which is far worse than a failed
# backup: the next crash would lose everything since the last save.
if [ "$FLUSHED" = "yes" ]; then
  timeout 25 docker exec minecraft rcon-cli save-on >/dev/null 2>&1 \
    && log "  saving re-enabled" \
    || log "  WARNING saving could NOT be re-enabled - run 'docker exec minecraft rcon-cli save-on' by hand NOW"
fi

# Verify the archive is readable rather than trusting the exit code. A tar that
# exits 0 and produces an unreadable file is the failure mode worth catching.
if [ "$RC" -ne 0 ] || [ ! -s "$ARCHIVE" ] || ! tar -tzf "$ARCHIVE" >/dev/null 2>&1; then
  log "FAIL  $ARCHIVE is missing, empty or not a readable tar.gz (tar rc=$RC)"
  rm -f "$ARCHIVE" 2>/dev/null
  exit 1
fi

SIZE_MB=$(( $(stat -c '%s' "$ARCHIVE" 2>/dev/null || echo 0) / 1048576 ))
log "OK    world-${STAMP}.tgz (${SIZE_MB} MB), verified readable"

# Prune only AFTER a verified new archive exists, so a failing backup can never
# delete the last good one.
PRUNED=$(find "$DEST" -name 'world-*.tgz' -mtime "+${RETENTION_DAYS}" -print -delete 2>/dev/null | wc -l | tr -d ' ')
log "  pruned $PRUNED archive(s) older than ${RETENTION_DAYS}d; $(find "$DEST" -name 'world-*.tgz' 2>/dev/null | wc -l | tr -d ' ') kept"
log "=== mc-backup DONE ==="
