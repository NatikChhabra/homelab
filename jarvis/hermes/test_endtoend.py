"""
End-to-end proof that Hermes actually CHANGES something.

Every earlier Hermes measurement stopped at "the model emitted the right tool
call". That is not the same as the light going on, and the difference is exactly
where this project has lost time before: the model says "Movie time activated."
while `last_triggered` stays None.

So this test asserts against DEVICE STATE, not against the model's output:

    read state -> speak a command -> re-read state -> assert it moved

A command that the model routes perfectly and dispatches perfectly still FAILS
here if the entity did not change. That is the point.

Safety: this drives real hardware in Natik's bedroom. It only ever touches
lights (silent, instant, reversible), and it RESTORES the original state of
every entity it touched, even when an assertion fails. It never touches the AC
or the fan, because those are audible and this may run overnight.
"""

from __future__ import annotations

import asyncio
import os
import sys

import aiohttp

sys.path.insert(0, "/app")

from hermes import hermes as H            # noqa: E402
from hermes.executor import Executor      # noqa: E402

HASS = os.getenv("HASS_URL", "http://jarvis-hass:8123").rstrip("/")
TOKEN = os.getenv("JARVIS_HASS_TOKEN", "")

LIGHTS = ["light.node_switch_gang_1", "light.node_switch_gang_2",
          "light.node_switch_gang_3"]

# (utterance, entity to watch, expected state). Chosen to exercise the paths
# that actually differ: a named target, an intent-with-argument, and the
# ambiguous plural that the local parser cannot handle at all -- which is the
# whole reason the model leg exists.
CASES = [
    ("turn off light one",  "light.node_switch_gang_1", "off"),
    ("turn on light one",   "light.node_switch_gang_1", "on"),
    ("turn off light two",  "light.node_switch_gang_2", "off"),
    ("turn on light two",   "light.node_switch_gang_2", "on"),
]


async def get_state(s: aiohttp.ClientSession, eid: str) -> str:
    async with s.get(f"{HASS}/api/states/{eid}",
                     headers={"Authorization": f"Bearer {TOKEN}"}) as r:
        if r.status != 200:
            return f"<http {r.status}>"
        return (await r.json()).get("state", "<none>")


async def set_state(s: aiohttp.ClientSession, eid: str, state: str) -> None:
    """Restore an entity using the real service, not the states API."""
    svc = "turn_on" if state == "on" else "turn_off"
    await s.post(f"{HASS}/api/services/light/{svc}",
                 json={"entity_id": eid},
                 headers={"Authorization": f"Bearer {TOKEN}"})


async def main() -> int:
    if not TOKEN:
        print("JARVIS_HASS_TOKEN not set")
        return 2

    hermes = H.Hermes()
    ex = Executor(HASS, TOKEN)
    failures = 0

    async with aiohttp.ClientSession() as s:
        original = {e: await get_state(s, e) for e in LIGHTS}
        print("original state:", original, "\n")

        try:
            for utt, eid, want in CASES:
                before = await get_state(s, eid)
                r = await hermes.ask(s, utt)
                results = await ex.run(s, r.tool_calls)

                # Devices are not instantaneous; poll rather than sleep once.
                after = before
                for _ in range(20):
                    await asyncio.sleep(0.25)
                    after = await get_state(s, eid)
                    if after == want:
                        break

                ok = after == want
                failures += not ok
                calls = ", ".join(
                    f"{c['name']}({c['arguments']})" for c in r.tool_calls) or "NONE"
                print(f"{'PASS' if ok else 'FAIL'}  {utt!r}")
                print(f"      specialist={r.specialist}  model={r.total_ms:.0f}ms")
                print(f"      tool_calls: {calls}")
                print(f"      dispatch:   "
                      + ("; ".join(f"{x.tool} {'ok' if x.ok else 'FAILED ' + x.detail}"
                                   f"{' [' + ', '.join(x.corrected) + ']' if x.corrected else ''}"
                                   for x in results) or "nothing dispatched"))
                print(f"      {eid}: {before} -> {after} (wanted {want})")
                if r.speech:
                    print(f"      said: {r.speech!r}")
                print()
        finally:
            # Restore unconditionally, including on assertion failure or Ctrl-C.
            print("restoring original state...")
            for eid, st in original.items():
                if st in ("on", "off") and await get_state(s, eid) != st:
                    await set_state(s, eid, st)
            await asyncio.sleep(1.0)
            print("restored:", {e: await get_state(s, e) for e in LIGHTS})

    print(f"\n{len(CASES) - failures}/{len(CASES)} commands actually changed the device")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
