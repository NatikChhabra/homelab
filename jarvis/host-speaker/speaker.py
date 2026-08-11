"""
Speak from the SERVER's speakers -- specifically, the Echo Dot over Bluetooth.

Why this service has to exist at all
------------------------------------
Until now JARVIS only ever spoke in a BROWSER. `voice_server.py` streams PCM
over a websocket and the page plays it through Web Audio, which means JARVIS is
silent unless a tab is open and focused. Natik wants him audible in the room the
way the films have him: no tab, no laptop lid, just a voice.

Docker cannot do this. Docker Desktop on Windows has no audio passthrough --
there is no host sound device inside a container, and no flag that adds one. So
playback has to happen in a HOST process, and this is that process.

Why the Echo Dot works
----------------------
Measured on this machine:

    Get-PnpDevice -Class AudioEndpoint
    Speakers (Echo Dot-3HS)    OK

The Echo is paired over Bluetooth A2DP and Windows already presents it as an
ordinary render endpoint. Nothing Alexa-specific is involved -- no Amazon
account, no skill, no cloud. JARVIS is simply using the Echo as a speaker, which
also means this keeps working with any other output: the Realtek speakers, the
WH-1000XM4, the realme Buds.

Device is selected BY NAME SUBSTRING, not by index. PortAudio indices are not
stable -- they shift whenever a Bluetooth device connects or drops, so an index
pinned today points at the wrong speaker tomorrow. A substring survives that,
and it survives Windows appending "2 - " prefixes on reconnect.

Resampling
----------
Kokoro synthesises at 24 kHz. Bluetooth A2DP endpoints commonly refuse 24 kHz
and expect 44.1 or 48 kHz. Rather than let PortAudio fail or silently pick
something wrong, the stream is resampled to whatever the chosen device actually
reports as its default rate. Linear interpolation is enough here: this is
speech, and the alternative is a scipy dependency for no audible gain.

Security
--------
Loopback bind and a bearer token from Stacks/secrets.env, matching the existing
host-helper design. This process can make noise in Natik's bedroom at any hour,
so it should not be reachable from the LAN.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import sys
import threading
import time
import urllib.request
from pathlib import Path

import numpy as np
import sounddevice as sd
from aiohttp import web

log = logging.getLogger("jarvis.speaker")

# Substring match, in priority order. The Echo is the point of this service, but
# a sane fallback chain means JARVIS still speaks if it is off or out of range.
PREFERRED = [
    s.strip() for s in os.getenv(
        "JARVIS_SPEAKER_PREFER",
        "Echo Dot,WH-1000XM4,realme Buds,Speakers (Realtek",
    ).split(",") if s.strip()
]

KOKORO_URL = os.getenv("KOKORO_URL", "http://localhost:8880")
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "bm_george(3)+bm_lewis(3)+bm_fable(1)")
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "0.92"))
KOKORO_RATE = 24000

PORT = int(os.getenv("JARVIS_SPEAKER_PORT", "8100"))


def load_token() -> str:
    """Read the shared token from secrets.env. Never hardcode it."""
    env = os.getenv("JARVIS_HELPER_TOKEN")
    if env:
        return env
    secrets = Path(__file__).resolve().parent.parent / "secrets.env"
    if secrets.exists():
        for line in secrets.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("JARVIS_HELPER_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# --- device selection ------------------------------------------------------

def list_outputs() -> list[dict]:
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0:
            out.append({
                "index": i,
                "name": d["name"],
                "channels": d["max_output_channels"],
                "rate": int(d["default_samplerate"]),
            })
    return out


def pick_device(prefer: list[str] | None = None) -> tuple[int | None, str, int]:
    """
    Resolve a device by name substring. Returns (index, name, samplerate).

    Falls through the preference list in order, so an Echo that is powered off
    costs a fallback rather than silence.
    """
    devices = list_outputs()
    for want in (prefer or PREFERRED):
        for d in devices:
            if want.lower() in d["name"].lower():
                return d["index"], d["name"], d["rate"]
    if devices:
        try:
            default_idx = sd.default.device[1]
            for d in devices:
                if d["index"] == default_idx:
                    return d["index"], d["name"] + " (system default)", d["rate"]
        except Exception:  # noqa: BLE001
            pass
        d = devices[0]
        return d["index"], d["name"] + " (first available)", d["rate"]
    return None, "NO OUTPUT DEVICE", 48000


def resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear resample int16 mono. See the module docstring on why not scipy."""
    if src == dst or pcm.size == 0:
        return pcm
    n_out = int(round(pcm.size * dst / src))
    x_old = np.linspace(0.0, 1.0, pcm.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, pcm.astype(np.float32)).astype(np.int16)


# --- synthesis + playback --------------------------------------------------

def synth(text: str, voice: str | None = None) -> np.ndarray:
    """Fetch headerless PCM from Kokoro. `pcm` avoids the streaming-WAV header
    bug documented in fix_wav_header.py, where the data chunk size is the
    0xFFFFFFFF unknown-length sentinel and players read past the audio.

    Docker Desktop on Windows drops Kokoro TCP connections (WinError 10054).
    Retry once with a short pause before giving up.
    """
    body = json.dumps({
        "model": "kokoro",
        "voice": voice or KOKORO_VOICE,
        "input": text,
        "response_format": "pcm",
        "speed": KOKORO_SPEED,
    }).encode()
    req = urllib.request.Request(
        f"{KOKORO_URL}/v1/audio/speech", data=body,
        headers={"Content-Type": "application/json"})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
            return np.frombuffer(raw, dtype="<i2")
        except (OSError, ConnectionError, urllib.error.URLError):
            if attempt == 2:
                raise
            time.sleep(0.3)


class Speaker:
    """Serialises playback so two replies never overlap into noise."""

    def __init__(self) -> None:
        self.q: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.lock = threading.Lock()
        self.last_error = ""
        self.spoken = 0
        threading.Thread(target=self._worker, daemon=True).start()

    def say(self, text: str, voice: str | None = None) -> None:
        self.q.put((text, voice))

    def play_pcm(self, pcm: np.ndarray, src_rate: int = KOKORO_RATE) -> str:
        idx, name, rate = pick_device()
        if idx is None:
            raise RuntimeError("no output device")
        audio = resample(pcm, src_rate, rate)
        with self.lock:
            # blocking=True: sd.play() returns immediately otherwise and the
            # next queued line would cut this one off mid-sentence.
            sd.play(audio, samplerate=rate, device=idx, blocking=True)
        return name

    def _worker(self) -> None:
        while True:
            text, voice = self.q.get()
            try:
                where = self.play_pcm(synth(text, voice))
                self.spoken += 1
                log.info("spoke %r on %s", text[:60], where)
            except Exception as exc:  # noqa: BLE001 - never kill the worker
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.error("playback failed: %s", self.last_error)
            finally:
                self.q.task_done()


SPEAKER = Speaker()
TOKEN = load_token()


# --- HTTP ------------------------------------------------------------------

def authorised(request: web.Request) -> bool:
    if not TOKEN:
        return True  # no token configured; loopback bind is the only guard
    header = request.headers.get("Authorization", "")
    return header == f"Bearer {TOKEN}"


async def h_say(request: web.Request) -> web.Response:
    if not authorised(request):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "bad json"}, status=400)
    text = (body.get("text") or "").strip()
    if not text:
        return web.json_response({"error": "no text"}, status=400)
    if len(text) > 2000:
        return web.json_response({"error": "text too long"}, status=400)
    wait = bool(body.get("wait"))
    SPEAKER.say(text, body.get("voice"))
    if wait:
        SPEAKER.q.join()
    idx, name, rate = pick_device()
    return web.json_response({"ok": True, "device": name, "rate": rate,
                              "queued": SPEAKER.q.qsize()})


async def h_devices(request: web.Request) -> web.Response:
    idx, name, rate = pick_device()
    return web.json_response({
        "selected": {"index": idx, "name": name, "rate": rate},
        "preferred_order": PREFERRED,
        "outputs": list_outputs(),
    })


async def h_health(request: web.Request) -> web.Response:
    idx, name, rate = pick_device()
    return web.json_response({
        "status": "ok" if idx is not None else "no_device",
        "device": name, "rate": rate,
        "spoken": SPEAKER.spoken, "queued": SPEAKER.q.qsize(),
        "last_error": SPEAKER.last_error,
        "token_required": bool(TOKEN),
    })


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.post("/say", h_say),
        web.get("/devices", h_devices),
        web.get("/health", h_health),
    ])
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="JARVIS host speaker")
    ap.add_argument("--list", action="store_true", help="list output devices and exit")
    ap.add_argument("--test", metavar="TEXT", nargs="?", const=
                    "Good evening, sir. All systems are running normally.",
                    help="speak a line and exit")
    ap.add_argument("--device", help="override device name substring")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.list:
        idx, name, rate = pick_device([args.device] if args.device else None)
        print(f"{'idx':>4}  {'rate':>6}  {'ch':>2}  name")
        for d in list_outputs():
            mark = "->" if d["index"] == idx else "  "
            print(f"{mark}{d['index']:>2}  {d['rate']:>6}  {d['channels']:>2}  {d['name']}")
        print(f"\nselected: {name} @ {rate} Hz")
        return 0

    if args.test is not None:
        pcm = synth(args.test)
        idx, name, rate = pick_device([args.device] if args.device else None)
        print(f"synth: {pcm.size} samples ({pcm.size / KOKORO_RATE:.2f}s @ {KOKORO_RATE} Hz)")
        print(f"device: {name} @ {rate} Hz")
        audio = resample(pcm, KOKORO_RATE, rate)
        print(f"resampled to {audio.size} samples; playing...")
        sd.play(audio, samplerate=rate, device=idx, blocking=True)
        print("done")
        return 0

    log.info("JARVIS speaker on http://127.0.0.1:%d  (token %s)",
             args.port, "required" if TOKEN else "NOT SET - loopback only")
    idx, name, rate = pick_device()
    log.info("output device: %s @ %d Hz", name, rate)
    # Loopback only. This process can make noise in a bedroom at any hour.
    web.run_app(build_app(), host="127.0.0.1", port=args.port, print=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
