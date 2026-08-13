"""
Always-on JARVIS. Host microphone in, Echo Dot out, no browser anywhere.

Why this exists
---------------
Every version of JARVIS before this one could only hear through a BROWSER TAB.
`voice_server.py` bridges a page's Web Audio microphone to the Wyoming services,
which works but means JARVIS is deaf unless a tab is open and focused. Natik
wants what the films have: a room that listens, permanently, with nothing on
screen.

Docker cannot supply this. Docker Desktop on Windows exposes no host microphone
and no host speaker, so both ends of the loop have to live in a host process.
This is that process. It reuses the containers for everything that is not
hardware:

    host mic (sounddevice)
      -> openWakeWord   :10400   "hey jarvis"
      -> Whisper        :10300   speech to text
      -> router.py              local intent first (~26 ms, no model)
           -> Hermes            specialist model leg, only when local fails
      -> Kokoro         :8880   text to speech
      -> Echo Dot               via host-speaker/speaker.py

Wake word runs CONTINUOUSLY over a rolling stream; speech is only recorded after
a detection. That ordering matters for privacy as much as for cost: audio
between wake words is fed to a local detector and discarded, never transcribed,
never stored, never leaves the machine.

Endpointing
-----------
After the wake word, recording stops on silence rather than after a fixed
window. A fixed window either truncates "set the air conditioner to twenty two
degrees" or makes "lights off" wait pointlessly. Silence is measured as RMS
below a threshold for a continuous run, with a hard ceiling so a noisy room
cannot record forever.

Barge-in
--------
The microphone is muted while JARVIS is speaking. Without this the Echo's own
output is picked up by the laptop microphone and re-triggers the wake word --
JARVIS talks to himself, which is both a loop and, on a 4 GB GPU, a real way to
wedge the machine.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host-speaker"))

import wyoming_client as wy  # noqa: E402

log = logging.getLogger("jarvis.ears")


def _load_secrets() -> None:
    """Load JARVIS secrets from the central secrets.env into the process env.

    The scheduled-task launcher (start-ears.ps1) does this too, but ears.py
    may also be started directly or restarted by the task manager after the
    ps1 script has already exited. Reading the file ourselves guarantees the
    token is present regardless of how the process was launched.
    """
    for candidate in (
        Path(__file__).resolve().parent.parent.parent / "secrets.env",  # C:/ServerData/Stacks/secrets.env
        Path(__file__).resolve().parent.parent / "secrets.env",          # C:/ServerData/Stacks/jarvis/secrets.env
    ):
        if candidate.is_file():
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"^([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
                if m:
                    val = m.group(2).strip().strip('"').strip("'")
                    os.environ.setdefault(m.group(1), val)
            log.debug("secrets loaded from %s", candidate)
            return
    log.warning("secrets.env not found; HA calls may fail")


# Load secrets before any env-dependent config is read below.
_load_secrets()

RATE = 16000          # Wyoming services in this stack are all 16 kHz mono
BLOCK = 1280          # 80 ms at 16 kHz; openWakeWord's natural chunk
WIDTH = 2

WAKE_HOST = os.getenv("WAKE_HOST", "localhost")
WAKE_PORT = int(os.getenv("WAKE_PORT", "10400"))
WHISPER_HOST = os.getenv("WHISPER_HOST", "localhost")
WHISPER_PORT = int(os.getenv("WHISPER_PORT", "10300"))
WAKE_WORD = os.getenv("WAKE_WORD", "hey_jarvis")
# HA URL for the router fallback path. Docker Desktop on Windows uses
# host.docker.internal to reach the host from inside a container, but ears.py
# runs on the HOST, so localhost is correct here.
HASS_URL = os.getenv("JARVIS_HASS_URL", "http://localhost:8123")

# Acknowledgment sound played when wake word is detected.
# Options: jarvis_online (rising sweep), ready (double beep),
#          listening (soft chime), activated (three-tone).
JARVIS_BEEP = os.getenv("JARVIS_BEEP", "jarvis_online")

# Endpointing. RMS is on int16 scale, so 500 is quiet-room noise and normal
# speech sits in the thousands.
SILENCE_RMS = float(os.getenv("JARVIS_SILENCE_RMS", "500"))
SILENCE_HOLD_S = float(os.getenv("JARVIS_SILENCE_HOLD", "0.8"))
MAX_UTTERANCE_S = float(os.getenv("JARVIS_MAX_UTTERANCE", "20.0"))
# Heartbeat interval. A missing heartbeat in the log for >2x this interval
# means ears.py is stuck or the log sink is broken -- both are silent failures
# that make the whole assistant deaf while Docker reports all containers healthy.
HEARTBEAT_S = 30
MIN_UTTERANCE_S = 1.5     # below this it is noise, not a command

# Audio captured BEFORE the wake word fires is kept in a short ring buffer and
# prepended to the recording. openWakeWord reports a detection slightly after
# the phrase completes, and people run straight on -- "hey jarvis turn the
# lights off" with no pause. Without this the first word is clipped.
PREROLL_S = 0.5

# --- Double-clap activation ---------------------------------------------------
# Detect a double hand clap as an alternative to "hey jarvis". A clap is a short
# high-energy transient with a fast rise from silence.  Two claps within a short
# window fire the same utterance handler the wake word would, with the remainder
# of the sentence still captured by silence endpointing.
CLAP_MIN_RMS = float(os.getenv("JARVIS_CLAP_MIN_RMS", "4000"))
CLAP_MIN_SILENCE_RMS = float(os.getenv("JARVIS_CLAP_MIN_SILENCE_RMS", "800"))
CLAP_MIN_SILENCE_S = float(os.getenv("JARVIS_CLAP_MIN_SILENCE_S", "0.08"))
CLAP_MAX_GAP_S = float(os.getenv("JARVIS_CLAP_MAX_GAP", "0.8"))
CLAP_REFRACT_S = float(os.getenv("JARVIS_CLAP_REFRACT", "1.5"))


def rms(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))


# Virtual device name fragments that indicate audio routing hijacks.
# If ears.py picks one of these, a voice changer or loopback driver is
# silently intercepting the mic. Log a warning so the user notices.
_VIRTUAL_FRAGMENTS = frozenset([
    "clownfish", "cable", "vb-audio", "loopback", "hub",
    "virtual", "wave", "stub", "fake",
])

def pick_input(prefer: str | None = None) -> tuple[int | None, str]:
    """Choose a capture device by name substring, falling back to the default."""
    candidates = [(i, d) for i, d in enumerate(sd.query_devices())
                  if d["max_input_channels"] > 0]
    wanted = prefer or os.getenv("JARVIS_MIC_PREFER", "Microphone Array,Microphone")
    for want in [w.strip() for w in wanted.split(",") if w.strip()]:
        for i, d in candidates:
            # Bluetooth hands-free mics are 8 kHz and sound terrible; skip them
            # unless explicitly asked for. The laptop array is better.
            if "hands-free" in d["name"].lower() and "hands-free" not in want.lower():
                continue
            if want.lower() in d["name"].lower():
                name = d["name"]
                # Warn if this looks like a virtual/routed device
                if any(v in name.lower() for v in _VIRTUAL_FRAGMENTS):
                    log.warning("SELECTED DEVICE APPEARS VIRTUAL: %s -- check for voice changers, loopback drivers, or audio hubs", name)
                return i, name
    if candidates:
        i, d = candidates[0]
        name = d["name"]
        if any(v in name.lower() for v in _VIRTUAL_FRAGMENTS):
            log.warning("SELECTED DEVICE APPEARS VIRTUAL: %s -- check for voice changers, loopback drivers, or audio hubs", name)
        return i, name + " (first available)"
    return None, "NO INPUT DEVICE"


class WakeDetector:
    """
    Persistent connection to the Wyoming openWakeWord service.

    Kept open across the whole session rather than reconnected per chunk: the
    detector holds streaming state, and reconnecting would reset its context
    every 80 ms and detect nothing at all.
    """

    def __init__(self, host: str, port: int, name: str) -> None:
        self.host, self.port, self.name = host, port, name
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        # Detection events arrive asynchronously between audio-chunk writes.
        # A separate task keeps draining the socket so a detection can never be
        # lost to an untimely buffer flush.
        self._detections: asyncio.Queue[str] = asyncio.Queue()
        self._read_task: asyncio.Task | None = None

    async def close(self) -> None:
        if self._read_task:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
            self._read_task = None
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
        self.reader = self.writer = None

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        await wy.write_event(self.writer, wy.Event("detect", {"names": [self.name]}))
        await wy.write_event(self.writer, wy.Event(
            "audio-start", {"rate": RATE, "width": WIDTH, "channels": 1, "timestamp": 0}))
        self._read_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """Continuously read detection events so the socket never fills up."""
        while self.reader:
            try:
                ev = await wy.read_event(self.reader)
            except (asyncio.CancelledError, asyncio.IncompleteReadError, ConnectionError, OSError):
                return
            except Exception:  # noqa: BLE001 - a corrupt frame must not kill the loop
                continue
            if ev is None:
                return
            if ev.type == "detection":
                self._detections.put_nowait(ev.data.get("name") or self.name)

    async def feed(self, pcm: bytes) -> None:
        """Push one audio chunk. Non-blocking; detections land in the queue."""
        if not self.writer or not self.reader:
            await self.connect()
        assert self.writer and self.reader
        await wy.write_event(self.writer, wy.Event(
            "audio-chunk", {"rate": RATE, "width": WIDTH, "channels": 1}, pcm))

    def poll_detection(self) -> bool:
        """True if a wake-word detection is waiting. Oldest-first, drained."""
        try:
            while True:
                self._detections.get_nowait()
                return True
        except asyncio.QueueEmpty:
            return False


class ClapDetector:
    """
    Double-clap detection on the pre-wake-word audio stream.

    A clap is a short high-energy transient: a fast rise from a quiet background
    to a loud peak and back. It is recognised on a 80 ms block's RMS alone --
    claps are tens of dB above any room noise -- plus the requirement that the
    level actually came *from* silence, so a door slam or the fan ramping up is
    not a clap.

    Two claps with a short quiet gap between them fire a double-clap hit. A
    refractory window after the hit stops JARVIS's own speech or the tail of a
    clap re-triggering it.
    """

    def __init__(self) -> None:
        self.clapped = 0.0          # wall clock of the first clap of a pair
        self.last_fired = -1e9      # wall clock of the last double-clap hit
        self.prev_level = 0.0       # RMS of the block just before the current one

    def feed(self, pcm: bytes) -> bool:
        """Feed one audio block. True when a double clap completes."""
        now = time.monotonic()
        level = rms(np.frombuffer(pcm, dtype="<i2"))
        prev = self.prev_level
        self.prev_level = level

        # Quiet: it is the gap between two claps, or the background between
        # pairs. A quiet run longer than the pair window clears a pending
        # first clap (it was a single clap).
        if level < CLAP_MIN_RMS:
            if self.clapped and now - self.clapped > CLAP_MAX_GAP_S:
                self.clapped = 0.0
            return False
        if now - self.last_fired < CLAP_REFRACT_S:
            # The first clap of a new pair during the refractory window is
            # ignored; the pending state is cleared so the window can start
            # clean once it expires.
            self.clapped = 0.0
            return False
        if self.clapped == 0.0:
            # First clap of a pair: it must rise from a quiet background, so a
            # door slam or music is not a clap.
            if prev > CLAP_MIN_SILENCE_RMS:
                return False
            self.clapped = now
            return False

        gap = now - self.clapped
        self.clapped = now          # allow a triple clap to match the last two
        if gap < CLAP_MIN_SILENCE_S or gap > CLAP_MAX_GAP_S:
            return False
        self.last_fired = now
        return True

    def reset(self) -> None:
        self.clapped = 0.0


class Ears:
    def __init__(self, mic: str | None = None, speak: bool = True) -> None:
        self.mic_index, self.mic_name = pick_input(mic)
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.muted = False          # True while JARVIS speaks (barge-in guard)
        self.speak_enabled = speak
        self.heard = 0
        self.claps = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        # Router and conversation state are built once and reused. Rebuilding
        # the router per utterance would throw away the conversation ids, so
        # every follow-up would start a new conversation and lose its referent.
        self._router = None
        self._conv: dict[str, str | None] = {}
        # Acknowledgment beep: plays when wake word fires so the user knows
        # JARVIS heard them. Set JARVIS_BEEP=ready/listening/activated to pick.
        self._beep_path = os.path.join(
            str(Path(__file__).resolve().parent / "beeps" / f"{JARVIS_BEEP}.wav")
        )

    # --- capture -----------------------------------------------------------

    def _callback(self, indata, frames, time_info, status) -> None:  # noqa: ANN001
        if status:
            log.debug("input status: %s", status)
        if self.muted or self.loop is None:
            return
        # Called on PortAudio's thread; hand off to the event loop thread-safely.
        self.loop.call_soon_threadsafe(self.q.put_nowait, bytes(indata))

    # --- the loop ----------------------------------------------------------

    async def run(self) -> None:
        import aiohttp

        self.loop = asyncio.get_running_loop()
        if self.mic_index is None:
            log.error("no input device found")
            return

        self._reconnect_attempt = 0
        detector = WakeDetector(WAKE_HOST, WAKE_PORT, WAKE_WORD)
        await detector.connect()
        clap = ClapDetector()
        log.info("listening on %s (wake word %r, double-clap)", self.mic_name, WAKE_WORD)

        # Heartbeat: a periodic log line so the watchdog can detect staleness.
        async def _heartbeat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_S)
                log.debug("heartbeat ok")
        hb_task = asyncio.create_task(_heartbeat())

        preroll: deque[bytes] = deque(maxlen=int(PREROLL_S * RATE / BLOCK) + 1)

        with sd.RawInputStream(samplerate=RATE, blocksize=BLOCK, device=self.mic_index,
                               dtype="int16", channels=1, callback=self._callback):
            async with aiohttp.ClientSession() as http:
                while True:
                    chunk = await self.q.get()
                    preroll.append(chunk)
                    try:
                        await detector.feed(chunk)
                    except Exception as exc:  # noqa: BLE001
                        self._reconnect_attempt += 1
                        log.warning("wake detector dropped (%s); reconnecting (attempt %d)", exc, self._reconnect_attempt)
                        await detector.close()
                        await asyncio.sleep(min(2 ** self._reconnect_attempt, 8))
                        try:
                            await detector.connect()
                        except Exception as exc2:  # noqa: BLE001
                            log.error("wake reconnect failed: %s", exc2)
                            await asyncio.sleep(min(2 ** (self._reconnect_attempt + 1), 16))
                        self._reconnect_attempt = 0
                        continue

                    fired = detector.poll_detection()
                    if not fired and clap.feed(chunk):
                        fired = True
                        self.claps += 1
                        log.info("double clap detected (#%d)", self.claps)
                    if not fired:
                        continue

                    self.heard += 1
                    log.info("activation (#%d)", self.heard)
                    clap.reset()
                    # Play acknowledgment sound so the user knows JARVIS heard them
                    try:
                        await self._play_beep(http)
                    except Exception as exc:  # noqa: BLE001
                        log.debug("beep failed: %s", exc)
                    await self.handle_utterance(http, b"".join(preroll))
                    preroll.clear()
                    # Drain anything captured while we were busy, so a stale
                    # backlog cannot immediately re-trigger.
                    while not self.q.empty():
                        self.q.get_nowait()

    async def record_until_silence(self) -> bytes:
        """Record after the wake word, stopping on a sustained quiet run."""
        parts: list[bytes] = []
        quiet_for = 0.0
        started = time.perf_counter()
        block_s = BLOCK / RATE
        while True:
            try:
                chunk = await asyncio.wait_for(self.q.get(), timeout=2.0)
            except asyncio.TimeoutError:
                break
            parts.append(chunk)
            level = rms(np.frombuffer(chunk, dtype="<i2"))
            quiet_for = quiet_for + block_s if level < SILENCE_RMS else 0.0
            elapsed = time.perf_counter() - started
            if quiet_for >= SILENCE_HOLD_S and elapsed > MIN_UTTERANCE_S:
                break
            if elapsed > MAX_UTTERANCE_S:
                log.info("hit max utterance length")
                break
        return b"".join(parts)

    async def handle_utterance(self, http, preroll: bytes) -> None:  # noqa: ANN001
        t0 = time.perf_counter()
        audio = preroll + await self.record_until_silence()
        dur = len(audio) / 2 / RATE
        if dur < MIN_UTTERANCE_S:
            log.info("too short (%.2fs); ignoring", dur)
            return
        # Reject audio that is too quiet even if long enough -- a silent room
        # segment that survived the duration gate (e.g. a long exhalation after
        # a clap) would otherwise spend STT cycles and return an empty transcript.
        audio_rms = rms(np.frombuffer(audio, dtype="<i2"))
        if audio_rms < SILENCE_RMS * 0.5:
            log.info("too quiet (rms %.0f); ignoring", audio_rms)
            return

        t_rec = time.perf_counter()

        try:
            text = await wy.transcribe(WHISPER_HOST, WHISPER_PORT, audio, language="")
        except Exception as exc:  # noqa: BLE001
            log.error("transcribe failed: %s", exc)
            return
        t_stt = time.perf_counter()
        text = (text or "").strip()
        if not text:
            log.info("empty transcript; ignoring")
            return
        log.info("heard: %r  (%.2fs audio, stt %.0fms)",
                 text, dur, (t_stt - t_rec) * 1000)

        reply = await self.think(http, text)
        t_think = time.perf_counter()
        log.info("reply: %r  (think %.0fms)", reply, (t_think - t_stt) * 1000)

        if reply and self.speak_enabled:
            await self.say(reply)
        log.info("total wake-to-spoken: %.0f ms", (time.perf_counter() - t0) * 1000)

    async def think(self, http, text: str) -> str:  # noqa: ANN001
        """Local intent first, model second. Mirrors router.py's contract."""
        token = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
        try:
            from router import IntentRouter
            if self._router is None:
                self._router = IntentRouter(HASS_URL, token)
            # `_conv` persists the conversation ids across turns, so follow-ups
            # like "make it warmer" keep their context instead of starting over.
            result = await self._router.process(http, text, self._conv)
            return result.speech
        except Exception as exc:  # noqa: BLE001
            log.warning("router unavailable (%s); asking HA directly", exc)
            try:
                async with http.post(
                    f"{HASS_URL}/api/conversation/process",
                    json={"text": text, "language": "en"},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=__import__("aiohttp").ClientTimeout(total=60),
                ) as resp:
                    data = await resp.json()
                return (data.get("response", {}).get("speech", {})
                        .get("plain", {}).get("speech", ""))
            except Exception as exc2:  # noqa: BLE001
                log.error("conversation failed: %s", exc2)
                return ""

    async def _play_beep(self, http) -> None:  # noqa: ANN001
        """Play an acknowledgment beep through the Echo Dot speaker.

        Mutes the mic during playback to avoid the beep being picked up and
        re-triggering the wake word. Uses the same speaker pipeline as say().
        """
        import speaker as SP
        if not SP.SPEAKER:
            return
        try:
            import wave
            with wave.open(self._beep_path, "rb") as wf:
                rate = wf.getframerate()
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                frames = wf.readframes(wf.getnframes())
            if channels != 1 or sample_width != 2:
                log.debug("beep format not mono-16bit, skipping")
                return
            SP.SPEAKER.play_pcm(frames, src_rate=rate)
            log.debug("played beep: %s", self._beep_path)
        except FileNotFoundError:
            log.debug("beep file not found: %s", self._beep_path)
        except Exception as exc:  # noqa: BLE001
            log.debug("beep failed: %s", exc)

    async def say(self, text: str) -> None:
        """Speak through the Echo, with the microphone muted for the duration."""
        import speaker as SP
        self.muted = True
        try:
            pcm = await asyncio.to_thread(SP.synth, text)
            await asyncio.to_thread(SP.SPEAKER.play_pcm, pcm)
        except Exception as exc:  # noqa: BLE001
            log.error("speech failed: %s", exc)
        finally:
            # Drain whatever the mic buffered during playback before unmuting,
            # so the tail of JARVIS's own voice cannot re-trigger the wake word.
            await asyncio.sleep(0.25)
            while not self.q.empty():
                self.q.get_nowait()
            self.muted = False


def main() -> int:
    ap = argparse.ArgumentParser(description="Always-on JARVIS listener")
    ap.add_argument("--mic", help="input device name substring")
    ap.add_argument("--list", action="store_true", help="list input devices and exit")
    ap.add_argument("--no-speak", action="store_true", help="transcribe only")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    if args.list:
        idx, name = pick_input(args.mic)
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                mark = "->" if i == idx else "  "
                print(f"{mark}{i:>3}  {int(d['default_samplerate']):>6} Hz  {d['name']}")
        print(f"\nselected: {name}")
        return 0

    ears = Ears(args.mic, speak=not args.no_speak)
    try:
        asyncio.run(ears.run())
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
