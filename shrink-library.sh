#!/bin/bash
# ============================================================================
# shrink-library.sh -- nightly GPU re-encode of H.264/older video to HEVC.
#
# Added 2026-11-01. Reuses Jellyfin's own ffmpeg + the RTX 3050 (NVENC) via
# `docker exec jellyfin`, so there is no extra app to keep updated.
#
# Settings were picked by measurement, not guesswork: on a Netflix 1080p
# WEB-DL sample, hevc_nvenc cq 24 came out 15% BIGGER than the source; cq 28
# gave 62% of the size at SSIM 0.971 (indistinguishable on a TV); cq 30 gave
# 46% at 0.968. Quality wins here, so cq 28.
#
# Safety rules (each one guards against losing or damaging a file):
#   - skip HEVC/AV1, files < 500 MB, files modified in the last 24 h
#     (still importing), and anything with link count > 1 (still seeding --
#     replacing it would break the torrent and double disk use)
#   - skip the whole run if anyone is watching Jellyfin
#   - encode to a temp file in the same folder; replace ONLY if duration
#     matches within 2 s and the result is at least 10% smaller
#   - at most MAX_FILES per night so the GPU isn't busy all morning
#   - the new file keeps a NEW mtime on purpose: Jellyfin only re-probes codec
#     info when the modified date changes (keeping the old one left it "h264")
# Then asks Sonarr/Radarr to rescan and Jellyfin to refresh.
# ============================================================================
set -u
# Git Bash rewrites /data/... into C:/Program Files/Git/data/... when passing
# arguments to docker exec; container paths must reach the container untouched.
export MSYS_NO_PATHCONV=1
LOG=/c/ServerData/Stacks/logs/shrink-library.log
MAX_FILES=${MAX_FILES:-12}
CQ=28
DRY=${DRY_RUN:-0}
# 2026-11-03: one encode hung for 6 h (host memory starvation) and burned the whole night.
# Each encode now gets ENCODE_TIMEOUT seconds (the longest real one took ~7 min); a file that
# times out goes on SKIP_LIST and is not retried until someone removes its line.
ENCODE_TIMEOUT=${ENCODE_TIMEOUT:-1800}
# Stay inside run-job.ps1's 18000 s Timeout: an encode that could overrun RUN_BUDGET is not started,
# because run-job's kill stops this script but not ffmpeg inside the container (2026-11-03).
RUN_BUDGET=${RUN_BUDGET:-16200}
RUN_START=$(date +%s)
SKIP_LIST=/c/ServerData/Stacks/logs/shrink-library-skip.txt
log(){ printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG"; }

set -a; . /c/ServerData/Stacks/secrets.env; set +a
JF="Authorization: MediaBrowser Token=$JELLYFIN_KEY"
# Fail loudly (non-zero exit -> run-job.ps1 marks the Kuma heartbeat down) when
# the run cannot work at all, instead of finishing "processed=0" with exit 0.
docker exec jellyfin true >/dev/null 2>&1 || { log "FAIL docker/jellyfin unreachable"; exit 1; }
fails=0

log "=== shrink-library START (max $MAX_FILES files, cq $CQ, dry=$DRY)"
watching=$(docker exec jellyfin sh -c "curl -s -m 10 -H '$JF' http://localhost:8096/Sessions" 2>/dev/null | grep -c '"NowPlayingItem"')
if [ "${watching:-0}" -gt 0 ]; then log "someone is watching ($watching session(s)) - skipping tonight"; exit 0; fi

# Partial temp files from an encode that was killed (reboot, USB drop) are never
# re-encoded or cleaned, because the scan below skips *.shrinking.*. Only one run
# exists at a time, so any left now are dead (2026-11-10 review).
[ "$DRY" != 1 ] && docker exec jellyfin sh -c 'find /data/library -type f -name "*.shrinking.*" -print -delete' 2>/dev/null \
  | while IFS= read -r t; do log "removed leftover partial encode: $t"; done
done_n=0; saved=0
while IFS= read -r f; do
  [ "$done_n" -ge "$MAX_FILES" ] && break
  info=$(docker exec jellyfin sh -c "
    f=\"\$1\"; c=\$(/usr/lib/jellyfin-ffmpeg/ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 \"\$f\")
    echo \"\$c \$(stat -c '%s %h %Y' \"\$f\")\"" _ "$f")
  read -r codec size links mtime <<< "$info"
  case "$codec" in hevc|av1|"") continue ;; esac
  [ "$size" -lt 500000000 ] && continue
  [ "$links" -gt 1 ] && { log "skip (seeding/hardlinked): $f"; continue; }
  [ $(( $(date +%s) - mtime )) -lt 86400 ] && continue
  if [ -f "$SKIP_LIST" ] && grep -Fxq -- "$f" "$SKIP_LIST"; then log "skip (timed out before, see $SKIP_LIST): $f"; continue; fi
  if [ "$DRY" = 1 ]; then log "would shrink: $f ($((size/1048576)) MB, $codec)"; done_n=$((done_n+1)); continue; fi
  if [ $(( $(date +%s) - RUN_START + ENCODE_TIMEOUT + 60 )) -gt "$RUN_BUDGET" ]; then log "time budget reached (${RUN_BUDGET}s) - stopping before the next encode"; break; fi

  log "encoding: $f ($((size/1048576)) MB, $codec)"
  res=$(docker exec jellyfin sh -c '
    f="$1"; cq="$2"; lim="$3"; FF=/usr/lib/jellyfin-ffmpeg/ffmpeg; FP=/usr/lib/jellyfin-ffmpeg/ffprobe
    ext="${f##*.}"; tmp="${f%.*}.shrinking.$ext"; own=$(stat -c %u:%g "$f")
    timeout -k 30 "$lim" $FF -v error -nostdin -y -hwaccel cuda -hwaccel_output_format cuda -i "$f" -map 0 -c copy -c:v:0 hevc_nvenc -preset p7 -tune hq \
        -rc vbr -cq "$cq" -b:v 0 -spatial_aq 1 -temporal_aq 1 -aq-strength 8 -rc-lookahead 32 -bf 4 -b_ref_mode middle \
        -tag:v:0 hvc1 "$tmp"; rc=$?
    if [ "$rc" -ne 0 ]; then rm -f "$tmp" "${f%.*}.shrinking.nfo"; case "$rc" in 124|137) echo "FAIL timeout after ${lim}s (rc $rc)" ;; *) echo "FAIL encode (rc $rc)" ;; esac; exit; fi
    d1=$($FP -v error -show_entries format=duration -of csv=p=0 "$f"); d2=$($FP -v error -show_entries format=duration -of csv=p=0 "$tmp")
    s1=$(stat -c %s "$f"); s2=$(stat -c %s "$tmp")
    ok=$(awk -v a="$d1" -v b="$d2" -v x="$s1" -v y="$s2" "BEGIN{print ((a-b<2 && b-a<2) && y < x*0.9) ? 1 : 0}")
    rm -f "${f%.*}.shrinking.nfo"   # Jellyfin writes metadata for the temp file while it exists
    if [ "$ok" = 1 ]; then if mv -f "$tmp" "$f"; then chown "$own" "$f"; echo "OK $s1 $s2"; else rm -f "$tmp"; echo "FAIL replace (mv failed, original kept)"; fi; else rm -f "$tmp"; echo "KEPT original (dur $d1 vs $d2, size $s1 vs $s2)"; fi' _ "$f" "$CQ" "$ENCODE_TIMEOUT")
  case "$res" in
    OK*) read -r _ s1 s2 <<< "$res"; saved=$((saved + s1 - s2)); log "  done: $((s1/1048576)) -> $((s2/1048576)) MB" ;;
    *)   log "  $res"; case "$res" in FAIL*|"") fails=$((fails+1)) ;; esac
         case "$res" in "FAIL timeout"*) echo "$f" >> "$SKIP_LIST"; log "  added to $SKIP_LIST" ;; esac ;;
  esac
  done_n=$((done_n+1))
done < <(docker exec jellyfin sh -c 'find /data/library -type f \( -iname "*.mkv" -o -iname "*.mp4" \) ! -name "*.shrinking.*" | sort')

if [ "$DRY" != 1 ] && [ "$done_n" -gt 0 ]; then
  docker exec sonarr sh -c "curl -s -o /dev/null -X POST -H 'X-Api-Key: $(docker exec sonarr sh -c "grep -o '<ApiKey>[^<]*' /config/config.xml | cut -d'>' -f2")' -H 'Content-Type: application/json' -d '{\"name\":\"RescanSeries\"}' http://localhost:8989/api/v3/command"
  docker exec radarr sh -c "curl -s -o /dev/null -X POST -H 'X-Api-Key: $(docker exec radarr sh -c "grep -o '<ApiKey>[^<]*' /config/config.xml | cut -d'>' -f2")' -H 'Content-Type: application/json' -d '{\"name\":\"RescanMovie\"}' http://localhost:7878/api/v3/command"
  docker exec jellyfin sh -c "curl -s -o /dev/null -X POST -H '$JF' http://localhost:8096/Library/Refresh"
fi
log "SUMMARY processed=$done_n saved=$((saved/1048576)) MB failures=$fails"
[ "$fails" -gt 0 ] && exit 1
exit 0
