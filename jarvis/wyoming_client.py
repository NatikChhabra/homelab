#!/usr/bin/env python3
"""
Minimal async Wyoming protocol client.

Wyoming is a raw-TCP JSONL protocol, not HTTP. A browser cannot speak it, and
`curl http://host:10300/` will never succeed against it -- the old api.py
health check did exactly that and so reported every voice service as down.

Wire format (per rhasspy/wyoming README):

    {"type": ..., "data": {...}, "data_length": N, "payload_length": M}\n
    <N bytes of UTF-8 JSON, merged on top of header "data">
    <M bytes of binary payload, typically raw PCM>

Both length-prefixed sections are optional. The data_length section is easy to
miss and its absence silently corrupts the stream, so it is handled explicitly.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

# Wyoming services in this stack all speak 16 kHz / 16-bit / mono.
RATE = 16000
WIDTH = 2
CHANNELS = 1


@dataclass
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    payload: bytes | None = None


async def read_event(reader: asyncio.StreamReader) -> Event | None:
    """Read one event. Returns None at clean end of stream."""
    line = await reader.readline()
    if not line:
        return None

    header = json.loads(line.decode("utf-8"))
    data = header.get("data") or {}

    # Section 2: extra JSON that overrides/extends the header's own data.
    data_length = header.get("data_length")
    if data_length:
        extra = await reader.readexactly(data_length)
        data = {**data, **json.loads(extra.decode("utf-8"))}

    # Section 3: binary payload.
    payload = None
    payload_length = header.get("payload_length")
    if payload_length:
        payload = await reader.readexactly(payload_length)

    return Event(type=header["type"], data=data, payload=payload)


async def write_event(writer: asyncio.StreamWriter, event: Event) -> None:
    header: dict[str, Any] = {"type": event.type, "data": event.data}
    if event.payload:
        header["payload_length"] = len(event.payload)

    writer.write(json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n")
    if event.payload:
        writer.write(event.payload)
    await writer.drain()


def _audio_meta(timestamp: int | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"rate": RATE, "width": WIDTH, "channels": CHANNELS}
    if timestamp is not None:
        meta["timestamp"] = timestamp
    return meta


async def _send_audio(writer: asyncio.StreamWriter, pcm: bytes, chunk: int = 2048) -> None:
    """Stream PCM as audio-start / audio-chunk* / audio-stop."""
    await write_event(writer, Event("audio-start", _audio_meta(0)))
    for offset in range(0, len(pcm), chunk):
        await write_event(
            writer,
            Event("audio-chunk", _audio_meta(), pcm[offset : offset + chunk]),
        )
    await write_event(writer, Event("audio-stop", {"timestamp": 0}))


async def transcribe(host: str, port: int, pcm: bytes, language: str = "en") -> str:
    """Send PCM to a Wyoming ASR service and return the transcript text.

    Docker Desktop on Windows drops TCP connections between host and containers
    periodically (WinError 64). On a connection failure we close the dead socket,
    wait a beat, and retry once before giving up.
    """
    for attempt in (1, 2):
        try:
            reader, writer = await asyncio.open_connection(host, port)
            try:
                await write_event(writer, Event("transcribe", {"language": language}))
                await _send_audio(writer, pcm)

                while True:
                    event = await read_event(reader)
                    if event is None:
                        return ""
                    if event.type == "transcript":
                        return (event.data.get("text") or "").strip()
            finally:
                writer.close()
                await writer.wait_closed()
        except (OSError, ConnectionError, asyncio.CancelledError):
            if attempt == 2:
                raise
            await asyncio.sleep(0.4)
    # Both attempts failed and were re-raised; this line is unreachable
    # in normal operation but satisfies the type checker.
    return ""


async def synthesize(host: str, port: int, text: str, voice: str | None = None) -> bytes:
    """Send text to a Wyoming TTS service and return raw PCM (concatenated)."""
    reader, writer = await asyncio.open_connection(host, port)
    try:
        data: dict[str, Any] = {"text": text}
        if voice:
            data["voice"] = {"name": voice}
        await write_event(writer, Event("synthesize", data))

        chunks: list[bytes] = []
        while True:
            event = await read_event(reader)
            if event is None:
                break
            if event.type == "audio-chunk" and event.payload:
                chunks.append(event.payload)
            elif event.type in ("audio-stop", "synthesize-stopped"):
                break
        return b"".join(chunks)
    finally:
        writer.close()
        await writer.wait_closed()


async def synthesize_stream(
    host: str, port: int, text: str, voice: str | None = None
) -> AsyncIterator[bytes]:
    """
    Same as synthesize(), but yields each PCM chunk as the TTS produces it.

    WHY THIS MATTERS MORE THAN IT LOOKS: measured on this host, Piper takes
    ~2150 ms to render ~3.3 s of speech. Waiting for the whole buffer adds that
    full 2150 ms to every reply, and it is the single largest slice of the
    response time. Streaming turns it into time-to-first-audio, which is the
    number a person actually perceives -- the rest renders while Jarvis is
    already talking.
    """
    reader, writer = await asyncio.open_connection(host, port)
    try:
        data: dict[str, Any] = {"text": text}
        if voice:
            data["voice"] = {"name": voice}
        await write_event(writer, Event("synthesize", data))

        while True:
            event = await read_event(reader)
            if event is None:
                return
            if event.type == "audio-chunk" and event.payload:
                yield event.payload
            elif event.type in ("audio-stop", "synthesize-stopped"):
                return
    finally:
        writer.close()
        await writer.wait_closed()


async def describe(host: str, port: int, timeout: float = 5.0) -> dict[str, Any] | None:
    """Ask a Wyoming service what it is. Used as a real liveness probe."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        await write_event(writer, Event("describe", {}))
        while True:
            event = await asyncio.wait_for(read_event(reader), timeout=timeout)
            if event is None:
                return None
            if event.type == "info":
                return event.data
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
        return None
    finally:
        writer.close()
        await writer.wait_closed()
