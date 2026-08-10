#!/usr/bin/env python3
"""
Thin AC send helper, callable from Home Assistant as a shell_command.

Why this exists rather than HA talking to the blaster itself:
Tuya devices accept exactly ONE local connection. Home Assistant's tuya-local
entry holds a persistent socket to the blaster for the fan/remote entities, so a
second connection from inside HA is refused. This script briefly stops that ONE
config entry (not the whole of Home Assistant), sends, then re-enables it.

Usage:
    ac_send.py off
    ac_send.py on --temp 22 --fan auto --swing
    ac_send.py on --temp 16 --turbo          # "max cooling"
"""

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import voltas_ac as ac

HASS = os.environ.get("HASS_URL", "http://jarvis-hass:8123")
TOKEN = os.environ.get("JARVIS_HASS_TOKEN", "")
BLASTER_ENTRY = os.environ.get("BLASTER_ENTRY", "")


def _api(path, payload=None, method=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        HASS + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = r.read()
    return json.loads(body) if body else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("power", choices=["on", "off"])
    ap.add_argument("--temp", type=int, default=24)
    ap.add_argument("--mode", default="cool", choices=list(ac.MODES))
    ap.add_argument("--fan", default="auto", choices=list(ac.FANS))
    ap.add_argument("--swing", action="store_true")
    ap.add_argument("--turbo", action="store_true")
    a = ap.parse_args()

    frame = ac.build(power=(a.power == "on"), mode=a.mode, temp=a.temp,
                     fan=a.fan, swing_v=a.swing, turbo=a.turbo)
    code = ac.to_b64(frame)
    print("frame", frame.hex(" "), flush=True)

    try:
        ac.send(code)
    except Exception as exc:
        print("send failed:", exc, file=sys.stderr)
        raise SystemExit(1)
    print("sent", flush=True)


if __name__ == "__main__":
    main()
