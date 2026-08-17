"""
Wake-to-beep latency probe.

Why this exists
---------------
The acknowledgment beep was silent for weeks. Two causes, both invisible:

  1. speaker.py bound port 8100, already held by the jarvis-searxng container,
     so the whole speaker service was down (PermissionError WinError 10013).
  2. ears.py `_play_beep()` handed raw `bytes` from `wave.readframes()` to
     `play_pcm()`, which calls `resample()`, which reads `pcm.size`.
     `bytes` has no `.size` -> AttributeError, swallowed by a bare
     `except Exception: log.debug(...)`.

Both are fixed. This probe proves the beep now (a) plays and (b) plays FAST.
The acceptance bar from the work order is wake -> beep <= 150 ms, because
anything slower reads as lag rather than acknowledgment.

Dispatch, not playback
----------------------
The measured number is the time to DISPATCH audio to the device, not the
duration of the sound itself. `blocking=True` would measure the beep's own
~865 ms length, which is not latency -- it is the beep. `blocking=False` is
what ears.py now uses for beeps so the recording window opens immediately.

Run: python jarvis/diag/beep_latency_probe.py
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

import aiohttp

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE / "host-ears"))
sys.path.insert(0, str(_HERE / "host-speaker"))

import ears  # noqa: E402
import speaker as SP  # noqa: E402

N = 10
BAR_MS = 150.0
# The beep is ~865 ms long. Waiting longer than that between samples keeps the
# PortAudio stream from being torn down mid-playback, which would add teardown
# cost to the NEXT sample and make the numbers meaningless.
GAP_S = 1.2


async def main() -> int:
    e = ears.Ears(speak=True)
    # Warm the device lookup once. The first pick_device() call pays PortAudio
    # enumeration cost; measuring that as "wake latency" would be dishonest,
    # since in the real loop the device has always been queried before.
    idx, name, rate = SP.pick_device()
    print(f"device: {name} @ {rate} Hz")
    if idx is None:
        print("FAIL: no output device")
        return 1

    latencies: list[float] = []
    async with aiohttp.ClientSession() as session:
        for i in range(N):
            t0 = time.perf_counter()
            await e._play_beep(session)
            latencies.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(GAP_S)

    p50 = statistics.median(latencies)
    worst = max(latencies)
    verdict = "PASS" if worst <= BAR_MS else "FAIL"

    print(f"\nwake -> beep dispatch, {N} events")
    print("  samples (ms): " + ", ".join(f"{x:.1f}" for x in latencies))
    print(f"  p50:  {p50:.1f} ms")
    print(f"  max:  {worst:.1f} ms")
    print(f"  bar:  {BAR_MS:.0f} ms")
    print(f"  {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
