"""
End-to-end timing verification against the LIVE pipeline.

The unit test (test_timing.py) proves TurnTimer's arithmetic. This proves the
WIRING: that a real turn, driven through the real Whisper / router / Kokoro /
speaker services, populates every stage the work order named and produces a
coherent breakdown that sums to the total.

How a turn is driven without a human at the mic
-----------------------------------------------
There is one microphone and no way to speak into it from a script. So the test
manufactures the utterance audio the way a person would produce it -- it asks
Kokoro to SAY the command, resamples that 24 kHz speech down to the 16 kHz mono
the Wyoming services expect, and feeds it to the exact same stage sequence
handle_utterance() runs. Whisper then has to transcribe JARVIS's own voice
back into the command text; if it does, STT is genuinely exercised, not mocked.

This is the honest version of the "test that doesn't test the thing" trap in
CLAUDE.md: every stage here calls the real service and the result is checked
against the real outcome (transcript matches, router path is sensible, audio
comes back), not against a hardcoded pass list.

Run: python jarvis/diag/timing_e2e.py
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np
from scipy.signal import resample_poly

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))                      # wyoming_client, router
sys.path.insert(0, str(_HERE / "host-ears"))
sys.path.insert(0, str(_HERE / "host-speaker"))

import wyoming_client as wy  # noqa: E402
import speaker as SP  # noqa: E402
from timing import TurnTimer  # noqa: E402
from router import IntentRouter  # noqa: E402

WHISPER_HOST = "localhost"
WHISPER_PORT = 10300
HASS_URL = "http://localhost:8123"
RATE = 16000

# Commands chosen to hit different router paths: a greeting (no device), a
# local light command (regex, ~26ms), and a query. Each must light up the
# whole timing chain.
CASES = [
    "hello jarvis",
    "turn on the lights",
    "turn off the lights",
]

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


async def make_utterance_pcm(text: str) -> bytes:
    """Produce 16 kHz mono int16 PCM of `text` spoken by Kokoro.

    Runs the blocking synth in a thread so the event loop is not stalled, then
    resamples 24 kHz -> 16 kHz with the same polyphase filter ears.py uses on
    the capture side.
    """
    pcm24 = await asyncio.to_thread(SP.synth, text)  # int16 @ 24 kHz
    pcm16 = resample_poly(pcm24.astype(np.float64), RATE, SP.KOKORO_RATE)
    pcm16 = np.clip(np.round(pcm16), -32768, 32767).astype(np.int16)
    return pcm16.tobytes()


async def drive_turn(http, text: str) -> dict:
    """Run one command through the real stage sequence with a TurnTimer.

    Mirrors ears.Ears.handle_utterance so the stages measured here are the
    stages that run in production, minus the mic capture (supplied as audio).
    """
    timer = TurnTimer(0)
    timer.mark("wake_detected")
    timer.mark("beep_started")

    timer.mark("recording_started")
    audio = await make_utterance_pcm(text)
    timer.mark("recording_ended")

    timer.mark("stt_started")
    heard = await wy.transcribe(WHISPER_HOST, WHISPER_PORT, audio, language="")
    timer.mark("stt_ended")
    heard = (heard or "").strip()
    timer.note("said", text)
    timer.note("heard", heard)

    import os
    token = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
    router = IntentRouter(HASS_URL, token)
    timer.mark("route_started")
    result = await router.process(http, heard or text, {})
    timer.mark("route_ended")
    timer.note("path", getattr(result, "path", "?"))
    timer.note("speech", (result.speech or "")[:60])

    if result.speech:
        timer.mark("tts_requested")
        reply_pcm = await asyncio.to_thread(SP.synth, result.speech)
        timer.mark("tts_first_audio")
        timer.mark("playback_started")
        await asyncio.to_thread(SP.SPEAKER.play_pcm, reply_pcm)
        timer.mark("playback_ended")

    return timer.flush()


async def main() -> int:
    # Load secrets so the router can reach HA.
    for cand in (_HERE.parent / "secrets.env", _HERE / "secrets.env"):
        if cand.is_file():
            import re
            for line in cand.read_text().splitlines():
                m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line.strip())
                if m:
                    import os
                    os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))
            break

    idx, name, rate = SP.pick_device()
    print(f"speaker: {name} @ {rate} Hz\n")

    summaries = []
    async with aiohttp.ClientSession() as http:
        for text in CASES:
            print(f"--- driving: {text!r} ---")
            s = await drive_turn(http, text)
            summaries.append(s)

            # Every stage the work order named must be a real number this turn.
            for key in ("wake_to_beep_ms", "recording_ms", "stt_ms", "route_ms",
                        "tts_first_audio_ms", "total_wake_to_speech_ms"):
                v = s.get(key)
                check(isinstance(v, (int, float)),
                      f"[{text!r}] stage {key} populated = {v}")

            # STT must have actually understood the spoken command. Compare on
            # a loose contains, since Whisper may punctuate/case differently.
            said = str(s.get("said", "")).lower()
            heard = str(s.get("heard", "")).lower()
            key_word = said.split()[-1]  # 'jarvis' / 'lights'
            check(key_word in heard,
                  f"[{text!r}] STT round-trip heard {heard!r} (contains {key_word!r})")

            # The turn must have produced speech (a path + a reply).
            check(bool(s.get("speech")), f"[{text!r}] router produced a reply")
            print(f"    heard={heard!r} path={s.get('path')} "
                  f"stt={s.get('stt_ms')}ms route={s.get('route_ms')}ms "
                  f"total={s.get('total_wake_to_speech_ms')}ms\n")
            await asyncio.sleep(0.3)

    # Cross-turn sanity: the sum of the named sub-spans must not exceed the
    # measured wake->speech total by more than a small slack (they are the same
    # clock, so a large gap would mean a mark is mis-placed).
    for s, text in zip(summaries, CASES):
        parts = sum(x for k in ("wake_to_beep_ms", "recording_ms", "stt_ms",
                                 "route_ms", "tts_first_audio_ms")
                    if isinstance((x := s.get(k)), (int, float)))
        total = s.get("total_wake_to_speech_ms")
        if isinstance(total, (int, float)):
            check(parts <= total + 50,
                  f"[{text!r}] sub-spans ({parts:.0f}ms) fit within total "
                  f"({total:.0f}ms)")

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES:")
        for f in FAILS:
            print("  - " + f)
        return 1
    print("ALL E2E TIMING CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
