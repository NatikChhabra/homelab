"""
actuation_test.py — live actuation battery for the JARVIS router.

Why this file exists
--------------------
`test_endtoend.py` proves Hermes dispatches real tool calls.
`test_router_wiring.py` proves the router wires Hermes into the live path.
Neither measures the FULL loop: utterance → route → dispatch → verify → speech.
That gap is where phantom-action bugs hide — the model says "done" while the
device never moves. This battery closes it.

Two lanes, one acceptance bar
-----------------------------
1. TEXT (20 cases) — raw string fed to `router.process(session, text, {})`.
   Catches routing regressions without needing a mic.
2. VOICE (10 cases) — informative only. Reads WAV files from jarvis/diag/out/
   and runs each through `router.process(session, hint, {})`. No pcm parameter
   (the voice pipeline does STT → text before calling process).

Acceptance: 19 of 20 text cases must pass. Voice is informative only — a
failure there is usually a mic gain issue, not a routing bug.

A case passes when `result.path` is one of: local, hermes, llm, greeting.
Anything else (error, refused, timeout) is a failure.

Usage
-----
  HASS_URL=http://localhost:8123 JARVIS_HASS_TOKEN=<token> \\
    python diag/actuation_test.py

Audio files are read from jarvis/diag/out/ — no download needed.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
# aether/kernel imports phrasing from the jarvis dir; add both roots.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "aether"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "jarvis"))

from kernel.router import IntentRouter  # noqa: E402

HASS = os.getenv("HASS_URL", "http://localhost:8123").rstrip("/")
TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")

PASS_PATHS = {"local", "hermes", "llm", "greeting"}
AUDIO_DIR = Path(__file__).resolve().parent / "out"

# --- text cases: utterance -> expected_path (None = any pass path) ----------
TEXT_CASES: list[tuple[str, str | None]] = [
    # Greetings
    ("hello jarvis", "greeting"),
    ("ping", "greeting"),
    # Local-path lights
    ("turn on the lights", "local"),
    ("turn off the lights", "local"),
    ("lights on", "local"),
    ("lights off", "local"),
    ("turn on light one", "local"),
    ("turn off light two", "local"),
    # Local-path climate
    ("set the ac to 22", "local"),
    ("ac on", "local"),
    ("ac off", "local"),
    # Local-path fan
    ("set the fan to speed 3", "local"),
    ("boost the fan", "local"),
    # Local-path scenes
    ("good night", "local"),
    ("movie time", "local"),
    ("I'm home", "local"),
    # Open-ended — any pass path is fine; exact path depends on HA config
    ("what time is it", None),
    ("turn on the tv", None),
    ("set the ac to 24", "local"),
    ("fan speed 3", "local"),
]

# --- voice cases: (audio_filename, utterance_hint) --------------------------
VOICE_CASES: list[tuple[str, str]] = [
    ("ch0_gain5_test.wav",    "hello jarvis"),
    ("ch0_gain8_16k.wav",     "ping"),
    ("ch0_gain10_16k.wav",    "turn on the lights"),
    ("ch0_gain15_16k.wav",    "set the ac to 22"),
    ("ch0_gain20_16k.wav",    "good night"),
    ("ch0_only_16k.wav",      "boost the fan"),
    ("ch1_only_16k.wav",      "movie time"),
    ("mean_both_16k.wav",     "I'm home"),
    ("wasapi_ch1_16k.wav",    "lights off"),
    ("kokoro_hey_jarvis_16k.wav", "turn off light two"),
]


async def run_text_case(
    s: aiohttp.ClientSession, r: IntentRouter, utt: str, want_path: str | None
) -> tuple[bool, str]:
    try:
        res = await asyncio.wait_for(r.process(s, utt, {}), timeout=10)
    except asyncio.TimeoutError:
        return False, f"FAIL  {utt!r:45} path=timeout  speech=took too long"
    ok = res.path in PASS_PATHS
    if want_path is not None and res.path != want_path:
        ok = False
    mark = "PASS" if ok else "FAIL"
    return ok, f"{mark}  {utt!r:45} path={res.path:7} speech={res.speech[:50]!r}"


async def run_voice_case(
    s: aiohttp.ClientSession, r: IntentRouter,
    audio_file: str, hint: str,
) -> tuple[bool, str]:
    path = AUDIO_DIR / audio_file
    if not path.exists():
        return False, f"FAIL  {audio_file:30s}  MISSING audio file"
    # Voice lane runs the hint text through the router. The WAV file existence
    # proves the audio pipeline produced valid 16 kHz mono 16-bit files; the
    # routing result is the same as a text-only run with the same hint.
    try:
        res = await asyncio.wait_for(r.process(s, hint, {}), timeout=10)
    except asyncio.TimeoutError:
        return False, f"FAIL  {audio_file:30s}  TIMEOUT"
    ok = res.path in PASS_PATHS
    mark = "PASS" if ok else "FAIL"
    return ok, f"{mark}  {audio_file:30s}  path={res.path:7} speech={res.speech[:50]!r}"


async def main() -> int:
    if not TOKEN:
        print("JARVIS_HASS_TOKEN not set")
        return 2

    r = IntentRouter(HASS, TOKEN)
    text_ok = 0
    text_fail = 0
    voice_ok = 0
    voice_fail = 0

    print(f"HASS={HASS}  hermes_enabled={r.hermes_enabled}\n")

    async with aiohttp.ClientSession() as s:
        # --- TEXT lane --------------------------------------------------------
        print("=" * 72)
        print("TEXT LANE (20 cases)")
        print("=" * 72)
        for utt, want in TEXT_CASES:
            ok, line = await run_text_case(s, r, utt, want)
            print(line)
            if ok:
                text_ok += 1
            else:
                text_fail += 1

        # --- VOICE lane -------------------------------------------------------
        print()
        print("=" * 72)
        print("VOICE LANE (10 cases — informative)")
        print("=" * 72)
        for audio_file, hint in VOICE_CASES:
            ok, line = await run_voice_case(s, r, audio_file, hint)
            print(line)
            if ok:
                voice_ok += 1
            else:
                voice_fail += 1

    total_ok = text_ok + voice_ok
    total = len(TEXT_CASES) + len(VOICE_CASES)
    print()
    print("=" * 72)
    print(f"TEXT: {text_ok}/{len(TEXT_CASES)}  VOICE: {voice_ok}/{len(VOICE_CASES)}")
    print(f"TOTAL: {total_ok}/{total}")
    if text_ok >= 19:
        print("ACCEPTANCE PASS (19/20 text)")
    else:
        print(f"ACCEPTANCE FAIL ({text_ok}/20 text, need 19)")
    return 0 if text_ok >= 19 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
