#!/usr/bin/env python3
"""
Probe: what do the chat log and the conversation response look like for each
KIND of command? Informs the verification design in verify/closed_loop.py.

Specifically it answers:
  - What tool name does an exposed SCRIPT get? (built-in intents are HassTurnOn,
    but the fan and AC are scripts, and their shape was unknown.)
  - Is response.data.success populated for script calls, or only for built-ins?
  - Can we reproduce the zero-tool-call false confirmation on demand?
  - How long after the service call does the conversation response return? That
    gap is free settling time, and decides whether read-back needs a retry.

It snapshots every helper it could disturb and restores them at the end,
including on exception. It deliberately drives the FAN rather than the AC for
the infrared case: both are one-way IR with no feedback, so the fan proves the
same point without short-cycling a compressor.
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
AUTH = {"Authorization": f"Bearer {TOKEN}"}

# Everything a test below could move. Snapshotted and put back afterwards.
RESTORE = [
    "light.node_switch_gang_1",
    "light.node_switch_gang_2",
    "light.node_switch_gang_3",
    "input_number.fan_speed",
    "input_number.fan_last_speed",
    "input_boolean.fan_power",
    "input_number.ac_temperature",
    "input_boolean.ac_power",
]

UTTERANCES = [
    "turn on the lights",
    "set the fan to speed two",
    "turn the fan off",
    "what is the air conditioner set to",
    "set the air conditioner to 22",
]


async def states(s: aiohttp.ClientSession) -> dict:
    async with s.get(f"{BASE}/api/states", headers=AUTH) as r:
        return {x["entity_id"]: x for x in await r.json()}


async def snapshot(s: aiohttp.ClientSession) -> dict:
    all_states = await states(s)
    return {e: all_states[e]["state"] for e in RESTORE if e in all_states}


async def restore(s: aiohttp.ClientSession, snap: dict) -> None:
    print("\n=== restoring state ===")
    now = await states(s)
    for eid, want in snap.items():
        have = now.get(eid, {}).get("state")
        if have == want:
            continue
        domain = eid.split(".")[0]
        print(f"  {eid}: {have} -> {want}")
        if domain == "light":
            await s.post(
                f"{BASE}/api/services/light/turn_{want}",
                json={"entity_id": eid}, headers=AUTH,
            )
        elif domain == "input_number":
            await s.post(
                f"{BASE}/api/services/input_number/set_value",
                json={"entity_id": eid, "value": float(want)}, headers=AUTH,
            )
        elif domain == "input_boolean":
            await s.post(
                f"{BASE}/api/services/input_boolean/turn_{want}",
                json={"entity_id": eid}, headers=AUTH,
            )
        await asyncio.sleep(0.2)
    print("  restore pass complete")


async def chat_log(s: aiohttp.ClientSession, cid: str) -> tuple[list, int]:
    """Return (content list, fetch milliseconds)."""
    t = time.monotonic()
    try:
        ws = await s.ws_connect(f"{BASE}/api/websocket", timeout=10)
    except Exception as exc:  # noqa: BLE001
        return [{"role": "_error", "content": repr(exc)}], 0
    try:
        await ws.receive_json()
        await ws.send_json({"type": "auth", "access_token": TOKEN})
        if (await ws.receive_json()).get("type") != "auth_ok":
            return [{"role": "_error", "content": "auth failed"}], 0
        await ws.send_json(
            {"id": 1, "type": "conversation/chat_log/subscribe", "conversation_id": cid}
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            m = await ws.receive_json()
            if m.get("type") == "result" and not m.get("success", True):
                return [{"role": "_error", "content": json.dumps(m.get("error"))}], 0
            if m.get("type") == "event":
                data = m["event"].get("data", {})
                content = data.get("content", [])
                return content, int((time.monotonic() - t) * 1000)
    finally:
        await ws.close()
    return [], 0


async def run(s: aiohttp.ClientSession, text: str) -> None:
    print("\n" + "=" * 72)
    print(f"UTTERANCE: {text!r}")
    before = await states(s)
    t0 = time.monotonic()
    async with s.post(
        f"{BASE}/api/conversation/process",
        json={"text": text, "language": "en", "agent_id": AGENT},
        headers=AUTH, timeout=aiohttp.ClientTimeout(total=120),
    ) as r:
        conv = await r.json()
    conv_ms = int((time.monotonic() - t0) * 1000)
    t_return = time.time()

    resp = conv.get("response", {})
    speech = resp.get("speech", {}).get("plain", {}).get("speech")
    cid = conv.get("conversation_id")
    print(f"reply ({conv_ms} ms): {speech!r}")
    print(f"response_type: {resp.get('response_type')}")
    d = resp.get("data", {})
    print(f"data.success: {json.dumps(d.get('success'))[:200]}")
    print(f"data.failed : {json.dumps(d.get('failed'))[:200]}")

    content, log_ms = await chat_log(s, cid)
    print(f"\nchat log fetched in {log_ms} ms:")
    calls = 0
    for c in content:
        role = c.get("role")
        if role == "assistant":
            for tc in c.get("tool_calls") or []:
                calls += 1
                print(f"   TOOL CALL: {tc.get('tool_name')} args={json.dumps(tc.get('tool_args'))}")
            if c.get("content"):
                print(f"   assistant text: {c.get('content')!r}")
        elif role == "tool_result":
            tr = c.get("tool_result")
            print(f"   TOOL RESULT {c.get('tool_name')}: {json.dumps(tr)[:300]}")
        elif role == "_error":
            print(f"   CHATLOG ERROR: {c.get('content')}")
    print(f"   >>> total tool calls: {calls}")

    # Which entities moved, and how much settling time the reply bought us.
    after = await states(s)
    for eid, st in after.items():
        if eid not in before:
            continue
        if st["state"] != before[eid]["state"]:
            lu = st["last_updated"]
            import datetime
            ep = datetime.datetime.fromisoformat(lu.replace("Z", "+00:00")).timestamp()
            print(
                f"   CHANGED {eid}: {before[eid]['state']} -> {st['state']} "
                f"(settled {int((t_return - ep) * 1000)} ms BEFORE the reply returned)"
            )


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        snap = await snapshot(s)
        print("snapshot:", json.dumps(snap, indent=2))
        try:
            for u in UTTERANCES:
                await run(s, u)
                await asyncio.sleep(1.0)
        finally:
            await restore(s, snap)


asyncio.run(main())
