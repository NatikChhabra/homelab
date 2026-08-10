#!/usr/bin/env python3
"""
Minimal Wyoming TTS client -- just enough of the protocol to drive Piper.

Deliberately NOT importing the parent directory's wyoming_client.py: this
service builds from its own directory as the Docker context, so a cross-
directory import would either break the image or force the build context up a
level and drag the whole JARVIS tree into every layer. Only `synthesize` is
needed here, which is ~60 lines of the protocol.

Wire format (rhasspy/wyoming):

    {"type": ..., "data": {...}, "data_length": N, "payload_length": M}\\n
    <N bytes of UTF-8 JSON, merged on top of header "data">
    <M bytes of binary payload, raw PCM>

The data_length section is easy to miss and skipping it desynchronises the
stream silently, so it is handled explicitly.
"""
from __future__ import annotations

import asyncio
import json
import struct
from typing import Any, AsyncIterator

RATE = 16000
WIDTH = 2
CHANNELS = 1


async def _read_event(reader: asyncio.StreamReader) -> tuple[str, dict, bytes | None] | None:
    line = await reader.readline()
    if not line:
        return None
    header = json.loads(line.decode("utf-8"))
    data = header.get("data") or {}

    data_length = header.get("data_length")
    if data_length:
        extra = await reader.readexactly(data_length)
        data = {**data, **json.loads(extra.decode("utf-8"))}

    payload = None
    payload_length = header.get("payload_length")
    if payload_length:
        payload = await reader.readexactly(payload_length)

    return header["type"], data, payload


async def _write_event(writer: asyncio.StreamWriter, etype: str, data: dict) -> None:
    header: dict[str, Any] = {"type": etype, "data": data}
    writer.write(json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n")
    await writer.drain()


async def synthesize_stream(
    host: str, port: int, text: str, voice: str | None = None, timeout: float = 30.0
) -> AsyncIterator[bytes]:
    """Yield PCM chunks as Piper renders them."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=timeout
    )
    try:
        data: dict[str, Any] = {"text": text}
        if voice:
            data["voice"] = {"name": voice}
        await _write_event(writer, "synthesize", data)

        while True:
            event = await asyncio.wait_for(_read_event(reader), timeout=timeout)
            if event is None:
                return
            etype, _, payload = event
            if etype == "audio-chunk" and payload:
                yield payload
            elif etype in ("audio-stop", "synthesize-stopped"):
                return
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def synthesize(
    host: str, port: int, text: str, voice: str | None = None, timeout: float = 30.0
) -> bytes:
    chunks: list[bytes] = []
    async for chunk in synthesize_stream(host, port, text, voice, timeout):
        chunks.append(chunk)
    return b"".join(chunks)


def wav_bytes(pcm: bytes, rate: int = RATE, width: int = WIDTH, channels: int = CHANNELS) -> bytes:
    """
    Wrap raw PCM in a RIFF/WAVE header.

    Written by hand rather than via the `wave` module so it can be produced
    straight into memory without a temp file -- the announcement path writes
    the finished file exactly once, atomically.
    """
    byte_rate = rate * channels * width
    block_align = channels * width
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(pcm))
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block_align, width * 8)
        + b"data"
        + struct.pack("<I", len(pcm))
        + pcm
    )
