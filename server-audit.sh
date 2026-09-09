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
    # Retried, not single-shot. Incident 2026-07-28: this reported a hard FAIL
    # ("cannot reach api.themoviedb.org") for a host that was in fact reachable
    # on the very next attempt -- the resolver was returning an AAAA record on a
    # stack with IPv6 disabled, so roughly one connection in three was reset
    # before falling back to IPv4. A single blip must not read as an outage,
    # but a genuinely unreachable host still fails all three.
    for _try in 1 2 3; do
      if docker exec "$c" sh -c "wget -q -T 15 -O /dev/null https://api.themoviedb.org/3/ 2>&1 | grep -qiE '401|Unauthorized'" 2>/dev/null; then
        code=401; break
      fi
      sleep 2
    done
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
#
# Blocklisted releases are excluded outright. A blocklisted download is not an
# orphan Sonarr forgot about -- it is one that something already decided
# against, and rolling-window.sh clears exactly this shape (a completed download
# rejected as "Not an upgrade") by dropping the queue row, blocklisting the
# release and leaving the torrent seeding. Counting those turned a deliberate
# decision into a warning nothing could ever clear: the intended end state IS
# the file sitting there seeding, and the DownloadedEpisodesScan it asked for
# would refuse to import them every time.
BL_B64=$(sonarr "/api/v3/blocklist?pageSize=1000" \
         | jqs -r '.records[]?.sourceTitle' | base64 -w0 2>/dev/null)
orph=$(docker exec sonarr sh -c '
  printf "%s" "$1" | base64 -d > /tmp/audit-blocklist.txt 2>/dev/null || : > /tmp/audit-blocklist.txt
  n=0; b=0
  for d in /data/torrents/complete/*; do
    [ -e "$d" ] || continue
    base=$(basename "$d")
    # Substring, not equality: the torrent directory carries the tracker prefix
    # ("www.UIndex.org    -    <release>") while Sonarr blocklists the clean
    # release name, so an exact match never fires.
    skip=0
    while IFS= read -r bl; do
      [ -z "$bl" ] && continue
      case "$base" in *"$bl"*) skip=1; break ;; esac
    done < /tmp/audit-blocklist.txt
    [ "$skip" = "1" ] && continue
    # A season pack is orphaned only if NOTHING in it made it into the library.
    # Testing just the first file called every pack an orphan: the rolling
    # window imports the in-window episodes and deliberately leaves the rest, so
    # a 24-episode pack legitimately has 21 unlinked files in it.
    linked=0; any=0; tmpf=/tmp/audit-files.txt
    find "$d" -type f \( -name "*.mkv" -o -name "*.mp4" \) > "$tmpf" 2>/dev/null
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      any=1
      ino=$(stat -c %i "$f" 2>/dev/null)
      [ -z "$ino" ] && continue
      if find /data/library -inum "$ino" 2>/dev/null | grep -q .; then linked=1; break; fi
    done < "$tmpf"
    rm -f "$tmpf"
    [ "$any" = "0" ] && continue
    if [ "$linked" = "0" ]; then
      n=$((n+1)); b=$((b + $(du -s "$d" 2>/dev/null | cut -f1)))
    fi
  done
  rm -f /tmp/audit-blocklist.txt
  echo "$n $((b/1048576))"' _ "$BL_B64" 2>/dev/null)
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
    1)
      if [ "$t" = "ServerData - Update Audit" ] && [ -f /c/ServerData/Stacks/update-audit-staged.sh ]; then
        ok "task '$t' last result 1 (staged updates waiting -- run update-audit-staged.sh to apply)"
      else
        fail "task '$t' last result $r (non-zero = the job failed)"
      fi
      ;;
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
    # Offsite copy. Incident 2026-07-28: every backup lived on a second disk in
    # the SAME machine, which protects against a disk dying and nothing else --
    # fire, theft, ransomware or a dead PSU takes both copies together. An
    # offsite job that quietly stops running looks identical to one that works,
    # so the success stamp is checked rather than assumed.
    OFFSITE_STAMP="/c/ServerData/Stacks/.offsite-last-success"
    if [ -f "$OFFSITE_STAMP" ]; then
      off_age=$(( ( $(date +%s) - $(stat -c %Y "$OFFSITE_STAMP" 2>/dev/null || echo 0) ) / 3600 ))
      if [ "$off_age" -gt 72 ]; then
        fail "offsite backup last succeeded ${off_age}h ago - the only copy that survives losing this machine is stale"
      elif [ "$off_age" -gt 30 ]; then
        warn "offsite backup is ${off_age}h old (expected under 24h)"
      else
        ok "offsite backup current (${off_age}h old)"
      fi
    else
      warn "NO offsite backup configured - every copy is on this one machine; see backup-offsite.sh"
    fi

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
# glances, not server-room-glances-1: the server-room compose file gained an
# explicit container_name on 2026-07-28, which renamed the container. This list
# still held the old compose-generated name, so the very next audit would have
# reported "container server-room-glances-1 is 'missing'" while Glances was
# running perfectly. Section 19 now cross-checks this list against the
# container_name lines in Stacks/ so a rename cannot desynchronise it silently
# again.
OTHER_CONTAINERS="adguardhome immich-immich-server-1 immich-database-1 immich-redis-1 \
immich-immich-machine-learning-1 vaultwardem-vaultwarden-1 vikunja-vikunja-1 \
filebrowser-filebrowser-1 homarr-homarr-1 uptime-kuma-uptime-kuma-1 portainer \
glances navidrome"
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
check_port 4533  navidrome   "200 302"

# Portainer is HTTPS-only with a self-signed cert, so check_port's plain HTTP
# probe cannot be reused here.
pcode=$(curl -sk -o /dev/null -w '%{http_code}' -m 10 "https://localhost:9443/" 2>/dev/null)
case " 200 302 307 " in
  *" $pcode "*) ok "portainer (:9443) responds $pcode" ;;
  *) fail "portainer (:9443) returned '$pcode', expected 200/302/307" ;;
esac

# ---------------------------------------------------------------------------
sec "16. JARVIS voice assistant"
# Six containers in a dependency chain, and every one of them has a failure mode
# that "the container is running" cannot see. A Wyoming service accepts TCP
# connections minutes before its model has finished downloading. Ollama answers
# HTTP 200 with an empty model list. Home Assistant answers 401 to an
# authenticated probe forever while serving perfectly. So nothing in this
# section is satisfied by container state -- every check makes the thing answer.
# Incident 2026-08-09: jarvis-kokoro and jarvis-capabilities were both running in
# production and NEITHER was in this list, so the "unlisted container" check at
# the end of the audit flagged them as failing invisibly. Kokoro is now THE voice
# -- if it dies, JARVIS silently falls back to the Piper voice Natik rejected,
# which sounds like a quality regression rather than an outage. Adding a service
# without adding it here is the exact hole this audit exists to close.
JARVIS_CONTAINERS="jarvis-ollama jarvis-hass jarvis-whisper jarvis-piper jarvis-wake-word jarvis-voice jarvis-kokoro jarvis-capabilities jarvis-websearch jarvis-searxng"
for c in $JARVIS_CONTAINERS; do
  st=$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null)
  if [ "$st" != "running" ]; then
    fail "container $c is '${st:-missing}', expected running"
  else
    rc=$(docker inspect -f '{{.RestartCount}}' "$c" 2>/dev/null)
    hs=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c" 2>/dev/null)
    if [ "${rc:-0}" -gt 3 ]; then
      warn "container $c has restarted $rc times (crash loop?)"
    elif [ "$hs" = "unhealthy" ]; then
      # WARN here, not FAIL, and deliberately unlike sections 1 and 15.
      # Incident 2026-08-08: jarvis-hass reported unhealthy across 56
      # consecutive probes while Home Assistant was serving normally, because
      # the healthcheck curled an authenticated endpoint that answers 401 by
      # design -- and every probe was additionally logged as a failed login,
      # which walks HA towards banning its own healthcheck. That cost a day.
      # The response probes below are the authority for this stack; a
      # healthcheck that disagrees with them is a bug in the healthcheck.
      warn "container $c reports unhealthy - check it against the response probes below before believing it"
    else
      ok "container $c running (restarts=$rc)"
    fi
  fi
done

# Incident 2026-08-08: openwakeword_data did not exist and had to be created by
# hand. All five volumes are declared `external: true`, so compose will never
# create one for you -- it aborts the entire stack with an error that names the
# volume but not the fix, and the other five services never start either.
for v in ollama_data hass_config whisper_data tts_data openwakeword_data; do
  if docker volume inspect "$v" >/dev/null 2>&1; then
    ok "external volume $v exists"
  else
    fail "external volume $v is MISSING - 'docker compose up -d' in jarvis/ will refuse to start the whole stack; create it with 'docker volume create $v'"
  fi
done

# Wyoming is a raw TCP JSONL protocol, NOT HTTP. `curl http://localhost:10300/`
# ALWAYS fails against it, and that failure is NOT evidence of a fault -- the
# since-deleted api.py health endpoint did exactly that and reported all three
# voice services as down while every one of them was fine. So this speaks the
# real protocol: open the socket, send a `describe`, read the `info` reply.
#
# It counts INSTALLED MODELS rather than stopping at the handshake. These images
# fetch their models from HuggingFace on first run, so a container listens on
# its port for several minutes before it can actually transcribe or speak. A
# plain port check -- which is exactly what the compose healthchecks do -- is
# green through that entire window and green forever if the download failed.
#
# The probe runs inside jarvis-voice because that container already carries
# wyoming_client.py, a Python to run it, and a route onto the jarvis network.
# The reply is parsed IN the container on purpose: piper's `describe` is ~83KB
# of voice metadata, and section 12 already recorded what happens when a large
# API payload is dragged out through a shell variable.
JARVIS_WY=$(docker exec jarvis-voice python -c '
import asyncio, sys
sys.path.insert(0, "/app")
import wyoming_client as w

TARGETS = [("piper", "jarvis-piper", 10200, "tts"),
           ("whisper", "jarvis-whisper", 10300, "asr"),
           ("openwakeword", "jarvis-wake-word", 10400, "wake")]

async def main():
    for label, host, port, kind in TARGETS:
        try:
            info = await w.describe(host, port, timeout=10)
        except Exception as exc:
            print("%s|error|0|no|%s" % (label, type(exc).__name__))
            continue
        if not info:
            print("%s|no-reply|0|no|" % label)
            continue
        programs = info.get(kind) or []
        installed = 0
        wake = "no"
        for prog in programs:
            models = list(prog.get("models") or []) + list(prog.get("voices") or [])
            for m in models:
                if not m.get("installed"):
                    continue
                installed += 1
                if m.get("name") == "hey_jarvis":
                    wake = "yes"
        names = ",".join(p.get("name", "?") for p in programs) or "-"
        print("%s|%s|%d|%s|" % (label, names, installed, wake))

asyncio.run(main())
' 2>/dev/null | tr -d '\r')

if [ -z "$JARVIS_WY" ]; then
  fail "the Wyoming probe could not run inside jarvis-voice - piper/whisper/openwakeword are UNVERIFIED, which is not the same as healthy"
else
  # Read from a file, not a pipe. Section 8 records why: a `while read` on the
  # right of a pipe runs in a subshell, so both the counters and the report
  # buffer built inside it are silently discarded.
  WYF=$(mktemp)
  printf '%s\n' "$JARVIS_WY" > "$WYF"
  while IFS='|' read -r label programs installed wake err; do
    [ -z "${label:-}" ] && continue
    case "$programs" in
      error)
        fail "$label did not answer a Wyoming describe (${err:-connection failed}) - the port is published but nothing is serving the protocol behind it" ;;
      no-reply)
        fail "$label accepted the connection but never sent an 'info' reply - up, listening, and not serving" ;;
      *)
        if [ "${installed:-0}" -lt 1 ]; then
          fail "$label answers describe as '$programs' but has 0 INSTALLED models - the model download never finished, so it will accept audio and return nothing"
        else
          ok "$label answers Wyoming describe ($programs, $installed model(s) installed)"
        fi ;;
    esac
    # The entire stack is built around one phrase. openWakeWord ships several
    # pretrained wake models and only preloads whichever `--preload-model`
    # names, so a typo in that one compose argument leaves the service
    # completely healthy -- connected, answering describe, listening hard for
    # "okay nabu" -- and stone deaf to its own name, with nothing anywhere
    # reporting a fault. Assert the model by name rather than trusting the flag.
    if [ "$label" = "openwakeword" ]; then
      if [ "$wake" = "yes" ]; then
        ok "openwakeword has the hey_jarvis model installed"
      else
        fail "openwakeword does NOT list hey_jarvis - the wake word is missing, so nothing will ever trigger; check --preload-model in jarvis/docker-compose.yml"
      fi
    fi
  done < "$WYF"
  rm -f "$WYF"
fi

# Incident 2026-08-08: the compose healthcheck curled http://localhost:8123/api/,
# which REQUIRES a bearer token, so it returned 401 on every probe. The container
# sat "unhealthy" through 56 consecutive probes while Home Assistant was serving
# perfectly, and each probe was recorded as a failed login (http.ban warnings).
# /manifest.json needs no credential at all, so a non-200 here is a genuine
# outage rather than a missing token -- which is the whole point of probing it.
hacode=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "http://localhost:8123/manifest.json" 2>/dev/null)
case "$hacode" in
  200) ok "Home Assistant answers UNAUTHENTICATED on :8123 (/manifest.json 200)" ;;
  *)   fail "Home Assistant /manifest.json returned '$hacode', expected 200 - this endpoint needs no token, so this is a real outage and not an auth problem" ;;
esac

# A long-lived access token can be revoked from the Home Assistant UI at any
# moment, and nothing else on this box would notice. Every voice command goes
# through POST /api/conversation/process carrying this token: if it stops
# authenticating, the microphone still records, the transcript still appears on
# screen, and every command silently does nothing. Note that jarvis-voice's own
# /api/status reports `hass_token_configured` for a token that is PRESENT, not
# one that WORKS -- so it cannot catch this and this check is not redundant.
if [ -n "${JARVIS_HASS_TOKEN:-}" ]; then
  tcode=$(curl -s -o /dev/null -w '%{http_code}' -m 15 \
          -H "Authorization: Bearer $JARVIS_HASS_TOKEN" "http://localhost:8123/api/" 2>/dev/null)
  case "$tcode" in
    200)     ok "JARVIS_HASS_TOKEN still authenticates against Home Assistant (/api/ 200)" ;;
    401|403) fail "JARVIS_HASS_TOKEN is REVOKED or wrong (HTTP $tcode) - speech will transcribe fine and then do nothing at all; mint a new long-lived token in HA and update secrets.env" ;;
    *)       fail "Home Assistant /api/ returned '$tcode' with the stored token, expected 200" ;;
  esac
else
  fail "JARVIS_HASS_TOKEN is not set in $SECRETS_FILE - the voice bridge cannot execute a single command"
fi

# Two distinct failures, one endpoint. /api/tags answering proves the daemon is
# up and says nothing about whether a model is there. ollama_data is an external
# volume, so a volume recreated empty leaves Ollama cheerfully answering 200
# with {"models":[]} while every conversation dies at the model-load step.
OLL=$(curl -s -m 20 "http://localhost:11434/api/tags" 2>/dev/null)
nmodels=$(printf '%s' "$OLL" | jqs -r '.models | length')
case "${nmodels:-}" in
  ''|*[!0-9]*) fail "Ollama /api/tags unreadable on :11434 - the conversation agent has no backend at all" ;;
  0)           fail "Ollama answers but holds ZERO models - ollama_data was recreated empty; re-pull with 'docker exec jarvis-ollama ollama pull <model>'" ;;
  *)           ok "Ollama answers /api/tags with $nmodels model(s)" ;;
esac

# ...and the model Home Assistant is actually CONFIGURED to use has to be one of
# them. Read out of HA rather than hardcoded here: model selection for this
# stack is still open, so a name pinned into this script would go stale and
# start failing a perfectly healthy server. The failure this catches is the
# mirror image of the one above -- a conversation agent pointing at a model that
# was deleted, renamed or never pulled, which surfaces only as every single
# spoken command erroring out with nothing else looking wrong.
WANTED_MODEL=$(docker exec jarvis-hass python3 -c '
import json
try:
    entries = json.load(open("/config/.storage/core.config_entries"))["data"]["entries"]
except Exception:
    raise SystemExit
for entry in entries:
    if entry.get("domain") != "ollama":
        continue
    for sub in entry.get("subentries") or []:
        model = (sub.get("data") or {}).get("model")
        if model:
            print(model)
            raise SystemExit
' 2>/dev/null | tr -d '\r' | head -1)
if [ -z "$WANTED_MODEL" ]; then
  warn "no Ollama conversation agent is configured in Home Assistant - speech will transcribe but nothing will answer it"
elif [ "${nmodels:-0}" -gt 0 ]; then
  havem=$(printf '%s' "$OLL" | jqs -r --arg m "$WANTED_MODEL" '[.models[]?|select(.name==$m)]|length')
  if [ "${havem:-0}" -gt 0 ]; then
    ok "HA conversation agent model '$WANTED_MODEL' is present in Ollama"
  else
    fail "HA conversation agent is configured for model '$WANTED_MODEL' but Ollama does not have it - every voice command will error"
  fi
fi

# The bridge's own view of its dependencies, which is the view the browser gets.
# Checked IN ADDITION to the direct probes above, never instead of them: an
# earlier version of this very endpoint reported all three voice services down
# because it spoke HTTP at raw-TCP Wyoming ports. This endpoint agreeing with
# the independent probes is itself part of the signal; if they ever disagree,
# the direct probes win.
VST=$(curl -s -m 25 "http://localhost:3100/api/status" 2>/dev/null)
if printf '%s' "$VST" | grep -q '"whisper"'; then
  vbad=$(printf '%s' "$VST" | jqs -r '[ to_entries[] | select(.value|type=="string") | select(.value != "ok") | "\(.key)=\(.value)" ] | join(" ")')
  vtok=$(printf '%s' "$VST" | jqs -r '.hass_token_configured')
  if [ -n "$vbad" ]; then
    fail "jarvis-voice /api/status does not report every service ok: $vbad"
  else
    ok "jarvis-voice /api/status reports every service ok"
  fi
  if [ "$vtok" != "true" ]; then
    fail "jarvis-voice has no HA token in its environment - it can hear you and cannot act; check env_file in jarvis/docker-compose.yml"
  fi
else
  fail "jarvis-voice /api/status did not answer on :3100 - the browser client has no backend"
fi

# Incident 2026-08-08: the browser client requires a SECURE CONTEXT. Served over
# plain HTTP on a tailnet IP, `navigator.mediaDevices` is undefined, so the
# microphone dies with a bare TypeError and the page simply looks broken -- no
# log anywhere names the real cause. `tailscale serve` on 8443 is what supplies
# the HTTPS, so losing that one mapping silently kills voice input while all six
# containers stay green and every check above still passes.
#
# Port 443 is asserted in the same breath because it is the neighbouring
# mistake: `tailscale serve` run without an explicit port targets 443 and
# REPLACES the handler there rather than adding to it, so a careless re-run
# would take Vaultwarden off the tailnet. JARVIS is on 8443 specifically to
# avoid that, and this is what proves it stayed that way.
TS_EXE="/c/Program Files/Tailscale/tailscale.exe"
if [ -x "$TS_EXE" ]; then
  TSJ=$("$TS_EXE" serve status --json 2>/dev/null)
  ts_port() { printf '%s' "$TSJ" | jqs -r --arg p "$1" \
    '.Web // {} | to_entries[] | select((.key|split(":")|last) == $p) | .value.Handlers["/"].Proxy // empty'; }
  j8443=$(ts_port 8443)
  j443=$(ts_port 443)
  case "$j8443" in
    *:3100) ok "tailscale serve maps 8443 -> $j8443 (HTTPS, so getUserMedia works)" ;;
    "")     fail "tailscale serve has NO mapping on 8443 - the JARVIS UI is not served over HTTPS, so navigator.mediaDevices is undefined and the microphone cannot start" ;;
    *)      fail "tailscale serve 8443 points at '$j8443', expected localhost:3100 - the JARVIS UI is not behind that HTTPS mapping any more" ;;
  esac
  case "$j443" in
    *:8222) ok "tailscale serve still maps 443 -> $j443 (vaultwarden undisturbed)" ;;
    "")     fail "tailscale serve has NO mapping on 443 - Vaultwarden is off the tailnet" ;;
    *)      fail "tailscale serve 443 points at '$j443', expected 127.0.0.1:8222 - something displaced Vaultwarden's mapping" ;;
  esac
else
  warn "cannot run $TS_EXE - the JARVIS HTTPS mapping is unverified, so the microphone may be broken without any other symptom"
fi

# ---------------------------------------------------------------------------
sec "17. DNS filtering actually resolves"
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
sec "18. Immich library integrity"
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
    im_total=$(awk 'NF {n++} END {print n+0}' /tmp/_immich_paths.txt 2>/dev/null)
    if [ "${im_total:-0}" -gt 0 ]; then
      docker cp /tmp/_immich_paths.txt "$IM_SRV:/tmp/_audit_paths.txt" >/dev/null 2>&1
      # Emit the PATHS, not just a count. Verified 2026-07-28 against
      # bind-Immich_20260728-175630.tar.gz: none of the 105 known-lost files
      # are in any backup, because the loss predates the first backup on
      # 2026-07-27. They are gone permanently and no further run will change
      # that -- so a bare count means this check would FAIL forever, and an
      # audit that is permanently red is one people stop reading. That is the
      # same failure mode as having no check at all.
      #
      # Instead: the 105 already-lost paths are recorded in a baseline file, and
      # only a loss NOT in that baseline fails. The tripwire keeps working for
      # the next occurrence, which is the reason the check was written.
      #
      # The audit NEVER writes the baseline. It is a human decision to declare a
      # file permanently lost, and a check that quietly absorbs new losses into
      # its own baseline is worse than useless. Regenerate deliberately with:
      #   docker exec immich-database-1 psql -U "<user>" -d immich -t -A \
      #     -c 'SELECT "originalPath" FROM asset WHERE "deletedAt" IS NULL;' ...
      docker exec "$IM_SRV" sh -c '
        while IFS= read -r p; do
          [ -z "$p" ] && continue
          [ -f "$p" ] || printf "%s\n" "$p"
        done < /tmp/_audit_paths.txt' 2>/dev/null | tr -d '\r' | sort > /tmp/_immich_missing.txt
      docker exec "$IM_SRV" rm -f /tmp/_audit_paths.txt >/dev/null 2>&1
      rm -f /tmp/_immich_paths.txt
      im_missing=$(awk 'NF {n++} END {print n+0}' /tmp/_immich_missing.txt 2>/dev/null)
      IM_BASELINE="${IM_BASELINE:-/c/ServerData/Stacks/immich-lost-baseline.txt}"
      if [ "${im_missing:-0}" -eq 0 ]; then
        ok "Immich: all ${im_total} active assets have their original file"
      elif [ ! -f "$IM_BASELINE" ]; then
        fail "Immich: ${im_missing}/${im_total} assets have no original file on disk, and no baseline exists at $IM_BASELINE -- downloads for those fail, thumbnails still render so the UI looks fine"
      else
        # tr -d '\r' on the BASELINE side too, not just the docker side.
        # comm compares whole lines byte for byte. The baseline is gitignored
        # (*.txt) so it lives only on this Windows box, and one save from an
        # editor that writes CRLF would have appended \r to all 105 known-lost
        # paths -- none of which would then match, so every one reads as a
        # NEWLY missing original and this check hard-FAILs with a fabricated
        # fresh-loss alarm. Verified LF-only today; defended so it stays true.
        #
        # Computed once into a temp file rather than running the same comm
        # pipeline twice, once for the count and again for the example path.
        comm -23 /tmp/_immich_missing.txt \
             <(tr -d '\r' < "$IM_BASELINE" | grep -v '^#' | grep . | sort) \
             > /tmp/_immich_new.txt
        # Counted with awk, not `grep -c . || echo 0`. grep -c exits 1 on zero
        # matches, so under `set -o pipefail` the fallback fired IN ADDITION to
        # grep's own "0" and left this variable holding two lines ("0\n0"). The
        # numeric test below then aborted with "integer expected" -- printing a
        # raw bash error into the audit report and falling through to the else
        # branch. Same trap, and same fix, as the stuck-episodes count above.
        im_new=$(awk 'NF {n++} END {print n+0}' /tmp/_immich_new.txt 2>/dev/null)
        if [ "${im_new:-0}" -gt 0 ]; then
          fail "Immich: ${im_new} NEWLY missing original(s) beyond the ${im_missing} known lost -- this is a fresh loss, and a backup may still hold them. First: $(head -1 /tmp/_immich_new.txt)"
        else
          warn "Immich: ${im_missing}/${im_total} assets have no original file -- all of them are the known, permanently lost set recorded in $(basename "$IM_BASELINE"); nothing new"
        fi
      fi
      rm -f /tmp/_immich_missing.txt /tmp/_immich_new.txt
    else
      warn "Immich: could not read the asset list from Postgres"
    fi
  else
    warn "Immich: could not determine POSTGRES_USER from $IM_DB"
  fi
fi

# ---------------------------------------------------------------------------
sec "19. Docker daemon hygiene"
# Incident 2026-07-28: every container was running with the json-file log driver
# and NO size limit ("json-file map[]"). Container logs grow without bound, and
# they live on the Docker Desktop VM disk, so a single chatty container filling
# it takes down every service at once -- with no warning, because nothing here
# was looking. The daemon-level setting covers containers that have no compose
# file in this tree (Immich, Traccar, Portainer, AdGuard) as well as those that do.
DAEMON_JSON="${RW_DAEMON_JSON:-/c/Users/${USER:-$USERNAME}/.docker/daemon.json}"
if [ -r "$DAEMON_JSON" ]; then
  if grep -q '"max-size"' "$DAEMON_JSON"; then
    ok "docker daemon has a log size limit configured"
  else
    fail "docker daemon has NO log rotation - container logs grow until the VM disk fills and every service stops"
  fi
else
  warn "cannot read $DAEMON_JSON to confirm log rotation"
fi

# A daemon setting only reaches a container when that container is (re)created.
# Setting it and assuming it applied is the trap; this checks what the running
# containers actually have.
nolimit=0; checked=0
for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
  checked=$((checked+1))
  cfg=$(docker inspect "$c" --format '{{.HostConfig.LogConfig.Config}}' 2>/dev/null)
  case "$cfg" in
    *max-size*) ;;
    *) nolimit=$((nolimit+1)) ;;
  esac
done
if [ "$nolimit" -gt 0 ]; then
  warn "$nolimit/$checked running container(s) still have no log size limit - they keep the old setting until recreated (docker compose up -d)"
else
  ok "all $checked running containers have a log size limit"
fi

# Incident 2026-07-28: every 'docker pull' on this host failed with "A specified
# logon session does not exist", so AdGuard could not be started and four
# pending image updates could not be applied. Nothing detected it, because the
# update audit reports what is OUT OF DATE, not whether updating is possible.
if docker system info >/dev/null 2>&1; then
  ok "docker daemon is responsive"
else
  fail "docker system info failed - the daemon is not answering"
fi
# Incident 2026-07-28: Immich's compose file lived under
# ~/.docker/cagent/working_directories/<hash>/default/immich/ -- a Docker
# Desktop AI-agent scratch directory that Docker is free to delete -- and
# Portainer had no compose file at all, having been created with a bare
# `docker run`. Losing either would have meant rebuilding the photo library
# and the management UI from memory. Every container must be defined by a file
# in this tree, where it is backed up and in version control.
stray=0; stray_names=""
for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
  cf=$(docker inspect "$c" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>/dev/null)
  case "$cf" in
    *ServerData?Stacks*) ;;
    "") stray=$((stray+1)); stray_names="$stray_names $c(no-compose)" ;;
    *)  stray=$((stray+1)); stray_names="$stray_names $c" ;;
  esac
done
if [ "$stray" -gt 0 ]; then
  warn "$stray container(s) are not defined by a compose file in Stacks/ -- not backed up, not in version control:$stray_names"
else
  ok "every running container is defined from Stacks/"
fi

# Incident 2026-07-28: server-room/docker-compose.yml gained an explicit
# `container_name: glances`, which renamed the container from the
# compose-generated server-room-glances-1. Section 15's expected-container list
# still held the old name, so the next audit would have failed on a healthy
# service -- and the mirror-image mistake (a NEW service nobody adds to the
# list) fails the other way, silently, which is how AdGuard went missing.
#
# The check above asks "is every running container defined in Stacks/". These
# two ask the reverse, which is where both of those bugs actually live:
#   a) every container_name declared in Stacks/ is running, or is explicitly
#      recorded below as deliberately not deployed;
#   b) every running container appears in one of this script's own checked
#      lists, so a rename or an addition cannot quietly go unmonitored.
#
# NOT_DEPLOYED is an allowlist on purpose: "we chose not to run this" has to be
# a recorded decision, not the same silence as "this died". Empty right now --
# it held the three music-alexa containers until that stack was dropped on
# 2026-07-28. Keep the mechanism: the next deliberately-dormant stack needs it,
# and an empty list is not the same thing as no list.
NOT_DEPLOYED=""
declared=$(grep -rhE '^\s*container_name:\s*\S+' /c/ServerData/Stacks/*/docker-compose.yml 2>/dev/null \
           | sed -E 's/^\s*container_name:\s*//' | tr -d '\r' | sort -u)
missing=""; undeployed=0
for c in $declared; do
  case " $NOT_DEPLOYED " in *" $c "*) undeployed=$((undeployed+1)); continue ;; esac
  docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null | grep -q running || missing="$missing $c"
done
if [ -n "$missing" ]; then
  fail "container_name declared in Stacks/ but not running:$missing"
else
  ok "every container_name declared in Stacks/ is running ($undeployed deliberately not deployed)"
fi

unchecked=""
for c in $(docker ps --format '{{.Names}}' 2>/dev/null); do
  case " $EXPECTED $OTHER_CONTAINERS $JARVIS_CONTAINERS " in
    *" $c "*) ;;
    *) unchecked="$unchecked $c" ;;
  esac
done
if [ -n "$unchecked" ]; then
  fail "running container(s) absent from this audit's checked lists -- they fail invisibly:$unchecked"
else
  ok "every running container is covered by an audit check"
fi

# Gated on the helper EXISTING, not on credsStore being configured. An earlier
# version only checked when "credsStore" was present in config.json, so simply
# deleting that line made the audit go green while every pull still failed --
# a check that reports success because it stopped looking is worse than no
# check. Docker Desktop reinstates the line anyway.
if command -v docker-credential-desktop.exe >/dev/null 2>&1; then
  if docker-credential-desktop.exe list >/dev/null 2>&1; then
    ok "docker credential helper responds (image pulls can authenticate)"
  else
    # Known broken on this host and not fixable from here: restarting Docker
    # Desktop does not clear it, removing "credsStore" does not clear it
    # (Docker Desktop rewrites the file), and Windows Credential Manager holds
    # no docker entry at all. Use the workaround rather than fighting it.
    warn "docker credential helper is failing - plain 'docker pull' will error; use 'bash docker-pull.sh <image>' instead, which pulls via the daemon API"
  fi
fi


# ---------------------------------------------------------------------------
sec "20. JARVIS intent routing and prompt"
# Everything in this section exists because it broke on 2026-08-08/09.
#
# The stack can be perfectly healthy -- every container up, every Wyoming
# service answering -- while JARVIS is quietly 80x slower than it should be and
# confirming actions it never performed. Section 16 checks that the parts are
# alive. This section checks that the parts are WIRED THE WAY THEY WERE BUILT.

JARVIS_DIR="/c/ServerData/Stacks/jarvis"

# --- the live system prompt vs the repo ------------------------------------
# Incident 2026-08-08: the repo's jarvis-prompt.txt had the "movie, film,
# gaming -> Movie time." line removed because script.movie_time is deliberately
# NOT exposed to the model (exposing it would push the tool count to 18 and
# break the tested budget). The LIVE prompt, which lives in the config entry's
# options and not on disk, still had it. So the model kept saying "Movie time
# activated." while script.movie_time.last_triggered stayed None -- a spoken
# confirmation for an action that never happened, which is undetectable by the
# person listening. The two copies drift silently and nothing else notices.
if [ -r "$JARVIS_DIR/jarvis-prompt.txt" ]; then
  LIVE_PROMPT=$(docker exec jarvis-hass python3 -c '
import json
try:
    d = json.load(open("/config/.storage/core.config_entries", encoding="utf-8"))
except Exception:
    raise SystemExit(0)
for e in d["data"]["entries"]:
    if e.get("domain") != "ollama":
        continue
    for s in e.get("subentries", []):
        if s.get("subentry_type") == "conversation":
            print((s.get("data", {}).get("prompt") or "").strip())
            raise SystemExit(0)
' 2>/dev/null)
  REPO_PROMPT=$(tr -d '\r' < "$JARVIS_DIR/jarvis-prompt.txt" | sed -e 's/[[:space:]]*$//')
  LIVE_NORM=$(printf '%s' "$LIVE_PROMPT" | sed -e 's/[[:space:]]*$//')
  if [ -z "$LIVE_NORM" ]; then
    warn "could not read the live JARVIS system prompt from jarvis-hass - drift against the repo is UNVERIFIED"
  elif [ "$(printf '%s' "$LIVE_NORM" | md5sum)" = "$(printf '%s' "$REPO_PROMPT" | md5sum)" ]; then
    ok "live JARVIS system prompt matches jarvis-prompt.txt"
  else
    fail "live JARVIS system prompt DIFFERS from jarvis-prompt.txt - the model is being told something the repo does not say; redeploy with 'docker exec jarvis-hass python3 /tmp/patch_prompt_offline.py --apply' (HA must be stopped) and see jarvis/deploy_prompt.py"
  fi

  # The specific line that caused the phantom action. Checked by name because
  # a future edit could reintroduce it without changing anything else.
  if printf '%s' "$LIVE_NORM" | grep -qi "movie time"; then
    fail "the live prompt still references 'Movie time' - script.movie_time is NOT exposed to the model, so it will narrate the action instead of performing it"
  else
    ok "live prompt does not reference the unexposed Movie time script"
  fi
else
  warn "$JARVIS_DIR/jarvis-prompt.txt is missing - cannot check live prompt drift"
fi

# --- local-first routing ----------------------------------------------------
# Measured 2026-08-09: 12 of 15 non-actuating benchmark utterances resolve
# through HA's built-in parser at a median 18.3 ms, against 1472 ms through
# qwen2.5:3b. The failure mode is NOT an error -- if the custom sentences fail
# to load or the router is disabled, everything still works, just ~80x slower
# and burning GPU on every "lights on". Nothing user-visible reports that, so
# the router's own counters are the only signal.
ROUTER_JSON=$(curl -s -m 10 "http://localhost:3100/api/status" 2>/dev/null)
if [ -z "$ROUTER_JSON" ]; then
  warn "jarvis-voice /api/status did not answer - intent routing is UNVERIFIED"
else
  if printf '%s' "$ROUTER_JSON" | grep -q '"router"'; then
    ok "jarvis-voice exposes router statistics"
    R_LOCAL=$(printf '%s' "$ROUTER_JSON" | sed -n 's/.*"local_hits":[[:space:]]*\([0-9]*\).*/\1/p')
    R_LLM=$(printf '%s' "$ROUTER_JSON" | sed -n 's/.*"llm_hits":[[:space:]]*\([0-9]*\).*/\1/p')
    R_TOTAL=$(( ${R_LOCAL:-0} + ${R_LLM:-0} ))
    # Only meaningful once real commands have gone through; a freshly restarted
    # container legitimately reports 0/0 and that is not a fault.
    if [ "$R_TOTAL" -ge 20 ]; then
      R_SHARE=$(( ${R_LOCAL:-0} * 100 / R_TOTAL ))
      if [ "$R_SHARE" -lt 40 ]; then
        warn "only ${R_SHARE}% of the last $R_TOTAL commands were handled locally (expected ~80%) - custom sentences may have failed to load; check /config/custom_sentences/en/jarvis.yaml"
      else
        ok "local-first routing is handling ${R_SHARE}% of commands without the model"
      fi
    else
      ok "router counters present ($R_TOTAL commands since restart, too few to judge the split)"
    fi
  else
    fail "jarvis-voice /api/status has no router block - voice_server.py is running a build WITHOUT the local-first router, so every command pays full model latency"
  fi
fi

# --- the custom sentences that make local routing possible ------------------
# tuya-local wipes its devices/ directory on upgrade and this file lives in the
# same class of "config HA owns that an upgrade can drop". If it vanishes,
# argument extraction and the whole comfort table silently fall back to the LLM.
if MSYS_NO_PATHCONV=1 docker exec jarvis-hass test -f /config/custom_sentences/en/jarvis.yaml 2>/dev/null; then
  SENT_COUNT=$(MSYS_NO_PATHCONV=1 docker exec jarvis-hass sh -c "grep -c '^  Jarvis' /config/custom_sentences/en/jarvis.yaml" 2>/dev/null | tr -d '\r')
  if [ "${SENT_COUNT:-0}" -ge 15 ]; then
    ok "custom sentences installed ($SENT_COUNT JARVIS intents)"
  else
    warn "custom_sentences/en/jarvis.yaml has only ${SENT_COUNT:-0} JARVIS intents - expected at least 15; local routing coverage will be reduced"
  fi
else
  fail "/config/custom_sentences/en/jarvis.yaml is MISSING - argument extraction ('set the AC to 22') and the comfort table all fall back to the model at ~1.5s each"
fi

if MSYS_NO_PATHCONV=1 docker exec jarvis-hass test -f /config/packages/jarvis_intents.yaml 2>/dev/null; then
  ok "intent_script handlers installed"
else
  fail "/config/packages/jarvis_intents.yaml is MISSING - the custom sentences will match and then do NOTHING, which is worse than not matching"
fi

# --- entity aliases ---------------------------------------------------------
# Incident 2026-08-09: the lights are registered as "Gang 1/2/3". Nobody says
# that out loud, so every natural light command missed the local parser and
# went to the model. Aliases live in the entity registry, which HA rewrites on
# shutdown, so they are exactly the kind of thing that can vanish.
# NOTE the key. As of entity-registry minor_version 23 the aliases live under
# "aliases_v2"; the old "aliases" key is still present on every record but is now
# permanently EMPTY. Reading only "aliases" reports zero for a registry that is
# perfectly populated -- that briefly looked like the aliases had been lost in a
# restart. Read both so this survives the migration in either direction.
ALIAS_N=$(MSYS_NO_PATHCONV=1 docker exec jarvis-hass python3 -c '
import json
try:
    d = json.load(open("/config/.storage/core.entity_registry", encoding="utf-8"))
except Exception:
    raise SystemExit(0)
n = 0
for e in d["data"]["entities"]:
    if e.get("entity_id", "").startswith(("light.node_switch_gang", "fan.fan")):
        n += len(e.get("aliases_v2") or e.get("aliases") or [])
print(n)
' 2>/dev/null | tr -d '\r')
if [ "${ALIAS_N:-0}" -ge 10 ]; then
  ok "voice aliases present on the lights and fan ($ALIAS_N total)"
else
  warn "only ${ALIAS_N:-0} voice aliases on the lights/fan - natural phrasings like 'turn on light one' will miss the local parser and cost ~1.5s each; re-run 'docker exec jarvis-voice python /app/ha_aliases.py --apply'"
fi

# --- closed-loop verification actually loaded -------------------------------
# Incident 2026-08-09: the running jarvis-voice image had NO verify/ directory.
# The server logged "replies will NOT be checked against Home Assistant" once at
# startup and then spoke unverified confirmations for hours. The repo looked
# complete the whole time -- the file existed in git and not in the image.
# Capture to temp file instead of shell variable: the log is >1 MB and exceeds
# ARG_MAX when passed as an echo argument, which triggers SIGPIPE (exit 141)
# under pipefail — making grep appear to fail even when the match exists.
_docker_logs_tmp="/tmp/_audit_docker_logs_$$.txt"
docker logs jarvis-voice > "$_docker_logs_tmp" 2>&1
if grep -q "closed-loop verification enabled" "$_docker_logs_tmp"; then
  ok "closed-loop reply verification is enabled"
elif grep -qi "could not be imported" "$_docker_logs_tmp"; then
  fail "jarvis-voice could NOT import verify/closed_loop.py - replies are spoken WITHOUT being checked against Home Assistant, so the model can confirm actions it never took"
else
  warn "cannot tell from jarvis-voice logs whether closed-loop verification is enabled"
fi
rm -f "$_docker_logs_tmp"

# --- A6 closed-loop verification: structured log + no dead code -------------
# Incident 2026-08-16: router.py had a _verify_all() closure that rebound
# `speech` via nonlocal AFTER RouteResult had already been constructed. The
# spoken reply went out with the old string; complaints only appeared in the
# background task and were never heard. Delete that pattern, replace with one
# structured log line per verify call, and assert both in the audit.
_A6_REPO_DISPATCH=$(grep -c "A6 dispatching verify" "$JARVIS_DIR/router.py" 2>/dev/null || true)
_A6_REPO_DEAD=$(grep -c "nonlocal speech" "$JARVIS_DIR/router.py" 2>/dev/null || true)
_A6_REPO_LOG_LINE=$(grep -c 'A6 verify %s tool=' "$JARVIS_DIR/hermes/executor.py" 2>/dev/null || true)
if [ "$_A6_REPO_DEAD" -eq 0 ] && [ "$_A6_REPO_DISPATCH" -ge 1 ] && [ "$_A6_REPO_LOG_LINE" -ge 1 ]; then
  ok "A6 verify: no dead nonlocal-speech code; dispatch + structured log line present"
else
  fail "A6 verify broken in repo: dead_code=$_A6_REPO_DEAD dispatch=$_A6_REPO_DISPATCH log_line=$_A6_REPO_LOG_LINE"
fi

# Mismatch rate from docker logs (last 72 h). One line per verified actuation;
# MISMATCH = actuation did not produce the expected entity state.
_A6_MATCH=$(docker logs jarvis-voice --since "72h" 2>/dev/null | grep -c "A6 verify MATCH" || true)
_A6_MISMATCH=$(docker logs jarvis-voice --since "72h" 2>/dev/null | grep -c "A6 verify MISMATCH" || true)
_A6_TOTAL=$(( _A6_MATCH + _A6_MISMATCH ))
if [ "$_A6_TOTAL" -eq 0 ]; then
  warn "A6 verify: no actuation events in last 72 h of logs - mismatch rate is UNKNOWN"
elif [ "$_A6_MISMATCH" -eq 0 ]; then
  ok "A6 verify: $_A6_TOTAL actuations checked, 0 mismatches (100% success)"
else
  _PCT=$(( _A6_MISMATCH * 100 / _A6_TOTAL ))
  warn "A6 verify: $_A6_MISMATCH/_A6_TOTAL mismatches ($_PCT%) in last 72 h — actuation chain is failing somewhere"
fi


# ---------------------------------------------------------------------------
sec "21. Host stays awake"
# Incident 2026-08-09: the host entered Modern Standby at 00:24:41 and did not
# leave until 07:34:52 -- 7 h 10 m with every container unreachable. This box
# reports "Standby (S0 Low Power Idle) NETWORK DISCONNECTED", so during standby
# the laptop still looks powered on, fans and all, while the network stack is
# down. Natik reported it as "the whole server turns off".
#
# It also silently ate the night jobs: Daily Backup, Update Audit and Weekly
# Audit all reported "terminated" for runs scheduled inside that window. Those
# looked like three unrelated task bugs and were not -- they were this.
#
# The non-obvious part, and the reason this check tests the ASSERTION and not
# just the timeouts: when that outage happened every sleep timeout was ALREADY
# 0/never. Idle timeout was never the trigger. Lid close (LIDACTION=1) and
# direct SetSuspendState calls bypass the timeout entirely, and the vendor power
# app rewrites scheme values when it switches between its four profiles
# (Silent/Performance/Turbo/Balanced), quietly undoing powercfg. So a green
# "timeouts are 0" reading proves nothing on its own.
if command -v powercfg.exe >/dev/null 2>&1; then

  # 1. Timeouts and lid action, on EVERY scheme -- the vendor app can switch
  #    profiles at any time, so a setting fixed on only the active one is a
  #    setting that comes back.
  _sleepbad=""
  for _g in $(powercfg.exe -L 2>/dev/null | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'); do
    _nm=$(powercfg.exe -L 2>/dev/null | grep "$_g" | sed -E 's/.*\((.*)\).*/\1/' | tr -d '\r')
    for _pair in "SUB_SLEEP STANDBYIDLE sleep" "SUB_VIDEO VIDEOIDLE display-off" "SUB_BUTTONS LIDACTION lid-close"; do
      set -- $_pair
      _v=$(powercfg.exe -q "$_g" "$1" "$2" 2>/dev/null | grep -i 'Current AC Power Setting Index' | grep -oE '0x[0-9a-f]+' | head -1)
      [ -z "$_v" ] && continue
      if [ "$((_v))" -ne 0 ] 2>/dev/null; then
        _sleepbad="${_sleepbad}${_nm}/$3=$((_v)) "
      fi
    done
  done
  if [ -n "$_sleepbad" ]; then
    fail "host can sleep or blank its display: $_sleepbad-- set these to 0 on ALL schemes (see keep-awake.ps1 header)"
  else
    ok "sleep, display-off and lid-close are 0 (never) on every power scheme"
  fi

  # 2. The execution-state assertion. This is the load-bearing check: it is the
  #    only thing here that survives the vendor app rewriting a scheme.
  #    keep-awake.ps1 must appear in BOTH the DISPLAY and SYSTEM request lists.
  #    Checking "is the process running" is not equivalent -- the first version
  #    of that script ran happily for minutes while asserting nothing at all,
  #    because PowerShell 5.1 parses 0x80000000 as a negative Int32 and the
  #    [uint32] cast threw, leaving the flag $null and silently downgrading
  #    ES_CONTINUOUS to a one-shot nudge. Only the requests table caught it.
  # `-requests`, not `/requests`: Git Bash's MSYS path conversion rewrites the
  # slash form into a Windows path and powercfg answers "Invalid Parameters",
  # which this check would then read as "no assertion held" and FAIL on a
  # perfectly healthy host. powercfg accepts either prefix.
  _req=$(powercfg.exe -requests 2>/dev/null | tr -d '\r')
  _disp=$(printf '%s' "$_req" | awk '/^DISPLAY:/{f=1;next}/^[A-Z]+:/{f=0}f' | grep -ci 'powershell.exe')
  _sys=$(printf  '%s' "$_req" | awk '/^SYSTEM:/{f=1;next}/^[A-Z]+:/{f=0}f'  | grep -ci 'powershell.exe')
  if [ "${_disp:-0}" -ge 1 ] && [ "${_sys:-0}" -ge 1 ]; then
    ok "keep-awake assertion is held (powercfg /requests lists DISPLAY + SYSTEM)"
  elif [ "${_sys:-0}" -ge 1 ]; then
    warn "SYSTEM wake assertion is held but DISPLAY is not - the box stays up but the panel can still blank; check keep-awake.ps1 is passing ES_DISPLAY_REQUIRED"
  else
    fail "NO keep-awake assertion held - powercfg /requests shows no powershell.exe under DISPLAY/SYSTEM; the 'JARVIS - Keep Awake' task is dead or asserting nothing (a running process is NOT proof - see this section's comment)"
  fi

  # 3. Did we actually sleep since the last audit? Direct evidence beats
  #    inference from config. Kernel-Power 506 = entering Modern Standby.
  _ent=$(powershell.exe -NoProfile -NonInteractive -Command "try{(Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='Microsoft-Windows-Kernel-Power';Id=506;StartTime=(Get-Date).AddDays(-7)} -ErrorAction Stop|Measure-Object).Count}catch{0}" 2>/dev/null | tr -d '\r\n ')
  case "$_ent" in
    ''|*[!0-9]*) warn "could not read Kernel-Power 506 events - cannot confirm whether the host slept in the last 7 days" ;;
    0) ok "no Modern Standby entries in the last 7 days" ;;
    *) warn "host entered Modern Standby $_ent time(s) in the last 7 days - each one is an outage window with the network down; correlate with any 'terminated' scheduled tasks above" ;;
  esac

  # 4. Firmware-level S0 override. Added 2026-08-12 after keep-awake was green
  #    while the host still had Kernel-Power 506 entries from Modern Standby.
  #    PlatformAoAcOverride=0 disables Modern Standby on machines that honor it;
  #    a missing key means the OS can still enter S0 even with timeouts at 0.
  _aoac=$(powershell.exe -NoProfile -NonInteractive -Command "try{(Get-ItemProperty -Path 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Power' -Name PlatformAoAcOverride -ErrorAction Stop).PlatformAoAcOverride}catch{'missing'}" 2>/dev/null | tr -d '\r\n ')
  case "$_aoac" in
    0) ok "Modern Standby override is set (PlatformAoAcOverride=0); reboot required before powercfg /a reflects it" ;;
    missing) warn "Modern Standby override is missing - set HKLM\\SYSTEM\\CurrentControlSet\\Control\\Power\\PlatformAoAcOverride=0 and reboot, or S0 standby can still happen despite green timeouts" ;;
    *) warn "Modern Standby override has unexpected value '$_aoac' - expected PlatformAoAcOverride=0" ;;
  esac
else
  warn "powercfg.exe not on PATH - cannot verify the host is prevented from sleeping"
fi


# ---------------------------------------------------------------------------
sec "22. JARVIS image matches the repo"
# The single most expensive recurring failure in this project. THREE separate
# features shipped, were verified working, and then silently reverted:
#   verify/    - image predated it; jarvis-voice ran with closed-loop checking
#                OFF and spoke confirmations for actions it never performed
#   hermes/    - never COPYd, so voice_server.py could not import it and Hermes
#                stayed a benchmark that never touched production
#   hud.py     - docker cp'd into the running container; all six /api/hud/
#                panels worked until the host rebooted 2026-08-09 07:41, then
#                every one of them returned 404
#
# The mechanism every time: an agent writes a file to disk, copies it into the
# RUNNING container to test, confirms it works, and reports success. It is true
# -- right up until the container is recreated, at which point the image (which
# never got the file) wins and the feature vanishes. Nothing in a normal health
# check catches this, because the container is up and answering.
#
# So this compares the REPO against a FRESH container started from the image,
# not against the running one. Checking the running container would report
# green in exactly the scenario this exists to catch.
JDIR="/c/ServerData/Stacks/jarvis"
if [ -d "$JDIR" ] && docker image inspect jarvis-voice:latest >/dev/null 2>&1; then
  _drift=""; _missing=""; _checked=0
  # The file list is DERIVED FROM Dockerfile.ui, never hand-written. That is the
  # whole point of this block and it was learned the hard way.
  #
  # This check originally carried a hardcoded list. On 2026-08-09 hermes/__init__.py
  # was created one minute AFTER the image was built and only docker cp'd in, so
  # the image lacked it -- and because the hardcoded list did not name
  # __init__.py, this check reported GREEN on exactly the failure it exists to
  # catch. Without that file `import hermes.hermes` raises "'hermes' is not a
  # package"; router.py catches the error, logs one warning, and silently routes
  # everything to the 17-tool agent -- reintroducing the tool cliff invisibly.
  # A hand-maintained list only ever covers the failures you already had.
  #
  # A plain glob of *.py is NOT the alternative: this directory also holds ~30
  # host-side tools (IR capture, Tuya scripts, _probe_*) that must never be in
  # the image, and flagging them would train everyone to ignore this check.
  # Dockerfile.ui is the actual contract for what belongs in the image, so it is
  # the only correct source of truth.
  _files=""
  while IFS= read -r _line; do
    # "COPY a.py b.py ./" -> the files; "COPY hermes/ ./hermes/" -> expand the dir
    for _tok in $(printf '%s' "$_line" | sed -E 's/^COPY[[:space:]]+//' | sed -E 's/[[:space:]]+\.?\/?[^[:space:]]*\/?$//'); do
      case "$_tok" in
        */) _files="$_files $(cd "$JDIR" && find "${_tok%/}" -type f \
              \( -name '*.py' -o -name '*.js' -o -name '*.html' -o -name '*.css' \) \
              -not -path '*__pycache__*' 2>/dev/null | tr '\n' ' ')" ;;
        *)  _files="$_files $_tok" ;;
      esac
    done
  done <<EOF
$(grep '^COPY' "$JDIR/Dockerfile.ui" 2>/dev/null)
EOF
  for f in $_files; do
    [ -f "$JDIR/$f" ] || continue
    _checked=$((_checked+1))
    _r=$(md5sum "$JDIR/$f" 2>/dev/null | cut -d' ' -f1)
    _i=$(MSYS_NO_PATHCONV=1 docker run --rm --entrypoint sh jarvis-voice:latest -c "md5sum /app/$f 2>/dev/null | cut -d' ' -f1" 2>/dev/null | tr -d '\r\n')
    if [ -z "$_i" ]; then _missing="${_missing}$f "
    elif [ "$_r" != "$_i" ]; then _drift="${_drift}$f "; fi
  done
  if [ -n "$_missing" ]; then
    fail "in the repo but NOT in the jarvis-voice image: ${_missing}- these work only until the container is recreated, then they vanish; add them to Dockerfile.ui and rebuild"
  fi
  if [ -n "$_drift" ]; then
    fail "repo and image DIFFER for: ${_drift}- the running container is not what the repo says it is; rebuild with 'docker compose build jarvis-voice'"
  fi
  if [ -z "$_missing" ] && [ -z "$_drift" ]; then
    ok "jarvis-voice image matches the repo for all $_checked server files"
  fi

  # A file being present is not the same as the server importing it. hud.py was
  # present in the repo for hours while every panel 404'd.
  if curl -sf -m 8 "http://localhost:3100/api/hud/vitals" >/dev/null 2>&1; then
    ok "HUD panel endpoints are live (/api/hud/vitals answers)"
  else
    fail "hud.py is in the image but /api/hud/vitals does not answer - the module is present and NOT registered; check hud.register(app) in voice_server.py"
  fi
else
  warn "jarvis dir or jarvis-voice:latest image not found - skipping repo/image drift check"
fi


# ---------------------------------------------------------------------------
sec "23. PC agent runs in the interactive session"
# Incident 2026-08-09: port 8099 answered 200 and a previous session recorded
# "the PC agent is live on 8099". It was not. The responder was the OLD
# wol-helper.ps1, which serves only /health and /status; pc-agent.ps1 had been
# written (60 KB, 24 routes) and never deployed. The earlier check read the app
# allowlist out of the FILE and the health status off the PORT and reported them
# as one working service.
#
# So this checks the SERVICE NAME in the health payload, not the status code.
# Both scripts answer 200 on /health; only the payload distinguishes them.
#
# The session field is equally load-bearing. The old helper ran as
# LogonType=ServiceAccount, which lands in session 0 -- and session 0 has no
# desktop. Every window, input, screenshot and app-launch route would fail there
# while /health still returned a cheerful 200, because serving HTTP does not
# require a desktop. "interactive": true is the agent's own report of whether it
# can actually drive the machine.
_pc=$(curl -s -m 8 "http://localhost:8099/health" 2>/dev/null)
if [ -z "$_pc" ]; then
  fail "nothing answers http://localhost:8099/health - the PC agent is down; voice control of the PC, app launching and typing are all dead"
else
  _svc=$(printf '%s' "$_pc" | grep -oE '"service"[[:space:]]*:[[:space:]]*"[^"]+"' | cut -d'"' -f4)
  _int=$(printf '%s' "$_pc" | grep -oE '"interactive"[[:space:]]*:[[:space:]]*(true|false)' | grep -oE '(true|false)')
  case "$_svc" in
    jarvis-pc-agent)
      if [ "$_int" = "true" ]; then
        ok "PC agent is live on 8099 and interactive (can drive the desktop)"
      else
        fail "PC agent is running but interactive=$_int - it is in session 0 with no desktop, so app launch, typing, windows and screenshots will all fail while /health still returns 200; the task must use LogonType=Interactive"
      fi
      ;;
    jarvis-wol-helper)
      fail "port 8099 is served by the OLD wol-helper, not the PC agent - it has only /health and /status, so every PC control route is absent; enable the 'JARVIS - PC Agent' task and disable 'JARVIS - WOL Helper'"
      ;;
    *)
      warn "8099 answers but reports service='$_svc' - expected jarvis-pc-agent"
      ;;
  esac

  # WOL backward compatibility. pc-agent replaced wol-helper on the same port and
  # deliberately kept /status and /wake; losing them would silently break waking
  # the main PC, which is a feature Natik asked for specifically.
  _wolcode=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://localhost:8099/status" 2>/dev/null)
  # 401 is a PASS here: the route exists and is refusing an unauthenticated
  # request, which is the wanted behaviour. 404 would mean the route is gone.
  case "$_wolcode" in
    200|401) ok "WOL /status route preserved on 8099 (http $_wolcode)" ;;
    404)     fail "WOL /status route is GONE from 8099 - waking the main PC is broken" ;;
    *)       warn "WOL /status on 8099 returned http $_wolcode" ;;
  esac

  # File routes. Added 2026-08-09; /pc/file/open is the only route in the agent
  # that runs ShellExecute on a path, so its two allowlists are security
  # boundaries, not conveniences.
  _fcode=$(curl -s -m 8 -o /dev/null -w '%{http_code}' "http://localhost:8099/pc/folders" 2>/dev/null)
  case "$_fcode" in
    401) ok "file routes present on 8099 (/pc/folders answers, auth enforced)" ;;
    404) fail "/pc/folders is missing from 8099 - the running agent predates the file routes; restart it with host-helper/restart-agent.ps1 (Restart-ScheduledTask alone does NOT reload it - the old process keeps the port and the new one dies unable to bind)" ;;
    200) warn "/pc/folders answered 200 WITHOUT a token - authentication is not being enforced on the file routes" ;;
    *)   warn "/pc/folders returned http $_fcode" ;;
  esac

  # The extension allowlist must never grow an executable type. This is a static
  # check of the file rather than a live probe, because the failure mode is
  # someone adding ".lnk" or ".zip" to be helpful, and by the time it is live it
  # is one voice command from running something.
  _ff="/c/ServerData/Stacks/jarvis/host-helper/folders.json"
  if [ -f "$_ff" ]; then
    _danger=$(grep -oE '"\.(exe|bat|cmd|com|ps1|psm1|vbs|vbe|js|jse|wsf|msi|msp|scr|cpl|hta|reg|lnk|url|inf|jar|py|sh|apk|iso)"' "$_ff" \
              | grep -v '_extension_comment' | tr -d '"' | tr '\n' ' ')
    if [ -n "$_danger" ]; then
      fail "folders.json allows EXECUTABLE extensions: $_danger- /pc/file/open uses ShellExecute, so this is arbitrary code execution by voice; remove them"
    else
      ok "folders.json allows no executable extensions"
    fi
    # C:\ServerData holds secrets.env and every app database. It must not be an
    # openable root. Sync/ lives under it and IS allowed, which is intentional
    # and safe -- hence the anchored match on the root itself.
    if grep -qE '"path"[[:space:]]*:[[:space:]]*"C:\\\\ServerData\\\\?"' "$_ff"; then
      fail "folders.json exposes C:\\ServerData as an openable root - that directory holds secrets.env and every application database"
    else
      ok "folders.json does not expose the ServerData root"
    fi
  else
    warn "folders.json not found - the PC agent's file routes will refuse every request"
  fi
fi
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
sec "24. Every Hermes tool has somewhere to dispatch"
# THE INCIDENT THIS EXISTS FOR (2026-08-09)
# hermes.py carried six pc_* tools and a "pc" specialist for a full day, the PC
# agent on 8099 answered every one of them correctly by curl, and PC control was
# nevertheless completely dead by voice. executor.py had exactly two dispatch
# paths -- HA intents and HA scripts -- so `pc_app` was sent to
# `script.pc_app`, which does not exist. Home Assistant has 14 scripts and not
# one of them starts with pc_.
#
# The way it failed is the reason this check is worth its length. The executor
# reported exec_failed, Hermes fell through, and the big agent -- which holds no
# PC tool at all -- answered from its own head: "Opening Spotify, sir", with
# nothing launched. Nothing in any log said "broken". Every individual component
# tested green.
#
# So this derives the tool list FROM hermes.py and proves each tool has a real
# target: an HA intent, an HA script that exists, a context tool, or an entry in
# HTTP_ROUTES backed by a service that answers. A tool with no target is a
# guaranteed phantom action, so it is a FAIL, not a warning.
JDIR="/c/ServerData/Stacks/jarvis"
if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx jarvis-voice; then
  # Run INSIDE the container: it is the only place the real tool tables and the
  # real HTTP_ROUTES live, and reading them with grep from the host would be a
  # second, drifting copy of the truth. Same principle as deriving the image
  # file list from Dockerfile.ui rather than hand-listing it.
  _toolmap=$(docker exec jarvis-voice python -c "
import hermes.hermes as H, hermes.executor as E
for t in sorted(H.TOOLS):
    if t in E.INTENT_TOOLS: kind='intent'
    elif t in E.CONTEXT_TOOLS: kind='context'
    elif t in E.HTTP_ROUTES: kind='http:'+E.HTTP_ROUTES[t]
    elif hasattr(E, 'CALL_TOOLS') and t in E.CALL_TOOLS: kind='call'
    else: kind='script'
    print(t, kind)
" 2>/dev/null)
  if [ -z "$_toolmap" ]; then
    warn "could not read the Hermes tool table out of jarvis-voice - dispatch coverage unverified"
  else
    _hass_token=$(grep -E "^JARVIS_HASS_TOKEN=" "$SECRETS_FILE" 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d '\r')
    _scripts=$(curl -s -m 15 -H "Authorization: Bearer $_hass_token" \
                 "http://localhost:8123/api/states" 2>/dev/null \
               | tr ',' '\n' | grep -oE '"script\.[a-z0-9_]+"' | tr -d '"' | sed 's/^script\.//' | sort -u)
    _orphans=""; _n=0
    while read -r _tool _kind; do
      [ -n "$_tool" ] || continue
      _n=$((_n+1))
      case "$_kind" in
        intent|context|call) : ;;
        http:*) : ;;   # liveness of those services is checked below and in 23
        script)
          # An empty script list means HA or the token is broken, not that every
          # tool is orphaned. Reporting 14 phantom failures for one auth problem
          # would bury the real cause.
          if [ -n "$_scripts" ] && ! printf '%s\n' "$_scripts" | grep -qx "$_tool"; then
            _orphans="$_orphans $_tool"
          fi
          ;;
      esac
    done <<EOF
$_toolmap
EOF
    if [ -z "$_scripts" ]; then
      warn "could not list Home Assistant scripts - Hermes script-tool coverage unverified"
    elif [ -n "$_orphans" ]; then
      fail "Hermes tools with NO dispatch target:$_orphans - each one fails in the executor and falls through to an agent that cannot do it either, so JARVIS will confirm an action that never happened; add them to HTTP_ROUTES in hermes/executor.py or create the matching script"
    else
      ok "all $_n Hermes tools have a dispatch target (intent, script, context or HTTP service)"
    fi
  fi
else
  warn "jarvis-voice is not running - Hermes dispatch coverage unverified"
fi
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
sec "25. WhatsApp bridge"
_wa=$(curl -s -m 8 "http://localhost:8097/health" 2>/dev/null)
if [ -z "$_wa" ]; then
  fail "nothing answers http://localhost:8097/health - the WhatsApp bridge is down, so sending and reading messages by voice are both dead; the 'JARVIS - WhatsApp' task should restart it within 5 minutes"
else
  _wasvc=$(printf '%s' "$_wa" | grep -oE '"service"[[:space:]]*:[[:space:]]*"[^"]+"' | cut -d'"' -f4)
  if [ "$_wasvc" != "jarvis-whatsapp" ]; then
    warn "8097 answers but reports service='$_wasvc' - expected jarvis-whatsapp"
  else
    _linked=$(printf '%s' "$_wa" | grep -oE '"linked"[[:space:]]*:[[:space:]]*(true|false)' | grep -oE '(true|false)')
    if [ "$_linked" = "true" ]; then
      ok "WhatsApp bridge is linked and serving"
    else
      # Not a FAIL: the fix is a QR scan on Natik's phone, which no amount of
      # server-side work can perform. But it must not be silent either -- an
      # unlinked bridge means every "message mum" turn ends in a refusal.
      warn "WhatsApp bridge is running but NOT linked - scan the QR in jarvis/wa/wa-bridge.log from WhatsApp > Linked devices, or sending and reading stay dead"
    fi
  fi

  # The bridge must never answer an unauthenticated request. It holds the
  # ability to message real people.
  _wacode=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST \
              -H 'Content-Type: application/json' \
              -d '{"to":"mum","message":"audit probe - must never send"}' \
              "http://localhost:8097/send" 2>/dev/null)
  case "$_wacode" in
    401) ok "WhatsApp /send refuses an unauthenticated request (http 401)" ;;
    200) fail "WhatsApp /send ACCEPTED an unauthenticated request - anything that can reach loopback can message Natik's contacts; the bridge must fail closed" ;;
    *)   warn "WhatsApp /send returned http $_wacode to an unauthenticated request - expected 401" ;;
  esac
fi

# The contact allowlist is the only thing standing between a misheard word and a
# message to the wrong person, so its absence is a failure even though the
# bridge would then simply refuse everything.
_cf="/c/ServerData/Stacks/jarvis/wa/contacts.json"
if [ -f "$_cf" ]; then
  # Counted with a JSON parser, not a grep. contacts.json carries a long
  # "_comment" array explaining the allowlist, and a line-wise grep counted
  # every line of that prose as a contact -- it reported 15 when there are 2.
  _nc=$(python -c "
import json,sys
d=json.load(open(r'C:\ServerData\Stacks\jarvis\wa\contacts.json',encoding='utf-8'))
print(len([k for k in d if not k.startswith('_')]))
" 2>/dev/null || echo "?")
  if grep -qE '"(any|all|\*)"\s*:' "$_cf"; then
    fail "contacts.json contains a wildcard entry - the allowlist is the whole safety model for sending; remove it"
  else
    ok "WhatsApp contact allowlist present ($_nc entries)"
  fi
else
  fail "jarvis/wa/contacts.json is missing - the bridge refuses every send, so messaging is dead"
fi
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
sec "26. Minecraft server"

_mcdir="/c/ServerData/Stacks/minecraft"
_mccompose="$_mcdir/docker-compose.yml"

if [ ! -f "$_mccompose" ]; then
  warn "minecraft/docker-compose.yml is missing - the stack was removed or never restored"
else
  # INCIDENT THIS GUARDS (2026-09-09, designed-in rather than observed): this
  # host has 15.4 GB of RAM and the WSL2 VM is capped. A Minecraft JVM with no
  # container ceiling grows until the VM is exhausted, and the Linux OOM killer
  # then picks its victim by score -- which is jarvis-ollama or a voice
  # container long before it is Minecraft. That kills the voice loop silently
  # and the only symptom is "JARVIS stopped answering". mem_limit makes Docker
  # kill Minecraft instead, keeping the blast radius inside this stack.
  if grep -qE '^\s*mem_limit:' "$_mccompose"; then
    ok "minecraft has a mem_limit (voice loop is protected from its OOM)"
  else
    fail "minecraft/docker-compose.yml has NO mem_limit - an unbounded Minecraft JVM will exhaust the WSL VM and the OOM killer will take a JARVIS container, not Minecraft; restore mem_limit"
  fi

  # INCIDENT THIS GUARDS (2026-09-09): the minecraft stack was written without
  # the dns: override every other outbound-calling service here carries. The
  # modpack installer then failed nine times in a row with
  #   java.net.UnknownHostException: Failed to resolve 'api.curseforge.com'
  #   DnsErrorCauseException: Query failed with SERVFAIL
  # OOMKilled was false and the exit code was 0, so it read as a memory problem
  # for several minutes and was not one. The ISP resolver SERVFAILs external
  # API hostnames; this is the same defect that silently kills TMDb lookups in
  # the streaming stack, and it is invisible until something needs the network.
  if grep -qE '^\s*dns:' "$_mccompose"; then
    ok "minecraft has the dns: override (CurseForge API resolvable)"
  else
    fail "minecraft/docker-compose.yml has NO dns: override - the ISP resolver SERVFAILs api.curseforge.com, so mod installs and updates fail with UnknownHostException while looking like a memory fault; add dns: 1.1.1.1 / 8.8.8.8"
  fi

  # Geyser cannot translate modded blocks/items, so a Bedrock listener on a
  # Tensura server is a port that either refuses joins or drops players into a
  # world they cannot render. Publishing it would look like crossplay works.
  if grep -qE '^\s*-\s*"?19132' "$_mccompose"; then
    warn "minecraft publishes 19132 (Bedrock) - Geyser cannot translate this modpack's custom content, so that port cannot serve a working game; see minecraft/README.md"
  else
    ok "no Bedrock port published (correct for a client-side-mod server)"
  fi

  # RCON is an unauthenticated-beyond-the-password full server console. It is
  # reachable by container name on minecraft_default for the Homarr tile and
  # must never be bound to a host port.
  if grep -qE '^\s*-\s*"?[0-9.]*:?25575' "$_mccompose"; then
    fail "minecraft publishes RCON 25575 on the host - that is a full server console on the LAN; keep it internal to minecraft_default"
  else
    ok "RCON is not published to the host"
  fi

  # Deliberately NOT probing tcp/25565. ENABLE_AUTOPAUSE SIGSTOPs the JVM when
  # idle, and any TCP connect is exactly the knock that wakes it. A weekly
  # audit probe would resume a 6 GB JVM every Sunday and quietly defeat the
  # feature. Container state is read from Docker instead.
  # STOPPED IS THE NORMAL STATE and must not warn. Minecraft is opt-in on this
  # host: it holds ~5.4 GB of 15.4 GB, so it runs only while someone is playing.
  # An earlier version of this check warned on anything but "running", which
  # would have trained everyone to ignore a warning that fires every single day.
  # A check that cries wolf is worse than no check.
  _mcstate=$(docker inspect -f '{{.State.Status}}' minecraft 2>/dev/null)
  case "$_mcstate" in
    running)          ok "minecraft is running (autopause not probed by design)" ;;
    exited|created)   ok "minecraft is stopped - the normal idle state on this host" ;;
    "")               warn "minecraft container does not exist - stack was removed or never created" ;;
    *)                warn "minecraft container state is '$_mcstate' - expected running, exited or created" ;;
  esac
fi

# INCIDENT THIS GUARDS (2026-09-09): the leading indicator nobody was watching.
# Minecraft (~5.4 GB) came back automatically under restart:always while nobody
# was playing, and the WSL VM ran out of swap -- SwapFree fell to 880 kB of
# 3.1 GB. AdGuard did not stop and did not crash (restarts=0, status=running),
# but it was starved badly enough that its upstream DoH queries timed out and
# DNS FOR THE WHOLE HOUSE went down. Every container looked healthy. Section 17
# catches DNS once it has already failed; this catches the condition that
# causes it, while there is still time to act.
_vmstat=$(docker run --rm valkey/valkey:9 sh -c 'grep -E "^(MemAvailable|SwapTotal|SwapFree)" /proc/meminfo' 2>/dev/null)
if [ -n "$_vmstat" ]; then
  _memavail=$(printf '%s' "$_vmstat" | awk '/MemAvailable/{print int($2/1024)}')
  _swaptot=$(printf '%s' "$_vmstat"  | awk '/SwapTotal/{print int($2/1024)}')
  _swapfree=$(printf '%s' "$_vmstat" | awk '/SwapFree/{print int($2/1024)}')
  _swappct=100
  [ "${_swaptot:-0}" -gt 0 ] && _swappct=$(( _swapfree * 100 / _swaptot ))

  if [ "${_memavail:-0}" -lt 700 ]; then
    fail "WSL VM has only ${_memavail} MB available - the box is thrashing; AdGuard stays 'running' while its upstream DNS times out, which takes the whole house offline. Stop Minecraft (docker compose --project-directory /c/ServerData/Stacks/minecraft stop) or park stacks with mc-gamemode.ps1 -On"
  elif [ "${_memavail:-0}" -lt 1500 ]; then
    warn "WSL VM available memory is ${_memavail} MB - tight; anything heavy started now will push it into swap"
  else
    ok "WSL VM available memory is ${_memavail} MB"
  fi

  if [ "${_swappct:-100}" -lt 15 ]; then
    fail "WSL VM swap is ${_swappct}% free (${_swapfree}/${_swaptot} MB) - swap exhaustion is what preceded the 2026-09-09 DNS outage; something is oversized for this host"
  elif [ "${_swappct:-100}" -lt 40 ]; then
    warn "WSL VM swap is only ${_swappct}% free (${_swapfree}/${_swaptot} MB) - memory pressure is building"
  else
    ok "WSL VM swap is ${_swappct}% free"
  fi
fi

# Minecraft must NOT be on restart:always. At ~5.4 GB on a 15.4 GB host it
# would resurrect itself on every engine restart, including when nobody is
# playing -- which is exactly how the box reached swap exhaustion.
if [ -f "$_mccompose" ]; then
  if grep -qE '^\s*restart:\s*unless-stopped' "$_mccompose"; then
    ok "minecraft uses restart: unless-stopped (cannot resurrect itself when stopped)"
  else
    fail "minecraft is not on restart: unless-stopped - with restart:always it restarts itself on every engine restart and starves AdGuard into a house-wide DNS outage"
  fi
fi

# INCIDENT THIS GUARDS: game mode stops the media, photo and monitoring stacks
# to free ~3.9 GB for Minecraft. Forgetting to run `mc-gamemode.ps1 -Off` after
# playing leaves Sonarr, Radarr and Immich down indefinitely, and nothing else
# reports it -- downloads simply stop happening. Worse, every port check above
# fails at once, which reads like the media stack has broken rather than like
# it was deliberately stopped. This turns that confusing flood into one cause.
# The reference container must be one game mode NEVER stops, or the check
# cannot fire. This first used jarvis-voice and was silently broken from the
# moment mc-gamemode.ps1 gained -IncludeJarvis: that switch stops jarvis too,
# so "jarvis up AND sonarr down" was unsatisfiable and the audit cheerfully
# reported "game mode is off" while eight stacks were parked. AdGuard is the
# correct reference -- it is protected unconditionally, because it is DNS for
# the whole tailnet.
_ref_up=$(docker inspect -f '{{.State.Status}}' adguardhome 2>/dev/null)
_sonarr_up=$(docker inspect -f '{{.State.Status}}' sonarr 2>/dev/null)
if [ "$_ref_up" = "running" ] && [ "$_sonarr_up" != "running" ]; then
  warn "GAME MODE APPEARS TO BE ON: jarvis is up but sonarr is not. If you finished playing Minecraft, run  Stacks\minecraft\mc-gamemode.ps1 -Off  -- until you do, nothing downloads and the *arr failures above are a consequence of this, not separate faults"
else
  ok "game mode is off (media stack is not parked)"
fi

# The world is the only irreplaceable part -- the 600 MB modpack re-downloads
# from CurseForge, a world does not. minecraft_data is excluded from
# backup-volumes.sh on purpose (it would put ~15 GB x 14 days on a 180 GB
# drive), so this is the check that the narrower backup is actually happening.
_mcbk="/d/Backups/minecraft"
if [ ! -d "$_mcbk" ]; then
  if [ "$_mcstate" = "running" ]; then
    warn "no Minecraft world backup directory at D:\Backups\minecraft - the server is running but its world has never been backed up; run minecraft\mc-backup.sh"
  else
    ok "no Minecraft world backup yet (server has not run)"
  fi
else
  _newest=$(find "$_mcbk" -name 'world-*.tgz' -mtime -8 2>/dev/null | head -1)
  if [ -n "$_newest" ]; then
    ok "Minecraft world backup is under 8 days old"
  else
    warn "no Minecraft world backup newer than 8 days in D:\Backups\minecraft - the world is the one thing that cannot be re-downloaded; check the scheduled job"
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

# Pre-report guard: scan the report content for leaked secrets before it hits
# disk. Incident 2026-08-16: a report file was served over HTTP with a
# plaintext Tuya device ID still in it, caught by scan-secrets.sh after the
# fact. This check catches it before the write.
_TMP_REPORT=$(mktemp /c/ServerData/Stacks/.audit-report-tmp.XXXXXX)
printf '%s' "$LINES" > "$_TMP_REPORT" 2>/dev/null
_SCAN_OUT=$(bash /c/ServerData/Stacks/scripts/scan-secrets.sh "$_TMP_REPORT" 2>&1) && _SCAN_RC=0 || _SCAN_RC=$?
if [ "$_SCAN_RC" -eq 0 ]; then
  mv "$_TMP_REPORT" "$REPORT" 2>/dev/null || cp "$_TMP_REPORT" "$REPORT"
  rm -f "$_TMP_REPORT"
else
  # Leak found — refuse to write the report. Emit the scan output so the
  # operator knows what leaked and where.
  rm -f "$_TMP_REPORT"
  printf '%s\n' "$_SCAN_OUT" | while IFS= read -r _line; do
    [ -n "$_line" ] && _emit "  CRITICAL $_line"
  done
  # Count the leak as a failure so the exit code reflects the problem.
  FAIL=$((FAIL + 1))
fi
[ "$QUIET" = "true" ] && printf 'audit: %s ok, %s warn, %s fail (report: %s)\n' "$OK" "$WARN" "$FAIL" "$REPORT"

# run-job.ps1 treats any non-zero exit as a FAILED job (event-log error + a
# "down" heartbeat to Uptime Kuma). Warnings are advisory, so the scheduled
# weekly run sets this and only a real FAIL raises the alarm. Run it by hand
# without the flag to get the warning-sensitive exit code.
if [ "$FAIL" -gt 0 ]; then exit 2
elif [ "$WARN" -gt 0 ] && [ "${RW_AUDIT_OK_ON_WARN:-false}" != "true" ]; then exit 1
else exit 0; fi
