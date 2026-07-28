#!/bin/bash
# ============================================================================
# server-audit.sh -- read-only health audit of the whole home server.
#
# WHY THIS EXISTS: every problem it checks for is one that actually happened
# and was not noticed until something was visibly broken. Each check carries a
# comment naming the real incident, so nobody later "simplifies" away a check
# that exists for a reason.
#
# STRICTLY READ-ONLY. It changes nothing, deletes nothing, restarts nothing.
# Safe to run at any time, including while downloads are in flight.
#
# Exit: 0 = all clear, 1 = warnings, 2 = failures.
# Run:  bash /c/ServerData/Stacks/server-audit.sh
#       RW_AUDIT_QUIET=true  -> only WARN/FAIL lines (used by the weekly task)
# ============================================================================

set -uo pipefail

QUIET="${RW_AUDIT_QUIET:-false}"
REPORT="${RW_AUDIT_REPORT:-/c/ServerData/Stacks/server-audit-report.txt}"

# Credentials live in secrets.env (ACL-restricted to the owner), not here. The
# qBittorrent password in particular was sitting in plaintext in this file, which
# the nightly backup then copied into every archive on D:.
SECRETS_FILE="${RW_SECRETS_FILE:-/c/ServerData/Stacks/secrets.env}"
if [ -r "$SECRETS_FILE" ]; then
  # shellcheck disable=SC1090
  . "$SECRETS_FILE"
else
  printf 'FATAL  cannot read %s -- refusing to run without credentials\n' "$SECRETS_FILE" >&2
  exit 2
fi
: "${SONARR_KEY:?SONARR_KEY missing}"; : "${JELLYFIN_KEY:?JELLYFIN_KEY missing}"
: "${PROWLARR_KEY:?PROWLARR_KEY missing}"
QBIT_USER="${QB_USER:?QB_USER missing}"
QBIT_PASS="${QB_PASS:?QB_PASS missing}"

# Thresholds
DISK_WARN_PCT=85          # warn when a drive passes this
DISK_FAIL_PCT=95
STALL_HOURS=6             # a torrent at 0 bytes for longer than this is stuck
MIN_DHT_NODES=20          # below this, the BitTorrent transport is not healthy

OK=0; WARN=0; FAIL=0
LINES=""

_emit() { LINES="${LINES}$1"$'\n'; [ "$QUIET" = "true" ] || printf '%s\n' "$1"; }
ok()   { OK=$((OK+1));   [ "$QUIET" = "true" ] || printf '  OK    %s\n' "$*"; LINES="${LINES}  OK    $*"$'\n'; }
warn() { WARN=$((WARN+1)); _emit "  WARN  $*"; }
fail() { FAIL=$((FAIL+1)); _emit "  FAIL  $*"; }
sec()  { _emit ""; _emit "== $* =="; }

sonarr()  { docker exec sonarr sh -c "curl -s -m 20 -H 'X-Api-Key: $SONARR_KEY' 'http://localhost:8989$1'" 2>/dev/null; }
radarr_k() { docker exec radarr sh -c "sed -n 's|.*<ApiKey>\(.*\)</ApiKey>.*|\1|p' /config/config.xml" 2>/dev/null | tr -d '\r'; }
jf()      { docker exec jellyfin sh -c "curl -s -m 20 -H 'X-Emby-Token: $JELLYFIN_KEY' 'http://localhost:8096$1'" 2>/dev/null; }
jqs()     { docker exec -i sonarr jq "$@" 2>/dev/null; }

_emit "============================================================"
_emit " SERVER AUDIT  $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
_emit "============================================================"

# ---------------------------------------------------------------------------
sec "1. Containers"
# Incident: nothing here yet, but a silently exited container is the cheapest
# possible thing to miss.
EXPECTED="qbittorrent prowlarr flaresolverr sonarr radarr bazarr jellyfin jellyseerr maintainerr cleanuparr"
for c in $EXPECTED; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
  if [ "$st" != "running" ]; then
    fail "container $c is '${st:-missing}', expected running"
  else
    rc=$(docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null)
    hs=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null)
    if [ "${rc:-0}" -gt 3 ]; then
      warn "container $c has restarted $rc times (crash loop?)"
    elif [ "$hs" = "unhealthy" ]; then
      fail "container $c reports unhealthy"
    else
      ok "container $c running (restarts=$rc)"
    fi
  fi
done

# ---------------------------------------------------------------------------
sec "2. Web endpoints"
check_port() {  # $1=port $2=label $3=acceptable codes (space separated)
  local code; code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 "http://localhost:$1/" 2>/dev/null)
  case " $3 " in
    *" $code "*) ok "$2 (:$1) responds $code" ;;
    *) fail "$2 (:$1) returned '$code', expected one of: $3" ;;
  esac
}
check_port 8080  qbittorrent "200 401 403"
check_port 9696  prowlarr    "200 302"
check_port 8191  flaresolverr "200 405"
check_port 8989  sonarr      "200 302"
check_port 7878  radarr      "200 302"
check_port 6767  bazarr      "200 302"
check_port 8096  jellyfin    "200 302"
check_port 5055  jellyseerr  "200 307 302"
check_port 6246  maintainerr "200 302"
check_port 11011 cleanuparr  "200 302"
check_port 7575  homarr      "200 302"
check_port 3001  uptime-kuma "200 302"

# ---------------------------------------------------------------------------
sec "3. Media mount (the 450G drive must actually be mounted)"
# Incident 2026-07-27: after moving media to the new D: drive, Docker Desktop
# had not picked the drive up, so every container silently got an EMPTY 127M
# directory at /data instead of the real disk. Sonarr saw an empty library and
# would happily have re-downloaded everything. Checking that /data merely
# "exists" is useless -- it always exists. Check the SIZE and the CONTENT.
for c in sonarr radarr bazarr jellyfin qbittorrent; do
  sz=$(docker exec "$c" sh -c "df -BG /data 2>/dev/null | tail -1 | awk '{print \$2}' | tr -d 'G'" 2>/dev/null)
  has=$(docker exec "$c" sh -c "ls /data 2>/dev/null | tr '\n' ' '" 2>/dev/null)
  if [ -z "$sz" ]; then
    fail "$c: cannot stat /data at all"
  elif [ "$sz" -lt 100 ]; then
    fail "$c: /data is only ${sz}G -- the media drive is NOT mounted (empty stub)"
  elif ! printf '%s' "$has" | grep -q library || ! printf '%s' "$has" | grep -q torrents; then
    fail "$c: /data mounted (${sz}G) but missing library/ or torrents/ -- got: $has"
  else
    ok "$c: /data = ${sz}G with library+torrents"
  fi
done

# ---------------------------------------------------------------------------
sec "4. Disk space"
while read -r fs size used avail pct mnt; do
  case "$mnt" in /c|/d) ;; *) continue ;; esac
  p=${pct%\%}
  if [ "$p" -ge "$DISK_FAIL_PCT" ]; then fail "$mnt is ${pct} full (${avail} free)"
  elif [ "$p" -ge "$DISK_WARN_PCT" ]; then warn "$mnt is ${pct} full (${avail} free)"
  else ok "$mnt ${pct} used, ${avail} free"; fi
done < <(df -h /c /d 2>/dev/null | tail -n +2)

# ---------------------------------------------------------------------------
sec "5. Hardlink efficiency"
# Incident 2026-07-27: robocopy does NOT preserve hardlinks. Moving media to the
# new drive silently turned every hardlinked library file into a full second
# copy, costing 11G. Sonarr keeps using hardlinks for new imports, so this only
# shows up as slow, invisible disk bloat.
dup=$(docker exec sonarr sh -c '
  n=0; b=0
  for f in $(find /data/library -type f \( -name "*.mkv" -o -name "*.mp4" \) 2>/dev/null); do
    l=$(stat -c %h "$f" 2>/dev/null)
    if [ "${l:-1}" -eq 1 ]; then
      sz=$(stat -c %s "$f" 2>/dev/null)
      if find /data/torrents -type f -size "${sz}c" 2>/dev/null | grep -q .; then
        n=$((n+1)); b=$((b+sz))
      fi
    fi
  done
  echo "$n $b"' 2>/dev/null)
dn=$(printf '%s' "$dup" | awk '{print $1+0}')
db=$(printf '%s' "$dup" | awk '{print int($2/1073741824)}')
if [ "${dn:-0}" -gt 0 ]; then
  warn "$dn library file(s) are full copies not hardlinks (~${db}G wasted) - re-link them to /data/torrents"
else
  ok "all library files are hardlinked (no duplicate media on disk)"
fi

# ---------------------------------------------------------------------------
sec "6. BitTorrent transport"
# Incident 2026-07-27: every torrent sat at 0 bytes "downloading metadata" for
# hours. It was a wedged qBittorrent session, cleared by a restart. A curl probe
# of tracker URLs is NOT a valid test (it times out even when healthy) and led
# to a completely wrong "the ISP is blocking BitTorrent" conclusion. The only
# trustworthy signal is qBittorrent's own transfer/info.
QB=$(docker exec qbittorrent sh -c "
  C=/tmp/.audit_ck
  curl -s -c \$C -o /dev/null 'http://localhost:8080/api/v2/auth/login' --data 'username=$QBIT_USER&password=$QBIT_PASS'
  curl -s -m 15 -b \$C 'http://localhost:8080/api/v2/transfer/info'" 2>/dev/null)
if printf '%s' "$QB" | grep -q dht_nodes; then
  dht=$(printf '%s' "$QB" | jqs -r '.dht_nodes')
  cs=$(printf '%s' "$QB" | jqs -r '.connection_status')
  [ "$cs" = "connected" ] && ok "qBittorrent connection_status=connected" || fail "qBittorrent connection_status=$cs"
  if [ "${dht:-0}" -lt "$MIN_DHT_NODES" ]; then
    fail "DHT has only ${dht} nodes (<$MIN_DHT_NODES) - peer discovery is broken, restart qbittorrent"
  else
    ok "DHT healthy ($dht nodes)"
  fi
else
  fail "cannot read qBittorrent transfer/info (auth or API problem)"
fi

# Stuck-at-zero detection: a torrent that has been added a long time ago and
# still has 0 bytes is the exact signature of the wedge above.
QT=$(docker exec qbittorrent sh -c "
  C=/tmp/.audit_ck
  curl -s -m 20 -b \$C 'http://localhost:8080/api/v2/torrents/info'" 2>/dev/null)
if printf '%s' "$QT" | grep -q '"hash"'; then
  now=$(date -u +%s)
  stuck=$(printf '%s' "$QT" | jqs -r --argjson now "$now" --argjson h "$STALL_HOURS" '
    [ .[] | select(.downloaded == 0 and .progress == 0 and ((($now - .added_on) / 3600) > $h)) ] | length')
  tot=$(printf '%s' "$QT" | jqs -r 'length')
  dling=$(printf '%s' "$QT" | jqs -r '[ .[] | select(.state|test("downloading|stalledDL|metaDL")) ] | length')
  if [ "${stuck:-0}" -gt 0 ]; then
    fail "$stuck torrent(s) stuck at 0 bytes for >${STALL_HOURS}h - restart qbittorrent, then re-check"
    printf '%s' "$QT" | jqs -r --argjson now "$now" --argjson h "$STALL_HOURS" '
      .[] | select(.downloaded == 0 and .progress == 0 and ((($now - .added_on)/3600) > $h))
      | "          stuck: \(.name[0:64])"' | while read -r l; do _emit "$l"; done
  else
    ok "no torrents stuck at zero ($tot in client, $dling active)"
  fi
fi

# ---------------------------------------------------------------------------
sec "7. Outbound DNS / metadata providers"
# Incident 2026-07-27: Jellyfin was the only *arr-family container without a
# dns: override, so it used ISP DNS, which hijacks api.themoviedb.org to a dead
# IP. Result: no artwork or metadata at all, while every other HTTPS call
# worked, so it looked like a TMDb outage rather than a config gap.
for c in sonarr radarr jellyfin jellyseerr; do
  # Retried on purpose. A single probe reported a hard FAIL for Jellyfin on
  # 2026-07-28 purely because an 18GB download was saturating the link at that
  # moment -- the very next request returned 401. A transient timeout must not
  # look identical to a DNS misconfiguration, or the audit trains you to ignore
  # it. Three tries with a rising timeout; only a consistent failure is real.
  code=""
  for t in 15 30 45; do
    code=$(docker exec "$c" sh -c "curl -s -o /dev/null -w '%{http_code}' -m $t https://api.themoviedb.org/3/ 2>/dev/null" 2>/dev/null)
    case "$code" in 401|200) break ;; esac
    sleep 2
  done
  if [ -z "$code" ] || [ "$code" = "000" ]; then
    # Seerr ships wget, not curl. A 401 there surfaces as a non-zero exit with
    # "401 Unauthorized" on stderr, which still proves the host was reached.
    if docker exec "$c" sh -c "wget -q -T 15 -O /dev/null https://api.themoviedb.org/3/ 2>&1 | grep -qiE '401|Unauthorized'" 2>/dev/null; then
      code=401
    fi
  fi
  case "$code" in
    401|200) ok "$c reaches api.themoviedb.org ($code)" ;;
    *)       fail "$c cannot reach api.themoviedb.org (got '${code:-no-http-client}') - check dns: override in compose" ;;
  esac
done

# ---------------------------------------------------------------------------
sec "8. App health endpoints"
sh_health() { # $1=label $2=json
  local n t m tmp; n=$(printf '%s' "$2" | jqs -r 'length' 2>/dev/null)
  if [ -z "$n" ]; then warn "$1 health endpoint unreadable"; return; fi
  if [ "$n" = "0" ]; then ok "$1 health clean"; return; fi
  # Written via a temp file rather than a pipe: a `while read` on the right of a
  # pipe runs in a subshell, so both the counters AND the report buffer built up
  # inside it were being discarded.
  tmp=$(mktemp)
  printf '%s' "$2" | jqs -r '.[] | "\(.type)|\(.message)"' > "$tmp"
  while IFS='|' read -r t m; do
    [ -z "${t:-}" ] && continue
    case "$t" in
      error) fail "$1: $m" ;;
      *)     warn "$1: $m" ;;
    esac
  done < "$tmp"
  rm -f "$tmp"
}
sh_health sonarr   "$(sonarr /api/v3/health)"
RK=$(radarr_k)
sh_health radarr   "$(docker exec radarr sh -c "curl -s -m 20 -H 'X-Api-Key: $RK' 'http://localhost:7878/api/v3/health'" 2>/dev/null)"
sh_health prowlarr "$(docker exec prowlarr sh -c "curl -s -m 20 -H 'X-Api-Key: $PROWLARR_KEY' 'http://localhost:9696/api/v1/health'" 2>/dev/null)"

# ---------------------------------------------------------------------------
sec "9. Library consistency"
# Incident 2026-07-27: 16G of completed downloads sat unimported because the old
# rolling-window bug had removed the torrents; Sonarr will never import an
# orphan on its own, so the library stayed empty while the disk was full.
SER=$(sonarr /api/v3/series)
if printf '%s' "$SER" | grep -q '"id"'; then
  nser=$(printf '%s' "$SER" | jqs -r 'length')
  ok "Sonarr tracks $nser series"
  # Fetch the queue ONCE. The previous version walked every series twice (once
  # to print, once to re-count, because the printing loop ran in a subshell and
  # its counters were lost), which doubled the API calls for no benefit.
  QUEUE=$(sonarr "/api/v3/queue?pageSize=500")
  MISSING_ANY=0; MISSING_TOTAL=0
  TMPS=$(mktemp)
  printf '%s' "$SER" | jqs -r '.[] | "\(.id)|\(.title)"' > "$TMPS"
  while IFS='|' read -r sid title; do
    [ -z "${sid:-}" ] && continue
    eps=$(sonarr "/api/v3/episode?seriesId=$sid")
    mon=$(printf '%s'  "$eps"   | jqs -r '[.[]|select(.monitored==true)]|length')
    have=$(printf '%s' "$eps"   | jqs -r '[.[]|select(.hasFile==true)]|length')
    miss=$(printf '%s' "$eps"   | jqs -r '[.[]|select(.monitored==true and .hasFile==false)]|length')
    qn=$(printf '%s'   "$QUEUE" | jqs -r --argjson s "$sid" '[.records[]?|select(.seriesId==$s)]|length')
    # REQUESTED-SEASON CHECK. Season-level `monitored` is what a Seerr request
    # sets. On 2026-07-27 the user requested Food Wars SEASON 2 and the
    # automation pulled SEASON 1 instead, because rolling-window.sh only looked
    # at episodes and picked "earliest unwatched" across the whole series. If an
    # episode is monitored in a season the user never asked for, that regression
    # is back -- so it is checked explicitly rather than trusted.
    reqs=$(sonarr "/api/v3/series/$sid" | jqs -r '[.seasons[]?|select(.monitored==true and .seasonNumber>0)|.seasonNumber]|join(",")')
    stray=$(printf '%s' "$eps" | jqs -r --arg r "$reqs" '
        ($r | split(",") | map(tonumber)) as $req
        | [ .[] | select(.monitored==true and (.seasonNumber as $s | ($req | index($s)) == null)) ]
        | length')
    if [ "${stray:-0}" -gt 0 ]; then
      fail "$title: $stray episode(s) monitored OUTSIDE the requested season(s) [$reqs] - wrong season being fetched"
    fi

    # Counted regardless of the queue. An episode with a STALLED download is
    # still a missing episode: We Are All Trying Here S01E03 sat missing with a
    # complete 2.9GB copy orphaned on disk, and the orphan check stayed quiet
    # because the episode "had something queued" -- a download stuck at 84% that
    # was never going to finish.
    MISSING_TOTAL=$((MISSING_TOTAL + ${miss:-0}))

    if [ "${miss:-0}" -gt 0 ] && [ "${qn:-0}" -eq 0 ]; then
      warn "$title: $miss monitored episode(s) missing with nothing downloading"
      MISSING_ANY=$((MISSING_ANY + miss))
    else
      ok "$title: monitored=$mon onDisk=$have queued=$qn (requested seasons: ${reqs:-none})"
    fi
  done < "$TMPS"
  rm -f "$TMPS"
else
  fail "Sonarr series API unreadable"
fi

# Completed downloads with no hardlink into the library.
#
# NOT every one of these is a problem. A download that lost an upgrade race --
# a WEB-DL grabbed after a BluRay was already imported -- is correctly left
# unimported and is only redundant, not broken. The genuinely bad case, and the
# one that cost 16G of invisible waste on 2026-07-27, is an unimported download
# sitting there WHILE the library is still missing episodes. So the count is
# split, and only the second kind is a warning.
orph=$(docker exec sonarr sh -c '
  n=0; b=0
  for d in /data/torrents/complete/*; do
    [ -e "$d" ] || continue
    f=$(find "$d" -type f \( -name "*.mkv" -o -name "*.mp4" \) 2>/dev/null | head -1)
    [ -z "$f" ] && continue
    ino=$(stat -c %i "$f" 2>/dev/null)
    if ! find /data/library -inum "$ino" 2>/dev/null | grep -q .; then
      n=$((n+1)); b=$((b + $(du -s "$d" 2>/dev/null | cut -f1)))
    fi
  done
  echo "$n $((b/1048576))"' 2>/dev/null)
on=$(printf '%s' "$orph" | awk '{print $1+0}')
ob=$(printf '%s' "$orph" | awk '{print $2+0}')
# MISSING_ANY is set by the per-series loop above.
if [ "${on:-0}" -gt 0 ] && [ "${MISSING_TOTAL:-0}" -gt 0 ]; then
  warn "$on completed download(s) unimported (~${ob}G) while episodes are still missing - run a DownloadedEpisodesScan"
elif [ "${on:-0}" -gt 0 ]; then
  ok "$on completed download(s) unimported (~${ob}G) - superseded/redundant releases, library is not missing anything because of them"
else
  ok "every completed download is linked into the library"
fi

# ---------------------------------------------------------------------------
sec "10. Rolling window automation"
# Incident 2026-07-27: the task exited 1 every 20 minutes for hours (season-pack
# cancels returning 404) and nobody noticed, because a failing scheduled task is
# invisible unless you go looking.
for t in "ServerData - Rolling Window" "ServerData - Orphan Cleanup" "ServerData - Update Audit" "ServerData - Weekly Audit" "ServerData - Daily Backup" "ServerData - Mount Watchdog"; do
  r=$(powershell.exe -NoProfile -Command "\$i=Get-ScheduledTaskInfo -TaskName '$t' -EA SilentlyContinue; if(\$i){\$i.LastTaskResult}else{'missing'}" 2>/dev/null | tr -d '\r ')
  # Windows returns status codes here, not just exit codes:
  #   267009 SCHED_S_TASK_RUNNING   - running right now (this audit is itself
  #                                   launched by one of these, so it always
  #                                   sees its own task in this state)
  #   267011 SCHED_S_TASK_HAS_NOT_RUN
  #   267014 SCHED_S_TASK_TERMINATED
  case "$r" in
    0)       ok "task '$t' last result 0" ;;
    missing) warn "scheduled task '$t' does not exist" ;;
    "")      warn "scheduled task '$t' state unreadable" ;;
    267009)  ok "task '$t' is running right now" ;;
    267011)  ok "task '$t' has not run yet" ;;
    267014)  warn "task '$t' was terminated (hit its time limit?)" ;;
    *)       fail "task '$t' last result $r (non-zero = the job failed)" ;;
  esac
done

if [ -f /c/ServerData/Stacks/stuck-episodes.txt ]; then
  # grep -c exits 1 on zero matches, which under `set -o pipefail` used to leave
  # this variable holding two lines ("0" plus an echoed fallback) and blow up the
  # numeric test. Count with awk instead, which always exits 0.
  ns=$(awk '!/^#/ && NF {n++} END {print n+0}' /c/ServerData/Stacks/stuck-episodes.txt 2>/dev/null)
  if [ "${ns:-0}" -gt 0 ]; then
    warn "$ns episode(s) listed as STUCK in stuck-episodes.txt"
  else
    ok "no stuck episodes"
  fi
fi

# ---------------------------------------------------------------------------
sec "11. Quality profile / dub preference"
# Incident 2026-07-27: upgradeAllowed was false, so once a subbed release was
# grabbed Sonarr would never replace it with a dub -- the stated preference was
# silently unreachable.
for pair in "sonarr:8989:$SONARR_KEY:v3" "radarr:7878:$(radarr_k):v3"; do
  app=${pair%%:*}; rest=${pair#*:}; port=${rest%%:*}; rest=${rest#*:}; key=${rest%%:*}
  P=$(docker exec "$app" sh -c "curl -s -m 20 -H 'X-Api-Key: $key' 'http://localhost:$port/api/v3/qualityprofile/4'" 2>/dev/null)
  ua=$(printf '%s' "$P" | jqs -r '.upgradeAllowed')
  cf=$(printf '%s' "$P" | jqs -r '.cutoffFormatScore')
  ds=$(printf '%s' "$P" | jqs -r '[.formatItems[]?|select(.name=="Dual Audio")|.score]|first // 0')
  if [ "$ua" != "true" ]; then
    fail "$app profile 4: upgradeAllowed=false - a sub grabbed now can never be upgraded to dub"
  elif [ "${ds:-0}" -le 0 ]; then
    fail "$app profile 4: 'Dual Audio' scores ${ds} - dub is not actually preferred"
  elif [ "${cf:-0}" -le 0 ]; then
    warn "$app profile 4: cutoffFormatScore=${cf} - upgrades stop too early to reach a dub"
  else
    ok "$app profile 4: upgrades on, Dual Audio +${ds}, cutoff ${cf}"
  fi
done

# ---------------------------------------------------------------------------
sec "12. Indexers"
# The indexer payload is ~550KB. Ask Prowlarr to count server-side instead of
# shipping all of it through a shell variable and jq, which was flaky enough to
# produce a false "API unreadable" failure on a perfectly healthy Prowlarr.
en=""; tt=""
for _try in 1 2 3; do
  CNT=$(docker exec prowlarr sh -c "curl -s -m 30 -H 'X-Api-Key: $PROWLARR_KEY' 'http://localhost:9696/api/v1/indexer' | jq -r '\"\\([.[]|select(.enable==true)]|length)|\\(length)\"'" 2>/dev/null)
  en=${CNT%%|*}; tt=${CNT##*|}
  case "$en" in ''|*[!0-9]*) sleep 3; continue ;; esac
  break
done
case "$en" in
  ''|*[!0-9]*) fail "Prowlarr indexer API unreadable after 3 tries" ;;
  *)
    if [ "$en" -lt 5 ]; then fail "only $en/$tt indexers enabled - searches will find little"
    else ok "$en/$tt indexers enabled"; fi ;;
esac

# ---------------------------------------------------------------------------
sec "13. Backups"
# A backup job that quietly stops is indistinguishable from one that works until
# the day you need it. Before 2026-07-28 backups were not scheduled at all and
# the newest archive was days old without anything noticing. Three things are
# checked: that backups are RECENT, that the important ones are PRESENT, and
# that at least one is actually RESTORABLE rather than a corrupt tarball.
BK_DIR="${BV_BACKUP_DIR:-/d/Backups}"
if [ ! -d "$BK_DIR" ]; then
  fail "backup directory $BK_DIR does not exist"
else
  newest=$(ls -t "$BK_DIR"/*.tar.gz 2>/dev/null | head -1)
  if [ -z "$newest" ]; then
    fail "no backup archives in $BK_DIR at all"
  else
    age_h=$(( ( $(date +%s) - $(stat -c %Y "$newest" 2>/dev/null || echo 0) ) / 3600 ))
    if [ "$age_h" -gt 48 ]; then
      fail "newest backup is ${age_h}h old - the daily backup job has stopped running"
    elif [ "$age_h" -gt 26 ]; then
      warn "newest backup is ${age_h}h old (expected under 24h)"
    else
      ok "backups current (newest ${age_h}h old, $(ls "$BK_DIR"/*.tar.gz 2>/dev/null | wc -l | tr -d ' ') archives)"
    fi

    # The ones whose loss would be unrecoverable. bind-Immich is the photo
    # ORIGINALS; immich_immich-db is only the database. Incident 2026-07-28:
    # 105 originals were lost and the database was perfectly intact, which is
    # precisely why the DB backup alone proves nothing.
    for must in sonarr_config jellyfin_config bind-Vaultwarden immich_immich-db bind-Immich; do
      if ls "$BK_DIR"/${must}_*.tar.gz >/dev/null 2>&1; then
        ok "backup present: $must"
      else
        fail "NO backup for $must - irreplaceable data is unprotected"
      fi
    done

    # Restore test. An archive that cannot be extracted is not a backup.
    probe=$(ls -t "$BK_DIR"/sonarr_config_*.tar.gz 2>/dev/null | head -1)
    if [ -n "$probe" ]; then
      tdir=$(mktemp -d)
      if tar -xzf "$probe" -C "$tdir" 2>/dev/null && [ -n "$(ls -A "$tdir" 2>/dev/null)" ]; then
        nf=$(find "$tdir" -type f 2>/dev/null | wc -l | tr -d ' ')
        ok "restore test passed: sonarr_config extracts cleanly ($nf files)"
      else
        fail "restore test FAILED: $(basename "$probe") will not extract - backups are not usable"
      fi
      rm -rf "$tdir" 2>/dev/null
    fi

    # Photo-count regression guard. Incident 2026-07-28: 105 irreplaceable
    # originals disappeared from the library over roughly three weeks and
    # nothing noticed, because every individual backup "succeeded" -- they were
    # just faithfully backing up a shrinking library. A backup that runs
    # perfectly while the thing it protects quietly empties out is worthless.
    # Comparing consecutive archives catches shrinkage while older archives are
    # still inside the 14-day retention window and the files can still be
    # restored.
    im_new=$(ls -t "$BK_DIR"/bind-Immich_*.tar.gz 2>/dev/null | head -1)
    im_old=$(ls -t "$BK_DIR"/bind-Immich_*.tar.gz 2>/dev/null | sed -n '2p')
    if [ -n "$im_new" ] && [ -n "$im_old" ]; then
      n_new=$(tar -tzf "$im_new" 2>/dev/null | grep '^Immich/library/admin/' | grep -cv '/$')
      n_old=$(tar -tzf "$im_old" 2>/dev/null | grep '^Immich/library/admin/' | grep -cv '/$')
      if [ "${n_new:-0}" -lt "${n_old:-0}" ]; then
        fail "Immich backup SHRANK: newest holds $n_new originals, previous held $n_old -- $((n_old-n_new)) photo(s) lost, restore from $(basename "$im_old") before it ages out"
      else
        ok "Immich backup holds $n_new originals (previous $n_old, not shrinking)"
      fi
    fi
  fi
fi

# ---------------------------------------------------------------------------
sec "14. Stray resources"
# Orphaned volumes are leftovers from stacks that were removed. They are not
# harmful in themselves, but they are backed up every night, so they quietly
# cost backup time and space forever. Reported, never deleted -- they hold user
# data (Nextcloud, wger) and that call belongs to the owner, not this script.
orphv=$(docker volume ls -qf dangling=true 2>/dev/null | grep -Ev '^[0-9a-f]{64}$' || true)
nov=$(printf '%s' "$orphv" | grep -c . || true)
if [ "${nov:-0}" -gt 0 ]; then
  warn "$nov orphaned volume(s) from removed stacks (still being backed up nightly):"
  printf '%s
' "$orphv" | while read -r v; do
    [ -z "$v" ] && continue
    _emit "          $v"
  done
else
  ok "no orphaned volumes"
fi

dang=$(docker images -f dangling=true -q 2>/dev/null | grep -c . || true)
[ "${dang:-0}" -gt 0 ] && warn "$dang dangling image(s) - reclaim with: docker image prune" || ok "no dangling images"

exited=$(docker ps -aq -f status=exited 2>/dev/null | grep -c . || true)
[ "${exited:-0}" -gt 0 ] && warn "$exited exited container(s) left behind" || ok "no exited containers"

# The recycle bin is a safety net, not storage. It self-prunes at 14 days, but
# if it ever grows large that is worth knowing before the disk fills.
rb=$(docker exec sonarr sh -c "du -sm /data/recyclebin 2>/dev/null | cut -f1" 2>/dev/null)
if [ "${rb:-0}" -gt 51200 ]; then
  warn "recycle bin is $((rb/1024))G - unusually large"
else
  ok "recycle bin ${rb:-0}MB (auto-pruned after 14 days)"
fi

# Credentials must not be sitting in the scripts, which the nightly backup
# copies -- and, since 2026-07-28, which git publishes.
leak=$(grep -lE '^(SONARR_KEY|JELLYFIN_KEY|PROWLARR_KEY|QB_PASS|QB_USER)=["'"'"']?[A-Za-z0-9]' \
        /c/ServerData/Stacks/*.sh 2>/dev/null | grep -v secrets.env | grep -c . || true)
if [ "${leak:-0}" -gt 0 ]; then
  fail "$leak script(s) contain hardcoded credentials - move them to secrets.env"
else
  ok "no credentials hardcoded in scripts"
fi

# Incident 2026-07-28: the check above only ever looked at *.sh, so three live
# Uptime Kuma push tokens sat hardcoded in run-job.ps1 and were caught only by
# a manual scan of the first commit. A push token lets anyone holding it forge
# an "up" heartbeat, hiding a job that has actually stopped. PowerShell counts.
ps_leak=$(grep -lE "(PushToken|ApiKey|Password|Token)\s*=\s*['\"][A-Za-z0-9+/=_-]{12,}['\"]" \
        /c/ServerData/Stacks/*.ps1 2>/dev/null | grep -c . || true)
if [ "${ps_leak:-0}" -gt 0 ]; then
  fail "$ps_leak PowerShell script(s) contain a hardcoded token - move it to secrets.env"
else
  ok "no credentials hardcoded in PowerShell scripts"
fi

# The repository must never carry the secret files. Incident 2026-07-28: this
# tree was published to GitHub; secrets.env holds the qBittorrent password and
# every *arr API key, homarr/.env holds the key that decrypts every credential
# Homarr stores. .gitignore is an allowlist so it fails closed, but a wrong
# edit to it would be silent, so the result is asserted here rather than assumed.
if [ -d /c/ServerData/Stacks/.git ]; then
  tracked=$(cd /c/ServerData/Stacks && git ls-files 2>/dev/null \
            | grep -E '(^|/)(secrets\.env|\.env)$' | grep -c . || true)
  if [ "${tracked:-0}" -gt 0 ]; then
    fail "git is tracking $tracked secret file(s) - run: git rm --cached <file> and rotate every key in it"
  else
    ok "git tracks no secret files"
  fi
fi

# ---------------------------------------------------------------------------
sec "15. Non-media stacks"
# Incident 2026-07-28: AdGuard Home had been removed from this host entirely and
# NOTHING here noticed, because sections 1 and 2 only ever looked at the ten
# streaming containers. The audit cheerfully reported "67 ok, 0 failures" while
# the container did not exist. Tailscale's DNS override was then pointed at this
# host, every tailnet device sent its queries to a port with nothing listening,
# and name resolution died on every device at once. The lesson is not "add
# AdGuard" -- it is that a service absent from this list fails invisibly. Every
# stack gets checked here, not just the media ones.
OTHER_CONTAINERS="adguardhome immich-immich-server-1 immich-database-1 immich-redis-1 \
immich-immich-machine-learning-1 vaultwardem-vaultwarden-1 vikunja-vikunja-1 \
filebrowser-filebrowser-1 homarr-homarr-1 uptime-kuma-uptime-kuma-1 portainer \
server-room-glances-1"
for c in $OTHER_CONTAINERS; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
  if [ "$st" != "running" ]; then
    fail "container $c is '${st:-missing}', expected running"
  else
    rc=$(docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null)
    hs=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null)
    if [ "${rc:-0}" -gt 3 ]; then
      warn "container $c has restarted $rc times (crash loop?)"
    elif [ "$hs" = "unhealthy" ]; then
      fail "container $c reports unhealthy"
    else
      ok "container $c running (restarts=$rc)"
    fi
  fi
done

check_port 3080  adguard-ui  "200 302"
check_port 2283  immich      "200 302"
check_port 8222  vaultwarden "200 302"
check_port 3456  vikunja     "200 302"
check_port 8095  filebrowser "200 302"
check_port 61208 glances     "200 302"

# Portainer is HTTPS-only with a self-signed cert, so check_port's plain HTTP
# probe cannot be reused here.
pcode=$(curl -sk -o /dev/null -w '%{http_code}' -m 10 "https://localhost:9443/" 2>/dev/null)
case " 200 302 307 " in
  *" $pcode "*) ok "portainer (:9443) responds $pcode" ;;
  *) fail "portainer (:9443) returned '$pcode', expected 200/302/307" ;;
esac

# ---------------------------------------------------------------------------
sec "16. DNS filtering actually resolves"
# Incident 2026-07-28 (same outage): "the container is running" was never the
# real question. Port 53 has to ANSWER. A container that is up but not serving
# DNS breaks every device pointed at it just as hard as a missing one, so this
# resolves a real name through it rather than trusting container state.
if docker inspect -f '{{.State.Status}}' adguardhome >/dev/null 2>&1; then
  dnsres=$(nslookup -timeout=5 example.com 127.0.0.1 2>&1)
  if printf '%s' "$dnsres" | grep -qiE '^Address(es)?:[[:space:]]*[0-9]' ; then
    ok "AdGuard answers DNS on 127.0.0.1:53"
  else
    fail "AdGuard is running but port 53 did not resolve example.com -- devices pointed at it will lose all DNS"
  fi
  # A resolver that answers but has stopped filtering is a silent regression:
  # everything keeps working, the ad blocking just quietly stops.
  if [ -r /c/ServerData/AdGuard/conf/AdGuardHome.yaml ]; then
    if grep -q '^  protection_enabled: true' /c/ServerData/AdGuard/conf/AdGuardHome.yaml; then
      ok "AdGuard filtering is enabled"
    else
      warn "AdGuard is resolving but protection_enabled is not true -- nothing is being blocked"
    fi
    # Bootstrap must not point at the ISP router: CLAUDE.md records that the
    # ISP resolver hijacks lookups, and bootstrap is what resolves the DoH
    # upstream hostnames in the first place.
    if grep -A3 '^  bootstrap_dns:' /c/ServerData/AdGuard/conf/AdGuardHome.yaml | grep -qE '192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[01])\.'; then
      warn "AdGuard bootstrap_dns points at a LAN router -- the ISP resolver can hijack DoH bootstrap"
    else
      ok "AdGuard bootstrap uses public resolvers"
    fi
  fi
else
  warn "adguardhome container does not exist -- DNS filtering is not running at all"
fi

# ---------------------------------------------------------------------------
sec "17. Immich library integrity"
# Incident 2026-07-28: downloading any photo from Immich failed with
# "ENOENT: no such file or directory, access '/data/library/admin/...'".
# 105 of 812 active assets had thumbnails, previews and EXIF rows -- so the
# originals existed at import on 2026-07-05 -- but the original files were gone
# from disk. Thumbnails still render, so the library LOOKS fine in the UI and
# only a download reveals the loss. Backups only began 2026-07-27 and the
# recycle bin was empty, so all 105 were unrecoverable. This check exists so the
# next occurrence is caught while a backup can still fix it.
IM_DB=immich-database-1
IM_SRV=immich-immich-server-1
if docker inspect -f '{{.State.Status}}' "$IM_SRV" 2>/dev/null | grep -q running; then
  # POSTGRES_USER is read from the container rather than hardcoded -- it
  # contains a space, which is exactly the kind of thing that rots in a script.
  im_user=$(docker inspect "$IM_DB" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
            | sed -n 's/^POSTGRES_USER=//p' | head -1)
  if [ -n "$im_user" ]; then
    docker exec "$IM_DB" psql -U "$im_user" -d immich -t -A \
      -c 'SELECT "originalPath" FROM asset WHERE "deletedAt" IS NULL;' 2>/dev/null \
      | tr -d '\r' > /tmp/_immich_paths.txt
    im_total=$(grep -c . /tmp/_immich_paths.txt 2>/dev/null || echo 0)
    if [ "${im_total:-0}" -gt 0 ]; then
      docker cp /tmp/_immich_paths.txt "$IM_SRV:/tmp/_audit_paths.txt" >/dev/null 2>&1
      im_missing=$(docker exec "$IM_SRV" sh -c '
        m=0; while IFS= read -r p; do
          [ -z "$p" ] && continue
          [ -f "$p" ] || m=$((m+1))
        done < /tmp/_audit_paths.txt; echo "$m"' 2>/dev/null)
      docker exec "$IM_SRV" rm -f /tmp/_audit_paths.txt >/dev/null 2>&1
      rm -f /tmp/_immich_paths.txt
      if [ "${im_missing:-0}" -gt 0 ]; then
        fail "Immich: ${im_missing}/${im_total} assets have no original file on disk -- downloads for those fail, thumbnails still render so the UI looks fine"
      else
        ok "Immich: all ${im_total} active assets have their original file"
      fi
    else
      warn "Immich: could not read the asset list from Postgres"
    fi
  else
    warn "Immich: could not determine POSTGRES_USER from $IM_DB"
  fi
fi

# ---------------------------------------------------------------------------
_emit ""
_emit "============================================================"
# run-job.ps1 scrapes the last line matching SUMMARY for its status text and
# for the Uptime Kuma heartbeat message, so emit one in that shape.
_emit " SUMMARY  ok=$OK  warnings=$WARN  failures=$FAIL"
_emit " RESULT: $OK ok, $WARN warning(s), $FAIL failure(s)"
_emit "============================================================"

printf '%s' "$LINES" > "$REPORT" 2>/dev/null
[ "$QUIET" = "true" ] && printf 'audit: %s ok, %s warn, %s fail (report: %s)\n' "$OK" "$WARN" "$FAIL" "$REPORT"

# run-job.ps1 treats any non-zero exit as a FAILED job (event-log error + a
# "down" heartbeat to Uptime Kuma). Warnings are advisory, so the scheduled
# weekly run sets this and only a real FAIL raises the alarm. Run it by hand
# without the flag to get the warning-sensitive exit code.
if [ "$FAIL" -gt 0 ]; then exit 2
elif [ "$WARN" -gt 0 ] && [ "${RW_AUDIT_OK_ON_WARN:-false}" != "true" ]; then exit 1
else exit 0; fi
