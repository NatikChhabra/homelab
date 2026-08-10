import asyncio, json, sys, time
import websockets

URL = "ws://localhost:3100/ws"

# commands supplied as argv, else a default battery
CMDS = sys.argv[1:] or [
    "is my pc on",
    "what am i doing",
    "is chrome open",
    "what's my cpu at",
    "what's playing",
]

async def main():
    async with websockets.connect(URL, max_size=None, open_timeout=15) as ws:
        # wait for ready
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            m = json.loads(raw)
            if m.get("type") == "ready":
                break
        for cmd in CMDS:
            t0 = time.time()
            await ws.send(json.dumps({"type": "text", "text": cmd}))
            reply = None
            route = None
            # read frames until we get the 'reply'
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=45)
                except asyncio.TimeoutError:
                    reply = "<TIMEOUT>"
                    break
                if isinstance(raw, (bytes, bytearray)):
                    continue
                m = json.loads(raw)
                t = m.get("type")
                if t == "reply":
                    reply = m.get("text")
                    route = m.get("route")
                    break
                if t == "error":
                    reply = "<ERROR: %s>" % m.get("message")
                    break
            dt = int((time.time() - t0) * 1000)
            print(f"Q: {cmd}\n   -> [{dt}ms route={route}] {reply}\n", flush=True)
            await asyncio.sleep(1.0)

asyncio.run(main())
