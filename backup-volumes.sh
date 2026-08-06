#!/bin/bash
# ============================================================================
# backup-volumes.sh -- back up every piece of irreplaceable server state.
#
# WHY THIS MATTERS: the media on D: can always be re-downloaded. Everything this
# script captures cannot. Vaultwarden holds real passwords, Immich holds the
# photo database, Sonarr/Radarr hold the whole library history, Jellyfin holds
# the watch state that rolling-window.sh makes DELETION decisions from.
#
# Before 2026-07-27 this listed 9 volumes by hand and said "not scheduled by
# default", so 14 volumes -- Immich's database and Vaultwarden's among them --
# had never been backed up once, and the newest archive was days stale. Volumes
# are now DISCOVERED, not hardcoded, so a new stack is covered automatically.
#
# Destination is D:, a DIFFERENT PHYSICAL DISK from the C: SSD holding the live
# data. A backup on the same disk as the original is not a backup.
#
# Archives are verified by reading them back, and pruned after RETENTION_DAYS.
# ============================================================================

set -uo pipefail

BACKUP_DIR="${BV_BACKUP_DIR:-/d/Backups}"
RETENTION_DAYS="${BV_RETENTION_DAYS:-14}"
LOG_FILE="${BV_LOG_FILE:-/c/ServerData/Stacks/backup-volumes.log}"
DATESTAMP=$(date +%Y%m%d-%H%M%S)

# An image already present locally: Docker's credential helper fails on this
# host, so anything needing a registry pull would break the backup entirely.
HELPER_IMAGE="${BV_HELPER_IMAGE:-valkey/valkey:9}"

# Deliberately NOT backed up:
#   immich_model-cache - downloaded ML models, ~820MB, regenerate on demand
#   *_static           - build artefacts
#   64-hex names       - anonymous volumes, no durable state
SKIP_RE='^(immich_model-cache|wger_wger_static|[0-9a-f]{64})$'

# App data that is bind-mounted rather than in a Docker volume, so volume
# discovery alone would silently miss it. Vaultwarden is first on purpose.
# Navidrome added 2026-07-28. Its navidrome/docker-compose.yml bind-mounts
# C:/ServerData/Navidrome/plugins and the comment there states the plugins are
# picked up by the nightly backup. They were not: volume discovery covers
# navidrome_data, but a bind mount outside this list is invisible to it, so the
# claim was false the moment it was written. A third-party WASM plugin set that
# has to be re-sourced by hand after a restore is exactly the kind of state
# this script exists to hold.
BIND_DIRS="Vaultwarden AdGuard Traccar Immich Vikunja Navidrome"

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"; }

# Keep the log from growing without bound.
if [ -f "$LOG_FILE" ] && [ "$(wc -c < "$LOG_FILE" 2>/dev/null | tr -d ' ')" -gt 2097152 ]; then
  mv -f "$LOG_FILE" "${LOG_FILE}.1" 2>/dev/null
fi

mkdir -p "$BACKUP_DIR" 2>/dev/null
if [ ! -d "$BACKUP_DIR" ]; then
  log "FATAL  backup destination $BACKUP_DIR missing and could not be created"
  exit 2
fi

OK=0; BAD=0; BYTES=0

log "=========================================================="
log "backup-volumes START  dest=$BACKUP_DIR  retention=${RETENTION_DAYS}d"
log "=========================================================="

verify() {  # $1=archive path -> 0 if it is a readable, non-empty tar.gz
  [ -s "$1" ] || return 1
  tar -tzf "$1" >/dev/null 2>&1
}

# ---- 1. Named Docker volumes (discovered, not hardcoded) -------------------
for VOL in $(docker volume ls --format '{{.Name}}' 2>/dev/null | grep -Ev "$SKIP_RE" | sort); do
  A="${VOL}_${DATESTAMP}.tar.gz"
  MSYS_NO_PATHCONV=1 docker run --rm \
    -v "${VOL}:/source:ro" -v "${BACKUP_DIR}:/backup" \
    --entrypoint sh "$HELPER_IMAGE" \
    -c "tar -czf /backup/${A} -C /source ." >/dev/null 2>&1
  if verify "$BACKUP_DIR/$A"; then
    sz=$(wc -c < "$BACKUP_DIR/$A" | tr -d ' ')
    BYTES=$((BYTES+sz)); OK=$((OK+1)); log "  ok    volume $VOL ($((sz/1024)) KB)"
  else
    BAD=$((BAD+1)); log "  FAIL  volume $VOL -- archive missing or unreadable"
    rm -f "$BACKUP_DIR/$A" 2>/dev/null
  fi
done

# ---- 2. Bind-mounted app data ----------------------------------------------
for D in $BIND_DIRS; do
  [ -d "/c/ServerData/$D" ] || { log "  skip  bind   $D (not present)"; continue; }
  A="bind-${D}_${DATESTAMP}.tar.gz"
  tar -czf "$BACKUP_DIR/$A" -C /c/ServerData "$D" 2>/dev/null
  if verify "$BACKUP_DIR/$A"; then
    sz=$(wc -c < "$BACKUP_DIR/$A" | tr -d ' ')
    BYTES=$((BYTES+sz)); OK=$((OK+1)); log "  ok    bind   $D ($((sz/1024)) KB)"
  else
    BAD=$((BAD+1)); log "  FAIL  bind   $D -- archive missing or unreadable"
    rm -f "$BACKUP_DIR/$A" 2>/dev/null
  fi
done

# ---- 3. Compose files + automation scripts ---------------------------------
# Tiny, and it is what makes a restore reproducible instead of guesswork.
A="stacks-config_${DATESTAMP}.tar.gz"
tar -czf "$BACKUP_DIR/$A" -C /c/ServerData \
    --exclude='Stacks/logs' --exclude='Stacks/*.log' Stacks CLAUDE.md 2>/dev/null
if verify "$BACKUP_DIR/$A"; then
  sz=$(wc -c < "$BACKUP_DIR/$A" | tr -d ' ')
  BYTES=$((BYTES+sz)); OK=$((OK+1)); log "  ok    stacks config + CLAUDE.md ($((sz/1024)) KB)"
else
  BAD=$((BAD+1)); log "  FAIL  stacks config"
fi

# ---- 4. Prune ---------------------------------------------------------------
PRUNED=$(find "$BACKUP_DIR" -name '*.tar.gz' -mtime +"$RETENTION_DAYS" 2>/dev/null | wc -l | tr -d ' ')
find "$BACKUP_DIR" -name '*.tar.gz' -mtime +"$RETENTION_DAYS" -delete 2>/dev/null
[ "${PRUNED:-0}" -gt 0 ] && log "  pruned $PRUNED archive(s) older than ${RETENTION_DAYS} days"

TOTAL=$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)
log "SUMMARY  archives=$OK  failed=$BAD  written=$((BYTES/1048576))MB  store=$TOTAL"
log "=========================================================="

[ "$BAD" -gt 0 ] && exit 2
exit 0
