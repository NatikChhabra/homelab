#!/usr/bin/env python3
"""
Outbound speech. Announcements are PUSH, so they cost the model zero tools --
nothing is exposed to the LLM here, the service simply speaks.

TWO SINKS, ALWAYS BOTH, BECAUSE NEITHER IS RELIABLE ALONE
  1. WebSocket subscribers on /ws/announce get the PCM stream live. Same frame
     shape as voice_server.py's browser protocol, so wiring the existing UI to
     it later is a few lines rather than a redesign.
  2. Every announcement is also written as a finished .wav into
     data/announcements/ with a queue record. A container on Windows has no
     audio device, so this file is what the host-side player actually plays.
     If no browser is open, sink 2 is the only reason the house hears anything.

RESTRAINT IS THE FEATURE. An assistant that talks too much gets muted, and a
muted assistant protects nothing. Proactive speech (not user-requested) passes
three independent gates -- quiet hours, a severity floor, and de-duplication --
and a rate limit on top. User-requested speech (a timer the user personally set)
bypasses quiet hours, because suppressing a 7 a.m. alarm at 7 a.m. would be a
bug, not restraint.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime

import wyoming

log = logging.getLogger("capabilities.announce")

PIPER_HOST = os.getenv("PIPER_HOST", "jarvis-piper")
PIPER_PORT = int(os.getenv("PIPER_PORT", "10200"))
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_GB-alan-medium")

DATA_DIR = os.getenv("CAPABILITIES_DATA", "/data")
AUDIO_DIR = os.path.join(DATA_DIR, "announcements")

# Quiet hours in local time. Proactive speech is held, not dropped: a failure
# raised at 03:00 is spoken once at the start of the day instead of vanishing.
QUIET_START = os.getenv("QUIET_START", "22:30")
QUIET_END = os.getenv("QUIET_END", "07:30")

# Proactive rate limits. Deliberately mean.
MIN_GAP_S = int(os.getenv("ANNOUNCE_MIN_GAP_S", "1800"))   # 30 min between pushes
MAX_PER_DAY = int(os.getenv("ANNOUNCE_MAX_PER_DAY", "4"))

# Keep finished audio around long enough for a player to pick it up, then bin it
# so the volume does not grow without bound.
AUDIO_TTL_S = int(os.getenv("ANNOUNCE_AUDIO_TTL_S", "900"))


def _parse_hhmm(value: str, fallback: dtime) -> dtime:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value or "")
    if not m:
        return fallback
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return fallback
    return dtime(hour, minute)


def in_quiet_hours(now: datetime) -> bool:
    start = _parse_hhmm(QUIET_START, dtime(22, 30))
    end = _parse_hhmm(QUIET_END, dtime(7, 30))
    t = now.time()
    if start <= end:
        return start <= t < end
    return t >= start or t < end   # window crosses midnight


@dataclass
class Announcement:
    text: str
    kind: str = "proactive"        # proactive | timer | test
    urgent: bool = False           # bypasses quiet hours
    created: float = field(default_factory=time.time)


class Announcer:
    """Synthesises with Piper and fans the result out to every sink."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._last_proactive_at = 0.0
        self._day_key = ""
        self._day_count = 0
        self.history: list[dict] = []
        os.makedirs(AUDIO_DIR, exist_ok=True)

    # --- WebSocket sink ---------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _fanout(self, item) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                # A wedged client must not stall the announcement or hold audio
                # for everyone else. Drop it and let it reconnect.
                log.warning("announce subscriber too slow; dropping it")
                self._subscribers.discard(q)

    # --- Gates ------------------------------------------------------------
    def _rate_ok(self, now: datetime) -> tuple[bool, str]:
        day = now.strftime("%Y-%m-%d")
        if day != self._day_key:
            self._day_key, self._day_count = day, 0
        if self._day_count >= MAX_PER_DAY:
            return False, "daily-cap"
        if time.time() - self._last_proactive_at < MIN_GAP_S:
            return False, "min-gap"
        return True, ""

    # --- Main entry point -------------------------------------------------
    async def speak(self, ann: Announcement, now: datetime | None = None) -> dict:
        """
        Render and dispatch. Returns a result record describing what happened,
        including the reason if it was suppressed -- callers log the reason, so
        silence is always explainable after the fact.
        """
        now = now or datetime.now()

        if ann.kind == "proactive":
            if in_quiet_hours(now) and not ann.urgent:
                return self._record(ann, "suppressed", "quiet-hours")
            ok, why = self._rate_ok(now)
            if not ok:
                return self._record(ann, "suppressed", why)

        async with self._lock:
            try:
                pcm = await wyoming.synthesize(
                    PIPER_HOST, PIPER_PORT, ann.text, PIPER_VOICE
                )
            except (OSError, asyncio.TimeoutError, ValueError) as exc:
                log.error("piper synthesis failed: %s", exc)
                return self._record(ann, "failed", f"tts: {exc}")

            if not pcm:
                return self._record(ann, "failed", "tts-empty")

            self._fanout({
                "type": "announce-start",
                "text": ann.text,
                "kind": ann.kind,
                "rate": wyoming.RATE,
                "width": wyoming.WIDTH,
                "channels": wyoming.CHANNELS,
            })
            for offset in range(0, len(pcm), 8192):
                self._fanout(pcm[offset : offset + 8192])
            self._fanout({"type": "announce-end", "bytes": len(pcm)})

            wav_path = self._write_wav(ann, pcm)

        if ann.kind == "proactive":
            self._last_proactive_at = time.time()
            self._day_count += 1

        seconds = round(len(pcm) / (wyoming.RATE * wyoming.WIDTH * wyoming.CHANNELS), 2)
        log.info(
            "announced (%s): %r [%d bytes / %.2fs, %d ws sink(s), wav=%s]",
            ann.kind, ann.text, len(pcm), seconds, self.subscriber_count,
            os.path.basename(wav_path or "-"),
        )
        return self._record(
            ann, "spoken", "", pcm_bytes=len(pcm), seconds=seconds,
            wav=wav_path, ws_sinks=self.subscriber_count,
        )

    def _write_wav(self, ann: Announcement, pcm: bytes) -> str | None:
        """
        Write the .wav atomically, then append the queue record.

        Order matters: the host player watches the queue file, so the audio must
        already be complete and closed before it is advertised. Writing to .part
        first means a player can never grab a half-written file.
        """
        try:
            os.makedirs(AUDIO_DIR, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            slug = re.sub(r"[^a-z0-9]+", "-", ann.text.lower())[:40].strip("-") or "announce"
            name = f"{stamp}-{ann.kind}-{slug}.wav"
            final = os.path.join(AUDIO_DIR, name)
            part = final + ".part"
            with open(part, "wb") as fh:
                fh.write(wyoming.wav_bytes(pcm))
            os.replace(part, final)

            with open(os.path.join(AUDIO_DIR, "queue.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "wav": name, "text": ann.text, "kind": ann.kind,
                    "at": datetime.now().isoformat(timespec="seconds"),
                }) + "\n")
            self._prune()
            return final
        except OSError as exc:
            log.error("could not write announcement wav: %s", exc)
            return None

    def _prune(self) -> None:
        cutoff = time.time() - AUDIO_TTL_S
        with contextlib.suppress(OSError):
            for name in os.listdir(AUDIO_DIR):
                if not name.endswith((".wav", ".part")):
                    continue
                path = os.path.join(AUDIO_DIR, name)
                with contextlib.suppress(OSError):
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)

    def _record(self, ann: Announcement, outcome: str, reason: str, **extra) -> dict:
        rec = {
            "text": ann.text, "kind": ann.kind, "outcome": outcome,
            "reason": reason, "at": datetime.now().isoformat(timespec="seconds"),
            **extra,
        }
        if outcome == "suppressed":
            log.info("announcement suppressed (%s): %r", reason, ann.text)
        self.history.append(rec)
        del self.history[:-50]
        return rec
