#!/usr/bin/env python3
"""
Measure how long Home Assistant takes to REPORT a new state after we write it.

WHY THIS EXISTS: closed-loop verification reads an entity back after the model
acts. If the read happens before Tuya's state has settled, verification sees the
OLD value and reports a mismatch that never existed -- a false alarm, which is
almost as corrosive to trust as the false confirmation it was built to catch.

So the settling time is not a guess. It is measured here, and VERIFY_SETTLE_MS
in verify/closed_loop.py is set from the numbers this prints.

Run inside the jarvis-voice container, which already holds the HA token:

    docker cp verify/measure_settling.py jarvis-voice:/tmp/m.py
    docker exec jarvis-voice python3 /tmp/m.py

It toggles ONE light (default light.node_switch_gang_3) and always restores the
original state, including on error. It touches no IR device: the AC and fan have
no feedback path, so there is nothing to measure there by definition.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
BASE = os.getenv("HASS_URL", "http://jarvis-hass:8123").rstrip("/")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

ENTITY = os.getenv("PROBE_ENTITY", "light.node_switch_gang_3")
TRIALS = int(os.getenv("PROBE_TRIALS", "5"))


def req(path: str, data=None):
    body = json.dumps(data).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body, headers=HEADERS)
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.load(resp)


def state_of(eid: str) -> dict:
    return req(f"/api/states/{eid}")


def main() -> None:
    original = state_of(ENTITY)["state"]
    print(f"entity   : {ENTITY}")
    print(f"original : {original}\n")

    settle_samples: list[float] = []
    call_samples: list[float] = []

    try:
        for trial in range(1, TRIALS + 1):
            current = state_of(ENTITY)["state"]
            target = "off" if current == "on" else "on"

            result: dict = {}

            def fire() -> None:
                t = time.monotonic()
                try:
                    req(f"/api/services/light/turn_{target}", {"entity_id": ENTITY})
                    result["call_ms"] = (time.monotonic() - t) * 1000
                except Exception as exc:  # noqa: BLE001 - reporting only
                    result["error"] = repr(exc)

            t0 = time.monotonic()
            th = threading.Thread(target=fire)
            th.start()

            # Poll as fast as HA will answer, so the resolution of "when did the
            # new value become visible" is the poll cost itself (~2-5 ms), not an
            # artificial sleep.
            seen_ms = None
            polls = 0
            while (time.monotonic() - t0) < 10.0:
                s = state_of(ENTITY)["state"]
                polls += 1
                if s == target:
                    seen_ms = (time.monotonic() - t0) * 1000
                    break
            th.join()

            call_ms = result.get("call_ms", float("nan"))
            if seen_ms is not None:
                settle_samples.append(seen_ms)
                call_samples.append(call_ms)
            print(
                f"trial {trial}: {current} -> {target} | "
                f"service call returned at {call_ms:7.1f} ms | "
                f"new state visible at {seen_ms if seen_ms is not None else -1:7.1f} ms | "
                f"{polls} polls"
                + (f" | ERROR {result['error']}" if "error" in result else "")
            )
            time.sleep(1.0)
    finally:
        # Restore no matter what happened above. Leaving a light in the wrong
        # state after a measurement run is not acceptable.
        try:
            now = state_of(ENTITY)["state"]
            if now != original:
                req(f"/api/services/light/turn_{original}", {"entity_id": ENTITY})
                time.sleep(0.6)
            print(f"\nrestored : {state_of(ENTITY)['state']} (original {original})")
        except Exception as exc:  # noqa: BLE001
            print(f"\nRESTORE FAILED: {exc!r} -- check {ENTITY} by hand")

    if settle_samples:
        print(
            "\nservice call returns : min %.0f  max %.0f ms"
            % (min(call_samples), max(call_samples))
        )
        print(
            "state visible after  : min %.0f  max %.0f  mean %.0f ms"
            % (
                min(settle_samples),
                max(settle_samples),
                sum(settle_samples) / len(settle_samples),
            )
        )
        print(
            "\nNOTE: 'state visible' is measured from the moment the service call "
            "was ISSUED.\nVerification runs AFTER the call has already returned, so the "
            "budget that\nmatters is (visible - call returned) = %.0f ms worst case."
            % max(s - c for s, c in zip(settle_samples, call_samples))
        )


if __name__ == "__main__":
    main()
