import asyncio
import aiohttp
import json
import time

async def test():
    url = "ws://172.30.0.1:3100/ws"
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url) as ws:
            msg = await asyncio.wait_for(ws.receive(), timeout=5)
            print("READY:", msg.data[:100])

            # Test greeting fast-path
            t0 = time.time()
            await ws.send_json({"type": "text", "text": "Jarvis, can you hear me?"})

            for i in range(10):
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    elapsed = (time.time() - t0) * 1000
                    if isinstance(msg.data, bytes):
                        print(f"MSG {i} ({elapsed:.0f}ms): [bytes {len(msg.data)}]")
                    else:
                        d = json.loads(msg.data)
                        rtype = d.get("type", "")
                        route = d.get("route", "")
                        text = d.get("text", "")[:80]
                        print(f"MSG {i} ({elapsed:.0f}ms): type={rtype} route={route} text={text}")
                        if rtype in ("reply", "error", "idle"):
                            break
                except asyncio.TimeoutError:
                    print(f"TIMEOUT after {i} msgs")
                    break

            # Test another greeting
            t0 = time.time()
            await ws.send_json({"type": "text", "text": "what is up?"})

            for i in range(10):
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    elapsed = (time.time() - t0) * 1000
                    if isinstance(msg.data, bytes):
                        print(f"MSG {i} ({elapsed:.0f}ms): [bytes {len(msg.data)}]")
                    else:
                        d = json.loads(msg.data)
                        rtype = d.get("type", "")
                        route = d.get("route", "")
                        text = d.get("text", "")[:80]
                        print(f"MSG {i} ({elapsed:.0f}ms): type={rtype} route={route} text={text}")
                        if rtype in ("reply", "error", "idle"):
                            break
                except asyncio.TimeoutError:
                    print(f"TIMEOUT after {i} msgs")
                    break

            # Test local path
            t0 = time.time()
            await ws.send_json({"type": "text", "text": "turn on the lights"})

            for i in range(10):
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5)
                    elapsed = (time.time() - t0) * 1000
                    if isinstance(msg.data, bytes):
                        print(f"MSG {i} ({elapsed:.0f}ms): [bytes {len(msg.data)}]")
                    else:
                        d = json.loads(msg.data)
                        rtype = d.get("type", "")
                        route = d.get("route", "")
                        text = d.get("text", "")[:80]
                        print(f"MSG {i} ({elapsed:.0f}ms): type={rtype} route={route} text={text}")
                        if rtype in ("reply", "error", "idle"):
                            break
                except asyncio.TimeoutError:
                    print(f"TIMEOUT after {i} msgs")
                    break

asyncio.run(test())
