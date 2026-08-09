#!/usr/bin/env python3
"""before/after latency and accuracy loop against the live services.

Measures:
  - local intent path (HA conversation.home_assistant)
  - hermes path (router -> hermes.ask -> executor)
  - full round-trip for end-to-end latency
  - STT language detection confidence + transcripts from a local mic

Run once before code/config changes to get baseline numbers, then again after.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import numpy as np
import sounddevice as sd

sys_path = str(Path(__file__).resolve().parent.parent)
if sys_path not in __import__('sys').path:
    __import__('sys').path.insert(0, sys_path)

import wyoming_client as wy  # noqa: E402
from router import IntentRouter  # noqa: E402

BASELINE = {
    "local": [
        "turn on the lights",
        "turn off the lights",
        "lights on",
        "lights off",
        "turn the fan off",
        "fan speed 2",
        "turn on light one",
        "turn off light two",
        "toggle the tv",
        "good night",
    ],
    "hermes": [
        "is my pc on",
        "turn on light three",
        "set the ac to 22",
        "feeling cold",
        "any messages",
        "boost the fan",
        "movie time",
        "what time is it",
    ],
    "stt_samples": [
        "hey jarvis turn off light one",
        "hey jarvis turn the fan off",
        "hey jarvis set the ac to 24",
        "hey jarvis good night",
        "hey jarvis feeling hot",
        "hey jarvis toggle the tv",
        "hey jarvis lights off",
        "hey jarvis boost the fan",
        "hey jarvis what time is it",
        "hey jarvis turn off light two",
    ],
}


@dataclass
class Timings:
    local_ms: list[float]
    hermes_ms: list[float]
    end_to_end_ms: list[float]
    stt_ms: list[float]
    stt_lang: list[str]
    stt_conf: list[float]
    stt_transcript: list[str]
    stt_intent: list[str]
    stt_intent_ms: list[float]


def pick_mic(prefer: str | None = None) -> tuple[int | None, str]:
    candidates = [(i, d) for i, d in enumerate(sd.query_devices()) if d['max_input_channels'] > 0]
    wanted = prefer or os.getenv('JARVIS_MIC_PREFER', 'Microphone Array,Microphone')
    for want in [w.strip() for w in wanted.split(',') if w.strip()]:
        for i, d in candidates:
            if want.lower() in d['name'].lower():
                return i, d['name']
    if candidates:
        i, d = candidates[0]
        return i, d['name']
    return None, 'NO INPUT'


async def measure_local(router: IntentRouter, http: aiohttp.ClientSession, utterances: list[str], conv: dict) -> list[float]:
    timings = []
    for utt in utterances:
        t0 = time.perf_counter()
        r = await router.process(http, utt, conv)
        timings.append((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(0.05)
    return timings


async def measure_hermes(router: IntentRouter, http: aiohttp.ClientSession, utterances: list[str], conv: dict) -> list[float]:
    timings = []
    for utt in utterances:
        t0 = time.perf_counter()
        r = await router.process(http, utt, conv)
        timings.append((time.perf_counter() - t0) * 1000)
        await asyncio.sleep(0.05)
    return timings


def percentile(xs: list[float], p: float) -> float:
    s = sorted(xs)
    k = (len(s) - 1) * p
    f = int(k)
    c = f + 1
    if c >= len(s):
        return s[f]
    return s[f] + (k - f) * (s[c] - s[f])


async def measure_stt(http: aiohttp.ClientSession, mic_index: int, utterances: list[str]) -> list[dict]:
    results = []
    for utt in utterances:
        print(f"  [stt] say: {utt}")
        input("  press enter when ready")
        recording = sd.rec(int(6 * 16000), samplerate=16000, channels=1, dtype='int16', device=mic_index)
        sd.wait()
        pcm = recording[:, 0].tobytes()
        t0 = time.perf_counter()
        transcript, info = await asyncio.to_thread(_transcribe_blocking, pcm)
        ms = (time.perf_counter() - t0) * 1000
        results.append({
            "utterance": utt,
            "transcript": transcript,
            "language": info.get('language', ''),
            "language_prob": info.get('language_prob', 0.0),
            "ms": ms,
            "local_route_ms": 0.0,
            "intent": "",
        })
        if transcript:
            conv: dict[str, str | None] = {}
            r = await router.process(http, transcript, conv)
            results[-1]['intent'] = r.path
            results[-1]['local_route_ms'] = r.total_ms
    return results


def _transcribe_blocking(pcm: bytes) -> tuple[str, dict]:
    import socket, json as _json

    with socket.create_connection(('localhost', 10300)) as sock:
        def write(evt):
            data = {'type': evt.type, 'data': evt.data}
            if evt.payload:
                data['payload_length'] = len(evt.payload)
            sock.sendall((_json.dumps(data) + '\n').encode())
            if evt.payload:
                sock.sendall(evt.payload)

        write(wy.Event('transcribe', {'language': ''}))
        write(wy.Event('audio-start', {'rate': 16000, 'width': 2, 'channels': 1, 'timestamp': 0}))
        for offset in range(0, len(pcm), 2048):
            write(wy.Event('audio-chunk', {'rate': 16000, 'width': 2, 'channels': 1}, pcm[offset:offset+2048]))
        write(wy.Event('audio-stop', {'timestamp': 0}))

        buf = b''
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        lines = buf.split(b'\n')
        for line in lines:
            try:
                evt = _json.loads(line)
                if evt.get('type') == 'transcript':
                    return evt.get('data', {}).get('text', ''), evt.get('data', {})
            except Exception:
                continue
    return '', {}


async def main() -> None:
    mic_index, mic_name = pick_mic()
    if mic_index is None:
        raise SystemExit('no mic found')
    print(f'mic: {mic_name}')
    token = os.getenv('JARVIS_HASS_TOKEN') or os.getenv('HA_API_TOKEN', '')
    if not token:
        raise SystemExit('JARVIS_HASS_TOKEN/HA_API_TOKEN not set')
    async with aiohttp.ClientSession() as http:
        conv: dict[str, str | None] = {}
        router = IntentRouter(os.getenv('HASS_URL', 'http://localhost:8123'), token)
        print('measuring local timings')
        local_ms = await measure_local(router, http, BASELINE['local'], conv)
        print('measuring hermes timings')
        hermes_ms = await measure_hermes(router, http, BASELINE['hermes'], conv)
        print('measuring stt timings')
        stt = await measure_stt(http, mic_index, BASELINE['stt_samples'])
        out = Timings(
            local_ms=local_ms,
            hermes_ms=hermes_ms,
            end_to_end_ms=[],
            stt_ms=[x['ms'] for x in stt],
            stt_lang=[x['language'] for x in stt],
            stt_conf=[x['language_prob'] for x in stt],
            stt_transcript=[x['transcript'] for x in stt],
            stt_intent=[x['intent'] for x in stt],
            stt_intent_ms=[x['local_route_ms'] for x in stt],
        )
        Path('latency_results.json').write_text(json.dumps({
            'local_ms': out.local_ms,
            'hermes_ms': out.hermes_ms,
            'stt_ms': out.stt_ms,
            'stt_lang': out.stt_lang,
            'stt_conf': out.stt_conf,
            'stt_transcript': out.stt_transcript,
            'stt_intent': out.stt_intent,
            'stt_intent_ms': out.stt_intent_ms,
        }, indent=2))
        print('saved latency_results.json')
        print(f'local p50={percentile(out.local_ms, 0.5):.0f}ms p95={percentile(out.local_ms, 0.95):.0f}ms')
        print(f'hermes p50={percentile(out.hermes_ms, 0.5):.0f}ms p95={percentile(out.hermes_ms, 0.95):.0f}ms')
        print(f'stt p50={percentile(out.stt_ms, 0.5):.0f}ms p95={percentile(out.stt_ms, 0.95):.0f}ms')


if __name__ == '__main__':
    asyncio.run(main())
