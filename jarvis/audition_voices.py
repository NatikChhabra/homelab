#!/usr/bin/env python3
"""
Audition Kokoro voices and blends on REAL JARVIS lines, and measure TTFA.

Run inside jarvis-voice so it reaches jarvis-kokoro by container name:
    docker exec jarvis-voice python //app/audition_voices.py

Two things are measured per candidate, and they are NOT the same number:
  * ttfa_ms  -- wall time until the first PCM byte arrives with stream=True.
                This is what the listener experiences as responsiveness and the
                only figure comparable to Piper's streaming first-chunk time.
  * total_ms -- wall time to the last byte.

Lines come from jarvis-prompt.txt and the local intents, deliberately: a voice
that sounds good on "testing 1 2 3" can still land wrong on the six phrases
Natik will actually hear a hundred times a day. Short clipped confirmations are
the hard case for TTS, because there is no room to recover from a bad prosody
guess.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
import wave
from pathlib import Path

KOKORO = os.getenv("KOKORO_URL", "http://jarvis-kokoro:8880").rstrip("/")
OUT = Path(os.getenv("VOICES_OUT", "/app/ui/static/voices"))
RATE = 24000

# The real thing. Kept short because the system prompt caps replies at 12 words.
LINES = [
    "Lights on.",
    "AC at 22, sir.",
    "Maximum cooling, sir.",
    "Good night, sir.",
    "Everything off.",
    "I've set the air conditioning to 22 and the fan to four.",
]

# Singles first, then blends. Blend weights use kokoro-fastapi's own syntax.
CANDIDATES = [
    ("bm_george", "bm_george"),
    ("bm_lewis", "bm_lewis"),
    ("bm_daniel", "bm_daniel"),
    ("bm_fable", "bm_fable"),
    ("blend_george_lewis", "bm_george+bm_lewis"),
    ("blend_george_fable", "bm_george+bm_fable"),
    ("blend_george_daniel", "bm_george+bm_daniel"),
    ("blend_fable_lewis", "bm_fable+bm_lewis"),
    ("blend_daniel_fable", "bm_daniel+bm_fable"),
    ("blend_george_fable_lewis", "bm_george+bm_fable+bm_lewis"),
    # Weighted: keep George's authority dominant, borrow Fable's warmth.
    ("blend_george2_fable1", "bm_george(2)+bm_fable(1)"),
    ("blend_george3_lewis1", "bm_george(3)+bm_lewis(1)"),
    # A touch of American gravitas under a British lead. Included because the
    # film voice is not purely RP -- it has a low, dry chest tone.
    ("blend_george_onyx", "bm_george(3)+am_onyx(1)"),
]

SPEED = 0.92


def synth(voice: str, text: str, stream: bool = True) -> tuple[bytes, int, int]:
    """POST to Kokoro, return (pcm, ttfa_ms, total_ms)."""
    body = json.dumps({
        "model": "kokoro",
        "voice": voice,
        "input": text,
        "response_format": "pcm",
        "speed": SPEED,
        "stream": stream,
    }).encode()
    req = urllib.request.Request(
        f"{KOKORO}/v1/audio/speech", data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    ttfa = None
    chunks = []
    with urllib.request.urlopen(req, timeout=120) as resp:
        while True:
            chunk = resp.read(4096)
            if not chunk:
                break
            if ttfa is None:
                ttfa = int((time.monotonic() - t0) * 1000)
            chunks.append(chunk)
    total = int((time.monotonic() - t0) * 1000)
    return b"".join(chunks), (ttfa if ttfa is not None else total), total


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # 300 ms of silence between lines so the sample is listenable as one file
    # rather than six phrases jammed together.
    gap = b"\x00" * (RATE * 2 * 3 // 10)

    print(f"{'candidate':28} {'ttfa_ms':>8} {'total_ms':>9} {'audio_s':>8} {'bytes':>9}")
    results = []
    for name, voice in CANDIDATES:
        pcms = []
        ttfas = []
        totals = []
        ok = True
        for line in LINES:
            try:
                pcm, ttfa, total = synth(voice, line)
            except Exception as exc:  # noqa: BLE001 - report and continue
                print(f"{name:28} FAILED: {type(exc).__name__}: {exc}")
                ok = False
                break
            if not pcm:
                print(f"{name:28} FAILED: empty audio")
                ok = False
                break
            pcms.append(pcm)
            ttfas.append(ttfa)
            totals.append(total)
        if not ok:
            continue

        joined = gap.join(pcms)
        write_wav(OUT / f"{name}.wav", joined)
        audio_s = len(joined) / (RATE * 2)
        # Median TTFA over the six lines, so one cold outlier does not define it.
        med = sorted(ttfas)[len(ttfas) // 2]
        print(f"{name:28} {med:8d} {sum(totals):9d} {audio_s:8.2f} {len(joined):9d}")
        results.append({
            "name": name, "voice": voice, "ttfa_ms_median": med,
            "ttfa_ms_all": ttfas, "total_ms_sum": sum(totals),
            "audio_s": round(audio_s, 2),
        })

    (OUT / "_measurements.json").write_text(json.dumps(results, indent=2))
    print(f"\nwrote {len(results)} samples to {OUT}")


if __name__ == "__main__":
    main()
