"""
Read and repair the entity registry so the LOCAL parser can match natural speech.

The whole value of local-first routing is capped by naming. Measured
2026-08-09 with perf/probe_local.py: every scripted action matched locally in
11-45 ms, and every light command FAILED -- not because the parser is weak, but
because the lights are registered as "Node Switch Gang 1/2/3". Nobody says that.

  "turn on the ceiling light" -> Sorry, I am not aware of any device called ceiling
  "turn on the lights"        -> Sorry, I couldn't understand that

Both are fixable without the model:
  1. ALIASES on each light, so a spoken name resolves.
  2. AREA membership, so a plural/ambiguous "turn on the lights" has a target
     set to resolve against.

Registry writes are WebSocket-only (`config/entity_registry/update`); the REST
API cannot set aliases. Run this INSIDE the jarvis-voice container, which
already has aiohttp and the HA token in its environment:

    docker exec jarvis-voice python /app/ha_aliases.py --dry-run
    docker exec jarvis-voice python /app/ha_aliases.py --apply

Note HA reads the named volume `hass_config` and rewrites `.storage` on
shutdown, so editing registry JSON on disk is not an option -- it gets
overwritten. The API is the only durable path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import aiohttp

HASS = os.getenv("HASS_URL", "http://jarvis-hass:8123").rstrip("/")
TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")

# Aliases are deliberately GENERIC. Natik has not yet said which gang is which
# room (FOR-NATIK.md item 4), so naming gang 2 "bedside lamp" would be a guess,
# and a wrong name is a command that silently targets the wrong relay. These are
# the phrasings that are true regardless of which room a gang turns out to be.
ALIASES: dict[str, list[str]] = {
    "light.node_switch_gang_1": ["light one", "light 1", "first light", "gang one", "gang 1"],
    "light.node_switch_gang_2": ["light two", "light 2", "second light", "gang two", "gang 2"],
    "light.node_switch_gang_3": ["light three", "light 3", "third light", "gang three", "gang 3"],
    "fan.fan": ["ceiling fan", "the fan", "pankha"],
}

# Area membership is what makes a plural "turn on the lights" resolvable at all.
AREA_NAME = "Bedroom"
AREA_MEMBERS = [
    "light.node_switch_gang_1",
    "light.node_switch_gang_2",
    "light.node_switch_gang_3",
    "fan.fan",
]


class WS:
    """Minimal authenticated HA WebSocket client."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.session = session
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._id = 0

    async def connect(self) -> None:
        self.ws = await self.session.ws_connect(f"{HASS}/api/websocket", timeout=30)
        hello = await self.ws.receive_json()
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"unexpected greeting: {hello}")
        await self.ws.send_json({"type": "auth", "access_token": TOKEN})
        ack = await self.ws.receive_json()
        if ack.get("type") != "auth_ok":
            raise RuntimeError(f"auth failed: {ack}")

    async def cmd(self, **payload) -> dict:
        assert self.ws is not None
        self._id += 1
        payload["id"] = self._id
        await self.ws.send_json(payload)
        while True:
            msg = await self.ws.receive_json()
            if msg.get("id") == self._id:
                return msg

    async def close(self) -> None:
        if self.ws:
            await self.ws.close()


async def main(apply: bool) -> int:
    if not TOKEN:
        print("no HA token in environment", file=sys.stderr)
        return 2

    async with aiohttp.ClientSession() as session:
        ws = WS(session)
        await ws.connect()

        ents = (await ws.cmd(type="config/entity_registry/list")).get("result", [])
        by_id = {e["entity_id"]: e for e in ents}
        areas = (await ws.cmd(type="config/area_registry/list")).get("result", [])
        area_by_name = {a["name"]: a for a in areas}

        print(f"registry: {len(ents)} entities, {len(areas)} areas")
        for a in areas:
            print(f"  area: {a['name']} ({a['area_id']})")

        print("\ncurrent state of the entities we care about:")
        for eid in ALIASES:
            e = by_id.get(eid)
            if not e:
                print(f"  MISSING {eid}")
                continue
            print(f"  {eid}\n     name={e.get('name') or e.get('original_name')!r}"
                  f" aliases={e.get('aliases')} area_id={e.get('area_id')}")

        area = area_by_name.get(AREA_NAME)
        if not area:
            print(f"\narea {AREA_NAME!r} does not exist")
            if apply:
                res = await ws.cmd(type="config/area_registry/create", name=AREA_NAME)
                area = res.get("result")
                print(f"  created: {area}")
        if area:
            print(f"\narea {AREA_NAME} = {area['area_id']}")

        changes = 0
        for eid, wanted in ALIASES.items():
            e = by_id.get(eid)
            if not e:
                continue
            have = set(e.get("aliases") or [])
            merged = sorted(have | set(wanted))
            need_alias = merged != sorted(have)
            need_area = area is not None and eid in AREA_MEMBERS and e.get("area_id") != area["area_id"]
            if not (need_alias or need_area):
                continue
            changes += 1
            print(f"\n{eid}")
            if need_alias:
                print(f"  aliases {sorted(have)} -> {merged}")
            if need_area:
                print(f"  area {e.get('area_id')} -> {area['area_id']}")
            if apply:
                payload = {"type": "config/entity_registry/update", "entity_id": eid}
                if need_alias:
                    payload["aliases"] = merged
                if need_area:
                    payload["area_id"] = area["area_id"]
                res = await ws.cmd(**payload)
                print(f"  -> success={res.get('success')} {res.get('error', '')}")

        print(f"\n{changes} entities need changes; apply={apply}")
        await ws.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default is read-only)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.apply)))
