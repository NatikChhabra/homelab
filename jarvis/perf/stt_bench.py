#!/usr/bin/env python3
"""
STT accuracy + latency benchmark against the live jarvis-whisper container.

Generates deterministic test audio via piper (16kHz, matches whisper input),
sends it through the Wyoming protocol, and records:
  - transcript text
  - latency (ms)
  - (if readable from logs) detected language + confidence

This is a SELF-CONTAINED measurement: it talks to jarvis-piper (10200) and
jarvis-whisper (10300) directly from the host. Nothing here actuates devices.

Usage:
  python stt_bench.py [--reps N] [--out FILE]

Run BEFORE and AFTER each whisper config change; compare transcripts + p50/p95.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path

import numpy as np

sys_path = str(Path(__file__).resolve().parent.parent)
if sys_path not in __import__('sys').path:
    __import__('sys').path.insert(0, sys_path)

import wyoming_client as wy  # noqa: E402  (after sys.path fix)

PIPER_HOST, PIPER_PORT = "localhost", 10200
WHISPER_HOST, WHISPER_PORT = "localhost", 10300

# (intent_text, expected_substring) -- the expected substring is what must
# appear in the transcript for a "hit". Kept loose because TTS audio is clean
# but whisper may insert punctuation.
UTTERANCES = [
    # English commands (Natik's real ones from tonight's log)
    ("turn on light one", "light"),
    ("turn off light one", "light"),
    ("turn off the lights", "lights"),
    ("set the fan to speed three", "fan"),
    ("turn the fan off", "fan"),
    ("boost the fan", "fan"),
    ("set the ac to twenty two", "ac"),
    ("toggle the tv", "tv"),
    ("good night", "night"),
    ("what time is it", "time"),
    ("is my pc on", "pc"),
    # Hinglish (the ones that misroute)
    ("lights chalu karo", "light"),
    ("lights band karo", "light"),
    ("fan chalu karo", "fan"),
    ("fan band karo", "fan"),
    ("ac band karo", "ac"),
]


def resample_to_16k(pcm: bytes, src_rate: int) -> bytes:
    """Linear-interp resample int16 PCM to 16kHz mono."""
    if src_rate == 16000:
        return pcm
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    n_out = int(len(x) * 16000 / src_rate)
    x_old = np.linspace(0, len(x) - 1, n_out)
    x_new = np.interp(x_old, np.arange(len(x)), x)
    return x_new.astype("<i2").tobytes()


async def synth_piper(text: str) -> bytes:
    """Synthesize text via piper, return 16k int16 PCM."""
    pcm = await wy.synthesize(PIPER_HOST, PIPER_PORT, text, voice="en_GB-alan-medium")
    # piper's en_GB-alan-medium is 22050 Hz
    return resample_to_16k(pcm, 22050)


async def transcribe(pcm: bytes, language: str = "") -> tuple[str, float]:
    """Send PCM to whisper. Returns (text, latency_ms)."""
    reader, writer = await asyncio.open_connection(WHISPER_HOST, WHISPER_PORT)
    try:
        await wy.write_event(writer, wy.Event("transcribe", {"language": language}))
        await wy._send_audio(writer, pcm)
        t0 = time.perf_counter()
        while True:
            ev = await wy.read_event(reader)
            if ev is None:
                return "", (time.perf_counter() - t0) * 1000
            if ev.type == "transcript":
                return (ev.data.get("text") or "").strip(), (time.perf_counter() - t0) * 1000
    finally:
        writer.close()
        await writer.wait_closed()


async def run(language: str, reps: int) -> dict:
    results = []
    for text, expect in UTTERANCES:
        pcm = await synth_piper(text)
        lats = []
        hits = 0
        transcripts = []
        for _ in range(reps):
            t, ms = await transcribe(pcm, language)
            lats.append(ms)
            transcripts.append(t)
            if expect.lower() in t.lower():
                hits += 1
        results.append({
            "intent": text,
            "expect": expect,
            "lang": language or "auto",
            "hits": hits,
            "reps": reps,
            "transcripts": transcripts,
            "lat_ms": lats,
        })
    return {"language": language, "results": results}


def summarize(language: str, data: dict) -> None:
    lats = [ms for r in data["results"] for ms in r["lat_ms"]]
    hits = sum(r["hits"] for r in data["results"])
    total = sum(r["reps"] for r in data["results"])
    print(f"\n===== language={language or 'auto'} =====")
    print(f"  accuracy: {hits}/{total} ({100*hits/total:.0f}%)")
    for r in data["results"]:
        mark = "OK " if r["hits"] == r["reps"] else "BAD"
        print(f"  {mark} {r['intent']!r:32} {r['hits']}/{r['reps']}  "
              f"transcripts: {[t[:50] for t in r['transcripts']]}")
    if lats:
        s = sorted(lats)
        p50 = s[len(s)//2]
        p95 = s[int(len(s)*0.95)]
        print(f"  latency: p50={p50:.0f}ms  p95={p95:.0f}ms  max={max(lats):.0f}ms  "
              f"(n={len(lats)})")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--lang", default="auto", help="language hint: auto, en, hi, or empty")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    lang = "" if a.lang == "auto" else a.lang
    data = await run(lang, a.reps)
    summarize(lang, data)
    if a.out:
        Path(a.out).write_text(json.dumps(data, indent=2))
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    asyncio.run(main())
