#!/usr/bin/env python3
"""
Speech synthesis, with a choice of engine and a fallback that always speaks.

WHY THIS MODULE EXISTS: the voice is the product. Piper's `en_GB-alan-medium`
was rejected as northern English rather than RP, and every other Piper voice
auditioned was worse -- `en_GB-vctk-medium` speakers are barely intelligible,
because VCTK is an unpolished research corpus, not a production voice. The
ceiling there is the ENGINE, not the voice file, so this adds Kokoro-82M
(kokoro-fastapi, OpenAI-compatible HTTP) alongside Piper.

Two hard rules encoded here:

1. JARVIS MUST ALWAYS SPEAK. Kokoro is an extra network dependency on a box
   whose GPU is already shared three ways. If it is down, slow, or returns
   nothing, this falls back to Piper rather than leaving the user in silence.
   The fallback is decided BEFORE the first chunk reaches the browser, because
   `speak-start` declares the sample rate and the rate cannot change mid-stream.

2. CHUNKS HANDED TO THE BROWSER MUST HAVE EVEN LENGTH. ui/app.js parses each
   websocket frame independently with `getInt16(i * 2)` starting at byte 0 of
   that frame. Wyoming payloads are always sample-aligned so Piper never hit
   this, but Kokoro streams raw PCM over HTTP chunked transfer, which may split
   anywhere -- including mid-sample. One odd-length chunk byte-swaps every
   sample in the NEXT chunk, which is not a subtle artefact: it is full-scale
   white noise. The carry buffer below is the whole defence.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator

import aiohttp

import wyoming_client as wy

log = logging.getLogger("jarvis.tts")

# --- Configuration ---------------------------------------------------------
# TTS_ENGINE=kokoro|piper. Defaults to kokoro; piper stays reachable both as an
# explicit setting and as the automatic fallback.
TTS_ENGINE = os.getenv("TTS_ENGINE", "kokoro").strip().lower()

KOKORO_URL = os.getenv("KOKORO_URL", "http://jarvis-kokoro:8880").rstrip("/")
# Blends are expressed with '+' and are passed through untouched, e.g.
# "bm_george+bm_fable". kokoro-fastapi parses the weighting itself.
KOKORO_VOICE = os.getenv("KOKORO_VOICE", "bm_george+bm_fable")
# Slightly under 1.0 reads as composed rather than hurried. Jarvis is never in
# a rush; this is a character choice, not a performance one.
KOKORO_SPEED = float(os.getenv("KOKORO_SPEED", "0.92"))
# Kokoro-82M synthesises at 24 kHz natively. NOT resampled to 16 kHz here: the
# browser is told the real rate in `speak-start` and plays it as-is, so
# downsampling would only throw away the treble that makes the voice sound
# clean in the first place.
KOKORO_RATE = int(os.getenv("KOKORO_RATE", "24000"))

# Time budget for the FIRST audio chunk. Past this, Kokoro is considered dead
# for this turn and Piper takes over. Generous, because Natik explicitly traded
# latency for voice quality -- but bounded, because dead air is still a failure.
KOKORO_FIRST_CHUNK_TIMEOUT = float(os.getenv("KOKORO_FIRST_CHUNK_TIMEOUT", "8.0"))
KOKORO_TOTAL_TIMEOUT = float(os.getenv("KOKORO_TOTAL_TIMEOUT", "60.0"))

PIPER = (os.getenv("PIPER_HOST", "jarvis-piper"), int(os.getenv("PIPER_PORT", "10200")))
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_GB-alan-medium")


def _even(chunk: bytes, carry: bytearray) -> bytes:
    """
    Return a sample-aligned slice, holding any odd trailing byte in `carry`.

    See the module docstring: an odd-length frame corrupts the FOLLOWING frame
    in the browser, so alignment is enforced here rather than hoped for.
    """
    if carry:
        chunk = bytes(carry) + chunk
        carry.clear()
    if len(chunk) % 2:
        carry.extend(chunk[-1:])
        chunk = chunk[:-1]
    return chunk


async def _piper_stream(text: str) -> AsyncIterator[bytes]:
    async for chunk in wy.synthesize_stream(*PIPER, text, PIPER_VOICE):
        yield chunk


async def _kokoro_stream(
    http: aiohttp.ClientSession, text: str, voice: str
) -> AsyncIterator[bytes]:
    """
    Stream raw PCM from kokoro-fastapi's OpenAI-compatible endpoint.

    `response_format: pcm` returns headerless little-endian int16 mono, which is
    exactly what the websocket contract already carries -- asking for wav here
    would put a 44-byte RIFF header in the first frame and the browser would
    render it as a click before every reply.
    """
    payload = {
        "model": "kokoro",
        "voice": voice,
        "input": text,
        "response_format": "pcm",
        "speed": KOKORO_SPEED,
        "stream": True,
    }
    timeout = aiohttp.ClientTimeout(
        total=KOKORO_TOTAL_TIMEOUT, sock_read=KOKORO_FIRST_CHUNK_TIMEOUT
    )
    async with http.post(
        f"{KOKORO_URL}/v1/audio/speech", json=payload, timeout=timeout
    ) as resp:
        if resp.status != 200:
            body = (await resp.text())[:200]
            raise aiohttp.ClientError(f"kokoro http {resp.status}: {body}")
        carry = bytearray()
        async for chunk in resp.content.iter_any():
            if not chunk:
                continue
            aligned = _even(chunk, carry)
            if aligned:
                yield aligned


async def stream_speech(
    http: aiohttp.ClientSession, text: str, voice: str | None = None
) -> AsyncIterator[tuple[int, bytes]]:
    """
    Yield (sample_rate, pcm_chunk) for `text`, choosing the engine and falling
    back to Piper if Kokoro cannot deliver a first chunk.

    The rate is yielded WITH each chunk rather than returned up front because
    the caller cannot know which engine won until audio actually arrives, and
    `speak-start` must carry the rate of the engine that did.
    """
    if TTS_ENGINE == "piper":
        async for chunk in _piper_stream(text):
            yield wy.RATE, chunk
        return

    chosen = voice or KOKORO_VOICE
    gen = _kokoro_stream(http, text, chosen)
    first: bytes | None = None
    try:
        # Only the FIRST chunk is raced against the timeout. Once Kokoro has
        # started producing audio it has proven itself, and cutting it off
        # mid-sentence to restart on Piper would make Jarvis stutter and repeat
        # himself -- worse than simply being slow.
        first = await asyncio.wait_for(
            gen.__anext__(), timeout=KOKORO_FIRST_CHUNK_TIMEOUT
        )
    except StopAsyncIteration:
        first = None
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        log.warning("kokoro unavailable (%s); falling back to piper", exc)
        first = None

    if first is None:
        await gen.aclose()
        log.warning("kokoro produced no audio; speaking with piper instead")
        async for chunk in _piper_stream(text):
            yield wy.RATE, chunk
        return

    yield KOKORO_RATE, first
    try:
        async for chunk in gen:
            yield KOKORO_RATE, chunk
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
        # Mid-stream failure: the listener has already heard the opening words,
        # so restarting on Piper would repeat them. Truncate instead and log it.
        log.warning("kokoro stream cut short: %s", exc)


async def probe(http: aiohttp.ClientSession) -> dict:
    """
    Liveness for /api/status. Reports the engine actually in force, so a silent
    fallback to Piper is visible from outside instead of only in the logs.
    """
    info: dict = {"engine": TTS_ENGINE, "voice": KOKORO_VOICE if TTS_ENGINE == "kokoro" else PIPER_VOICE}
    if TTS_ENGINE != "kokoro":
        return info
    try:
        async with http.get(
            f"{KOKORO_URL}/v1/audio/voices", timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                voices = data.get("voices", data) if isinstance(data, dict) else data
                info["kokoro"] = "ok"
                info["kokoro_voice_count"] = len(voices) if isinstance(voices, list) else None
            else:
                info["kokoro"] = f"http-{resp.status}"
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        info["kokoro"] = "unreachable"
        log.debug("kokoro probe failed: %s", exc)
    return info
