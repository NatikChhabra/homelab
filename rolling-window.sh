#!/bin/bash
# ============================================================================
# rolling-window.sh — cross-season-aware rolling download window.
#
# WHAT IT DOES, per monitored Sonarr series:
#   1. Ask Jellyfin which episodes are CONFIRMED watched (real playback data).
#   2. Determine the ACTIVE season = the season containing the earliest
#      unwatched episode. Every later season is DORMANT: fully unmonitored,
#      never searched.
#   3. Inside the active season, keep the next WINDOW_SIZE unwatched episodes
#      monitored; unmonitor everything beyond that.
#   4. UNLOCK RULE: when the active season has <= WINDOW_SIZE unwatched
#      episodes left, the next season is promoted to active as well and gets
#      the same window treatment. This is what lets a show roll over a season
#      boundary without ever pre-fetching a whole later season.
#   5. Watched episodes that still have files on disk are deleted (Sonarr
#      episodefile delete), letting the window advance.
#
# SAFETY:
#   - DRY_RUN=true means report only. Nothing is changed or deleted.
#   - An episode is only ever treated as watched if Jellyfin reports
#     UserData.Played == true. Never inferred from age or grab date.
#   - AMBIGUITY GUARD: if a series cannot be matched in Jellyfin at all while
#     it has files on disk, the series is SKIPPED and logged. Never guessed.
#   - PACK GUARD: a download backing more than one queue entry is a season pack
#     and is never removed from the client, because that one torrent also
#     carries the episodes the window wants. See cancel_download.
#   - DUB EXEMPTION: a dubbed PACK is kept in full and is exempt from the
#     window, because dual-audio releases are essentially never posted as
#     singles. Single-episode releases still obey WINDOW_SIZE, dubbed or not.
#   - Every decision is written to LOG_FILE.
#
# Env overrides (used by the scheduled wrapper):
#   RW_DRY_RUN=true|false   RW_WINDOW_SIZE=N   RW_DELETE_WATCHED=true|false
# ============================================================================

set -uo pipefail

# ----------------------------- CONFIG ---------------------------------------
WINDOW_SIZE="${RW_WINDOW_SIZE:-3}"      # unwatched episodes queued ahead of watch position
DRY_RUN="${RW_DRY_RUN:-true}"           # true = report only
DELETE_WATCHED="${RW_DELETE_WATCHED:-true}"  # delete files of confirmed-watched episodes

# Keep at most WINDOW_SIZE unwatched files on disk per show. Anything further
# ahead than the window is deleted even though it was never watched -- this is
# a deliberate exception to the "only delete confirmed-watched" rule, approved
# 2026-07-26 so the disk cap matches the 3-episode download window. Nothing is
# lost permanently: an episode deleted this way is re-grabbed automatically
# once the watch position advances and it re-enters the window.
DELETE_UNWATCHED_BEYOND_WINDOW="${RW_DELETE_UNWATCHED:-true}"

# Setting an episode monitored does NOT make Sonarr look for it. Monitoring only
# tells Sonarr "accept this if it shows up"; the RSS sync just reads what
# indexers have posted recently. For anything that is not a brand-new release
# (a 2015 anime, say) RSS will never surface it, so a monitored episode sits
# missing forever -- observed with Food Wars S1E20. An explicit EpisodeSearch is
# what actually grabs it.
SEARCH_MISSING="${RW_SEARCH_MISSING:-true}"

# ANIME PREFERENCE LADDER (2026-07-27), best to worst:
#   1. a DUBBED season pack    -> window does NOT apply, full season kept
#   2. dubbed single episodes  -> window applies (3 at a time)
#   3. subbed single episodes  -> window applies (3 at a time)
#
# Dub first, because that is what actually gets watched. For most anime the dub
# exists ONLY as a pack: of 916 Food Wars results, every single dual-audio one
# was a season or complete-series release. An EpisodeSearch rejects packs
# outright ("Full season pack"), so letting anime search episodes first
# guarantees a SUB gets grabbed while a well-seeded dub pack goes untouched --
# observed on Food Wars S2, where Sonarr took [Erai-raws] (sub) with a 54-seeder
# dual-audio pack sitting available.
#
# So anime tries a SeasonSearch FIRST. Packs are allowed there, and the profile
# scores Dual Audio / Dubbed +500 against a sub's 0, so the dub wins that same
# search. Only when the season search yields nothing does the cooldown lapse and
# the episode-search path take over, which is where tiers 2 and 3 get reached --
# and there the same +500 keeps a dub single ahead of a sub single.
#
# Non-anime series are unaffected: they go straight to episode searches and keep
# the 3-episode window exactly as before.
ANIME_SEASON_SEARCH="${RW_ANIME_SEASON_SEARCH:-true}"
# Don't re-search the same episode more often than this. Without a cooldown the
# 20-minute schedule would hammer every indexer for the same missing episode
# forever when no release exists.
SEARCH_COOLDOWN_HOURS="${RW_SEARCH_COOLDOWN_HOURS:-6}"
SEARCH_LEDGER="${RW_SEARCH_LEDGER:-/c/ServerData/Stacks/search-ledger.txt}"
# After this many failed search attempts an episode is declared STUCK: reported
# loudly, and no longer allowed to occupy a window slot (see below).
STUCK_THRESHOLD="${RW_STUCK_THRESHOLD:-3}"
STUCK_FILE="${RW_STUCK_FILE:-/c/ServerData/Stacks/stuck-episodes.txt}"

# Jellyfin keeps showing an episode after its file is deleted until the library
# is rescanned, so deletions leave ghosts in the UI. Refresh after any change.
REFRESH_JELLYFIN="${RW_REFRESH_JELLYFIN:-true}"
WATCHED_BY_ANY_USER=true                # any user counts as watched

# Library scoping. Only series whose Sonarr rootFolderPath matches one of these
# are touched. Movies are structurally out of scope: this script only ever
# talks to Sonarr, never Radarr.
ALLOWED_ROOTS="/data/library/tv /data/library/anime"

LOG_FILE="${RW_LOG_FILE:-/c/ServerData/Stacks/rolling-window.log}"

SONARR_URL="http://localhost:8989"
JELLYFIN_URL="http://localhost:8096"

# Credentials live in secrets.env (ACL-restricted to the owner), not in this
# file. They used to be hardcoded here, which put every API key into a
# world-readable script that is also swept into the nightly backup tarball.
SECRETS_FILE="${RW_SECRETS_FILE:-/c/ServerData/Stacks/secrets.env}"
if [ -r "$SECRETS_FILE" ]; then
  # shellcheck disable=SC1090
  . "$SECRETS_FILE"
else
  printf 'FATAL  cannot read %s -- refusing to run without credentials\n' "$SECRETS_FILE" >&2
  exit 2
fi
: "${SONARR_KEY:?SONARR_KEY missing from $SECRETS_FILE}"
: "${JELLYFIN_KEY:?JELLYFIN_KEY missing from $SECRETS_FILE}"
: "${PROWLARR_KEY:?PROWLARR_KEY missing from $SECRETS_FILE}"

# --- DIRECT DUB-PACK GRAB (anime) -------------------------------------------
# Sonarr's AUTOMATIC search for anime queries by ABSOLUTE episode number, since
# these series carry useSceneNumbering=true -- it looks for "Shokugeki no Souma
# 25", not "Food Wars Season 2". A dub pack named only "[crane0922] Food Wars!
# Season 2 ... [Dual Audio]", with no episode numbers anywhere in the title, is
# therefore never surfaced by Sonarr's own search, no matter how well seeded.
# Verified 2026-07-28: Sonarr parses that exact title perfectly when handed it
# (seasonNumber=2, fullSeason=true, 13 episodes matched to the right series) and
# imports it happily -- it simply never finds it. Meanwhile it kept grabbing a
# subbed release, which is how Season 1 got pulled when Season 2 was requested.
#
# So for anime the script finds the pack itself through Prowlarr and grabs it via
# Prowlarr's download client, which drops it into qBittorrent under Sonarr's
# category for normal import. No credentials live here: Prowlarr owns the
# download client.
#
# SAFETY: every candidate is validated through Sonarr's own /api/v3/parse and is
# only grabbed if it maps to THIS series id AND the exact season wanted. That
# check is what makes unattended grabbing safe, and it is precisely what would
# have prevented the wrong-season incident.
DUB_GRAB="${RW_DUB_GRAB:-true}"
DUB_MIN_SEEDERS="${RW_DUB_MIN_SEEDERS:-5}"
DUB_MAX_GB="${RW_DUB_MAX_GB:-60}"
# ----------------------------------------------------------------------------

# Rotate before writing: this log is appended on every run (72/day at the
# 20-minute schedule) and would otherwise grow without limit.
LOG_MAX_BYTES="${RW_LOG_MAX_BYTES:-5242880}"   # 5 MB
if [ -f "$LOG_FILE" ]; then
  _sz=$(wc -c < "$LOG_FILE" 2>/dev/null | tr -d ' ')
  if [ "${_sz:-0}" -gt "$LOG_MAX_BYTES" ]; then
    mv -f "$LOG_FILE" "${LOG_FILE}.1" 2>/dev/null
  fi
fi

log() { printf '%s %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" | tee -a "$LOG_FILE"; }

sonarr()     { docker exec sonarr sh -c "curl -s -H 'X-Api-Key: $SONARR_KEY' '$SONARR_URL$1'"; }
sonarr_delq() { docker exec sonarr sh -c "curl -s -o /dev/null -w '%{http_code}' -X DELETE -H 'X-Api-Key: $SONARR_KEY' '$SONARR_URL$1'"; }
sonarr_put() { docker exec sonarr sh -c "curl -s -X PUT -H 'X-Api-Key: $SONARR_KEY' -H 'Content-Type: application/json' -d '$2' '$SONARR_URL$1'"; }
sonarr_del() { docker exec sonarr sh -c "curl -s -o /dev/null -w '%{http_code}' -X DELETE -H 'X-Api-Key: $SONARR_KEY' '$SONARR_URL$1'"; }
jf()         { docker exec jellyfin sh -c "curl -s -H 'X-Emby-Token: $JELLYFIN_KEY' '$JELLYFIN_URL$1'"; }
jf_post()    { docker exec jellyfin sh -c "curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-Emby-Token: $JELLYFIN_KEY' '$JELLYFIN_URL$1'"; }
sonarr_post() { docker exec sonarr sh -c "curl -s -o /dev/null -w '%{http_code}' -X POST -H 'X-Api-Key: $SONARR_KEY' -H 'Content-Type: application/json' -d '$2' '$SONARR_URL$1'"; }
jq_c()       { docker exec -i sonarr jq "$@"; }

EXIT_CODE=0
SKIPPED=0

log "=========================================================="
log "rolling-window START  WINDOW_SIZE=$WINDOW_SIZE  DRY_RUN=$DRY_RUN  DELETE_WATCHED=$DELETE_WATCHED"
log "=========================================================="

# ---- 0. Preflight: both containers must answer, or we abort loudly ---------
if ! sonarr "/api/v3/system/status" | grep -q '"version"'; then
  log "FATAL  Sonarr API unreachable — aborting, nothing touched."
  exit 2
fi
if ! jf "/System/Info" | grep -q '"Version"'; then
  log "FATAL  Jellyfin API unreachable — aborting, nothing touched."
  exit 2
fi

# ---- 1. Map Jellyfin series to Sonarr series -------------------------------
# Titles differ between the two (Sonarr "Food Wars!" vs Jellyfin
# "Food Wars: Shokugeki no Soma"), so never match on name. Key on IMDb ID,
# fall back to the library path, which both sides report identically.
# Built as the union over every user: a single account can have libraries
# excluded from it (Family acc cannot see Anime), so one user's view is not
# the whole library.
JF_SERIES_RAW=$(mktemp)
JF_SERIES=$(mktemp)      # lines: jfSeriesId|imdbId|path|name
for uid in $(jf "/Users" | jq_c -r '.[].Id'); do
  jf "/Items?userId=$uid&Recursive=true&IncludeItemTypes=Series&Fields=Path,ProviderIds&EnableTotalRecordCount=false" \
    | jq_c -r '.Items[]? | "\(.Id)|\(.ProviderIds.Imdb // "")|\(.Path // "")|\(.Name)"' >> "$JF_SERIES_RAW" 2>/dev/null
done
sort -u "$JF_SERIES_RAW" > "$JF_SERIES"; rm -f "$JF_SERIES_RAW"

log "Jellyfin series indexed: $(wc -l < "$JF_SERIES" | tr -d ' ')"

# resolve_jf_series <imdbId> <sonarrPath> -> prints jfSeriesId, or empty
resolve_jf_series() {
  local imdb="$1" path="$2" hit=""
  if [ -n "$imdb" ] && [ "$imdb" != "null" ]; then
    hit=$(awk -F'|' -v k="$imdb" '$2==k{print $1; exit}' "$JF_SERIES")
  fi
  if [ -z "$hit" ] && [ -n "$path" ]; then
    hit=$(awk -F'|' -v k="$path" '$3==k{print $1; exit}' "$JF_SERIES")
  fi
  printf '%s' "$hit"
}

# ---- 1b. Collect confirmed-watched episodes, keyed by Jellyfin series id ----
WATCHED_FILE=$(mktemp)   # lines: jfSeriesId|Season|Episode|User
USER_COUNT=0

for uid in $(jf "/Users" | jq_c -r '.[].Id'); do
  uname=$(jf "/Users/$uid" | jq_c -r '.Name')
  USER_COUNT=$((USER_COUNT+1))
  jf "/Items?userId=$uid&Recursive=true&IncludeItemTypes=Episode&Fields=UserData,ParentIndexNumber,IndexNumber,SeriesId&EnableTotalRecordCount=false" \
    | jq_c -r --arg u "$uname" '
        .Items[]?
        | select(.UserData.Played == true)
        | select(.ParentIndexNumber != null and .IndexNumber != null)
        | "\(.SeriesId)|\(.ParentIndexNumber)|\(.IndexNumber)|\($u)"' >> "$WATCHED_FILE" 2>/dev/null
done

log "Jellyfin users scanned: $USER_COUNT"
log "Confirmed-watched episode records: $(wc -l < "$WATCHED_FILE" | tr -d ' ')"

if [ "$USER_COUNT" -eq 0 ]; then
  log "FATAL  Jellyfin returned zero users — cannot confirm watch state. Aborting."
  rm -f "$WATCHED_FILE" "$JF_SERIES"
  exit 2
fi

if [ "$WATCHED_BY_ANY_USER" = "true" ]; then MIN_WATCHERS=1; else MIN_WATCHERS=$USER_COUNT; fi

jf_watched() {  # $1=jfSeriesId $2=season $3=episode -- Jellyfin's live view
  local n
  [ -z "$1" ] && return 1
  n=$(grep -Fc "$1|$2|$3|" "$WATCHED_FILE" 2>/dev/null || true)
  [ "${n:-0}" -ge "$MIN_WATCHERS" ]
}

# ---- DURABLE WATCH LEDGER --------------------------------------------------
# Jellyfin's Played flag lives on the library item, and the item disappears when
# the file is deleted -- so deleting a watched episode ERASES the proof that it
# was watched. Without this ledger the watch position silently resets to E1,
# the window slides back to the start of the series, every later file is judged
# "beyond window", and already-seen episodes get re-downloaded. Observed for
# real on 2026-07-26: the count went from 16 confirmed-watched to 0 the moment
# the Food Wars files were removed.
#
# So every confirmation Jellyfin gives is written here, keyed by SONARR ids
# (stable, and independent of whether the file still exists). Once an episode
# is in the ledger it stays watched forever.
LEDGER="${RW_LEDGER:-/c/ServerData/Stacks/watched-ledger.txt}"
[ -f "$LEDGER" ] || : > "$LEDGER"

ledger_has() {  # $1=sonarrSeriesId $2=season $3=episode
  grep -Fq "$1|$2|$3|" "$LEDGER" 2>/dev/null
}
ledger_add() {  # $1=sonarrSeriesId $2=season $3=episode
  ledger_has "$1" "$2" "$3" && return 0
  printf '%s|%s|%s|%s\n' "$1" "$2" "$3" "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" >> "$LEDGER"
  LEDGER_ADDED=$((LEDGER_ADDED+1))
}

# was_watched <sonarrSeriesId> <jfSeriesId> <season> <episode>
# Jellyfin is the only thing that can ADD a confirmation; the ledger makes it
# permanent. Never infers watched state from anything else.
was_watched() {
  if jf_watched "$2" "$3" "$4"; then
    ledger_add "$1" "$3" "$4"
    return 0
  fi
  ledger_has "$1" "$3" "$4"
}

LEDGER_ADDED=0
log "Watch ledger: $(wc -l < "$LEDGER" | tr -d ' ') episodes previously confirmed watched"

# ---- 1c. Snapshot Sonarr's download queue ----------------------------------
# Unmonitoring an episode does NOT cancel a download already in flight: the
# queue item stays, and the torrent sits in the client forever consuming disk.
# Cleanuparr will not reap these either -- a paused download is not "stalled",
# so none of its rules match. The script that orphaned them has to clean them.
#
# downloadId is captured because one torrent can back many queue entries: a
# season pack gives Sonarr one queue row per episode, all sharing a downloadId.
# Cancelling any one of them with removeFromClient=true deletes the whole
# torrent, taking the in-window episodes with it. See cancel_download.
QUEUE_TMP=$(mktemp)   # lines: episodeId|queueId|downloadId|title
sonarr "/api/v3/queue?pageSize=1000&includeUnknownSeriesItems=true" \
  | jq_c -r '.records[]? | select(.episodeId != null) | "\(.episodeId)|\(.id)|\(.downloadId // "")|\(.title)"' \
  > "$QUEUE_TMP" 2>/dev/null
log "Sonarr queue items with an episode: $(wc -l < "$QUEUE_TMP" | tr -d ' ')"

# drop_file <episodeFileId> <label> -- delete an unwatched file that sits
# beyond the window, so files on disk never exceed WINDOW_SIZE per show.
drop_file() {
  local efid="$1" label="$2" code
  [ "$DELETE_UNWATCHED_BEYOND_WINDOW" != "true" ] && return 0
  [ -z "$efid" ] || [ "$efid" = "0" ] || [ "$efid" = "null" ] && return 0
  # Dub pack episodes are kept: see DUB EXEMPTION above.
  if is_dub_file "$efid"; then
    log "    DUB KEEP        $label (from a dubbed release - exempt from the window)"
    TOTAL_DUBKEEP=$((TOTAL_DUBKEEP+1))
    return 0
  fi
  if [ "$DRY_RUN" = "true" ]; then
    log "    WOULD DROP FILE $label (unwatched, beyond window, fileId=$efid)"
    TOTAL_DROP=$((TOTAL_DROP+1)); return 0
  fi
  code=$(sonarr_del "/api/v3/episodefile/$efid")
  if [ "$code" = "200" ] || [ "$code" = "204" ]; then
    log "    DROPPED FILE    $label (unwatched, beyond window, fileId=$efid, http=$code)"
    TOTAL_DROP=$((TOTAL_DROP+1))
  else
    log "    DROP FAILED     $label (fileId=$efid, http=$code) - left in place"
    EXIT_CODE=1
  fi
}

# ---- SEARCH LEDGER ---------------------------------------------------------
# episodeId|lastSearchedUtcEpoch -- enforces SEARCH_COOLDOWN_HOURS.
[ -f "$SEARCH_LEDGER" ] || : > "$SEARCH_LEDGER"
SEARCH_BATCH=""      # space separated episode ids to search this run

# Ledger line format: episodeId|lastSearchEpoch|attempts
# (2-field lines from before attempt-counting are read as attempts=1)
search_due() {  # $1=episodeId -> 0 if it may be searched now
  local last now
  last=$(awk -F'|' -v e="$1" '$1==e {print $2}' "$SEARCH_LEDGER" | tail -1)
  [ -z "$last" ] && return 0
  now=$(date -u +%s)
  [ $(( now - last )) -ge $(( SEARCH_COOLDOWN_HOURS * 3600 )) ]
}

search_attempts() {  # $1=episodeId -> prints attempt count (0 if never)
  local a
  a=$(awk -F'|' -v e="$1" '$1==e {print ($3==""?1:$3)}' "$SEARCH_LEDGER" | tail -1)
  printf '%s' "${a:-0}"
}

search_mark() { # $1=episodeId -- record an attempt, incrementing the counter
  local tmp n
  n=$(search_attempts "$1"); n=$(( n + 1 ))
  tmp=$(mktemp)
  grep -v "^$1|" "$SEARCH_LEDGER" > "$tmp" 2>/dev/null
  printf '%s|%s|%s\n' "$1" "$(date -u +%s)" "$n" >> "$tmp"
  mv "$tmp" "$SEARCH_LEDGER"
}

# An episode is STUCK once it has been searched STUCK_THRESHOLD times across
# separate cooldown windows and still has no file and nothing in the queue.
# Retrying forever in silence is how E20 went unnoticed.
is_stuck() {  # $1=episodeId
  local n
  n=$(search_attempts "$1")
  [ "${n:-0}" -ge "$STUCK_THRESHOLD" ] && [ -z "$(queue_id_for "$1")" ]
}

# Ask Sonarr why nothing was grabbed, so the report says something actionable
# instead of just "stuck". Capped per run because this hits every indexer.
STUCK_REASON_BUDGET="${RW_STUCK_REASON_BUDGET:-3}"
stuck_reason() {  # $1=episodeId
  if [ "$STUCK_REASON_BUDGET" -le 0 ]; then printf 'reason lookup skipped (budget spent this run)'; return; fi
  STUCK_REASON_BUDGET=$((STUCK_REASON_BUDGET-1))
  local raw
  raw=$(sonarr "/api/v3/release?episodeId=$1" 2>/dev/null)
  printf '%s' "$raw" | jq_c -r '
    if (type != "array") then "could not query releases"
    elif (length == 0) then "no releases found on any indexer"
    else
      ([.[] | select(.approved == true)] | length) as $ok
      | if $ok > 0 then "\($ok) release(s) approved - grab may be in progress"
        else
          ([.[].rejections[]?] | group_by(.) | map({r: .[0], n: length})
           | sort_by(-.n) | .[0:2] | map("\(.n)x \(.r)") | join("; "))
          + " (across \(length) releases)"
        end
    end' 2>/dev/null || printf 'reason unavailable'
}

# queue_episode_search <episodeId> <label> -- queue a missing in-window episode
# for the batched EpisodeSearch fired at the end of the run.
queue_episode_search() {
  local eid="$1" label="$2"
  [ "$SEARCH_MISSING" != "true" ] && return 0
  # already downloading? then it is not missing, leave it alone
  [ -n "$(queue_id_for "$eid")" ] && return 0
  if ! search_due "$eid"; then
    log "    search on cooldown for $label (last attempt < ${SEARCH_COOLDOWN_HOURS}h ago)"
    return 0
  fi
  SEARCH_BATCH="$SEARCH_BATCH $eid"
  log "    QUEUED SEARCH  $label (monitored but missing)"
}

prowlarr() { docker exec prowlarr sh -c "curl -s -m 200 -H 'X-Api-Key: $PROWLARR_KEY' '$1'"; }

# sonarr_parse_ok <releaseTitle> <seriesId> <season> -- does Sonarr itself agree
# this release is that series and that season? Guessing from the title is how
# the wrong season gets grabbed, so the decision is delegated to Sonarr's parser.
sonarr_parse_ok() {
  local title="$1" want_sid="$2" want_sn="$3" enc out
  enc=$(printf '%s' "$title" | jq_c -sRr @uri 2>/dev/null)
  [ -z "$enc" ] && return 1
  out=$(docker exec sonarr sh -c "curl -s -m 45 -H 'X-Api-Key: $SONARR_KEY' '$SONARR_URL/api/v3/parse?title=$enc'" 2>/dev/null)
  printf '%s' "$out" | jq_c -e --argjson sid "$want_sid" --argjson sn "$want_sn" '
      (.series.id? == $sid)
      and (.parsedEpisodeInfo.seasonNumber? == $sn)
      and ((.episodes | length) > 0)' >/dev/null 2>&1
}

# dub_pack_grab <seriesId> <seriesTitle> <season> -- find and grab a dubbed pack
# for exactly this series/season. Returns 0 if something was grabbed.
dub_pack_grab() {
  local sid="$1" stitle="$2" sn="$3" q res n cand title seeds gb _e _s _n _m _h _f
  [ "$DUB_GRAB" != "true" ] && return 1

  # Never grab a second copy while something for this season is already in the
  # queue -- without this the 20-minute schedule would keep re-grabbing the same
  # 18GB pack every run until it finished importing.
  while IFS='|' read -r _e _s _n _m _h _f; do
    [ -z "${_e:-}" ] && continue
    [ "$_s" != "$sn" ] && continue
    if [ -n "$(queue_id_for "$_e")" ]; then
      log "    dub grab skipped: S${sn} already has a download in the queue"
      return 1
    fi
  done <<< "$ORDERED"
  q=$(printf '%s Season %s Dual Audio' "$stitle" "$sn" | jq_c -sRr @uri 2>/dev/null)
  [ -z "$q" ] && return 1

  # The grab must reuse the SAME cached search, so search and grab happen back to
  # back inside Prowlarr; its release cache expires quickly and a stale guid
  # fails with "Couldn't find requested release in cache".
  res=$(prowlarr "http://localhost:9696/api/v1/search?query=${q}&limit=80")
  n=$(printf '%s' "$res" | jq_c -r 'length' 2>/dev/null)
  case "$n" in ''|*[!0-9]*) log "    dub search failed for S${sn}"; return 1 ;; esac
  log "    dub search: $n results for '${stitle} Season ${sn}'"

  # Candidates: dubbed, enough seeders, not oversized, best-seeded first.
  printf '%s' "$res" | jq_c -r --argjson ms "$DUB_MIN_SEEDERS" --argjson mg "$DUB_MAX_GB" '
      [ .[]
        | select((.title|test("dual[ ._-]?audio|dubbed|eng(lish)?[ ._-]?dub";"i")))
        | select((.seeders // 0) >= $ms)
        | select(((.size // 0) / 1073741824) <= $mg) ]
      | sort_by(-(.seeders // 0)) | .[0:6][]
      | "\(.seeders)|\(.size)|\(.guid)|\(.indexerId)|\(.title)"' 2>/dev/null > "$DUBTMP"

  while IFS='|' read -r seeds size guid ixid title; do
    [ -z "${title:-}" ] && continue
    gb=$(( ${size:-0} / 1073741824 ))
    if ! sonarr_parse_ok "$title" "$sid" "$sn"; then
      log "    reject (not this series/season per Sonarr): ${title:0:58}"
      continue
    fi
    if [ "$DRY_RUN" = "true" ]; then
      log "    WOULD GRAB DUB  S${sn}: ${title:0:56} (seeds=$seeds ${gb}GB)"
      TOTAL_DUBGRAB=$((TOTAL_DUBGRAB+1)); return 0
    fi
    printf '{"guid":%s,"indexerId":%s}' "$(printf '%s' "$guid" | jq_c -Rs .)" "$ixid" > "$GRABTMP"
    docker cp "$GRABTMP" prowlarr:/tmp/rw_grab.json >/dev/null 2>&1
    code=$(docker exec prowlarr sh -c "curl -s -o /dev/null -w '%{http_code}' -m 90 -X POST -H 'X-Api-Key: $PROWLARR_KEY' -H 'Content-Type: application/json' --data-binary @/tmp/rw_grab.json 'http://localhost:9696/api/v1/search'")
    if [ "$code" = "200" ] || [ "$code" = "201" ]; then
      log "    GRABBED DUB     S${sn}: ${title:0:56}"
      log "                    (seeds=$seeds  ${gb}GB  verified by Sonarr as this series/season)"
      TOTAL_DUBGRAB=$((TOTAL_DUBGRAB+1))
      return 0
    fi
    log "    grab failed (http=$code): ${title:0:52}"
  done < "$DUBTMP"

  log "    no dubbed pack passed validation for S${sn}"
  return 1
}

# queue_id_for <episodeId> -> prints queueId, or empty
queue_id_for() { awk -F'|' -v e="$1" '$1==e {print $2; exit}' "$QUEUE_TMP"; }

# queue_dlid_for <episodeId> -> prints the downloadId backing it, or empty
queue_dlid_for() { awk -F'|' -v e="$1" '$1==e {print $3; exit}' "$QUEUE_TMP"; }

# queue_title_for <episodeId> -> prints the release title of its queue row
queue_title_for() { awk -F'|' -v e="$1" '$1==e {print $4; exit}' "$QUEUE_TMP"; }

# ---- DUB EXEMPTION ---------------------------------------------------------
# A dubbed release is nearly always a whole-season or complete-series pack (no
# dual-audio singles exist for most anime), so capping it at WINDOW_SIZE would
# mean never keeping a dub at all. Ruling, 2026-07-27: a dub PACK is exempt from
# the window -- the full season is kept. Single-episode releases, dubbed or not,
# still obey the 3-episode rule. So the exemption is deliberately narrow: it
# needs BOTH a dub marker in the title AND the release to be a real pack.
is_dub_title() { printf '%s' "${1:-}" | grep -qiE 'dual[ ._-]?audio|dubbed|eng(lish)?[ ._-]?dub'; }

# is_dub_file <episodeFileId> -- was this file imported from a dub release?
#
# The renamed library file often carries NO dub marker at all: the Food Wars
# dual-audio pack imported as "[Prof] S01E18 - The Fried Chicken of Youth.mkv",
# with "Dual Audio" only in the DOWNLOAD FOLDER name. sceneName is also empty
# for anything imported by a manual DownloadedEpisodesScan, and customFormats
# comes back empty on those too. Checking the stored name alone therefore missed
# a real dub and drop_file deleted both episodes right after importing them.
#
# So this checks two things, in order:
#   1. what Sonarr recorded  (sceneName / relativePath / releaseGroup / formats)
#   2. the ORIGINAL torrent path, found by matching the file's inode -- imports
#      are hardlinks (copyUsingHardlinks=true), so the library file and the
#      downloaded file share an inode, and the torrent path still contains the
#      folder name with the dub marker in it.
is_dub_file() {
  local fid="$1" meta lib ino src
  [ -z "$fid" ] || [ "$fid" = "0" ] || [ "$fid" = "null" ] && return 1

  meta=$(sonarr "/api/v3/episodefile/$fid" 2>/dev/null \
         | jq_c -r '"\(.sceneName // "") \(.relativePath // "") \(.releaseGroup // "") \([.customFormats[]?.name] | join(" "))"' 2>/dev/null)
  is_dub_title "$meta" && return 0

  # Follow the hardlink back to the download folder.
  lib=$(sonarr "/api/v3/episodefile/$fid" 2>/dev/null | jq_c -r '.path // ""' 2>/dev/null)
  [ -z "$lib" ] && return 1
  ino=$(docker exec sonarr sh -c "stat -c %i \"$lib\" 2>/dev/null")
  [ -z "$ino" ] && return 1
  src=$(docker exec sonarr sh -c "find /data/torrents -inum $ino 2>/dev/null | head -1")
  [ -z "$src" ] && return 1
  is_dub_title "$src"
}

# dl_share_count <downloadId> -> how many queue entries that one torrent backs.
# 1 = a single-episode release. >1 = a season pack.
dl_share_count() {
  [ -z "$1" ] && { printf '0'; return; }
  awk -F'|' -v d="$1" '$3==d {n++} END {print n+0}' "$QUEUE_TMP"
}

# cancel_download <episodeId> <label> -- remove queue item AND the torrent.
# blocklist=false: the release is fine, we simply no longer want it, so it must
# stay eligible if the window later advances onto this episode again.
#
# PACK GUARD (added 2026-07-27): a season pack is ONE torrent that Sonarr lists
# as many queue rows sharing a downloadId. removeFromClient=true on any row
# deletes that torrent, so cancelling episode 4 of a pack also destroys the
# in-window episodes 1-3 riding in it. That is exactly what happened to The
# Mentalist: cancelling S1E4 killed the S01 pack, the following 19 cancels then
# 404'd because their queue rows died with it, and the show ended up with zero
# files despite 23 successful grabs.
#
# So: only single-episode torrents are removed from the client. A pack is left
# to finish, and the surplus episodes it carries are reclaimed from disk by
# drop_file once they import (DELETE_UNWATCHED_BEYOND_WINDOW). Costs some
# transient disk; never destroys an episode the window wants.
cancel_download() {
  local eid="$1" label="$2" qid dlid shares code
  qid=$(queue_id_for "$eid")
  [ -z "$qid" ] && return 0
  dlid=$(queue_dlid_for "$eid")
  shares=$(dl_share_count "$dlid")
  if [ "${shares:-0}" -gt 1 ]; then
    # A dub pack is exempt from the window entirely, so it is never cancelled
    # even when every episode it carries is out of window.
    if is_dub_title "$(queue_title_for "$eid")"; then
      log "    DUB PACK KEEP   $label (dubbed pack of $shares episodes - exempt from the window)"
      TOTAL_PACKSAFE=$((TOTAL_PACKSAFE+1))
      return 0
    fi
    case " ${PROTECTED_DLIDS:-} " in
      *" $dlid "*)
        log "    PACK-SAFE SKIP  $label (queueId=$qid shares one torrent with $shares episodes,"
        log "                    including in-window ones - removing it would delete the whole"
        log "                    pack, so it is left to finish; surplus dropped after import)"
        TOTAL_PACKSAFE=$((TOTAL_PACKSAFE+1))
        return 0
        ;;
    esac
    # Pack backs nothing we want -> safe to remove the torrent outright.
    log "    pack $dlid ($shares episodes) carries no in-window episode - cancelling for real"
  fi
  if [ "$DRY_RUN" = "true" ]; then
    log "    WOULD CANCEL download for $label (queueId=$qid, removeFromClient=true)"
    TOTAL_CANCEL=$((TOTAL_CANCEL+1)); return 0
  fi
  code=$(sonarr_delq "/api/v3/queue/$qid?removeFromClient=true&blocklist=false&skipRedownload=true")
  if [ "$code" = "200" ] || [ "$code" = "204" ]; then
    log "    CANCELLED download for $label (queueId=$qid, removed from client, http=$code)"
    TOTAL_CANCEL=$((TOTAL_CANCEL+1))
  elif [ "$code" = "404" ]; then
    # The row is already gone -- the outcome we wanted. Not a failure, and it
    # must not fail the scheduled task.
    log "    ALREADY GONE    $label (queueId=$qid no longer in queue, http=404)"
    TOTAL_CANCEL=$((TOTAL_CANCEL+1))
  else
    log "    CANCEL FAILED for $label (queueId=$qid, http=$code) - left in place"
    EXIT_CODE=1
  fi
}

# ---- 2. Walk each Sonarr series --------------------------------------------
SERIES_TMP=$(mktemp)
sonarr "/api/v3/series" | jq_c -c '.[] | {id, title, monitored, rootFolderPath, path, imdbId, seriesType}' > "$SERIES_TMP"

TOTAL_KEEP=0; TOTAL_UNMON=0; TOTAL_DEL=0; TOTAL_DORMANT=0; TOTAL_CANCEL=0; TOTAL_DROP=0; TOTAL_STUCK=0
TOTAL_PACKSAFE=0; TOTAL_DUBKEEP=0; TOTAL_DUBGRAB=0
DUBTMP=$(mktemp); GRABTMP=$(mktemp)
STUCK_SKIPS=0
STUCK_TMP=$(mktemp)

while IFS= read -r srow; do
  [ -z "$srow" ] && continue
  sid=$(printf    '%s' "$srow" | jq_c -r '.id')
  stitle=$(printf '%s' "$srow" | jq_c -r '.title')
  smon=$(printf   '%s' "$srow" | jq_c -r '.monitored')
  sroot=$(printf  '%s' "$srow" | jq_c -r '.rootFolderPath')
  spath=$(printf  '%s' "$srow" | jq_c -r '.path // ""')
  simdb=$(printf  '%s' "$srow" | jq_c -r '.imdbId // ""')
  stype=$(printf  '%s' "$srow" | jq_c -r '.seriesType // "standard"')

  [ "$smon" != "true" ] && { log "SKIP  [$stitle] series not monitored"; continue; }

  in_scope=false
  for root in $ALLOWED_ROOTS; do
    case "$sroot" in "$root"*) in_scope=true ;; esac
  done
  if [ "$in_scope" != "true" ]; then
    log "SKIP  [$stitle] rootFolderPath '$sroot' outside allowed libraries"
    continue
  fi

  log "----------------------------------------------------------"
  log "SERIES [$stitle] (sonarrId=$sid)"
  STUCK_SKIPS=0

  EPS=$(sonarr "/api/v3/episode?seriesId=$sid")
  # id|season|episode|monitored|hasFile|episodeFileId
  ORDERED=$(printf '%s' "$EPS" | jq_c -r '
    [ .[] | select(.seasonNumber > 0) ]
    | sort_by(.seasonNumber, .episodeNumber)
    | .[] | "\(.id)|\(.seasonNumber)|\(.episodeNumber)|\(.monitored)|\(.hasFile)|\(.episodeFileId)"')

  if [ -z "$ORDERED" ]; then
    log "  SKIP  no regular-season episodes returned by Sonarr"
    SKIPPED=$((SKIPPED+1)); continue
  fi

  # --- which season packs are carrying episodes we still want? --------------
  # Built BEFORE any action below, because the pack guard in cancel_download
  # has to know about in-window episodes that come LATER in the walk (a pack
  # holding watched E1 plus in-window E2-E4 is reached at E1 first).
  #
  # Monitored-at-start is the right proxy for "wanted": it is exactly the
  # window the previous run established. A pack backing none of them -- a
  # dormant season grabbed by RSS, say -- stays cancellable, so the guard does
  # not turn into a licence to download whole seasons we will never watch.
  # Worst case that costs one extra 20-minute cycle: this run unmonitors the
  # dormant episodes, the next run sees them unmonitored and cancels the pack.
  NEED_SEASON_SEARCH=0
  SEASON_TO_SEARCH=""
  PROTECTED_DLIDS=""
  while IFS='|' read -r eid sn en emon ehas efid; do
    [ -z "${eid:-}" ] && continue
    [ "$emon" != "true" ] && continue
    _dl=$(queue_dlid_for "$eid")
    [ -z "$_dl" ] && continue
    case " $PROTECTED_DLIDS " in *" $_dl "*) ;; *) PROTECTED_DLIDS="$PROTECTED_DLIDS $_dl" ;; esac
  done <<< "$ORDERED"
  if [ -n "$PROTECTED_DLIDS" ]; then
    log "  packs carrying wanted episodes:$PROTECTED_DLIDS"
  fi

  # --- resolve to Jellyfin, and guard against unmatched series --------------
  jfid=$(resolve_jf_series "$simdb" "$spath")
  HAS_ANY_FILE=$(printf '%s\n' "$ORDERED" | awk -F'|' '$5=="true"{print;exit}')
  if [ -z "$jfid" ]; then
    if [ -n "$HAS_ANY_FILE" ]; then
      log "  SKIP  AMBIGUOUS: files on disk but no matching Jellyfin series"
      log "        (imdb='$simdb' path='$spath'). Library not scanned, or"
      log "        metadata missing. Watch state unconfirmable — nothing touched."
      SKIPPED=$((SKIPPED+1)); continue
    fi
    log "  no Jellyfin match and no files on disk — treating as fully unwatched"
  else
    jfname=$(awk -F'|' -v k="$jfid" '$1==k{print $4; exit}' "$JF_SERIES")
    log "  jellyfin match : '$jfname' (id=$jfid, via imdb='$simdb')"
  fi

  # --- watch position = furthest confirmed-watched episode ------------------
  POS_S=0; POS_E=0; WATCHED_COUNT=0
  while IFS='|' read -r eid sn en emon ehas efid; do
    [ -z "${eid:-}" ] && continue
    if was_watched "$sid" "$jfid" "$sn" "$en"; then
      WATCHED_COUNT=$((WATCHED_COUNT+1))
      if [ "$sn" -gt "$POS_S" ] || { [ "$sn" -eq "$POS_S" ] && [ "$en" -gt "$POS_E" ]; }; then
        POS_S=$sn; POS_E=$en
      fi
    fi
  done <<< "$ORDERED"

  # --- WHICH SEASONS DID THE USER ACTUALLY ASK FOR? -------------------------
  # Season-level `monitored` is what a Seerr request sets. Ignoring it was a real
  # bug (2026-07-27): the user requested Food Wars SEASON 2, Sonarr correctly
  # recorded S2 monitored / S1 not, and this script -- which only ever looked at
  # episodes -- still picked S1 as "earliest unwatched" and pulled down Season 1.
  # Requested seasons are the user's explicit intent and must win over the
  # script's own idea of where to start.
  REQ_SEASONS=$(sonarr "/api/v3/series/$sid" \
                | jq_c -r '[.seasons[]? | select(.monitored == true and .seasonNumber > 0) | .seasonNumber] | join(" ")' 2>/dev/null)
  if [ -z "${REQ_SEASONS// /}" ]; then
    log "  SKIP  no season is monitored — nothing has been requested for this series"
    SKIPPED=$((SKIPPED+1)); continue
  fi
  log "  requested seasons: $REQ_SEASONS"
  season_requested() { case " $REQ_SEASONS " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

  # --- ACTIVE SEASON = earliest unwatched episode IN A REQUESTED SEASON -----
  ACTIVE_SEASON=""
  while IFS='|' read -r eid sn en emon ehas efid; do
    [ -z "${eid:-}" ] && continue
    season_requested "$sn" || continue
    if ! was_watched "$sid" "$jfid" "$sn" "$en"; then ACTIVE_SEASON=$sn; break; fi
  done <<< "$ORDERED"

  if [ -z "$ACTIVE_SEASON" ]; then
    log "  every episode confirmed watched — no active season."
    ACTIVE_SEASON=-1
    REMAINING=0
  else
    REMAINING=0
    while IFS='|' read -r eid sn en emon ehas efid; do
      [ -z "${eid:-}" ] && continue
      [ "$sn" -ne "$ACTIVE_SEASON" ] && continue
      was_watched "$sid" "$jfid" "$sn" "$en" || REMAINING=$((REMAINING+1))
    done <<< "$ORDERED"
  fi

  # --- UNLOCK: next season joins when active season is nearly done ----------
  # Only unlocks into a season the user actually requested -- rolling forward
  # into an unrequested season would pre-fetch a season nobody asked for.
  UNLOCKED_SEASON=-1
  if [ "$ACTIVE_SEASON" -ge 0 ] && [ "$REMAINING" -le "$WINDOW_SIZE" ] && [ "$REMAINING" -gt 0 ]; then
    if season_requested $((ACTIVE_SEASON+1)); then
      UNLOCKED_SEASON=$((ACTIVE_SEASON+1))
    fi
  fi

  if [ "$WATCHED_COUNT" -eq 0 ]; then
    log "  watch position: NONE watched yet -> window starts at first episode"
  else
    log "  watch position: S${POS_S}E${POS_E}  (confirmed watched: $WATCHED_COUNT episodes)"
  fi
  if [ "$ACTIVE_SEASON" -ge 0 ]; then
    log "  active season : S${ACTIVE_SEASON}  ($REMAINING unwatched episodes remaining)"
    if [ "$UNLOCKED_SEASON" -ge 0 ]; then
      log "  UNLOCK        : S${ACTIVE_SEASON} within $WINDOW_SIZE of done -> S${UNLOCKED_SEASON} promoted to active"
    else
      log "  later seasons : DORMANT (S${ACTIVE_SEASON} has $REMAINING left, unlock at <=$WINDOW_SIZE)"
    fi
  fi

  # --- walk forward and act ------------------------------------------------
  kept=0
  while IFS='|' read -r eid sn en emon ehas efid; do
    [ -z "${eid:-}" ] && continue

    if was_watched "$sid" "$jfid" "$sn" "$en"; then
      if [ "$ehas" = "true" ] && [ "$DELETE_WATCHED" = "true" ]; then
        if [ "$DRY_RUN" = "true" ]; then
          log "  WOULD DELETE    S${sn}E${en}  (confirmed watched, file present, fileId=$efid)"
        else
          code=$(sonarr_del "/api/v3/episodefile/$efid")
          if [ "$code" = "200" ] || [ "$code" = "204" ]; then
            log "  DELETED         S${sn}E${en}  (confirmed watched, fileId=$efid, http=$code)"
          else
            log "  DELETE FAILED   S${sn}E${en}  (fileId=$efid, http=$code) — left in place"
            EXIT_CODE=1
          fi
        fi
        TOTAL_DEL=$((TOTAL_DEL+1))
      else
        log "  watched         S${sn}E${en}  (file=$ehas) - no action"
      fi
      # A watched episode must never stay monitored, or Sonarr re-grabs the
      # very file we just deleted and the window never moves forward.
      if [ "$emon" = "true" ]; then
        if [ "$DRY_RUN" = "true" ]; then
          log "  WOULD UNMONITOR S${sn}E${en}  (already watched - stop re-downloading it)"
        else
          sonarr_put "/api/v3/episode/monitor" "{\"episodeIds\":[$eid],\"monitored\":false}" >/dev/null
          log "  UNMONITORED     S${sn}E${en}  (already watched - stop re-downloading it)"
        fi
        TOTAL_UNMON=$((TOTAL_UNMON+1))
      fi
      cancel_download "$eid" "S${sn}E${en} (already watched)"
      continue
    fi

    # unwatched from here on
    season_active=false
    [ "$sn" -eq "$ACTIVE_SEASON" ] && season_active=true
    [ "$UNLOCKED_SEASON" -ge 0 ] && [ "$sn" -eq "$UNLOCKED_SEASON" ] && season_active=true

    if [ "$season_active" != "true" ]; then
      if [ "$emon" = "true" ]; then
        if [ "$DRY_RUN" = "true" ]; then
          log "  WOULD DORMANT   S${sn}E${en}  (season not active — unmonitor, never search)"
        else
          sonarr_put "/api/v3/episode/monitor" "{\"episodeIds\":[$eid],\"monitored\":false}" >/dev/null
          log "  DORMANT         S${sn}E${en}  (season not active — unmonitored)"
        fi
        TOTAL_DORMANT=$((TOTAL_DORMANT+1))
      fi
      # Outside the emon guard on purpose: an episode unmonitored on an earlier
      # run can still have a live download from before it was unmonitored.
      cancel_download "$eid" "S${sn}E${en} (dormant season)"
      [ "$ehas" = "true" ] && drop_file "$efid" "S${sn}E${en} (dormant season)"
      continue
    fi

    if [ "$kept" -lt "$WINDOW_SIZE" ]; then
      # STUCK episodes must not hold a window slot hostage. If one is
      # unobtainable, occupying a slot would silently shrink the buffer -- with
      # WINDOW_SIZE=3 and one stuck episode you would only ever have 2 episodes
      # ready. Skip it (leaving it monitored, in case a release appears later)
      # and let the next episode take the slot instead. Capped at WINDOW_SIZE
      # skips per series so a wholly-unavailable season cannot make this monitor
      # the entire show.
      if [ "$ehas" != "true" ] && is_stuck "$eid" && [ "$STUCK_SKIPS" -lt "$WINDOW_SIZE" ]; then
        STUCK_SKIPS=$((STUCK_SKIPS+1))
        atts=$(search_attempts "$eid")
        reason=$(stuck_reason "$eid")
        log "  STUCK           S${sn}E${en}  ($atts search attempts, still missing)"
        log "                  reason: $reason"
        log "                  not taking a window slot; window extends to the next episode"
        printf '%s|S%sE%s|%s|%s|%s\n' "$stitle" "$sn" "$en" "$atts" "$reason" \
               "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" >> "$STUCK_TMP"
        TOTAL_STUCK=$((TOTAL_STUCK+1))
        # keep it monitored so Sonarr still accepts it if it ever shows up
        if [ "$emon" != "true" ] && [ "$DRY_RUN" != "true" ]; then
          sonarr_put "/api/v3/episode/monitor" "{\"episodeIds\":[$eid],\"monitored\":true}" >/dev/null
        fi
        # ESCALATION: single episodes are exhausted for this one, so anime may
        # now fall back to a season pack (which an EpisodeSearch always rejects).
        # This is what keeps packs LAST: singles get STUCK_THRESHOLD attempts
        # first, and among singles the profile score puts dub above sub.
        if [ "$ANIME_SEASON_SEARCH" = "true" ] && [ "$stype" = "anime" ]; then
          log "                  anime: single episodes exhausted -> allowing a season pack"
          NEED_SEASON_SEARCH=1
          SEASON_TO_SEARCH=$sn
        fi
        continue
      fi
      kept=$((kept+1))
      if [ "$emon" = "true" ]; then
        log "  KEEP in window  S${sn}E${en}  (already monitored, slot $kept/$WINDOW_SIZE)"
      else
        if [ "$DRY_RUN" = "true" ]; then
          log "  WOULD MONITOR   S${sn}E${en}  (fill window, slot $kept/$WINDOW_SIZE)"
        else
          sonarr_put "/api/v3/episode/monitor" "{\"episodeIds\":[$eid],\"monitored\":true}" >/dev/null
          log "  MONITORED       S${sn}E${en}  (fill window, slot $kept/$WINDOW_SIZE)"
        fi
      fi
      TOTAL_KEEP=$((TOTAL_KEEP+1))
      # In the window but no file on disk: monitoring alone will never fetch it,
      # so ask Sonarr to actually go looking.
      #
      # ANIME TRIES THE DUB PACK FIRST (2026-07-28), because Sonarr's own search
      # structurally cannot find one -- it queries anime by absolute episode
      # number, and a pack titled "Season 2" has no episode numbers in it. See
      # the DIRECT DUB-PACK GRAB block at the top. dub_pack_grab searches
      # Prowlarr, validates each candidate through Sonarr's parser so only the
      # right series AND season can be taken, and grabs via Prowlarr's client.
      #
      # Order after that: SeasonSearch (packs allowed, dub scores +500 over a
      # sub's 0), then plain EpisodeSearch for singles. Each step only runs when
      # the one before it found nothing, and all of them share one cooldown key,
      # so a season is never hammered more than once per SEARCH_COOLDOWN_HOURS.
      if [ "$ehas" != "true" ]; then
        if [ "$ANIME_SEASON_SEARCH" = "true" ] && [ "$stype" = "anime" ] && search_due "season-${sid}-${sn}"; then
          if [ "$DUB_GRAB" = "true" ] && dub_pack_grab "$sid" "$stitle" "$sn"; then
            search_mark "season-${sid}-${sn}"
          else
            NEED_SEASON_SEARCH=1
            SEASON_TO_SEARCH=$sn
          fi
        else
          queue_episode_search "$eid" "S${sn}E${en}"
        fi
      fi
    else
      if [ "$emon" = "true" ]; then
        if [ "$DRY_RUN" = "true" ]; then
          log "  WOULD UNMONITOR S${sn}E${en}  (beyond window of $WINDOW_SIZE)"
        else
          sonarr_put "/api/v3/episode/monitor" "{\"episodeIds\":[$eid],\"monitored\":false}" >/dev/null
          log "  UNMONITORED     S${sn}E${en}  (beyond window of $WINDOW_SIZE)"
        fi
        TOTAL_UNMON=$((TOTAL_UNMON+1))
      fi
      # Same reasoning as the dormant branch: cancel any in-flight download for
      # an episode that is beyond the window, monitored or not.
      cancel_download "$eid" "S${sn}E${en} (beyond window)"
      [ "$ehas" = "true" ] && drop_file "$efid" "S${sn}E${en} (beyond window)"
    fi
  done <<< "$ORDERED"

  # --- anime: one SeasonSearch instead of N EpisodeSearches ------------------
  # Keyed in the search ledger as season-<seriesId>-<season> so it obeys the
  # same cooldown as episode searches and cannot hammer indexers every 20 min.
  if [ "$NEED_SEASON_SEARCH" = "1" ] && [ -n "$SEASON_TO_SEARCH" ]; then
    _skey="season-${sid}-${SEASON_TO_SEARCH}"
    _inq=0
    while IFS='|' read -r _e _s _n _m _h _f; do
      [ -z "${_e:-}" ] && continue
      [ "$_s" != "$SEASON_TO_SEARCH" ] && continue
      [ -n "$(queue_id_for "$_e")" ] && { _inq=1; break; }
    done <<< "$ORDERED"
    if [ "$_inq" = "1" ]; then
      log "  season search skipped: S${SEASON_TO_SEARCH} already has something in the queue"
    elif ! search_due "$_skey"; then
      log "  season search on cooldown for S${SEASON_TO_SEARCH} (last attempt < ${SEARCH_COOLDOWN_HOURS}h ago)"
    elif [ "$DRY_RUN" = "true" ]; then
      log "  WOULD SEASON SEARCH  anime S${SEASON_TO_SEARCH} (dub packs are rejected by an episode search)"
    else
      _code=$(sonarr_post "/api/v3/command" "{\"name\":\"SeasonSearch\",\"seriesId\":$sid,\"seasonNumber\":$SEASON_TO_SEARCH}")
      if [ "$_code" = "201" ] || [ "$_code" = "200" ]; then
        log "  SEASON SEARCH   anime S${SEASON_TO_SEARCH} triggered (http=$_code) - packs allowed, dub preferred by profile score"
        search_mark "$_skey"
      else
        log "  SEASON SEARCH FAILED  S${SEASON_TO_SEARCH} (http=$_code)"
        EXIT_CODE=1
      fi
    fi
  fi

done < "$SERIES_TMP"

log "=========================================================="
# ---- 3. Finish the cycle ---------------------------------------------------
# Deleting and monitoring is only half a loop. Without these two steps the
# automation looks correct while nothing actually arrives and Jellyfin keeps
# showing episodes whose files are gone.

# 3a. Search for everything in-window that is still missing.
SEARCH_COUNT=0
if [ -n "$SEARCH_BATCH" ]; then
  IDS=$(printf '%s' "$SEARCH_BATCH" | tr ' ' '\n' | grep -v '^$' | paste -sd, -)
  SEARCH_COUNT=$(printf '%s' "$IDS" | tr ',' '\n' | grep -c . )
  if [ "$DRY_RUN" = "true" ]; then
    log "WOULD SEARCH  EpisodeSearch for episode ids: $IDS ($SEARCH_COUNT episodes)"
  else
    code=$(sonarr_post "/api/v3/command" "{\"name\":\"EpisodeSearch\",\"episodeIds\":[$IDS]}")
    if [ "$code" = "201" ] || [ "$code" = "200" ]; then
      log "SEARCH TRIGGERED  EpisodeSearch for ids: $IDS (http=$code)"
      for e in $SEARCH_BATCH; do search_mark "$e"; done
    else
      log "SEARCH FAILED     EpisodeSearch ids: $IDS (http=$code)"
      EXIT_CODE=1
    fi
  fi
else
  log "No searches needed (every in-window episode has a file, is downloading, or is on cooldown)"
fi

# 3b. Tell Jellyfin the library changed, so deleted episodes stop showing as
#     playable ghosts and newly imported ones appear without waiting.
if [ "$REFRESH_JELLYFIN" = "true" ] && [ $((TOTAL_DEL + TOTAL_DROP)) -gt 0 ]; then
  if [ "$DRY_RUN" = "true" ]; then
    log "WOULD REFRESH Jellyfin library ($((TOTAL_DEL + TOTAL_DROP)) files removed this run)"
  else
    code=$(jf_post "/Library/Refresh")
    if [ "$code" = "204" ] || [ "$code" = "200" ]; then
      log "JELLYFIN REFRESH triggered ($((TOTAL_DEL + TOTAL_DROP)) files removed, http=$code)"
    else
      log "JELLYFIN REFRESH FAILED (http=$code) - library may show deleted episodes until next scan"
      EXIT_CODE=1
    fi
  fi
fi

# 3c. Publish stuck episodes somewhere visible without reading logs.
if [ "$DRY_RUN" != "true" ]; then
  {
    echo "# Episodes the automation cannot obtain."
    echo "# Rewritten every run at $(date -u +'%Y-%m-%dT%H:%M:%SZ'). Empty below = nothing stuck."
    echo "# An episode lands here after $STUCK_THRESHOLD failed searches. It stays monitored,"
    echo "# but no longer occupies a window slot, so your buffer is not reduced."
    echo "#"
    echo "# series | episode | attempts | reason | first seen"
    if [ -s "$STUCK_TMP" ]; then cat "$STUCK_TMP"; fi
  } > "$STUCK_FILE"
fi

if [ "$TOTAL_STUCK" -gt 0 ]; then
  log "----------------------------------------------------------"
  if [ "$DRY_RUN" = "true" ]; then
    log "STUCK EPISODES: $TOTAL_STUCK (dry run - $STUCK_FILE not rewritten)"
  else
    log "STUCK EPISODES: $TOTAL_STUCK - see $STUCK_FILE"
  fi
  while IFS='|' read -r st ep at rs ts; do
    log "  $st $ep after $at attempts: $rs"
  done < "$STUCK_TMP"
fi

log "Watch ledger: added $LEDGER_ADDED new confirmations this run"
log "SUMMARY  window=$TOTAL_KEEP  unmonitored=$TOTAL_UNMON  dormant=$TOTAL_DORMANT  deleted=$TOTAL_DEL  cancelled-downloads=$TOTAL_CANCEL  pack-safe-skips=$TOTAL_PACKSAFE  dub-kept=$TOTAL_DUBKEEP  dub-grabbed=$TOTAL_DUBGRAB  dropped-files=$TOTAL_DROP  searched=$SEARCH_COUNT  STUCK=$TOTAL_STUCK  skipped-ambiguous=$SKIPPED"
if [ "$DRY_RUN" = "true" ]; then
  log "DRY_RUN=true — nothing was changed or deleted. Report only."
else
  log "LIVE MODE — changes above were applied."
fi
log "=========================================================="

rm -f "$WATCHED_FILE" "$JF_SERIES" "$SERIES_TMP" "$QUEUE_TMP" "$STUCK_TMP" "$DUBTMP" "$GRABTMP"
exit $EXIT_CODE
