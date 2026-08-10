"""
Deploy the repo's system prompt to the LIVE Ollama conversation subentry.

Why a script rather than the HA UI
----------------------------------
The prompt lives in two places and they drift. `jarvis-prompt.txt` in the repo
is reviewed; the copy HA actually sends to the model lives inside the config
entry's options, in `.storage`, which HA rewrites on shutdown -- so editing the
file on disk does not work, and the drift is invisible until something misfires.

The concrete failure this fixes: the live prompt still instructs the model to
call a "Movie time." script that is not exposed to it. The model dutifully says
"Movie time activated." while `script.movie_time.last_triggered` stays None.
server-audit.sh section 20 catches exactly this, and it stays FAIL until the
live prompt matches the repo. (The script is now reachable locally through
JarvisMovieTime in custom_sentences, at zero tool-budget cost.)

Config entry options are WebSocket-only -- there is no REST endpoint for
`config_entries/subentry/update`. It must also run INSIDE jarvis-hass, because
`config_entries/subentries/list` returns only ids and titles, never `data`, so
the current options have to be read from `/config/.storage/core.config_entries`
before they can be written back:

    docker exec jarvis-hass python3 /tmp/deploy_prompt.py --dry-run
    docker exec jarvis-hass python3 /tmp/deploy_prompt.py --apply

SECURITY: core.config_entries also holds both Tuya local keys. This script reads
only the ollama entry and never writes the file anywhere. A previous agent
dumped that whole file into the repo; do not reintroduce that.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import sys

import aiohttp

HASS = os.getenv("HASS_URL", "http://localhost:8123").rstrip("/")
TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
PROMPT_PATH = os.getenv("PROMPT_PATH", "/tmp/jarvis-prompt.txt")
STORAGE = os.getenv("STORAGE_PATH", "/config/.storage/core.config_entries")

# The key HA stores the system prompt under in an Ollama conversation subentry.
PROMPT_KEY = "prompt"


def read_live_subentries() -> list[tuple[str, str, str, dict]]:
    """(entry_id, subentry_id, title, data) for every ollama conversation subentry."""
    with open(STORAGE, encoding="utf-8") as fh:
        store = json.load(fh)
    out = []
    for entry in store["data"]["entries"]:
        if entry.get("domain") != "ollama":
            continue
        for sub in entry.get("subentries", []):
            if sub.get("subentry_type") != "conversation":
                continue
            out.append((
                entry["entry_id"], sub["subentry_id"],
                sub.get("title") or "", dict(sub.get("data") or {}),
            ))
    return out


class WS:
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

    async def cmd(self, **payload):
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

    with open(PROMPT_PATH, encoding="utf-8") as fh:
        wanted = fh.read().strip()

    async with aiohttp.ClientSession() as session:
        ws = WS(session)
        await ws.connect()

        subs = read_live_subentries()
        if not subs:
            print("no ollama conversation subentry found", file=sys.stderr)
            return 3

        changed = 0
        for entry_id, sub_id, title, data in subs:
            live = (data.get(PROMPT_KEY) or "").strip()
            print(f"subentry {sub_id} title={title!r} prompt_len={len(live)}")
            if live == wanted:
                print("  already matches the repo")
                continue
            changed += 1
            diff = list(difflib.unified_diff(
                live.splitlines(), wanted.splitlines(),
                fromfile="live", tofile="repo", lineterm="", n=1,
            ))
            print(f"  DIFFERS ({len(diff)} diff lines). Live -> repo:")
            for line in diff[:80]:
                print("    " + line)
            if len(diff) > 80:
                print(f"    ... {len(diff) - 80} more diff lines")

            if apply:
                data[PROMPT_KEY] = wanted
                # NOTE the PLURAL: the command is `config_entries/subentries/update`.
                # The singular `config_entries/subentry/update` returns
                # {'code': 'unknown_command'} -- which reads like a permissions
                # or version problem rather than a typo. The full list lives in
                # homeassistant/components/config/config_entries.py.
                res = await ws.cmd(
                    type="config_entries/subentries/update",
                    entry_id=entry_id,
                    subentry_id=sub_id,
                    data=data,
                    title=title,
                )
                ok = res.get("success")
                print(f"  -> update success={ok} {res.get('error', '')}")
                if not ok:
                    return 4

        print(f"\n{changed} subentries differ from the repo; apply={apply}")
        await ws.close()
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.apply)))
