#!/bin/bash
# ============================================================================
# orphan-cleanup.sh — reclaim torrent data whose library copy has been deleted.
#
# WHY THIS EXISTS
#   *arr imports are hardlinked: the library file and the torrent file are one
#   copy with two names (nlink=2). When rolling-window.sh deletes a watched
#   episode it removes only the library name, so the data survives under
#   torrents/ with nlink=1 and keeps consuming disk forever. Season-pack
#   torrents make it impossible to drop "the torrent for E17" at delete time,
#   because the pack is still needed by other in-window episodes. So the
#   reclaim has to happen later, out of band — that is this script.
#   (Cleanuparr's Download Cleaner is the "proper" tool but is 2FA-blocked.)
#
# SAFETY MODEL — three independent guards, all must pass before a file dies:
#   1. nlink == 1. A file still hardlinked into the library has nlink >= 2 and
#      is therefore unreachable by this script, by construction.
#   2. Not inside any folder belonging to a torrent qBittorrent still knows
#      about — ANY state, not just seeding. Deleting a stray .nfo/.txt out of a
#      live torrent's folder breaks that torrent (found the hard way: the
#      manual 2026-07-27 run would have done exactly this to WAATH S01E03).
#   3. qBittorrent must answer. If it cannot be reached the exclusion list
#      cannot be built, so the script ABORTS rather than guessing.
#
#   Every run logs the full would-delete list before touching anything, so the
#   log is a complete record even in live mode.
#
# Env overrides:
#   OC_DRY_RUN=true|false   OC_LOG_FILE=...   OC_ROOT=...
# ============================================================================

set -uo pipefail

DRY_RUN="${OC_DRY_RUN:-false}"
LOG_FILE="${OC_LOG_FILE:-/c/ServerData/Stacks/orphan-cleanup.log}"

# Host-side torrent root, and the path prefix qBittorrent reports internally.
# Media moved to D: on 2026-07-28. This still pointed at C:/ServerData/Media,
# a directory that no longer exists, so the scan matched nothing and the weekly
# job reported success while reclaiming exactly zero bytes. A wrong root fails
# silently and invisibly, so it is now checked explicitly below.
HOST_ROOT="${OC_ROOT:-/d/Media/torrents/complete}"
QB_PREFIX="/data/torrents/complete"

QB_URL="http://qbittorrent:8080"

# Credentials come from the ACL-restricted secrets file, never from this script.
SECRETS_FILE="${OC_SECRETS_FILE:-/c/ServerData/Stacks/secrets.env}"
if [ -r "$SECRETS_FILE" ]; then
  # shellcheck disable=SC1090
  . "$SECRETS_FILE"
else
  printf 'FATAL  cannot read %s -- refusing to run without credentials\n' "$SECRETS_FILE" >&2
  exit 2
fi
: "${QB_USER:?QB_USER missing from $SECRETS_FILE}"
: "${QB_PASS:?QB_PASS missing from $SECRETS_FILE}"

if [ ! -d "$HOST_ROOT" ]; then
  printf 'FATAL  torrent root %s does not exist -- refusing to run\n' "$HOST_ROOT" >&2
  exit 2
fi

LOG_MAX_BYTES="${OC_LOG_MAX_BYTES:-2097152}"   # 2 MB
if [ -f "$LOG_FILE" ]; then
  _sz=$(wc -c < "$LOG_FILE" 2>/dev/null | tr -d ' ')
  [ "${_sz:-0}" -gt "$LOG_MAX_BYTES" ] && mv -f "$LOG_FILE" "${LOG_FILE}.1" 2>/dev/null
fi

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"; }

# curl/jq borrowed from the sonarr container; nothing extra needed on the host.
sq() { docker exec -i sonarr sh -c "$1"; }
jqc() { docker exec -i sonarr jq "$@"; }

EXIT_CODE=0

log "=========================================================="
log "orphan-cleanup START  DRY_RUN=$DRY_RUN  root=$HOST_ROOT"
log "=========================================================="

[ -d "$HOST_ROOT" ] || { log "FATAL  torrent root '$HOST_ROOT' not found - aborting."; exit 2; }

# ---- Guard 3: qBittorrent must answer --------------------------------------
COOKIE=/tmp/oc_qb_cookie
sq "curl -s -c $COOKIE -o /dev/null -X POST --data 'username=$QB_USER&password=$QB_PASS' -H 'Referer: $QB_URL' '$QB_URL/api/v2/auth/login'" >/dev/null 2>&1
QB_VER=$(sq "curl -s -m 20 -b $COOKIE '$QB_URL/api/v2/app/version'" 2>/dev/null | tr -d '\r')

case "$QB_VER" in
  v*) log "qBittorrent reachable ($QB_VER)" ;;
  *)  log "FATAL  qBittorrent did not authenticate (got: '${QB_VER:-<empty>}')."
      log "       Without its torrent list the exclusion set cannot be built, and"
      log "       deleting blind could destroy files belonging to a live torrent."
      log "       Nothing was touched."
      exit 2 ;;
esac

# ---- Build the exclusion set: every torrent qBittorrent knows about ---------
EXCLUDE=$(mktemp)
sq "curl -s -m 60 -b $COOKIE '$QB_URL/api/v2/torrents/info'" \
  | jqc -r '.[] | .content_path // empty' 2>/dev/null > "$EXCLUDE"

TORRENT_COUNT=$(wc -l < "$EXCLUDE" | tr -d ' ')
log "Torrents known to qBittorrent: $TORRENT_COUNT (all states) - their files are off limits"

# Translate container paths to host paths, and reduce each to its top-level
# entry under the root so a whole torrent folder is protected, not just the
# exact content_path.
EXCLUDE_HOST=$(mktemp)
while IFS= read -r p; do
  [ -z "$p" ] && continue
  hp="${p/#$QB_PREFIX/$HOST_ROOT}"
  rel="${hp#$HOST_ROOT/}"
  top="${rel%%/*}"
  [ -n "$top" ] && printf '%s\n' "$top" >> "$EXCLUDE_HOST"
done < "$EXCLUDE"
sort -u "$EXCLUDE_HOST" -o "$EXCLUDE_HOST"

while IFS= read -r t; do [ -n "$t" ] && log "  protected: $t"; done < "$EXCLUDE_HOST"

is_protected() {  # $1 = path relative to HOST_ROOT
  local top="${1%%/*}"
  grep -Fxq "$top" "$EXCLUDE_HOST" 2>/dev/null
}

# ---- Find candidates: guard 1 (nlink==1) + guard 2 (not protected) ---------
CAND=$(mktemp)
cd "$HOST_ROOT" || { log "FATAL  cannot enter $HOST_ROOT"; exit 2; }
find . -type f -links 1 -printf '%s|%P\n' 2>/dev/null > "$CAND"

TOTAL=0; COUNT=0; SKIPPED=0
DELETE_LIST=$(mktemp)
while IFS='|' read -r sz rel; do
  [ -z "${rel:-}" ] && continue
  if is_protected "$rel"; then
    SKIPPED=$((SKIPPED+1))
    continue
  fi
  printf '%s|%s\n' "$sz" "$rel" >> "$DELETE_LIST"
  TOTAL=$((TOTAL + sz)); COUNT=$((COUNT+1))
done < "$CAND"

GB=$(awk -v b="$TOTAL" 'BEGIN{printf "%.2f", b/1073741824}')

# ---- Always log the full plan before acting --------------------------------
log "----------------------------------------------------------"
log "PLAN: $COUNT orphaned files, ${GB} GB  (skipped $SKIPPED file(s) inside live torrents)"
if [ "$COUNT" -gt 0 ]; then
  while IFS='|' read -r sz rel; do
    log "  WOULD DELETE  $(awk -v b="$sz" 'BEGIN{printf "%7.2f MB", b/1048576}')  $rel"
  done < "$DELETE_LIST"
fi

if [ "$COUNT" -eq 0 ]; then
  log "Nothing to reclaim - no orphaned files outside live torrents."
  log "SUMMARY  deleted=0  reclaimed=0.00GB  protected-torrents=$TORRENT_COUNT  skipped=$SKIPPED"
  rm -f "$EXCLUDE" "$EXCLUDE_HOST" "$CAND" "$DELETE_LIST"
  exit 0
fi

if [ "$DRY_RUN" = "true" ]; then
  log "DRY_RUN=true - nothing deleted."
  log "SUMMARY  would-delete=$COUNT  would-reclaim=${GB}GB  protected-torrents=$TORRENT_COUNT  skipped=$SKIPPED"
  rm -f "$EXCLUDE" "$EXCLUDE_HOST" "$CAND" "$DELETE_LIST"
  exit 0
fi

# ---- Act -------------------------------------------------------------------
DELETED=0; FAILED=0
while IFS='|' read -r sz rel; do
  # Re-check both guards immediately before unlinking: the file may have been
  # re-linked by an import in the seconds since the plan was built.
  if [ ! -f "$rel" ]; then FAILED=$((FAILED+1)); continue; fi
  nl=$(stat -c '%h' "$rel" 2>/dev/null)
  if [ "${nl:-1}" -ne 1 ]; then
    log "  SKIP (re-linked since plan)  $rel"
    SKIPPED=$((SKIPPED+1)); continue
  fi
  if is_protected "$rel"; then SKIPPED=$((SKIPPED+1)); continue; fi
  if rm -f -- "$rel" 2>/dev/null; then
    DELETED=$((DELETED+1))
  else
    log "  DELETE FAILED  $rel"
    FAILED=$((FAILED+1)); EXIT_CODE=1
  fi
done < "$DELETE_LIST"

# Tidy empty directories, but never one belonging to a live torrent.
while IFS= read -r d; do
  rel="${d#./}"
  is_protected "$rel" && continue
  rmdir "$d" 2>/dev/null && log "  removed empty dir  $rel"
done < <(find . -mindepth 1 -type d -empty 2>/dev/null)

log "----------------------------------------------------------"
log "DELETED $DELETED files, reclaimed ${GB} GB  (failed=$FAILED, skipped=$SKIPPED)"
log "SUMMARY  deleted=$DELETED  reclaimed=${GB}GB  protected-torrents=$TORRENT_COUNT  skipped=$SKIPPED  failed=$FAILED"
log "=========================================================="

rm -f "$EXCLUDE" "$EXCLUDE_HOST" "$CAND" "$DELETE_LIST"
exit $EXIT_CODE
