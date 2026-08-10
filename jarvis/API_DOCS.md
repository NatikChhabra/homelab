# JARVIS API Documentation

## Voice Server (`jarvis-voice`, port 3100)

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Main UI (React app) |
| GET | `/healthz` | Liveness probe |
| GET | `/api/status` | Service health + router stats |
| GET | `/api/verify` | Closed-loop verification status |
| GET | `/api/vitals` | CPU, memory, disk, containers (from Glances) |
| GET | `/api/devices` | Device states from HA |
| POST | `/api/toggle` | Toggle a device |
| GET | `/ws` | WebSocket voice pipeline |
| GET | `/api/hud/*` | HUD panels (registered by hud.py) |
| GET | `/download/hud` | Download JARVIS-HUD.rmskin |
| GET | `/static/*` | Static UI assets |

### `/api/status` Response

```json
{
  "hass": "ok",
  "hass_token_configured": true,
  "ollama": "ok",
  "whisper": "ok",
  "piper": "ok",
  "wake_word": "ok",
  "tts": {
    "engine": "kokoro",
    "voice": "bm_george(3)+bm_lewis(3)+bm_fable(1)",
    "kokoro": "ok",
    "kokoro_voice_count": 68
  },
  "router": {
    "local_hits": 0,
    "hermes_hits": 0,
    "llm_hits": 0,
    "refusals": 0,
    "greetings": 0,
    "errors": 0,
    "local_share": 0.0,
    "big_agent_share": 0.0,
    "local_median_ms": 0.0,
    "hermes_median_ms": 0.0,
    "llm_median_ms": 0.0
  }
}
```

## Web Search Service (`jarvis-websearch`, port 3101)

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/search` | Search the web |
| GET | `/healthz` | Liveness probe |
| GET | `/api/status` | Service status |
| GET | `/api/egress` | Egress audit log |

### POST `/search`

Request:
```json
{"q": "what is the weather in mumbai"}
```

Response:
```json
{
  "ok": true,
  "query": "what is the weather in mumbai",
  "answer": "Current temperature is 88 degrees.",
  "detail": "[1] Mumbai Weather - AccuWeather: ...",
  "sources": [{"title": "...", "url": "...", "engine": "google cse"}],
  "mode": "summarise",
  "upstream": "searxng",
  "elapsed_ms": 6214
}
```

Error response:
```json
{
  "ok": false,
  "error": "no-results",
  "answer": "Nothing found on that, sir.",
  "detail": "no results for '...'",
  "query": "..."
}
```

## Capabilities Service (`jarvis-capabilities`, port 3102)

### Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/healthz` | Liveness probe |
| GET | `/api/status` | Server status (CPU, RAM, containers) |
| POST | `/api/timer/cancel` | Cancel a timer |
| POST | `/api/timer/list` | List active timers |
| GET | `/api/announce/pending` | Pending announcements |
| GET | `/api/announce/file/<name>` | Play announcement audio |

### GET `/api/status`

Returns structured server health from Glances + audit report.

## Hermes Specialists

| Specialist | Tools | Role |
|------------|-------|------|
| lights | HassTurnOn, HassTurnOff, HassLightSet, GetLiveContext | Bedroom lights |
| climate | ac_set_temperature, ac_on, ac_off, ac_max_cool, ac_dry, GetLiveContext | Air conditioner |
| fan | fan_set, fan_boost, GetLiveContext | Ceiling fan |
| scenes | good_night, all_off, movie_time, tv_toggle | Whole-room routines |
| pc | pc_power, pc_media, pc_set_volume, pc_app, pc_type, pc_status | Windows PC |
| comms | wa_send, wa_read | WhatsApp |
| comfort | ac_set_temperature, ac_max_cool, ac_dry, fan_set, fan_boost, GetLiveContext | Combined AC+fan |
| research | web_search, web_fetch, server_status | Internet + server |
| chat | (none) | Conversation only |

## Routing Flow

```
utterance
  → phrasing.normalise (~0ms, string rewrite)
  → conversation.home_assistant (~10-60ms, local)
  → Hermes classify (~0ms, keyword scoring)
  → Hermes + Ollama (~500-1500ms, specialist)
  → conversation.jarvis (~750-5000ms, 17-tool agent)
```

Local wins when it returns action_done with no failures.
Hermes wins when it produces a tool call or talk_only speech.
LLM is fallback when both fail.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| JARVIS_HASS_TOKEN | (required) | HA API token |
| JARVIS_PC_TOKEN | (required) | PC agent token |
| JARVIS_WA_TOKEN | (required) | WhatsApp bridge token |
| JARVIS_WEBSEARCH_ENABLED | 1 | Disable to block all internet |
| SEARXNG_SECRET | (generated) | SearxNG session secret |
