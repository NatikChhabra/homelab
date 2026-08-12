
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
if docker exec jarvis-hass test -f /config/custom_sentences/en/jarvis.yaml 2>/dev/null; then
  SENT_COUNT=$(docker exec jarvis-hass sh -c "grep -c '^  Jarvis' /config/custom_sentences/en/jarvis.yaml" 2>/dev/null | tr -d '\r')
  if [ "${SENT_COUNT:-0}" -ge 15 ]; then
    ok "custom sentences installed ($SENT_COUNT JARVIS intents)"
  else
    warn "custom_sentences/en/jarvis.yaml has only ${SENT_COUNT:-0} JARVIS intents - expected at least 15; local routing coverage will be reduced"
  fi
else
  fail "/config/custom_sentences/en/jarvis.yaml is MISSING - argument extraction ('set the AC to 22') and the comfort table all fall back to the model at ~1.5s each"
fi

if docker exec jarvis-hass test -f /config/packages/jarvis_intents.yaml 2>/dev/null; then
  ok "intent_script handlers installed"
else
  fail "/config/packages/jarvis_intents.yaml is MISSING - the custom sentences will match and then do NOTHING, which is worse than not matching"
fi

# --- entity aliases ---------------------------------------------------------
# Incident 2026-08-09: the lights are registered as "Gang 1/2/3". Nobody says
# that out loud, so every natural light command missed the local parser and
# went to the model. Aliases live in the entity registry, which HA rewrites on
# shutdown, so they are exactly the kind of thing that can vanish.
ALIAS_N=$(docker exec jarvis-hass python3 -c '
import json
try:
    d = json.load(open("/config/.storage/core.entity_registry", encoding="utf-8"))
except Exception:
    raise SystemExit(0)
n = 0
for e in d["data"]["entities"]:
    if e.get("entity_id", "").startswith(("light.node_switch_gang", "fan.fan")):
        n += len(e.get("aliases") or [])
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
if docker logs jarvis-voice 2>&1 | grep -q "closed-loop verification enabled"; then
  ok "closed-loop reply verification is enabled"
elif docker logs jarvis-voice 2>&1 | grep -qi "could not be imported"; then
  fail "jarvis-voice could NOT import verify/closed_loop.py - replies are spoken WITHOUT being checked against Home Assistant, so the model can confirm actions it never took"
else
  warn "cannot tell from jarvis-voice logs whether closed-loop verification is enabled"
fi

# --- infrastructure health: the stuff section 16 does NOT cover --------------
# Section 16 checks that containers are up and their health endpoints answer.
# This block checks that the system is actually usable end-to-end. All of these
# have been silent failures at some point: a service returns 200 but cannot
# do its job, or a host process dies and nothing notices because Docker does
# not supervise it.

# --- Kokoro TTS synthesis test ------------------------------------------------
# Incident: Kokoro reported healthy on /health while the /v1/audio/speech
# endpoint returned 404 for hours. A synthesis call that returns <100 bytes
# is not audio -- it is an error JSON. This check actually generates 5 seconds
# of speech and verifies the output is real audio.
KOKORO_OUT=$(curl -s -m 15 -X POST http://localhost:8880/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"model":"kokoro","input":"JARVIS is operational","voice":"bm_george"}' \
  -o /tmp/_audit_kokoro.wav 2>/dev/null && wc -c < /tmp/_audit_kokoro.wav | tr -d ' ')
if [ "${KOKORO_OUT:-0}" -ge 8000 ] 2>/dev/null; then
  ok "Kokoro TTS synthesis produces real audio (${KOKORO_OUT} bytes)"
elif [ -n "${KOKORO_OUT}" ]; then
  fail "Kokoro TTS synthesis returned only ${KOKORO_OUT} bytes -- likely JSON error, not audio"
else
  warn "Kokoro TTS synthesis could not reach localhost:8880 -- TTS path is UNVERIFIED"
fi
rm -f /tmp/_audit_kokoro.wav

# --- searxng HTTP check -------------------------------------------------------
# Incident: searxng container was running but all engines returned 0 results
# because the config had no enabled engines. The service answered 200 for the
# front page while being completely useless.
SEARXNG_RESULT=$(curl -s -m 15 "http://localhost:8100/search?q=wikipedia_test&format=json" 2>/dev/null)
SEARXNG_N=$(printf '%s' "$SEARXNG_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('results',[])))" 2>/dev/null || echo 0)
if [ "${SEARXNG_N:-0}" -ge 1 ] 2>/dev/null; then
  ok "searxng returns real search results (${SEARXNG_N} results for test query)"
else
  fail "searxng returned no results for test query -- search is broken despite container being healthy"
fi

# --- websearch real query test -------------------------------------------------
# Incident: websearch /api/status reported "websearch": "ok" while Google CSE
# was suspended ("too many requests"). The service answered 200 but all queries
# fell back to Wikipedia with a note about refusing engines. The status check
# only verifies the HTTP endpoint exists, not that the upstream is usable.
WS_RESULT=$(curl -s -m 20 "http://localhost:3101/search?q=what+is+the+time" 2>/dev/null)
WS_OK=$(printf '%s' "$WS_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('ok',False))" 2>/dev/null)
WS_MODE=$(printf '%s' "$WS_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('mode','?'))" 2>/dev/null)
WS_REFUSING=$(printf '%s' "$WS_RESULT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(','.join(d.get('engines_refusing',[])))" 2>/dev/null)
if [ "$WS_OK" = "True" ]; then
  if [ -n "$WS_REFUSING" ] && [ "$WS_REFUSING" != "[]" ]; then
    warn "websearch works but upstream engines refusing: ${WS_REFUSING} (mode=${WS_MODE})"
  else
    ok "websearch real query succeeds (mode=${WS_MODE})"
  fi
else
  fail "websearch /search returned non-OK response -- web search is DOWN"
fi

# --- host-process liveness (outside Docker) ------------------------------------
# Incident: host-ears.py and host-speaker.py are Windows scheduled tasks, not
# Docker containers. When they crash, Docker healthcheck sees nothing wrong
# because the containers are fine -- only the audio pipeline is broken.
# Same for pc-agent (PowerShell scheduled task) and wa-bridge (Node.js).
for TASK in "JARVIS - Ears" "JARVIS - Speaker" "JARVIS - PC Agent"; do
  STATE=$(powershell -Command "Get-ScheduledTask -TaskName '$TASK' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty State" 2>/dev/null | tr -d ' \r')
  if [ "$STATE" = "Running" ]; then
    ok "host task '${TASK}' is Running"
  elif [ -z "$STATE" ]; then
    warn "host task '${TASK}' not found -- may not be installed"
  else
    fail "host task '${TASK}' is '${STATE}' -- not Running; audio/PC bridge is broken"
  fi
done

# wa-bridge is a Node.js process, not a scheduled task by name. Check via health.
WA_HEALTH=$(curl -s -m 5 http://localhost:8097/health 2>/dev/null)
WA_LINKED=$(printf '%s' "$WA_HEALTH" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('linked',False))" 2>/dev/null)
if printf '%s' "$WA_HEALTH" | grep -q '"service":"jarvis-whatsapp"'; then
  ok "wa-bridge health endpoint responding on port 8097"
  if [ "$WA_LINKED" = "True" ]; then
    ok "wa-bridge is linked to WhatsApp"
  else
    warn "wa-bridge is running but NOT linked to WhatsApp (awaiting QR scan)"
  fi
else
  fail "wa-bridge health endpoint NOT responding on port 8097 -- WhatsApp bridge is DOWN"
fi

# --- call-bridge liveness ------------------------------------------------------
# Returns JSON even when HFP is not registered; a connection failure means the
# bridge process is dead, not just unconfigured.
CALL_STATE=$(curl -s -m 5 http://localhost:8098/call/state 2>/dev/null)
if printf '%s' "$CALL_STATE" | grep -q '^{'; then
  ok "call-bridge /call/state responding on port 8098"
else
  fail "call-bridge /call/state NOT responding on port 8098 -- call bridge is DOWN"
fi

# --- VRAM headroom -------------------------------------------------------------
# Incident: Ollama evicted qwen2.5:3b under memory pressure, causing every
# LLM call to hit an 8-second cold start. With KEEP_ALIVE=-1 this should not
# happen, but the check confirms the model is actually resident.
GPU_MIБ=$(curl -s http://localhost:3100/api/status 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('gpu',{}).get('miB_used',0))" 2>/dev/null)
GPU_TOTAL=4096
if [ -n "$GPU_MIБ" ] && [ "$(printf '%s' "$GPU_MIБ" | grep -c '[0-9]')" -gt 0 ]; then
  GPU_PCT=$(python3 -c "print(int(float('${GPU_MIБ}')/${GPU_TOTAL}*100))" 2>/dev/null)
  GPU_FREE=$((GPU_TOTAL - $(python3 -c "print(int(float('${GPU_MIБ}')))")))
  if [ "${GPU_PCT:-0}" -lt 80 ]; then
    ok "VRAM headroom OK: ${GPU_MIБ} MiB / ${GPU_TOTAL} MiB used (${GPU_PCT}%), ${GPU_FREE} MiB free"
  elif [ "${GPU_PCT:-0}" -lt 95 ]; then
    warn "VRAM pressure: ${GPU_MIБ} MiB / ${GPU_TOTAL} MiB used (${GPU_PCT}%) -- eviction risk if another model loads"
  else
    fail "VRAM near-exhaustion: ${GPU_MIБ} MiB / ${GPU_TOTAL} MiB used (${GPU_PCT}%) -- Ollama may evict under load"
  fi
else
  warn "could not read GPU VRAM from /api/status -- headroom UNVERIFIED"
fi

# --- free space on C: ---------------------------------------------------------
# Incident: Docker overlay filled C: drive because log rotation was not
# configured for jarvis-voice. A full drive kills new container writes.
C_USED_PCT=$(df -h /c 2>/dev/null | tail -1 | awk '{print $5}' | tr -d '%' || echo "")
if [ -n "$C_USED_PCT" ] && [ "$C_USED_PCT" -ge 95 ] 2>/dev/null; then
  fail "C: drive at ${C_USED_PCT}% -- Docker and system operations at risk"
elif [ -n "$C_USED_PCT" ] && [ "$C_USED_PCT" -ge 85 ] 2>/dev/null; then
  warn "C: drive at ${C_USED_PCT}% -- consider cleaning Docker images or old logs"
else
  C_AVAIL=$(df -h /c 2>/dev/null | tail -1 | awk '{print $4}')
  ok "C: drive free space OK (${C_AVAIL} available)"
fi

# --- File Browser :8095 ------------------------------------------------------
# Undocumented service. Serves C:/ServerData/Sync/ as root. Useful for
# pulling reports off the box without Tailscale SSH. Not part of the JARVIS
# compose stack (it is a separate Docker container on the host) so it was
# missed by every earlier health check.
FB_HTML=$(curl -s -m 5 http://localhost:8095/ 2>/dev/null)
if printf '%s' "$FB_HTML" | grep -q 'File Browser'; then
  ok "File Browser :8095 responding (serves C:/ServerData/Sync/)"
else
  fail "File Browser :8095 NOT responding -- report access path is DOWN"
fi

# --- Custom sentences in HA container -----------------------------------------
# Incident 2026-08-12: custom sentences were missing from the running HA
# container despite existing in the repo. A docker cp fix was applied manually
# but did not survive container restarts. The fix is now a bind-mount in
# docker-compose.yml so the files are always present. This check asserts
# they exist inside the running container.
if docker exec jarvis-hass test -f /config/custom_sentences/en/jarvis.yaml 2>/dev/null; then
  ok "custom_sentences/en/jarvis.yaml exists in running HA container"
else
  fail "/config/custom_sentences/en/jarvis.yaml MISSING in HA container -- local routing broken"
fi

if docker exec jarvis-hass test -f /config/packages/jarvis_intents.yaml 2>/dev/null; then
  ok "packages/jarvis_intents.yaml exists in running HA container"
else
  fail "/config/packages/jarvis_intents.yaml MISSING in HA container -- intent handlers broken"
fi
