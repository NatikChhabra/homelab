"""
actuation_test.py — state-verified actuation battery for the JARVIS router.

Why this rewrite
----------------
The old version checked `res.path in PASS_PATHS` — that proved routing and
nothing else. A service call could return 200 while the device silently
discarded the frame (the IR blaster problem), or the entity stayed on
because the wrong ID was targeted. CLAUDE.md rule 4: "A test that doesn't
test the thing is worse than no test."

This version closes the loop:
  1. Read entity state BEFORE the command.
  2. Dispatch via the router (text or direct HA API for raw prove-it).
  3. Read entity state AFTER the command.
  4. Verify the state actually flipped.

Acceptance: 19 of 20 state-verified cycles pass. A "pass" means the router
returned local/hermes/llm/greeting AND the entity state flipped as expected.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "aether"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "jarvis"))

from kernel.router import IntentRouter  # noqa: E402

HASS = os.getenv("HASS_URL", "http://localhost:8123").rstrip("/")
TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
if not TOKEN:
    # Read from secrets.env if not in env — secrets.env is gitignored and
    # ACL-restricted; only the running user can read it.
    _secrets = Path(__file__).resolve().parent.parent.parent / "secrets.env"
    if _secrets.is_file():
        for _line in _secrets.read_text().splitlines():
            _line = _line.strip()
            if _line.startswith("JARVIS_HASS_TOKEN="):
                TOKEN = _line.split("=", 1)[1]
                break

# Live light entities — the only ones that physically move.
# Per CLAUDE.md rule 9: switch. domain, NOT light. domain.
LIGHT_EID_1 = "switch.3_1_node_smart_switch_switch_1"
LIGHT_EID_2 = "switch.3_1_node_smart_switch_switch_2"
LIGHT_EID_3 = "switch.3_1_node_smart_switch_switch_3"
# Map each utterance keyword to the entity we verify.
# Match on any word in the utterance, not just the first.
def _choose_eid(utt: str) -> str:
    low = utt.lower()
    for kw, eid in [
        ("light one", LIGHT_EID_1), ("light 1", LIGHT_EID_1), ("gang 1", LIGHT_EID_1),
        ("light two", LIGHT_EID_2), ("light 2", LIGHT_EID_2), ("gang 2", LIGHT_EID_2),
        ("light three", LIGHT_EID_3), ("light 3", LIGHT_EID_3), ("gang 3", LIGHT_EID_3),
        ("lights", LIGHT_EID_1),   # all-lights fast-path — verify switch_1
        ("light", LIGHT_EID_1),    # fallback: "turn on light" without number
    ]:
        if kw in low:
            return eid
    return LIGHT_EID_1

PASS_PATHS = {"local", "hermes", "llm", "greeting"}

# --- text cases: (utterance, expected_path_or_None, expected_light_state_or_None)
# expected_light_state: "on" or "off" — after the command, switch_1 must be in
# this state.  None means "no light entity to check".
TEXT_CASES: list[tuple[str, str | None, str | None]] = [
    # Greetings
    ("hello jarvis", "greeting", None),
    ("ping", "greeting", None),
    # Local-path lights — all target switch_1 for verification
    ("turn on the lights", "local", "on"),
    ("turn off the lights", "local", "off"),
    ("lights on", "local", "on"),
    ("lights off", "local", "off"),
    ("turn on light one", "local", "on"),
    ("turn off light two", "local", "off"),
    # Local-path climate
    ("set the ac to 22", "local", None),
    ("ac on", "local", None),
    ("ac off", "local", None),
    # Local-path fan
    ("set the fan to speed 3", "local", None),
    ("boost the fan", "local", None),
    # Local-path scenes
    ("good night", "local", None),
    ("movie time", "local", None),
    ("I'm home", "local", None),
    # Open-ended — any pass path is fine
    ("what time is it", None, None),
    ("turn on the tv", None, None),
    ("set the ac to 24", "local", None),
    ("fan speed 3", "local", None),
]


async def get_state(s: aiohttp.ClientSession, entity_id: str) -> str:
    """Read one entity's current state from HA. Returns '' on failure."""
    try:
        async with s.get(
            f"{HASS}/api/states/{entity_id}",
            headers={"Authorization": f"Bearer {TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=3),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                return str(data.get("state") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


async def run_text_case(
    s: aiohttp.ClientSession, r: IntentRouter,
    utt: str, want_path: str | None, want_state: str | None,
) -> tuple[bool, str]:
    """Run one text case. For light commands, verify desired state after dispatch."""
    eid = _choose_eid(utt) if want_state else ""
    before = await get_state(s, eid) if eid else ""

    try:
        res = await asyncio.wait_for(r.process(s, utt, {}), timeout=10)
    except asyncio.TimeoutError:
        return False, f"FAIL  {utt!r:45} path=timeout  speech=took too long"

    ok = res.path in PASS_PATHS
    if want_path is not None and res.path != want_path:
        ok = False

    state_verif = ""
    if eid:
        # Tuya switches need up to 4 s for state to propagate; retry once.
        after = await get_state(s, eid)
        if after != want_state:
            await asyncio.sleep(2)
            after = await get_state(s, eid)
        if after == want_state:
            state_verif = f"STATE_OK {before!r:>3} -> {after!r:>3}"
        elif after == before:
            state_verif = f"STATE_STUCK {before!r:>3} (not {want_state})"
            ok = False
        else:
            state_verif = f"STATE_WRONG {before!r:>3} -> {after!r:>3} (want {want_state})"
            ok = False

    mark = "PASS" if ok else "FAIL"
    extra = f"  {state_verif}" if state_verif else ""
    return ok, f"{mark}  {utt!r:45} path={res.path:7} speech={res.speech[:50]!r}{extra}"


async def run_light_cycling(s: aiohttp.ClientSession, r: IntentRouter, cycles: int = 10) -> tuple[int, int, list[str]]:
    """
    20 alternating on/off cycles via direct HA API.
    Returns (passed, total, lines).
    Tuya switches occasionally need >2 s for state propagation; retry once.
    """
    lines = []
    passed = 0
    for i in range(1, cycles * 2 + 1):
        want_on = (i % 2 == 1)
        action = "on" if want_on else "off"
        before = await get_state(s, LIGHT_EID_1)
        if not before:
            lines.append(f"SKIP cycle {i}: cannot read initial state")
            continue
        try:
            async with s.post(
                f"{HASS}/api/services/switch/turn_{action}",
                json={"entity_id": LIGHT_EID_1},
                headers={"Authorization": f"Bearer {TOKEN}"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                ok_status = resp.status in (200, 201)
        except Exception as exc:  # noqa: BLE001
            lines.append(f"FAIL cycle {i}: service call error: {exc}")
            continue
        expected = "on" if want_on else "off"
        after = await get_state(s, LIGHT_EID_1)
        if after != expected:
            await asyncio.sleep(2)
            after = await get_state(s, LIGHT_EID_1)
        if ok_status and after == expected:
            passed += 1
            lines.append(f"PASS cycle {i}: {before} -> {after} ({action})")
        else:
            lines.append(f"FAIL cycle {i}: {before} -> {after} (expected {expected}, status={resp.status if 'resp' in dir() else 'N/A'})")
    return passed, cycles * 2, lines


async def main() -> int:
    if not TOKEN:
        print("JARVIS_HASS_TOKEN not set")
        return 2

    r = IntentRouter(HASS, TOKEN)
    text_ok = 0
    text_fail = 0

    print(f"HASS={HASS}  hermes_enabled={r.hermes_enabled}  LIGHT_EID={LIGHT_EID_1}\n")

    async with aiohttp.ClientSession() as s:
        # --- TEXT lane --------------------------------------------------------
        print("=" * 72)
        print("TEXT LANE (20 cases — state-verified for lights)")
        print("=" * 72)
        for utt, want, want_state in TEXT_CASES:
            ok, line = await run_text_case(s, r, utt, want, want_state)
            print(line)
            if ok:
                text_ok += 1
            else:
                text_fail += 1

        # --- LIGHT CYCLE lane -------------------------------------------------
        print()
        print("=" * 72)
        print("LIGHT CYCLE LANE (20 alternating on/off, state-verified)")
        print("=" * 72)
        cycle_ok, cycle_total, cycle_lines = await run_light_cycling(s, r)
        for line in cycle_lines:
            print(line)

    print()
    print("=" * 72)
    print(f"TEXT:     {text_ok}/{len(TEXT_CASES)}")
    print(f"CYCLES:   {cycle_ok}/{cycle_total}")
    print(f"TOTAL:    {text_ok + cycle_ok}/{len(TEXT_CASES) + cycle_total}")
    # Acceptance: 19/20 text + state-verified cycles
    if text_ok >= 19 and cycle_ok >= 19:
        print("ACCEPTANCE PASS (19/20 text + 19/20 cycles)")
        return 0
    else:
        print(f"ACCEPTANCE FAIL (need 19/20 text AND 19/20 cycles)")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
