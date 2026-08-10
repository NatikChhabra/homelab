"""
Render JARVIS voice candidates from Kokoro.

The brief, in Natik's words on 2026-08-08 after hearing the first seven:
"blend_george_lewis ... i like this but the way of speaking of
blend_george_fable is great".

So the target is george+lewis TIMBRE with fable's DELIVERY. Those are two
different axes and kokoro-fastapi exposes both:

  - TIMBRE comes from the voice embedding, and embeddings can be blended, with
    optional weights: `bm_george(2)+bm_lewis(1)`. Weight controls how much of
    each speaker survives the mix.
  - DELIVERY -- pacing, how measured it sounds -- is mostly `speed`. The live
    setting is 0.92, already slowed from 1.0 because JARVIS should never sound
    hurried.

Adding fable at a LOW weight is the way to pull cadence toward fable without
losing the george+lewis voice Natik picked. A three-way equal blend was tried
first and washes out: at 1:1:1 no speaker dominates and it stops sounding like
the one he chose.

Every candidate speaks the same line, because comparing voices on different
sentences is not a comparison at all. The line is deliberately long enough to
expose cadence -- the seven earlier samples were too short to judge delivery,
which is exactly why Natik could praise one blend's voice and another's speech.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

KOKORO = os.getenv("KOKORO_URL", "http://localhost:8880")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "static", "voices")

LINE = (
    "Good evening, sir. The lights are on, the air conditioner is holding at "
    "twenty-two degrees, and everything is running normally. "
    "Shall I close things down for the night?"
)

# name -> (voice spec, speed)
CANDIDATES: dict[str, tuple[str, float]] = {
    # The two he named, re-rendered on the LONG line so delivery is audible.
    "A_george_lewis":            ("bm_george+bm_lewis", 0.92),
    "B_george_fable":            ("bm_george+bm_fable", 0.92),
    # The actual ask: george+lewis voice, fable pulled in for cadence only.
    "C_gl_fable_light":          ("bm_george(2)+bm_lewis(2)+bm_fable(1)", 0.92),
    "D_gl_fable_lighter":        ("bm_george(3)+bm_lewis(3)+bm_fable(1)", 0.92),
    "E_lewis_led_fable":         ("bm_lewis(3)+bm_george(2)+bm_fable(1)", 0.92),
    # Same blend as C, speed swept. If C is right but the pacing is off, this
    # isolates pacing without touching the voice.
    "F_gl_fable_slow":           ("bm_george(2)+bm_lewis(2)+bm_fable(1)", 0.86),
    "G_gl_fable_slower":         ("bm_george(2)+bm_lewis(2)+bm_fable(1)", 0.82),
    "H_gl_fable_brisk":          ("bm_george(2)+bm_lewis(2)+bm_fable(1)", 0.98),
}


def render(name: str, voice: str, speed: float) -> tuple[bool, str, float]:
    body = json.dumps({
        "model": "kokoro",
        "voice": voice,
        "input": LINE,
        "response_format": "wav",
        "speed": speed,
    }).encode()
    req = urllib.request.Request(
        f"{KOKORO}/v1/audio/speech", data=body,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            audio = r.read()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", 0.0
    ms = (time.perf_counter() - t0) * 1000
    if len(audio) < 5000:
        return False, f"suspiciously small: {len(audio)} bytes", ms
    path = os.path.join(OUT, f"cand_{name}.wav")
    with open(path, "wb") as fh:
        fh.write(audio)
    return True, f"{len(audio) // 1024} KB", ms


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    ok = 0
    print(f"{'candidate':<22} {'voice':<38} {'speed':>5}  {'render':>8}  result")
    for name, (voice, speed) in CANDIDATES.items():
        good, detail, ms = render(name, voice, speed)
        print(f"{name:<22} {voice:<38} {speed:>5}  {ms:>7.0f}ms  "
              f"{'OK  ' if good else 'FAIL'} {detail}")
        ok += good
    print(f"\n{ok}/{len(CANDIDATES)} rendered into {OUT}")
    # A weighted blend that silently falls back to a single voice would still
    # "succeed", so report sizes: identical byte counts across different weights
    # would mean the weight syntax was ignored.
    return 0 if ok == len(CANDIDATES) else 1


if __name__ == "__main__":
    sys.exit(main())
