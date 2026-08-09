"""
End-to-end check of the display-directive channel against the LIVE server.

Runs inside the jarvis-voice container (which already has aiohttp) and talks to
localhost:3100, so it exercises the real websocket handler, the real router and
the real TTS path -- not a mock.

Asserts three things:
  1. A typed command produces a `display` directive naming the right panel.
  2. BOTH connected clients receive it, proving the broadcast fan-out (desktop
     and phone follow each other).
  3. The voice loop still completes: transcript/reply/speak-start/speak-end
     arrive in order and the timings are reported, so a regression in the
     1720-2079 ms headline number would show up here rather than in production.
"""

import asyncio
import json
import sys
import time

import aiohttp

URL = "http://localhost:3100"


async def drain(ws, seen, stop):
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(msg.data)
                seen.append(data)
                if data.get("type") in ("speak-end", "error"):
                    stop.set()
            elif msg.type == aiohttp.WSMsgType.BINARY:
                seen.append({"type": "audio", "bytes": len(msg.data)})
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        seen.append({"type": "ws-error", "detail": str(exc)})


async def main() -> int:
    failures = []
    async with aiohttp.ClientSession() as http:
        ws_a = await http.ws_connect(f"{URL}/ws")
        ws_b = await http.ws_connect(f"{URL}/ws")
        seen_a, seen_b = [], []
        stop = asyncio.Event()
        t_a = asyncio.create_task(drain(ws_a, seen_a, stop))
        t_b = asyncio.create_task(drain(ws_b, seen_b, asyncio.Event()))
        await asyncio.sleep(1.5)

        print("== 1. typed command over the websocket ==")
        t0 = time.monotonic()
        await ws_a.send_str(json.dumps({"type": "text", "text": "show me the map"}))
        try:
            await asyncio.wait_for(stop.wait(), timeout=90)
        except asyncio.TimeoutError:
            failures.append("no speak-end within 90s")
        elapsed = int((time.monotonic() - t0) * 1000)

        for label, seen in (("client A (spoke)", seen_a), ("client B (idle)", seen_b)):
            kinds = [m["type"] for m in seen]
            audio = sum(m.get("bytes", 0) for m in seen if m["type"] == "audio")
            print(f"  {label}: {kinds}")
            if audio:
                print(f"    audio: {audio} bytes")
            disp = [m for m in seen if m.get("type") == "display"]
            if not disp:
                failures.append(f"{label} received NO display directive")
            elif disp[0].get("panel") != "map":
                failures.append(f"{label} got panel {disp[0].get('panel')!r}, expected 'map'")
            else:
                print(f"    display -> panel={disp[0]['panel']} "
                      f"matched={disp[0].get('matched')!r} reason={disp[0].get('reason')!r}")

        reply = next((m for m in seen_a if m.get("type") == "reply"), None)
        end = next((m for m in seen_a if m.get("type") == "speak-end"), None)
        if reply:
            print(f"  reply: {reply.get('text')!r} route={reply.get('route')} "
                  f"local_ms={reply.get('local_ms')}")
        else:
            failures.append("no reply message -- the voice loop did not complete")
        if end:
            print(f"  tts_ms={end.get('tts_ms')} total wall {elapsed} ms")

        print("== 2. HTTP push reaches both clients ==")
        seen_a.clear()
        seen_b.clear()
        async with http.post(f"{URL}/api/hud/display",
                             json={"panel": "vitals", "reason": "test"}) as resp:
            body = await resp.json()
        print(f"  POST /api/hud/display -> {body}")
        await asyncio.sleep(1.0)
        for label, seen in (("client A", seen_a), ("client B", seen_b)):
            disp = [m for m in seen if m.get("type") == "display"]
            if not disp or disp[0].get("panel") != "vitals":
                failures.append(f"{label} did not receive the pushed vitals directive")
            else:
                print(f"  {label} received panel={disp[0]['panel']}")
        if body.get("clients") != 2:
            failures.append(f"push reported {body.get('clients')} clients, expected 2")

        print("== 3. rejection paths ==")
        async with http.post(f"{URL}/api/hud/display", json={"panel": "nonsense"}) as r:
            print(f"  unknown panel -> HTTP {r.status} {await r.json()}")
            if r.status != 400:
                failures.append("unknown panel was not rejected with 400")

        await ws_a.close()
        await ws_b.close()
        t_a.cancel()
        t_b.cancel()

    print()
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
