#!/usr/bin/env python3
"""
Talk to the Zoook Smart IR blaster directly, using the DP scheme it actually has.

WHY THIS EXISTS
---------------
Home Assistant's tuya-local matched this blaster to the `s11_rfir_remote`
profile, which sends codes to DP 201 and reads learned codes from DP 202. This
blaster has NEITHER. Queried with Home Assistant stopped, it reports exactly one
data point:

    v3.3: {'devId': '853521608cce4edb3d49', 'dps': {'1': 'send_ir'}}

It is a "control_type=2" device: commands go to DPs 1-13, not 201/202. Writes to
DP 201 were accepted and silently discarded, so every send looked successful in
the logs while no infrared was ever emitted, and every learn window timed out
because nothing ever appears on DP 202. That combination -- success in the log,
silence in the room -- is what made this take so long to find.

Verified working: NEC codes sent through control_type=2 at protocol 3.3 turned
the Atomberg fan on.

ONE CONNECTION AT A TIME
------------------------
Tuya devices accept a single local connection. Home Assistant's tuya-local entry
holds it, so this script cannot talk to the blaster while that entry is loaded --
symptoms are error 904 ("Unexpected Payload") or garbled status. Stop the
container, or remove the Smart IR entry from tuya-local, before using this.

    docker stop jarvis-hass && ... && docker start jarvis-hass
"""

import base64
import json
import os
import struct
import sys
import time

DEVICE_ID = os.environ.get("TUYA_SMART_IR_DEVICE_ID") or "853521608cce4edb3d49"
DEVICE_IP = os.environ.get("TUYA_SMART_IR_HOST") or "192.168.29.236"
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

CODES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "ir-codes.json")

# NEC timing, 38 kHz carrier. Used to synthesise codes for devices whose
# protocol is already known (the Atomberg fan). Anything unknown gets learned
# from the physical remote instead of guessed -- see learn().
HDR_MARK, HDR_SPACE = 9000, 4500
BIT_MARK, ONE_SPACE, ZERO_SPACE = 560, 1690, 560


def nec_pulses(address, command):
    pulses = [HDR_MARK, HDR_SPACE]
    for field in (address, command):
        for i in range(16):
            pulses.append(BIT_MARK)
            pulses.append(ONE_SPACE if (field >> i) & 1 else ZERO_SPACE)
    pulses.append(BIT_MARK)
    return pulses


def pulses_to_b64(pulses):
    """Little-endian uint16 per duration. No compression -- matches tinytuya."""
    return base64.b64encode(
        struct.pack("<" + str(len(pulses)) + "H",
                    *(min(p, 0xFFFF) for p in pulses))
    ).decode("ascii")


def b64_to_pulses(code):
    raw = base64.b64decode(code)
    return list(struct.unpack("<" + str(len(raw) // 2) + "H", raw))


def connect():
    from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice
    ir = IRRemoteControlDevice(DEVICE_ID, DEVICE_IP, LOCAL_KEY, version=VERSION)
    ir.set_socketTimeout(6)
    # detect_control_type() fails on this device because it probes DP 201 first,
    # so set it explicitly. Determined empirically, not guessed.
    ir.control_type = 2
    return ir


def load_codes():
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE) as f:
            return json.load(f)
    return {}


def save_codes(codes):
    with open(CODES_FILE, "w") as f:
        json.dump(codes, f, indent=2, sort_keys=True)


def send(code_b64, repeat=1, gap=0.4):
    ir = connect()
    for _ in range(repeat):
        ir.send_button(code_b64)
        if repeat > 1:
            time.sleep(gap)


def learn(name, timeout=25):
    """Capture one button from a physical remote and store it under `name`."""
    ir = connect()
    print(f"\n>>> PRESS '{name}' ON THE REMOTE NOW  ({timeout}s window)",
          flush=True)
    code = ir.receive_button(timeout=timeout)
    if not code or not isinstance(code, str):
        print(f"    no code captured for {name}  ({code!r})", flush=True)
        return None
    codes = load_codes()
    codes[name] = code
    save_codes(codes)
    pulses = b64_to_pulses(code)
    print(f"    captured {name}: {len(pulses)} edges, {len(code)} b64 chars",
          flush=True)
    return code


FAN_NEC = {"off": 0x6E91, "1": 0x748B, "2": 0x6F90, "3": 0x758A,
           "4": 0x6C93, "5": 0x7788, "boost": 0x708F}


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: ir_blaster.py fan <off|1-5|boost> | learn <name>... "
                 "| send <name> | list | status")
    cmd = sys.argv[1]

    if cmd == "status":
        import tinytuya
        d = tinytuya.Device(DEVICE_ID, DEVICE_IP, LOCAL_KEY)
        d.set_version(VERSION); d.set_socketTimeout(6)
        print(d.status())

    elif cmd == "fan":
        what = sys.argv[2]
        if what not in FAN_NEC:
            sys.exit(f"fan: one of {list(FAN_NEC)}")
        code = pulses_to_b64(nec_pulses(0xF300, FAN_NEC[what]))
        send(code)
        print(f"fan {what} sent")

    elif cmd == "learn":
        for name in sys.argv[2:]:
            learn(name)
            time.sleep(1)

    elif cmd == "send":
        codes = load_codes()
        name = sys.argv[2]
        if name not in codes:
            sys.exit(f"unknown code {name!r}; have: {sorted(codes)}")
        send(codes[name])
        print(f"sent {name}")

    elif cmd == "list":
        for k, v in sorted(load_codes().items()):
            print(f"  {k:22} {len(b64_to_pulses(v)):4} edges")

    else:
        sys.exit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main()
