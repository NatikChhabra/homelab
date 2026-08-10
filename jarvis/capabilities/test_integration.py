#!/usr/bin/env python3
"""
Integration tests against a RUNNING capability service.

    python test_integration.py [base_url]        default http://localhost:3102

Covers the things unit tests cannot: that Piper really renders, that the
WebSocket sink really receives PCM, that the status sentences are short enough
to speak, and that every proactive gate really refuses.

Restart persistence is NOT tested here -- it needs the container destroyed
mid-flight, which is a shell operation. See the README for that procedure and
its recorded result.
"""
from __future__ import annotations

import asyncio
import json
import sys

import aiohttp

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:3102"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'}  {name}{(' -- ' + detail) if detail else ''}")
    if not ok:
        failures.append(name)


async def post(session, path, payload):
    async with session.post(f"{BASE}{path}", json=payload) as resp:
        return resp.status, await resp.json()


async def test_status(session) -> None:
    print("== status tool ==")
    for topic in ("server", "alerts", "briefing", "disk"):
        status_code, data = await post(session, "/api/status", {"topic": topic})
        speech = data.get("speech", "")
        words = len(speech.split())
        check(f"status({topic}) answers", status_code == 200 and bool(speech), speech)
        # Spoken replies cap near 12 words. Briefing is allowed more because it
        # is explicitly a multi-fact summary the user asked for.
        cap = 30 if topic == "briefing" else 16
        check(f"status({topic}) is speakable ({words} words, cap {cap})", words <= cap)
        check(f"status({topic}) is not a list", "\n" not in speech and speech.count(",") <= 3)


async def test_ws_sink(session) -> None:
    print("\n== websocket announcement sink ==")
    async with session.ws_connect(f"{BASE}/ws/announce") as ws:
        ready = await asyncio.wait_for(ws.receive_json(), timeout=10)
        check("sink handshake", ready.get("type") == "ready", json.dumps(ready))

        # Fire an announcement while subscribed.
        task = asyncio.create_task(
            post(session, "/api/announce/test", {"text": "Sink check. One two three."})
        )

        start = end = None
        pcm = 0
        deadline = asyncio.get_event_loop().time() + 60
        while asyncio.get_event_loop().time() < deadline:
            msg = await asyncio.wait_for(ws.receive(), timeout=60)
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                if data.get("type") == "announce-start":
                    start = data
                elif data.get("type") == "announce-end":
                    end = data
                    break
            elif msg.type == aiohttp.WSMsgType.BINARY:
                pcm += len(msg.data)
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

        _, result = await task
        check("announce-start received", start is not None, json.dumps(start or {}))
        check("PCM received over ws", pcm > 10000, f"{pcm} bytes")
        check("announce-end matches pcm", bool(end) and end.get("bytes") == pcm,
              f"end={end.get('bytes') if end else None} pcm={pcm}")
        check("service saw the sink", result.get("ws_sinks", 0) >= 1,
              f"ws_sinks={result.get('ws_sinks')}")
        check("wav also written", bool(result.get("wav")), str(result.get("wav")))


async def test_timer_roundtrip(session) -> None:
    print("\n== timer tool ==")
    await post(session, "/api/timer", {"action": "cancel", "label": "all"})

    _, data = await post(session, "/api/timer",
                         {"action": "set", "duration_or_time": "ten minutes", "label": "pasta"})
    check("set speaks back cleanly", data.get("speech") == "Pasta timer set for 10 minutes.",
          data.get("speech", ""))

    _, data = await post(session, "/api/timer",
                         {"action": "set", "duration_or_time": "2 hours", "label": ""})
    check("unlabelled timer", data.get("speech") == "Timer set for 2 hours.", data.get("speech", ""))

    _, data = await post(session, "/api/timer", {"action": "list"})
    check("list is short", data.get("active") == 2 and len(data.get("speech", "").split()) <= 12,
          data.get("speech", ""))

    # The empty-label trap: "" is a substring of everything, so a named cancel
    # used to take every anonymous timer with it.
    _, data = await post(session, "/api/timer", {"action": "cancel", "label": "the pasta timer"})
    check("named cancel takes exactly one", data.get("cancelled") == 1, str(data.get("cancelled")))

    _, data = await post(session, "/api/timer", {"action": "cancel", "label": "all"})
    check("cancel all clears the rest", data.get("cancelled") == 1, str(data.get("cancelled")))

    _, data = await post(session, "/api/timer",
                         {"action": "set", "duration_or_time": "", "label": "the pasta"})
    check("no duration asks instead of guessing",
          "How long" in data.get("speech", ""), data.get("speech", ""))


async def test_watch_dedupe(session) -> None:
    print("\n== proactive gates ==")
    async with session.get(f"{BASE}/api/debug/state") as resp:
        before = await resp.json()
    known = before["watcher"]["known_failures"]
    check("existing failures are known, not shouted about", known >= 1, f"{known} known")

    _, first = await post(session, "/api/watch/now", {})
    check("repeat check says nothing new",
          first.get("spoken") is False and first.get("reason") == "nothing-new",
          json.dumps(first))

    _, second = await post(session, "/api/watch/now", {})
    check("and stays quiet on the next pass", second.get("spoken") is False, json.dumps(second))


async def main() -> None:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        await test_timer_roundtrip(session)
        await test_status(session)
        await test_ws_sink(session)
        await test_watch_dedupe(session)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S): {', '.join(failures)}")
        sys.exit(1)
    print("all integration tests passed")


if __name__ == "__main__":
    asyncio.run(main())
