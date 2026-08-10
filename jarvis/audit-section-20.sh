
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
if docker logs jarvis-voice 2>&1 | tail -200 | grep -q "closed-loop verification enabled"; then
  ok "closed-loop reply verification is enabled"
elif docker logs jarvis-voice 2>&1 | tail -200 | grep -qi "could not be imported"; then
  fail "jarvis-voice could NOT import verify/closed_loop.py - replies are spoken WITHOUT being checked against Home Assistant, so the model can confirm actions it never took"
else
  warn "cannot tell from jarvis-voice logs whether closed-loop verification is enabled"
fi
