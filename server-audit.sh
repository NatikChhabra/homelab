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
jf()      { docker exec jellyfin sh -c "curl -s -m 20 -H 'Authorization: MediaBrowser Token=$JELLYFIN_KEY' 'http://localhost:8096$1'" 2>/dev/null; }
jqs()     { docker exec -i sonarr jq "$@" 2>/dev/null; }

_emit "============================================================"
_emit " SERVER AUDIT  $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
_emit "============================================================"

# ---------------------------------------------------------------------------
sec "1. Containers"
# Incident: nothing here yet, but a silently exited container is the cheapest
# possible thing to miss.
EXPECTED="qbittorrent prowlarr flaresolverr sonarr radarr bazarr jellyfin jellyseerr warp"
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
# FlareSolverr has no host port since 2026-11-01 (it was unauthenticated on the
# LAN); check it the way Prowlarr actually reaches it, over the compose network.
fs=$(MSYS_NO_PATHCONV=1 docker exec prowlarr sh -c 'curl -s -o /dev/null -m 10 -w %{http_code} http://flaresolverr:8191/' 2>/dev/null)
case "$fs" in 200|405) ok "flaresolverr (via prowlarr -> flaresolverr:8191) responds $fs" ;; *) fail "flaresolverr unreachable from prowlarr (got '$fs')" ;; esac
check_port 8989  sonarr      "200 302"
check_port 7878  radarr      "200 302"
check_port 6767  bazarr      "200 302"
check_port 8096  jellyfin    "200 302"
check_port 5055  jellyseerr  "200 307 302"
check_port 7575  homarr      "200 302 307"   # v2.10 redirects to /auth/login with 307
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
  # while-read, not for-in-$(find): every library path has spaces, so the old
  # loop split them into fragments that stat could not read -- no file was ever
  # actually examined and this check could only say OK (fixed 2026-11-10).
  find /data/library -type f \( -name "*.mkv" -o -name "*.mp4" \) 2>/dev/null > /tmp/hl_list
  while IFS= read -r f; do
    l=$(stat -c %h "$f" 2>/dev/null)
    if [ "${l:-1}" -eq 1 ]; then
      sz=$(stat -c %s "$f" 2>/dev/null)
      if find /data/torrents -type f -size "${sz}c" 2>/dev/null | grep -q .; then
        n=$((n+1)); b=$((b+sz))
      fi
    fi
  done < /tmp/hl_list
  rm -f /tmp/hl_list
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
  # 2026-10-25: "firewalled" only means no inbound connections (no port
  # forward on the JioFiber router). A test torrent still pulled 10.9 MB/s from
  # 42 seeds in that state, so it is a warning, not an outage.
  case "$cs" in
    connected)  ok "qBittorrent connection_status=connected" ;;
    firewalled) warn "qBittorrent is firewalled (no inbound peers) - downloads still work; forward port 6881 on the router to fix" ;;
    *)          fail "qBittorrent connection_status=$cs" ;;
  esac
  # 2026-10-25: libtorrent only grows its DHT table while a torrent is active.
  # An idle client sat at 0 nodes for hours, then reached 140 within 30 s of
  # adding a torrent -- so low DHT with nothing active is not a fault.
  act=$(docker exec qbittorrent sh -c "curl -s -m 15 -b /tmp/.audit_ck 'http://localhost:8080/api/v2/torrents/info?filter=active'" 2>/dev/null | jqs -r 'length' 2>/dev/null)
  if [ "${dht:-0}" -lt "$MIN_DHT_NODES" ] && [ "${act:-0}" -eq 0 ]; then
    ok "DHT idle ($dht nodes, no active torrents)"
  elif [ "${dht:-0}" -lt "$MIN_DHT_NODES" ]; then
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
# Added 2026-11-01: jellyfin + jellyseerr reach TMDb only through warp (Airtel
# resets most TMDb TLS handshakes). If the tunnel drops, metadata dies quietly.
wst=$(docker exec warp sh -c "curl -s -m 15 -x http://127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace" 2>/dev/null | sed -n 's/^warp=//p')
[ "$wst" = "on" ] && ok "warp tunnel is up (warp=on)" || fail "warp tunnel not working (warp=${wst:-no answer}) - jellyfin/jellyseerr lose TMDb; docker restart warp"
# Incident 2026-07-27: Jellyfin was the only *arr-family container without a
# dns: override, so it used ISP DNS, which hijacks api.themoviedb.org to a dead
# IP. Result: no artwork or metadata at all, while every other HTTPS call
# worked, so it looked like a TMDb outage rather than a config gap.
# Sonarr/Radarr do NOT call api.themoviedb.org themselves: their lookups go to the
# Servarr metadata services and posters come from image.tmdb.org. Probing TMDb's
# API from them only "passed" when Airtel's intermittent SNI block happened to be
# off (false FAIL on 2026-11-01). Test what they actually use.
for pair in "sonarr https://skyhook.sonarr.tv/" "radarr https://api.radarr.video/v1/ping" "radarr https://image.tmdb.org/t/p/w92/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg"; do
  set -- $pair; mc=""
  for t in 15 30; do mc=$(docker exec "$1" sh -c "curl -s -o /dev/null -w '%{http_code}' -m $t $2" 2>/dev/null); case "$mc" in 2??|3??|404) break ;; esac; sleep 2; done
  case "$mc" in 2??|3??|404) ok "$1 reaches ${2#https://} ($mc)" ;; *) fail "$1 cannot reach ${2#https://} (got '${mc:-none}') - metadata lookups/posters will fail" ;; esac
done
for c in jellyfin jellyseerr; do
  # Retried on purpose. A single probe reported a hard FAIL for Jellyfin on
  # 2026-07-28 purely because an 18GB download was saturating the link at that
  # moment -- the very next request returned 401. A transient timeout must not
  # look identical to a DNS misconfiguration, or the audit trains you to ignore
  # it. Three tries with a rising timeout; only a consistent failure is real.
  code=""
  # 2026-11-01: on Airtel most TLS handshakes to TMDb are reset (SNI filtering),
  # so jellyfin and jellyseerr reach it through the warp proxy. Test that path.
  PX=""; WPX=""
  case "$c" in jellyfin|jellyseerr) PX="-x http://warp:1080"; WPX="https_proxy=http://warp:1080" ;; esac
  for t in 15 30 45; do
    code=$(docker exec "$c" sh -c "curl -s $PX -o /dev/null -w '%{http_code}' -m $t https://api.themoviedb.org/3/ 2>/dev/null" 2>/dev/null)
    case "$code" in 401|200) break ;; esac
    sleep 2
  done
  # 2026-11-01: busybox wget in Seerr honours https_proxy only some of the time,
  # so its raw probe failed ~40% while the app's own searches (proxy set in
  # Settings > Network) were 3/3. Test the path users hit: Seerr's search API.
  if [ "$c" = "jellyseerr" ] && { [ -z "$code" ] || [ "$code" = "000" ]; }; then
    if docker exec jellyseerr sh -c 'K=$(node -e "console.log(require(\"/app/config/settings.json\").main.apiKey)"); wget -q -T 30 -O - --header "X-Api-Key: $K" "http://localhost:5055/api/v1/search?query=inception&page=1"' 2>/dev/null | grep -q '"totalResults":[1-9]'; then
      code=200
    fi
  fi
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
      if docker exec "$c" sh -c "$WPX wget -q -T 15 -O /dev/null https://api.themoviedb.org/3/ 2>&1 | grep -qiE '401|Unauthorized'" 2>/dev/null; then
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
    # OTHER DIRECTION (2026-11-10, After Life S3): an aired episode of a
    # requested season that is unmonitored, has no file and was never watched
    # will never download. Watched episodes are unmonitored and deleted on
    # purpose by rolling-window.sh, so the watch ledger is excluded. FAIL, not
    # WARN: the weekly run never alerts on WARN.
    gaps=$(printf '%s' "$eps" | jqs -r --arg r "$reqs" --arg now "$(date -u +%FT%TZ)" '
        ($r | split(",") | map(select(. != "") | tonumber)) as $req
        | .[] | select(.monitored==false and .hasFile==false and .seasonNumber>0
                       and (.airDateUtc // "9") < $now
                       and (.seasonNumber as $s | ($req | index($s)) != null))
        | "\(.seriesId)|\(.seasonNumber)|\(.episodeNumber)|"' \
      | grep -vf <(cut -d'|' -f1-3 /c/ServerData/Stacks/watched-ledger.txt 2>/dev/null | grep . | sed 's/^/^/; s/$/|/') | grep -c .)
    if [ "${gaps:-0}" -gt 0 ]; then
      fail "$title: $gaps aired episode(s) in requested season(s) [$reqs] are unmonitored with no file and not watched - they will never download"
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
for t in "ServerData - Rolling Window" "ServerData - Orphan Cleanup" "ServerData - Update Audit" "ServerData - Weekly Audit" "ServerData - Daily Backup" "ServerData - Mount Watchdog" "ServerData - Shrink Library" "ServerData - Immich Auto Albums" "ServerData - Offline Playlists" "ServerData - Music Dupe Guard" "ServerData - LB Discovery"; do
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
# 2026-11-03: College mode disables most server tasks and Server mode re-enables them. A task left
# Disabled (a forgotten switch, a manual test) never runs and never reports; say so.
_dis=$(powershell.exe -NoProfile -Command "(Get-ScheduledTask -TaskName 'ServerData - *' | ? State -eq 'Disabled').TaskName -join ', '" 2>/dev/null | tr -d '\r')
if [ -n "$_dis" ]; then warn "scheduled task(s) disabled: $_dis (College mode left on, or switched off by hand?)"; else ok "no ServerData task is disabled"; fi

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
    # bind-AdGuard / bind-Navidrome / bind-Sync added 2026-11-01 (audit: family DNS config,
    # music DB and the FileBrowser share were not on the must-list).
    # Freshness, not mere presence (2026-11-01): with 14-day retention a backup
    # that stopped a week ago still "had an archive". navidrome_data added: it
    # holds Navidrome's real DB, its own /data/backup copies and plugin stores.
    for must in sonarr_config jellyfin_config bind-Vaultwarden immich_immich-db bind-Immich bind-AdGuard bind-Navidrome bind-Sync bind-Actual bind-Octo navidrome_data; do
      _newest=$(ls -t "$BK_DIR"/${must}_*.tar.gz 2>/dev/null | head -1)
      if [ -z "$_newest" ]; then
        fail "NO backup for $must - irreplaceable data is unprotected"
      else
        _bh=$(( ( $(date +%s) - $(stat -c %Y "$_newest") ) / 3600 ))
        if [ "$_bh" -gt 27 ]; then fail "newest backup of $must is ${_bh} h old - the nightly backup has stopped"
        else ok "backup fresh: $must (${_bh} h old)"; fi
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
  printf '%s\n' "$orphv" | while read -r v; do
    [ -z "$v" ] && continue
    _emit "          $v"
  done
else
  ok "no orphaned volumes"
fi

# Containerd image store lists an untagged copy of an IN-USE image as dangling
# (seen 2026-11-02: jellyfin's own image; prune reclaims 0 B). Count only unused ones.
used=$(docker ps -aq | xargs -r docker inspect -f '{{.Image}}' 2>/dev/null | cut -c8-19)
dang=$(docker images -f dangling=true -q 2>/dev/null | grep -vxF -f <(printf '%s\n' $used) | grep -c . || true)
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
glances navidrome actual octo slskd yt-dlp-shim"
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
check_port 5006  actual      "200 302"
check_port 5274  octo        "200 302"

# Portainer is HTTPS-only with a self-signed cert, so check_port's plain HTTP
# probe cannot be reused here.
pcode=$(curl -sk -o /dev/null -w '%{http_code}' -m 10 "https://localhost:9443/" 2>/dev/null)
case " 200 302 307 " in
  *" $pcode "*) ok "portainer (:9443) responds $pcode" ;;
  *) fail "portainer (:9443) returned '$pcode', expected 200/302/307" ;;
esac

# ---------------------------------------------------------------------------
sec "16. Tailscale serve keeps Vaultwarden on 443"
# `tailscale serve` run without an explicit port targets 443 and REPLACES the
# handler there rather than adding to it, so a careless re-run takes
# Vaultwarden off the tailnet while its container stays green and every check
# above still passes.
TS_EXE="/c/Program Files/Tailscale/tailscale.exe"
if [ -x "$TS_EXE" ]; then
  TSJ=$("$TS_EXE" serve status --json 2>/dev/null)
  ts_port() { printf '%s' "$TSJ" | jqs -r --arg p "$1" \
    '.Web // {} | to_entries[] | select((.key|split(":")|last) == $p) | .value.Handlers["/"].Proxy // empty'; }
  j443=$(ts_port 443)
  case "$j443" in
    *:8222) ok "tailscale serve maps 443 -> $j443 (vaultwarden)" ;;
    "")     fail "tailscale serve has NO mapping on 443 - Vaultwarden is off the tailnet" ;;
    *)      fail "tailscale serve 443 points at '$j443', expected 127.0.0.1:8222 - something displaced Vaultwarden's mapping" ;;
  esac
else
  warn "cannot run $TS_EXE - the Vaultwarden HTTPS mapping on the tailnet is unverified"
fi
# Actual Budget (2026-11-02) needs HTTPS too; same failure shape on :5006.
if [ -x "$TS_EXE" ]; then
  case "$(ts_port 5006)" in
    *:5006) ok "tailscale serve maps 5006 -> actual" ;;
    *)      fail "tailscale serve has no 5006 -> 127.0.0.1:5006 mapping - Actual Budget unreachable from phone/PC (re-run: tailscale serve --bg --https=5006 http://127.0.0.1:5006)" ;;
  esac
fi

# ---------------------------------------------------------------------------
sec "17. DNS filtering actually resolves"
# Incident 2026-07-28 (same outage): "the container is running" was never the
# real question. Port 53 has to ANSWER. A container that is up but not serving
# DNS breaks every device pointed at it just as hard as a missing one, so this
# resolves a real name through it rather than trusting container state.
if docker inspect -f '{{.State.Status}}' adguardhome >/dev/null 2>&1; then
  dnsres=$(nslookup -timeout=5 example.com 127.0.0.1 2>&1)
  # Only Address lines AFTER 'Name:' are answers. nslookup always prints the
  # server's own 'Address: 127.0.0.1' header first, so the old grep matched it
  # and this check passed even with AdGuard dead (found 2026-11-10).
  if printf '%s' "$dnsres" | tr -d '\r' | awk '/^Name:/{f=1} f && /^Address(es)?:[[:space:]]*[0-9]/{ok=1} END{exit !ok}' ; then
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
# AUDIT_SKIP_IMMICH=1 skips this read-only check for ad-hoc runs (owner: no poking Immich);
# the scheduled weekly run keeps it, since it is what notices lost photo originals (2026-11-03).
if [ "${AUDIT_SKIP_IMMICH:-0}" = 1 ]; then ok "Immich integrity check skipped (AUDIT_SKIP_IMMICH=1)"
elif docker inspect -f '{{.State.Status}}' "$IM_SRV" 2>/dev/null | grep -q running; then
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
      MSYS_NO_PATHCONV=1 docker exec "$IM_SRV" rm -f /tmp/_audit_paths.txt >/dev/null 2>&1   # Git Bash mangled /tmp before
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
# 2026-11-02: octo/docker-compose.yml is upstream verbatim; its octo-sonic (2 GB
# "sounds alike" radio) and spotiflac profile are switched off on purpose in
# docker-compose.override.yml (host memory).
NOT_DEPLOYED="octo-sonic spotiflac"
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
  case " $EXPECTED $OTHER_CONTAINERS " in
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
sec "20. Host stays awake"
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
    fail "NO keep-awake assertion held - powercfg /requests shows no powershell.exe under DISPLAY/SYSTEM; keep-awake.ps1 is not running or is asserting nothing (a running process is NOT proof - see this section's comment)"
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
sec "21. Memory pressure (DNS outage leading indicator)"
# INCIDENT THIS GUARDS (2026-09-09): the leading indicator nobody was watching.
# An oversized container (then Minecraft, ~5.4 GB, since removed) pushed the
# WSL VM out of swap -- SwapFree fell to 880 kB of 3.1 GB. AdGuard did not stop
# and did not crash (restarts=0, status=running), but it was starved badly
# enough that its upstream DoH queries timed out and DNS FOR THE WHOLE HOUSE
# went down. Every container looked healthy. Section 17 catches DNS once it has
# already failed; this catches the condition that causes it, while there is
# still time to act.
_vmstat=$(docker run --rm valkey/valkey:9 sh -c 'grep -E "^(MemAvailable|SwapTotal|SwapFree)" /proc/meminfo' 2>/dev/null)
if [ -n "$_vmstat" ]; then
  _memavail=$(printf '%s' "$_vmstat" | awk '/MemAvailable/{print int($2/1024)}')
  _swaptot=$(printf '%s' "$_vmstat"  | awk '/SwapTotal/{print int($2/1024)}')
  _swapfree=$(printf '%s' "$_vmstat" | awk '/SwapFree/{print int($2/1024)}')
  _swappct=100
  [ "${_swaptot:-0}" -gt 0 ] && _swappct=$(( _swapfree * 100 / _swaptot ))

  if [ "${_memavail:-0}" -lt 700 ]; then
    fail "WSL VM has only ${_memavail} MB available - the box is thrashing; AdGuard stays 'running' while its upstream DNS times out, which takes the whole house offline. Find the oversized container with 'docker stats --no-stream' and stop it"
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
else
  # Used to vanish silently from the report (2026-11-10).
  warn "could not read WSL VM memory (docker run valkey/valkey:9 failed) - memory pressure NOT checked"
fi

# ---------------------------------------------------------------------------
_emit ""

# ---------------------------------------------------------------------------
sec "22. Monitoring + stability watchers"
# Added 2026-11-01 after a 145-agent audit found problems that nothing watched:
# 25/30 Kuma monitors had no alert, the host ran out of UDP ports 51 times, the
# USB media disk re-enumerated up to 8x/day (each one restarts all of Docker),
# and Windows sat at ~0.6 GB available. Each check below is read-only.
if [ -n "${KUMA_API_KEY:-}" ]; then
  _km=$(curl -s -m 10 -u ":$KUMA_API_KEY" http://127.0.0.1:3001/metrics 2>/dev/null | grep '^monitor_status{')
  # Not 'Weekly Audit': that monitor is this script's own result, so counting it
  # made one failed run fail every later run (seen 2026-11-02).
  _kdown=$(printf '%s\n' "$_km" | awk '/\} 0$/' | grep -v 'monitor_name="Weekly Audit"' | sed -E 's/.*monitor_name="([^"]+)".*/\1/' | paste -sd, -)
  _kn=$(printf '%s\n' "$_km" | grep -c '} ')
  if [ -z "$_km" ]; then warn "could not read Uptime Kuma metrics"
  elif [ -n "$_kdown" ]; then fail "Uptime Kuma shows DOWN: $_kdown"
  else ok "Uptime Kuma: all $_kn reporting monitors up"; fi
fi
_kna=$(MSYS_NO_PATHCONV=1 docker exec uptime-kuma-uptime-kuma-1 node -e "const s=require('@louislam/sqlite3');const d=new s.Database('/app/data/kuma.db',s.OPEN_READONLY);d.get('select count(*) n from monitor m where active=1 and not exists (select 1 from monitor_notification x where x.monitor_id=m.id)',(e,r)=>console.log(e?'?':r.n))" 2>/dev/null | tr -d '\r\n ')
case "$_kna" in 0) ok "every active Kuma monitor has a Discord alert attached" ;; ''|'?') warn "could not check Kuma alert wiring" ;; *) fail "$_kna active Kuma monitor(s) have NO notification - an outage there would be silent" ;; esac
_udp=$(powershell.exe -NoProfile -NonInteractive -Command 'try{(Get-WinEvent -FilterHashtable @{LogName="System";ProviderName="Tcpip";Id=4266;StartTime=(Get-Date).AddDays(-1)} -ErrorAction Stop|Measure-Object).Count}catch{0}' 2>/dev/null | tr -d '\r\n ')
if [ "${_udp:-0}" -ge 3 ]; then fail "host ran out of UDP ports ${_udp}x in 24 h (Tcpip 4266) - see logs/udp-ports.log for the owning process"
elif [ "${_udp:-0}" -ge 1 ]; then warn "host ran out of UDP ports ${_udp}x in 24 h (Tcpip 4266) - see logs/udp-ports.log"
else ok "no UDP port exhaustion (Tcpip 4266) in 24 h"; fi
_dre=$(powershell.exe -NoProfile -NonInteractive -Command 'try{(Get-WinEvent -FilterHashtable @{LogName="System";ProviderName="Microsoft-Windows-Ntfs";Id=98;StartTime=(Get-Date).AddDays(-1)} -ErrorAction Stop | Where-Object { $_.Message -like "Volume D:*" } | Measure-Object).Count}catch{0}' 2>/dev/null | tr -d '\r\n ')
if [ "${_dre:-0}" -ge 3 ]; then warn "USB media disk D: re-mounted ${_dre}x in 24 h (each drop makes mount-watchdog restart all of Docker, incl. AdGuard) - RTL9201 UAS bridge; try another port/cable"
else ok "USB media disk D: re-mounts in 24 h: ${_dre:-0}"; fi
_dump=$(ls -t /c/ServerData/Immich/backups/immich-db-backup-*.sql.gz 2>/dev/null | head -1)
if [ -z "$_dump" ]; then fail "no Immich DB dump found in /c/ServerData/Immich/backups"
else
  _age=$(( ( $(date +%s) - $(stat -c %Y "$_dump") ) / 3600 ))
  if [ "$_age" -gt 26 ]; then fail "newest Immich DB dump is ${_age} h old (expected daily at 03:30 IST)"
  elif gzip -t "$_dump" 2>/dev/null; then ok "Immich DB dump is ${_age} h old and passes gzip -t"
  else fail "newest Immich DB dump fails gzip -t: $(basename "$_dump")"; fi
fi
_hav=$(powershell.exe -NoProfile -NonInteractive -Command '[int](Get-Counter "\Memory\Available MBytes").CounterSamples.CookedValue' 2>/dev/null | tr -d '\r\n ')
if [ "${_hav:-9999}" -lt 600 ]; then fail "Windows has only ${_hav} MB available - host memory starvation (the AdGuard outage condition)"
elif [ "${_hav:-9999}" -lt 1000 ]; then warn "Windows has only ${_hav} MB available"
else ok "Windows available memory ${_hav} MB"; fi
_pmin=$(MSYS_NO_PATHCONV=1 powercfg.exe /q 27fa6203-3987-4dcc-918d-748559d549ec SUB_PROCESSOR PROCTHROTTLEMIN 2>/dev/null | tr -d '\r' | awk -F': ' '/Current AC/{print $2}')
if [ -n "$_pmin" ] && [ $(( _pmin )) -gt 16 ]; then warn "CPU minimum state on AC is $(( _pmin ))% (clocks pinned; expected 5%) - the ASUS app may have reset it; re-run Server mode"
else ok "CPU minimum state on AC: $(( ${_pmin:-5} ))%"; fi
_kx=$("/c/Program Files/Tailscale/tailscale.exe" status --json 2>/dev/null | python -c "import sys,json,datetime as d;e=json.load(sys.stdin)['Self'].get('KeyExpiry');print(int((d.datetime.fromisoformat(e.replace('Z','+00:00'))-d.datetime.now(d.timezone.utc)).days) if e else 'never')" 2>/dev/null)
case "$_kx" in never) ok "Tailscale key expiry disabled" ;; ''|*[!0-9-]*) warn "could not read Tailscale key expiry" ;; *) if [ "$_kx" -lt 30 ]; then fail "Tailscale node key expires in $_kx days - disable key expiry in the admin console or remote access dies"; else warn "Tailscale node key expires in $_kx days (disable key expiry in the admin console)"; fi ;; esac
# 2026-11-03 audit: D:\Media is excluded from Defender (scanning video over the flaky USB bridge
# is too heavy), so nothing scans torrent payloads. A media tree never needs executables, scripts
# or archives: any that appear are flagged here with their paths so they can be scanned by hand.
_mx=$(find /d/Media/torrents /d/Media/library /d/Media/music -type f \( -iname '*.exe' -o -iname '*.scr' -o -iname '*.bat' -o -iname '*.cmd' -o -iname '*.lnk' -o -iname '*.vbs' -o -iname '*.js' -o -iname '*.ps1' -o -iname '*.msi' -o -iname '*.com' -o -iname '*.zip' -o -iname '*.rar' -o -iname '*.7z' -o -iname '*.iso' -o -iname '*.dll' -o -iname '*.hta' -o -iname '*.jar' \) 2>/dev/null)
if [ -z "$_mx" ]; then ok "no executables, scripts or archives in D:\Media (Defender excludes that tree)"
else warn "$(printf '%s\n' "$_mx" | wc -l) executable/script/archive file(s) in D:\Media, which Defender skips - scan: $(printf '%s\n' "$_mx" | head -3 | tr '\n' ' ')"; fi

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
