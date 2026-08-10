"""
Prove the host-side voice legs without needing anyone to speak.

The physical test -- say "hey jarvis" and watch it work -- needs a human in the
room, and this build runs overnight. So this exercises everything that does NOT
require a live voice:

  1. openWakeWord  :10400  reachable, and speaks Wyoming (handshake + describe)
  2. Whisper       :10300  transcribes real audio captured from the host stack
  3. Kokoro        :8880   synthesises, and the PCM is sane (no clipping)
  4. The full text leg: transcript -> router -> spoken reply

Step 2 is the interesting one. Rather than shipping a fixture wav, it
synthesises a sentence with Kokoro, resamples 24 kHz -> 16 kHz, and asks Whisper
to read it back. That is a genuine round trip through both containers over the
host network, and it catches the failure this stack has actually had before:
`rhasspy/wyoming-whisper` on CUDA silently returning EMPTY transcripts in ~100 ms
while looking perfectly healthy.

What it deliberately does NOT prove: that the microphone hears the room, or that
openWakeWord fires on Natik's voice. Only a person saying "hey jarvis" proves
that, and it is the one-line morning test.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import wyoming_client as wy  # noqa: E402

WAKE = ("localhost", 10400)
WHISPER = ("localhost", 10300)
KOKORO = "http://localhost:8880"
VOICE = "bm_george(3)+bm_lewis(3)+bm_fable(1)"

SENTENCE = "Turn off light one and set the air conditioner to twenty two degrees."


def synth_16k(text: str) -> bytes:
    """Kokoro PCM at 24 kHz, linearly resampled to the 16 kHz Wyoming rate."""
    body = json.dumps({"model": "kokoro", "voice": VOICE, "input": text,
                       "response_format": "pcm", "speed": 0.92}).encode()
    req = urllib.request.Request(f"{KOKORO}/v1/audio/speech", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    pcm = np.frombuffer(raw, dtype="<i2")
    n_out = int(round(pcm.size * 16000 / 24000))
    x_old = np.linspace(0.0, 1.0, pcm.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, pcm.astype(np.float32)).astype(np.int16).tobytes()


async def main() -> int:
    fails = 0

    # 1. wake word service
    print("1. openWakeWord handshake")
    info = await wy.describe(*WAKE, timeout=5.0)
    if info:
        models = [m.get("name") for m in (info.get("wake") or [])][:6]
        print(f"   OK   reachable; models advertised: {models or 'none listed'}")
    else:
        print("   FAIL not reachable or not speaking Wyoming")
        fails += 1

    # a real streaming connection, which is what ears.py holds open
    try:
        reader, writer = await asyncio.open_connection(*WAKE)
        await wy.write_event(writer, wy.Event(
            "audio-start", {"rate": 16000, "width": 2, "channels": 1, "timestamp": 0}))
        silence = b"\x00\x00" * 1280
        for _ in range(5):
            await wy.write_event(writer, wy.Event(
                "audio-chunk", {"rate": 16000, "width": 2, "channels": 1}, silence))
        writer.close()
        await writer.wait_closed()
        print("   OK   streamed audio-start + chunks without error")
    except Exception as exc:  # noqa: BLE001
        print(f"   FAIL streaming failed: {type(exc).__name__}: {exc}")
        fails += 1

    # 2 + 3. kokoro -> whisper round trip
    print("\n2. Kokoro synthesis")
    t0 = time.perf_counter()
    try:
        pcm = synth_16k(SENTENCE)
        ms = (time.perf_counter() - t0) * 1000
        arr = np.frombuffer(pcm, dtype="<i2")
        peak = int(np.abs(arr).max())
        print(f"   OK   {arr.size / 16000:.2f}s at 16 kHz, {ms:.0f}ms, "
              f"peak {peak}/32767 ({100 * peak / 32767:.0f}%)")
        if peak >= 32760:
            print("   WARN clipping detected")
    except Exception as exc:  # noqa: BLE001
        print(f"   FAIL {type(exc).__name__}: {exc}")
        return 1

    print("\n3. Whisper transcription of that audio")
    t0 = time.perf_counter()
    try:
        text = await wy.transcribe(*WHISPER, pcm, language="")
        ms = (time.perf_counter() - t0) * 1000
    except Exception as exc:  # noqa: BLE001
        print(f"   FAIL {type(exc).__name__}: {exc}")
        return 1
    text = (text or "").strip()
    if not text:
        # The exact historical failure: CUDA whisper returning nothing, fast.
        print(f"   FAIL EMPTY transcript in {ms:.0f}ms "
              f"(this is the libcublas/CUDA silent-failure signature)")
        fails += 1
    else:
        print(f"   OK   {ms:.0f}ms -> {text!r}")
        want = {"light", "one", "air", "conditioner", "twenty", "two"}
        got = set(text.lower().replace(".", "").replace(",", "").split())
        hit = want & got
        print(f"        matched {len(hit)}/{len(want)} key words: {sorted(hit)}")
        if len(hit) < 3:
            print("   WARN transcript differs a lot from what was spoken")

    # 4. text leg
    print("\n4. Router (transcript -> reply)")
    try:
        import os

        import aiohttp

        from router import IntentRouter
        token = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
        if not token:
            secrets = Path(__file__).resolve().parent.parent.parent / "secrets.env"
            for line in secrets.read_text(errors="replace").splitlines():
                if line.startswith(("JARVIS_HASS_TOKEN=", "HA_API_TOKEN=")):
                    token = line.split("=", 1)[1].strip().strip('"')
                    break
        async with aiohttp.ClientSession() as s:
            r = IntentRouter("http://localhost:8123", token)
            # `process` takes a mutable per-session state dict holding the two
            # conversation ids; a fresh dict is a fresh conversation.
            res = await r.process(s, "what is the fan doing", {})
        print(f"   OK   path={res.path} local={res.local_ms:.0f}ms "
              f"llm={res.llm_ms:.0f}ms -> {res.speech!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"   FAIL {type(exc).__name__}: {exc}")
        fails += 1

    print(f"\n{'ALL LEGS OK' if not fails else str(fails) + ' LEG(S) FAILED'}")
    print("NOT covered here: the microphone hearing the room, and openWakeWord "
          "firing on a real voice.\nMorning test:  python host-ears/ears.py   then say "
          '"hey jarvis, turn off light one".')
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
