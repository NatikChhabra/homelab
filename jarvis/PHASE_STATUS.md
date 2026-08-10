# JARVIS Phase Status

Last updated: 2026-08-10

## Phase A: Critical Fixes — DONE

| Task | Status | Notes |
|------|--------|-------|
| A1: STT number mishearing | DONE | `_expand_word_numbers()` in phrasing.py converts "twenty one" → 21 |
| A2: Fan speed execution | DONE | Word-to-number rules for fan speeds 1-5 |
| A3: "feeling hot" speech | DONE | Local rule routes to `script.feeling_hot` with correct speech |

## Phase B: Voice Improvements — 2/3 DONE

| Task | Status | Notes |
|------|--------|-------|
| B1: Acknowledgment sounds | DONE | 4 WAV beeps in `host-ears/beeps/`, plays on wake word |
| B2: Dual mic support | NOT STARTED | ears.py supports single mic via `--mic`. Dual-mic mixing would need sounddevice multistream |
| B3: Voice tuning | DONE | Kokoro bm_george(3)+bm_lewis(3)+bm_fable(1) at 0.92 speed |

## Phase C: Companion System — DONE

| Task | Status | Notes |
|------|--------|-------|
| C1: Companion architecture | DONE | 9 Hermes specialists: lights, climate, fan, scenes, pc, comms, comfort, research, chat |
| C2: I'm home scene | DONE | Custom sentence + `script.im_home` (lights on, AC 24, fan 2) |
| C3: Web search + research | DONE | `research` specialist with web_search, web_fetch, server_status tools |
| C4: Server monitoring | DONE | server_status dispatches to capabilities service on port 3102 |

## Phase D: HUD — 2/2 DONE

| Task | Status | Notes |
|------|--------|-------|
| D1: Rainmeter HUD package | DONE | `/download/hud` serves JARVIS-HUD.rmskin (12KB zip) |
| D2: HUD feature additions | PARTIAL | Basic panels exist. Companion dispatch panel and research results panel not yet built |

## Phase E: Documentation — NOT STARTED

| Task | Status | Notes |
|------|--------|-------|
| E1: Architecture docs | NOT DONE | See API_DOCS.md for technical details |
| E2: Phase tracking | DONE | This file |
| E3: API docs | NOT DONE | See API_DOCS.md |

## Out of Scope / Deferred

- **browser-harness integration**: Available as Hermes tool candidate. Attaches to existing Chrome via CDP. Low priority until browser automation is actually needed.
- **Dual mic**: ears.py single-mic works. Dual-mic would require sounddevice multistream — defer until range is actually a problem.
- **Creative companion**: Not applicable to JARVIS's butler persona.

## Verified Working

- Voice pipeline: mic → wake word → Whisper → router → Hermes → Kokoro → speaker
- Local routing: ~26ms for 18+ commands
- Hermes routing: ~500-1500ms for complex commands
- Web search: 6s end-to-end via SearxNG
- HUD download: 200 OK, 12KB
- Server audit: 140 OK, 8 warnings, 2 failures (infra, not JARVIS)
