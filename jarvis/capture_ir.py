#!/usr/bin/env python3
"""
Capture several IR buttons in one sitting from the Zoook Smart IR blaster.

WHY A DEDICATED SCRIPT
----------------------
This blaster is a control_type=2 device: it sends on DP 1 and reports learned
codes on DP 2. tuya-local matched it to `s11_rfir_remote`, which uses DP 201/202
for both -- so sends were silently discarded and learns never saw anything. See
ir_blaster.py for the full write-up.

tinytuya's receive_button() also cannot be used: it probes DP 201 first and dies
with error 904 ("Unexpected Payload") on this hardware. Study mode is driven
manually here instead.

THE STALE-CODE TRAP
-------------------
DP 2 RETAINS the last learned code. Idle it reads 'AAAAAAAAAAAA', but after a
capture it keeps returning the previous button forever. Polling naively saves
that same code under every later name, producing a remote where every button does
the same thing and nothing looks wrong. Every code seen is therefore recorded in
`seen`, and a repeat is refused rather than saved.

ONE CONNECTION AT A TIME
------------------------
Tuya devices allow a single local connection, and Home Assistant holds it. Stop
jarvis-hass before running this or every read comes back garbled.
"""

import json
import os
import sys
import time

import tinytuya

DEVICE_ID = "853521608cce4edb3d49"
DEVICE_IP = "192.168.29.236"
LOCAL_KEY = ""
VERSION = 3.3


def _load_secrets():
    """Read secrets.env from this directory or the parent stack directory.

    The local key used to be hardcoded here. It is a credential, this repo is
    staged for public release, and CLAUDE.md forbids hardcoding one -- they get
    swept into the nightly backup. Values are never logged.
    """
    global DEVICE_ID, DEVICE_IP, LOCAL_KEY, VERSION
    here = os.path.dirname(os.path.abspath(__file__))
    env = {}
    for path in (os.path.join(here, "secrets.env"),
                 os.path.join(os.path.dirname(here), "secrets.env")):
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        env.setdefault(k.strip(), v.strip())
        except OSError:
            continue
    DEVICE_ID = env.get("TUYA_SMART_IR_DEVICE_ID", DEVICE_ID)
    DEVICE_IP = env.get("TUYA_SMART_IR_HOST", DEVICE_IP)
    LOCAL_KEY = env.get("TUYA_SMART_IR_LOCAL_KEY", "")
    VERSION = float(env.get("TUYA_SMART_IR_PROTOCOL", VERSION))
    if not LOCAL_KEY:
        sys.exit("TUYA_SMART_IR_LOCAL_KEY not found in secrets.env -- cannot "
                 "reach the blaster. Check Stacks/secrets.env or "
                 "Stacks/jarvis/secrets.env.")


_load_secrets()

CODES_FILE = "/work/ir-codes.json"

DP_MODE = "1"     # study / study_exit / send_ir
DP_CODE = "2"     # learned code appears here
IDLE = {"AAAAAAAAAAAA", "study", "study_exit", "send_ir", "study_key"}


def load():
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE) as f:
            return json.load(f)
    return {}


def save(codes):
    with open(CODES_FILE, "w") as f:
        json.dump(codes, f, indent=2, sort_keys=True)


def capture(dev, name, seen, timeout=55):
    """Open study mode and wait for one genuinely new code."""
    dev.set_value(DP_MODE, "study_exit", nowait=True)
    time.sleep(0.8)
    dev.set_value(DP_MODE, "study", nowait=False)

    print(f"\n{'=' * 58}", flush=True)
    print(f">>> PRESS:  {name}      ({timeout}s)", flush=True)
    print(f"{'=' * 58}", flush=True)

    end = time.time() + timeout
    nudge = time.time()
    while time.time() < end:
        try:
            msg = dev.receive()
        except Exception:
            msg = None
        if isinstance(msg, dict) and "dps" in msg:
            code = msg["dps"].get(DP_CODE)
            if isinstance(code, str) and len(code) > 20 and code not in IDLE:
                if code in seen:
                    # Stale DP 2 value, not a fresh press. Keep waiting.
                    pass
                else:
                    seen.add(code)
                    print(f"    CAPTURED {name}: {len(code)} chars", flush=True)
                    dev.set_value(DP_MODE, "study_exit", nowait=True)
                    return code
        if time.time() - nudge > 18:
            nudge = time.time()
            dev.set_value(DP_MODE, "study", nowait=True)
            print(f"    ...{int(end - time.time())}s left", flush=True)

    dev.set_value(DP_MODE, "study_exit", nowait=True)
    print(f"    MISSED {name} -- rerun for this one later", flush=True)
    return None


def main():
    names = sys.argv[1:]
    if not names:
        sys.exit("usage: capture_ir.py <name> [name...]")

    dev = tinytuya.Device(DEVICE_ID, DEVICE_IP, LOCAL_KEY)
    dev.set_version(VERSION)
    dev.set_socketTimeout(3)

    codes = load()
    seen = set(codes.values())
    got, missed = [], []

    for name in names:
        code = capture(dev, name, seen)
        if code:
            codes[name] = code
            save(codes)          # save as we go; a later miss loses nothing
            got.append(name)
        else:
            missed.append(name)
        time.sleep(1.5)

    print(f"\n{'=' * 58}", flush=True)
    print(f"captured: {got}", flush=True)
    print(f"missed  : {missed}", flush=True)
    print(f"total stored: {len(codes)}", flush=True)


if __name__ == "__main__":
    main()
