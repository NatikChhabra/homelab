"""Transparent logging proxy in front of Ollama.

Listens on :11435, forwards to jarvis-ollama:11434, and records the exact JSON
Home Assistant sends and the exact NDJSON Ollama streams back. This is the only
way to see the true tool list and tool-call arguments without trusting a
reading of the source.

Streams both directions chunk-by-chunk so measured latency stays honest.
One JSON object per line in /tmp/ollama-capture.jsonl.
"""

import json
import time

from aiohttp import web, ClientSession, ClientTimeout

UPSTREAM = "http://jarvis-ollama:11434"
CAPTURE = "/tmp/ollama-capture.jsonl"

HOP = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def record(kind, payload):
    rec = {"t": time.time(), "kind": kind, "payload": payload}
    with open(CAPTURE, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
        fh.flush()


async def handler(request):
    body = await request.read()
    is_chat = request.path.endswith("/api/chat")
    t_req = time.time()

    if is_chat and body:
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            record(
                "request",
                {
                    "model": parsed.get("model"),
                    "options": parsed.get("options"),
                    "keep_alive": parsed.get("keep_alive"),
                    "think": parsed.get("think"),
                    "messages": parsed.get("messages"),
                    "n_tools": len(parsed.get("tools") or []),
                    "tool_names": [
                        t.get("function", {}).get("name")
                        for t in (parsed.get("tools") or [])
                    ],
                    "tools": parsed.get("tools"),
                },
            )

    url = UPSTREAM + request.path_qs
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP | {"host"}}

    async with ClientSession(timeout=ClientTimeout(total=900)) as sess:
        async with sess.request(
            request.method, url, data=body or None, headers=headers
        ) as up:
            resp = web.StreamResponse(
                status=up.status,
                headers={k: v for k, v in up.headers.items() if k.lower() not in HOP},
            )
            await resp.prepare(request)

            buf = b""
            text_parts = []
            tool_calls = []
            t_first = None

            async for chunk in up.content.iter_any():
                if t_first is None:
                    t_first = time.time()
                await resp.write(chunk)
                if not is_chat:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    msg = obj.get("message") or {}
                    if msg.get("content"):
                        text_parts.append(msg["content"])
                    for tc in msg.get("tool_calls") or []:
                        tool_calls.append(tc)

            await resp.write_eof()

            if is_chat:
                record(
                    "response",
                    {
                        "text": "".join(text_parts),
                        "tool_calls": tool_calls,
                        "n_tool_calls": len(tool_calls),
                        "ttfb_ms": None if t_first is None else round((t_first - t_req) * 1000),
                        "total_ms": round((time.time() - t_req) * 1000),
                    },
                )
            return resp


app = web.Application(client_max_size=64 * 1024 * 1024)
app.router.add_route("*", "/{tail:.*}", handler)
web.run_app(app, host="0.0.0.0", port=11435, access_log=None)
