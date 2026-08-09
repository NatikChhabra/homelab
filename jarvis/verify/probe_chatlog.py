#!/usr/bin/env python3
"""
Probe: can we retrieve Home Assistant's real tool-call record for a conversation?

WHY THIS MATTERS: /api/conversation/process tells us nothing about whether the
model called a tool. It returns response_type "action_done" and empty
success/failed arrays whether the model drove a device or merely claimed to.
That is exactly the blind spot that let "AC set to 22" be spoken while no tool
ran at all.

HA 2026.8 keeps a chat log per conversation containing AssistantContent
(.tool_calls) and ToolResultContent (.tool_name, .tool_result). If we can read
that, verification rests on ground truth instead of inference.

This probe answers three questions:
  1. Does our long-lived token pass the @require_admin gate on the subscription?
  2. Does the log survive long enough to be read after the reply returns?
  3. Do tool_calls and tool_result actually appear for the Ollama agent?

Run:  docker exec jarvis-voice python3 /tmp/probe_chatlog.py "turn on the lights"
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import aiohttp

TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
BASE = os.getenv("HASS_URL", "http://jarvis-hass:8123").rstrip("/")
AGENT = os.getenv("CONVERSATION_AGENT", "conversation.jarvis")


async def main() -> None:
    text = sys.argv[1] if len(sys.argv) > 1 else "what time is it?"
    async with aiohttp.ClientSession() as s:
        t0 = time.monotonic()
        async with s.post(
            f"{BASE}/api/conversation/process",
            json={"text": text, "language": "en", "agent_id": AGENT},
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=90),
        ) as r:
            conv = await r.json()
        conv_ms = int((time.monotonic() - t0) * 1000)
        cid = conv.get("conversation_id")
        speech = (
            conv.get("response", {}).get("speech", {}).get("plain", {}).get("speech")
        )
        print(f"--- utterance: {text!r}")
        print(f"reply ({conv_ms} ms): {speech!r}")
        print(f"conversation_id: {cid}")
        print(f"response_type: {conv.get('response', {}).get('response_type')}")
        print(f"data: {json.dumps(conv.get('response', {}).get('data', {}))}")

        # Now try to read the chat log for that conversation.
        t1 = time.monotonic()
        ws = await s.ws_connect(f"{BASE}/api/websocket", timeout=10)
        print("\nauth_required:", (await ws.receive_json()).get("type"))
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        auth = await ws.receive_json()
        print("auth result:", auth.get("type"), auth.get("message", ""))
        if auth.get("type") != "auth_ok":
            print("!! token cannot open the websocket API")
            await ws.close()
            return

        await ws.send_json(
            {"id": 1, "type": "conversation/chat_log/subscribe", "conversation_id": cid}
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            msg = await ws.receive_json()
            if msg.get("type") == "result" and not msg.get("success", True):
                print("!! subscribe failed:", json.dumps(msg.get("error")))
                break
            if msg.get("type") == "event":
                ev = msg["event"]
                print(f"\nevent_type: {ev.get('event_type')}")
                print(f"chat-log fetch took {int((time.monotonic() - t1) * 1000)} ms")
                data = ev.get("data", {})
                content = data.get("content", data)
                print(f"\n--- chat log ({len(content) if isinstance(content, list) else '?'} entries)")
                for c in content if isinstance(content, list) else []:
                    role = c.get("role")
                    if role == "assistant":
                        print(f"  assistant: content={c.get('content')!r}")
                        for tc in c.get("tool_calls") or []:
                            print(f"     TOOL CALL -> {json.dumps(tc)}")
                        if not c.get("tool_calls"):
                            print("     (no tool_calls)")
                    elif role == "tool_result":
                        print(
                            f"  tool_result: {c.get('tool_name')} -> "
                            f"{json.dumps(c.get('tool_result'))[:400]}"
                        )
                    elif role == "user":
                        print(f"  user: {c.get('content')!r}")
                    else:
                        print(f"  {role}: {str(c.get('content'))[:120]!r}")
                break
        await ws.close()


asyncio.run(main())
