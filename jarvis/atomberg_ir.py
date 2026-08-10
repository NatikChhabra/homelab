#!/usr/bin/env python3
"""
Synthesise Atomberg fan infrared codes and send them through the Zoook blaster.

Atomberg Renesa Enzel remotes are plain infrared -- NOT Bluetooth or RF. (An
earlier assumption in this project that they were RF was wrong and cost time;
Atomberg's own FAQ states every fan ships with a conventional IR remote needing
no pairing.) So the existing blaster can drive the fan with no extra hardware.

The protocol is NEC with a fixed 16 bit address of 0xF300, taken from a
published ESPHome config for Atomberg fans:
https://gist.github.com/radiantly/54cea0c55bc4265e10130db4f0e48d15

    off       0x6E91
    speed 1   0x748B
    speed 2   0x6F90
    speed 3   0x758A
    speed 4   0x6C93
    speed 5   0x7788
    boost     0x708F

There is no separate "on" command: turning the fan on means sending whichever
speed you want. That matches the physical remote.

NEC timing (ESPHome defaults, 38 kHz carrier):
    header  9000 us mark, 4500 us space
    bit     560 us mark, then 1690 us space for 1 or 560 us space for 0
    footer  560 us mark
    16 bit address then 16 bit command, LSB first within each field.

Codes go out through tuya-local's `b64:` escape on remote.send_command, which
bypasses its learned-code store entirely -- so nothing has to be captured from
the physical remote first.

IR is fire-and-forget: the fan never reports back, so Home Assistant can only
ever track assumed state. If someone uses the physical remote, HA will be wrong
until the next command.
"""

import base64
import json
import struct
import subprocess
import sys
import urllib.request

HDR_MARK, HDR_SPACE = 9000, 4500
BIT_MARK, ONE_SPACE, ZERO_SPACE = 560, 1690, 560

ADDRESS = 0xF300
COMMANDS = {
    "off": 0x6E91,
    "1": 0x748B,
    "2": 0x6F90,
    "3": 0x758A,
    "4": 0x6C93,
    "5": 0x7788,
    "boost": 0x708F,
}


def nec_pulses(address: int, command: int):
    """Build an NEC pulse train, 16 bit address then 16 bit command, LSB first."""
    pulses = [HDR_MARK, HDR_SPACE]
    for field in (address, command):
        for i in range(16):
            pulses.append(BIT_MARK)
            pulses.append(ONE_SPACE if (field >> i) & 1 else ZERO_SPACE)
    pulses.append(BIT_MARK)
    return pulses


def pulses_to_tuya_b64(pulses):
    """Little-endian uint16 per duration. No compression -- see voltas_ir.py."""
    return base64.b64encode(
        struct.pack("<" + str(len(pulses)) + "H",
                    *(min(p, 0xFFFF) for p in pulses))
    ).decode("ascii")


def ha_token():
    out = subprocess.run(
        ["docker", "exec", "jarvis-voice", "printenv", "JARVIS_HASS_TOKEN"],
        capture_output=True, text=True, timeout=30)
    tok = out.stdout.strip()
    if not tok:
        sys.exit("could not read JARVIS_HASS_TOKEN")
    return tok


def send(code_b64, token, entity="remote.smart_ir"):
    req = urllib.request.Request(
        "http://localhost:8123/api/services/remote/send_command",
        data=json.dumps({"entity_id": entity,
                         "command": [f"b64:{code_b64}"]}).encode(),
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as f:
        return f.read()[:200]


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        sys.exit(f"usage: atomberg_ir.py <{'|'.join(COMMANDS)}> [--dry-run]")
    what = sys.argv[1]
    dry = "--dry-run" in sys.argv

    pulses = nec_pulses(ADDRESS, COMMANDS[what])
    code = pulses_to_tuya_b64(pulses)

    print(f"command : {what}  (NEC addr=0x{ADDRESS:04X} cmd=0x{COMMANDS[what]:04X})")
    print(f"pulses  : {len(pulses)} edges")
    print(f"b64     : {code[:72]}...")
    if dry:
        return
    print("sending...")
    print(send(code, ha_token()))
    print("sent")


if __name__ == "__main__":
    main()
