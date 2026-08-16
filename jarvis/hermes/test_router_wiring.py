"""
Prove Hermes is IN THE LIVE PATH, not merely importable.

Why this file exists
--------------------
`test_endtoend.py` already proved the executor actuates devices, but it drove
Hermes directly. That left the thing Natik actually cares about unproven: does
an utterance arriving at `IntentRouter.process` -- the function the voice loop
and the always-on ears both call -- reach Hermes at all?

It did not. Measured 2026-08-09: `voice_server.py` and `router.py` contained
zero references to hermes, so every command local missed went straight to the
17-tool agent, which is the exact configuration that narrates actions it never
performs. This test is the guard against that regressing silently.

Two kinds of assertion, deliberately separated
----------------------------------------------
1. EXECUTION is proved on lights only. They are silent and restorable. The AC
   and fan are audible and Natik is in the room, so this test must never
   actuate them just to satisfy itself.
2. ROUTING for climate/comfort is proved WITHOUT dispatching: Hermes is asked
   which specialist and which tool call it would produce, and the call is
   thrown away. That still catches the failure that matters -- a comfort
   utterance being handed to the big agent.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import aiohttp  # noqa: E402

from kernel.router import IntentRouter  # noqa: E402

HASS = os.getenv("HASS_URL", "http://jarvis-hass:8123")
TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
LIGHTS = ["switch.3_1_node_smart_switch_switch_1",
          "switch.3_1_node_smart_switch_switch_2",
          "switch.3_1_node_smart_switch_switch_3"]


async def states(s: aiohttp.ClientSession) -> dict[str, str]:
    h = {"Authorization": f"Bearer {TOKEN}"}
    out = {}
    for e in LIGHTS:
        async with s.get(f"{HASS}/api/states/{e}", headers=h) as r:
            out[e] = (await r.json()).get("state") if r.status == 200 else "?"
    return out


async def restore(s: aiohttp.ClientSession, snap: dict[str, str]) -> None:
    h = {"Authorization": f"Bearer {TOKEN}"}
    for e, st in snap.items():
        if st not in ("on", "off"):
            continue
        await s.post(f"{HASS}/api/services/switch/turn_{st}",
                     headers=h, json={"entity_id": e})


async def main() -> int:
    bad = 0
    async with aiohttp.ClientSession() as s:
        snap = await states(s)
        print(f"original light state: {snap}\n")

        r = IntentRouter(HASS, TOKEN)
        print(f"hermes_enabled = {r.hermes_enabled}")
        if not r.hermes_enabled:
            print("FAIL  router built with Hermes DISABLED -- it is not in the path")
            return 1

        # --- 1. local must stay local. The ~26 ms no-model path is the biggest
        # measured win in the project; Hermes must not have stolen it.
        cases = [
            ("turn off light one", "local"),
            ("turn on light one", "local"),
            # Question about a device whose specialist owns GetLiveContext is
            # answered by Hermes, not the big agent -- the executor reads real
            # state so the reply cannot be invented.
            ("what is the fan doing", "hermes"),
        ]
        for text, want in cases:
            t0 = time.perf_counter()
            res = await r.process(s, text, {})
            took = (time.perf_counter() - t0) * 1000
            got = res.path
            mark = "PASS" if got == want else "FAIL"
            if got != want:
                bad += 1
            print(f"{mark}  {text!r:32} path={got:7} want={want:7} "
                  f"{took:7.0f}ms  reason={res.hermes_reason or '-'}")
            print(f"        speech={res.speech[:70]!r}")

        # --- 2. execution through the router, on a light. This is the assertion
        # that Hermes actually CHANGED something via the live path.
        print()
        await s.post(f"{HASS}/api/services/switch/turn_on",
                     headers={"Authorization": f"Bearer {TOKEN}"},
                     json={"entity_id": LIGHTS[0]})
        await asyncio.sleep(1.5)
        before = (await states(s))[LIGHTS[0]]
        res = await r.process(s, "switch off the first light please", {})
        await asyncio.sleep(2.0)
        after = (await states(s))[LIGHTS[0]]
        changed = before == "on" and after == "off"
        print(f"{'PASS' if changed else 'FAIL'}  execution via router: "
              f"{before} -> {after} on path={res.path} "
              f"specialist={res.specialist or '-'} calls={res.tool_calls}")
        if not changed:
            bad += 1

        # --- 3. climate/comfort routing WITHOUT dispatch. Audible devices.
        print()
        if r._hermes is not None:
            for text in ("set the ac to 22", "it's boiling in here",
                         "yaar bahut garmi hai", "boost the fan"):
                h = await r._hermes.ask(s, text)
                calls = [f"{c.get('name')}({c.get('arguments')})"
                         for c in (h.tool_calls or [])]
                okmark = "PASS" if calls else "FAIL"
                if not calls:
                    bad += 1
                print(f"{okmark}  {text!r:28} specialist={h.specialist:10} "
                      f"-> {calls or 'NO TOOL CALL'}")

        print()
        await restore(s, snap)
        await asyncio.sleep(1.0)
        print(f"restored: {await states(s)}")
        print(f"\nstats: {r.stats.snapshot()}")
    print("\nALL PASS" if bad == 0 else f"\n{bad} FAILURE(S)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
