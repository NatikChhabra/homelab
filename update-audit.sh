#!/bin/bash
# ============================================================================
# update-audit.sh — daily container update audit.
#
# For every running container:
#   1. Compare the local image digest against the registry's current digest
#      for the same tag. Both are read anonymously over the registry HTTP API,
#      so this works with no Docker credential helper (see NOTE below).
#   2. When they differ, fetch the remote image config blob and read its
#      org.opencontainers.image.version label, then compare it to the local
#      label to classify the update as PATCH / MINOR / MAJOR.
#   3. Detect the "silent rename" pattern (the Seerr / Maintainerr case):
#      a tag that no longer exists, a repository that 404s, or an image whose
#      digest has not moved in a long time while still being tagged :latest.
#
# WHAT IT DOES NOT DO:
#   It never applies anything. `docker pull` cannot run unattended on this
#   host — Docker Desktop's credential helper (docker-credential-desktop)
#   fails outside an interactive logon session with
#     "A specified logon session does not exist."
#   so a scheduled task cannot pull. Instead this writes a staged apply script
#   listing exactly what to update; run it from an interactive shell.
#   MAJOR and RENAMED entries are never written into that script — they are
#   reported for a human decision, per standing instruction.
#
# Env overrides:
#   UA_LOG_FILE   UA_STAGE_FILE   UA_STALE_DAYS
# ============================================================================

set -uo pipefail

LOG_FILE="${UA_LOG_FILE:-/c/ServerData/Stacks/update-audit.log}"
STAGE_FILE="${UA_STAGE_FILE:-/c/ServerData/Stacks/update-audit-staged.sh}"
STALE_DAYS="${UA_STALE_DAYS:-180}"

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"; }

# curl/jq are borrowed from the sonarr container so this needs nothing on the host.
c()    { docker exec -i sonarr sh -c "$1"; }
jq_c() { docker exec -i sonarr jq "$@"; }

EXIT_CODE=0
N_CURRENT=0; N_PATCH=0; N_MINOR=0; N_MAJOR=0; N_RENAMED=0; N_STALE=0; N_ERROR=0
N_SKIPPED=0

log "=========================================================="
log "update-audit START  (stale threshold: ${STALE_DAYS}d)"
log "=========================================================="

# ---- registry helpers ------------------------------------------------------
# split_ref <imageRef> -> sets REG_HOST, REG_REPO, REG_TAG
split_ref() {
  local ref="$1" rest
  case "$ref" in
    */*) REG_HOST="${ref%%/*}"; rest="${ref#*/}" ;;
    *)   REG_HOST=""; rest="$ref" ;;
  esac
  # a first segment with no dot and no port is not a registry host (docker.io shorthand)
  case "$REG_HOST" in
    *.*|*:*|localhost) ;;
    *) rest="$ref"; REG_HOST="registry-1.docker.io" ;;
  esac
  # docker.io and index.docker.io are not the registry API endpoint
  case "$REG_HOST" in
    docker.io|index.docker.io|"") REG_HOST="registry-1.docker.io" ;;
  esac
  case "$rest" in
    *:*) REG_TAG="${rest##*:}"; REG_REPO="${rest%:*}" ;;
    *)   REG_TAG="latest";      REG_REPO="$rest" ;;
  esac
  if [ "$REG_HOST" = "registry-1.docker.io" ]; then
    case "$REG_REPO" in */*) ;; *) REG_REPO="library/$REG_REPO" ;; esac
  fi
}

# reg_token -> prints an anonymous pull token for REG_HOST/REG_REPO (may be empty)
reg_token() {
  local chal realm svc
  chal=$(c "curl -s -m 30 -I 'https://$REG_HOST/v2/$REG_REPO/manifests/$REG_TAG' | tr -d '\r' | grep -i '^www-authenticate:'")
  realm=$(printf '%s' "$chal" | sed -n 's/.*realm="\([^"]*\)".*/\1/p')
  svc=$(printf   '%s' "$chal" | sed -n 's/.*service="\([^"]*\)".*/\1/p')
  [ -z "$realm" ] && { printf ''; return; }
  c "curl -s -m 30 '$realm?service=$svc&scope=repository:$REG_REPO:pull'" \
    | jq_c -r '.token // .access_token // ""'
}

ACCEPT="-H 'Accept: application/vnd.oci.image.index.v1+json' -H 'Accept: application/vnd.docker.distribution.manifest.list.v2+json' -H 'Accept: application/vnd.oci.image.manifest.v1+json' -H 'Accept: application/vnd.docker.distribution.manifest.v2+json'"

reg_head_digest() {  # $1=token -> prints remote digest, or empty
  c "curl -s -m 30 -I -H 'Authorization: Bearer $1' $ACCEPT 'https://$REG_HOST/v2/$REG_REPO/manifests/$REG_TAG' | tr -d '\r' | sed -n 's/^[Dd]ocker-[Cc]ontent-[Dd]igest: //p'"
}

reg_status() {  # $1=token -> prints HTTP status for the manifest
  c "curl -s -m 30 -o /dev/null -w '%{http_code}' -H 'Authorization: Bearer $1' $ACCEPT 'https://$REG_HOST/v2/$REG_REPO/manifests/$REG_TAG'"
}

# reg_remote_version <token> -> prints the remote image's version label
# index -> amd64 manifest -> config blob -> .config.Labels
reg_remote_version() {
  local tok="$1" idx cfgdig man mandig
  idx=$(c "curl -s -m 60 -H 'Authorization: Bearer $tok' $ACCEPT 'https://$REG_HOST/v2/$REG_REPO/manifests/$REG_TAG'")
  mandig=$(printf '%s' "$idx" | jq_c -r '
    if .manifests then
      ( [ .manifests[] | select((.platform.architecture=="amd64") and (.platform.os=="linux")) ][0].digest // "" )
    else "" end' 2>/dev/null)
  if [ -n "$mandig" ] && [ "$mandig" != "null" ]; then
    man=$(c "curl -s -m 60 -H 'Authorization: Bearer $tok' $ACCEPT 'https://$REG_HOST/v2/$REG_REPO/manifests/$mandig'")
  else
    man="$idx"   # already a single-platform manifest
  fi
  cfgdig=$(printf '%s' "$man" | jq_c -r '.config.digest // ""' 2>/dev/null)
  [ -z "$cfgdig" ] || [ "$cfgdig" = "null" ] && { printf ''; return; }
  c "curl -sL -m 60 -H 'Authorization: Bearer $tok' 'https://$REG_HOST/v2/$REG_REPO/blobs/$cfgdig'" \
    | jq_c -r '(.config.Labels // .container_config.Labels // {})
               | (."org.opencontainers.image.version" // ."version" // "")' 2>/dev/null
}

# classify_bump <localVer> <remoteVer> -> PATCH | MINOR | MAJOR | UNKNOWN
classify_bump() {
  local a="$1" b="$2"
  [ -z "$a" ] || [ -z "$b" ] && { printf 'UNKNOWN'; return; }
  # strip any leading v and any trailing -ls123 / -suffix build metadata
  local ca cb
  ca=$(printf '%s' "$a" | sed 's/^[vV]//' | sed 's/[-+].*$//')
  cb=$(printf '%s' "$b" | sed 's/^[vV]//' | sed 's/[-+].*$//')
  local a1 a2 a3 b1 b2 b3
  a1=${ca%%.*}; a2=$(printf '%s' "$ca" | cut -d. -f2); a3=$(printf '%s' "$ca" | cut -d. -f3)
  b1=${cb%%.*}; b2=$(printf '%s' "$cb" | cut -d. -f2); b3=$(printf '%s' "$cb" | cut -d. -f3)
  case "$a1$b1" in *[!0-9]*) printf 'UNKNOWN'; return ;; esac
  if [ "$b1" -gt "$a1" ] 2>/dev/null; then printf 'MAJOR'; return; fi
  if [ "${b2:-0}" != "${a2:-0}" ]; then printf 'MINOR'; return; fi
  if [ "${b3:-0}" != "${a3:-0}" ]; then printf 'PATCH'; return; fi
  printf 'UNKNOWN'
}

# ---- staged apply script ---------------------------------------------------
{
  echo "#!/bin/bash"
  echo "# Staged by update-audit.sh on $(date -u +'%Y-%m-%dT%H:%M:%SZ')."
  echo "# Run from an INTERACTIVE shell — docker pull needs the credential helper,"
  echo "# which fails under Task Scheduler on this host."
  echo "# Only PATCH and MINOR updates are listed. MAJOR / RENAMED are excluded"
  echo "# by design and need an explicit decision; see update-audit.log."
  echo "set -euo pipefail"
  echo "echo 'Backing up config volumes first...'"
  echo "bash /c/ServerData/Stacks/backup-volumes.sh"
} > "$STAGE_FILE"
STAGED_ANY=false

# ---- walk running containers ----------------------------------------------
CONTAINERS=$(docker ps --format '{{.Names}}')

for cname in $CONTAINERS; do
  INFO=$(docker inspect "$cname" --format '{{.Config.Image}}|{{.Image}}|{{index .Config.Labels "com.docker.compose.project.working_dir"}}|{{index .Config.Labels "com.docker.compose.service"}}' 2>/dev/null)
  ref=$(printf   '%s' "$INFO" | cut -d'|' -f1)
  workdir=$(printf '%s' "$INFO" | cut -d'|' -f3)
  svc=$(printf   '%s' "$INFO" | cut -d'|' -f4)

  local_dig=$(docker image inspect "$ref" --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' 2>/dev/null)
  local_dig="${local_dig##*@}"
  local_ver=$(docker image inspect "$ref" --format '{{index .Config.Labels "org.opencontainers.image.version"}}' 2>/dev/null)
  [ "$local_ver" = "<no value>" ] && local_ver=""
  created=$(docker image inspect "$ref" --format '{{.Created}}' 2>/dev/null)

  log "----------------------------------------------------------"
  log "CONTAINER $cname"
  log "  image        : $ref"
  log "  local version: ${local_ver:-<no version label>}"

  if [ -z "$local_dig" ]; then
    log "  ERROR  no RepoDigest locally (image built or loaded, not pulled) — cannot audit"
    N_ERROR=$((N_ERROR+1)); EXIT_CODE=1; continue
  fi

  split_ref "$ref"
  tok=$(reg_token)
  status=$(reg_status "$tok")

  if [ "$status" = "404" ]; then
    log "  RENAMED/GONE  registry returned 404 for $REG_HOST/$REG_REPO:$REG_TAG"
    log "                The tag or repository no longer exists upstream. This is the"
    log "                silent-rename pattern — HELD for your decision, nothing applied."
    N_RENAMED=$((N_RENAMED+1)); continue
  fi
  # 429 is the registry rate-limiting us, not a fault in anything here. lscr.io
  # returns it regularly when several images are checked back to back, and on
  # 2026-07-28 three images hit it in one run and failed the whole scheduled job
  # -- a red "FAILED" for a condition that fixes itself by waiting a moment.
  # Back off and retry before treating it as an error.
  if [ "$status" = "429" ]; then
    for _b in 5 15 40; do
      log "  rate-limited (429) on $REG_HOST/$REG_REPO:$REG_TAG - backing off ${_b}s"
      sleep "$_b"
      tok=$(reg_token)
      status=$(reg_status "$tok")
      [ "$status" = "200" ] && break
      [ "$status" != "429" ] && break
    done
    [ "$status" = "200" ] && log "  recovered after backoff"
  fi

  if [ "$status" != "200" ]; then
    if [ "$status" = "429" ]; then
      # Still limited after three backoffs. Report it, but do NOT fail the job:
      # nothing is wrong with the server and there is no action for anyone.
      log "  SKIPPED  $REG_HOST/$REG_REPO:$REG_TAG still rate-limited (429) - will re-check next run"
      N_SKIPPED=$((N_SKIPPED+1)); continue
    fi
    log "  ERROR  registry returned HTTP $status for $REG_HOST/$REG_REPO:$REG_TAG"
    N_ERROR=$((N_ERROR+1)); EXIT_CODE=1; continue
  fi

  remote_dig=$(reg_head_digest "$tok")
  if [ -z "$remote_dig" ]; then
    log "  ERROR  registry gave no Docker-Content-Digest header"
    N_ERROR=$((N_ERROR+1)); EXIT_CODE=1; continue
  fi

  if [ "$remote_dig" = "$local_dig" ]; then
    age_days=""
    if [ -n "$created" ]; then
      cts=$(date -u -d "$created" +%s 2>/dev/null || echo "")
      [ -n "$cts" ] && age_days=$(( ( $(date -u +%s) - cts ) / 86400 ))
    fi
    # Staleness only means something for a FLOATING tag. A pinned tag like
    # postgres:14-vectorchord0.4.3 is supposed to sit still forever.
    floating=false
    case "$REG_TAG" in
      latest|release|stable|main|master|edge|nightly|develop) floating=true ;;
      *) case "$REG_TAG" in *[!0-9]*) ;; *) floating=true ;; esac ;;
    esac
    if [ "$floating" = "true" ] && [ -n "$age_days" ] && [ "$age_days" -gt "$STALE_DAYS" ]; then
      log "  STALE   up to date against tag '$REG_TAG', but that image is ${age_days} days old."
      log "          A floating tag frozen this long usually means the project moved"
      log "          to a new repository name. Verify upstream before trusting it."
      N_STALE=$((N_STALE+1))
    else
      log "  CURRENT  digest matches registry${age_days:+ (image age ${age_days}d)}"
      N_CURRENT=$((N_CURRENT+1))
    fi
    continue
  fi

  remote_ver=$(reg_remote_version "$tok")
  bump=$(classify_bump "$local_ver" "$remote_ver")
  log "  UPDATE AVAILABLE"
  log "    local  digest: $local_dig"
  log "    remote digest: $remote_dig"
  log "    remote version: ${remote_ver:-<no version label>}"
  log "    classified as : $bump"

  case "$bump" in
    PATCH|MINOR)
      if [ -n "$workdir" ] && [ -n "$svc" ]; then
        log "    -> staged as safe. Will be applied by $STAGE_FILE"
        {
          echo ""
          echo "# $cname : ${local_ver:-?} -> ${remote_ver:-?} ($bump)"
          echo "cd '$workdir' && docker compose pull '$svc' && docker compose up -d '$svc'"
        } >> "$STAGE_FILE"
        STAGED_ANY=true
      else
        log "    -> NOT staged: container has no compose project labels (started outside compose)."
        log "       Update it by hand; see CLAUDE.md note about AdGuard/Immich/Traccar."
      fi
      [ "$bump" = "PATCH" ] && N_PATCH=$((N_PATCH+1)) || N_MINOR=$((N_MINOR+1))
      ;;
    MAJOR)
      log "    -> HELD for approval. Major version bump is never applied automatically."
      N_MAJOR=$((N_MAJOR+1))
      ;;
    *)
      log "    -> HELD for approval. Version labels missing or unparseable, so the"
      log "       size of this change cannot be established. Treated as unsafe."
      N_MAJOR=$((N_MAJOR+1))
      ;;
  esac
done

if [ "$STAGED_ANY" != "true" ]; then
  echo "echo 'Nothing staged — no safe updates were found.'" >> "$STAGE_FILE"
fi
chmod +x "$STAGE_FILE" 2>/dev/null

# ---- Homarr dashboard integrity --------------------------------------------
# The calendar widget vanished once on 2026-07-26 with no trace in any log --
# Homarr does not record board mutations, so the cause was never established.
# This cannot prevent a recurrence, but it makes one visible within a day
# instead of only when somebody happens to look at the dashboard.
EXPECTED_WIDGETS="calendar downloads systemResources uptimeKuma clock"
MISSING_WIDGETS=""
HOMARR_KINDS=$(docker exec homarr-homarr-1 node -e "
const D=require('better-sqlite3');const d=new D('/appdata/db/db.sqlite',{readonly:true});
console.log(d.prepare('SELECT DISTINCT kind FROM item').all().map(x=>x.kind).join(' '));" 2>/dev/null | tr -d '\r')

log "----------------------------------------------------------"
if [ -z "$HOMARR_KINDS" ]; then
  log "HOMARR  could not read board (container down or schema changed) - not checked"
else
  for w in $EXPECTED_WIDGETS; do
    case " $HOMARR_KINDS " in *" $w "*) ;; *) MISSING_WIDGETS="$MISSING_WIDGETS $w" ;; esac
  done
  if [ -n "$MISSING_WIDGETS" ]; then
    log "HOMARR  WIDGET MISSING:$MISSING_WIDGETS"
    log "        A dashboard widget disappeared. Re-add it via the Homarr UI or"
    log "        the tRPC board.saveBoard call documented in the overnight report."
    EXIT_CODE=1
  else
    log "HOMARR  all expected widgets present ($HOMARR_KINDS)"
  fi
fi

log "=========================================================="
log "SUMMARY  current=$N_CURRENT  patch=$N_PATCH  minor=$N_MINOR  HELD(major/unknown)=$N_MAJOR  RENAMED=$N_RENAMED  stale=$N_STALE  rate-limited=$N_SKIPPED  errors=$N_ERROR"
if [ "$N_MAJOR" -gt 0 ] || [ "$N_RENAMED" -gt 0 ]; then
  log "ACTION NEEDED: $((N_MAJOR + N_RENAMED)) item(s) are waiting on your decision (see above)."
fi
if [ "$STAGED_ANY" = "true" ]; then
  log "Safe updates staged in $STAGE_FILE — run it from an interactive shell to apply."
fi
log "=========================================================="

exit $EXIT_CODE
