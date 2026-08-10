#!/usr/bin/env python3
"""
Synthesise Voltas AC infrared codes and send them through the Zoook blaster.

Why synthesise instead of learn
-------------------------------
`remote.learn_command` needs a human pointing the original remote at the blaster
inside a 30 second window, once per button. A fully featured AC needs power, a
temperature for every degree in its range, each mode, each fan speed and swing —
dozens of captures. Voltas is a documented protocol, so every one of those codes
can be computed instead.

tuya-local accepts a raw code with a `b64:` prefix (see `_extract_codes` in
remote.py), which skips its code store entirely. So a computed code can be sent
with no learning step at all:

    service: remote.send_command
    target: {entity_id: remote.smart_ir}
    data: {command: ["b64:<base64>"]}

Protocol, from IRremoteESP8266 `ir_Voltas.h` / `ir_Voltas.cpp`
(GPL, Copyright 2020 David Conran / manj9501 — consulted for the wire format):

    No header. Bit mark 1026 us for both 0 and 1; a 1 is followed by 2553 us of
    space, a 0 by 554 us. Footer is another 1026 us mark. 38 kHz carrier.
    10 bytes = 80 bits, transmitted MSB first within each byte.
    Checksum byte = (~sum(bytes[0..8])) & 0xFF.

Bit layout. C bitfields pack from the least significant bit up on little-endian,
so field order within each byte below is LSB first:

    byte 0  SwingH:1        SwingHChange:7
    byte 1  Mode:4          unused:1        FanSpeed:3
    byte 2  SwingV:3        Wifi:1  unused:1  Turbo:1  Sleep:1  Power:1
    byte 3  Temp:4          fixed 0b01:2    Econo:1   TempSet:1
    byte 4  OnTimerMins:6   unused:1        OnTimer12Hr:1
    byte 5  OffTimerMins:6  unused:1        OffTimer12Hr:1
    byte 6  fixed 0x3B
    byte 7  OnTimerHrs:4    OffTimerHrs:4
    byte 8  unused:5        Light:1  OffTimerEnable:1  OnTimerEnable:1
    byte 9  Checksum:8

UNVERIFIED against the physical unit until a code visibly works:
  - Temp encoding is taken as (degrees - 16); the field is 4 bits and the
    documented range is 16-30 C, which fits exactly.
  - The 2 bits in byte 3 documented as "typically 0b01" are set to 0b01.
  - Byte 6 is set to its documented typical value 0x3B.
  - This unit has no horizontal swing, so SwingHChange uses the documented
    kVoltasSwingHNoChange sentinel 0b0011001.
Treat a non-responding AC as a reason to re-check these, not as a dead end.
"""

import base64
import json
import os
import struct
import subprocess
import sys
import urllib.request

# --- protocol constants ------------------------------------------------------
BIT_MARK = 1026
ONE_SPACE = 2553
ZERO_SPACE = 554
FREQ = 38000
STATE_LEN = 10

MODE_FAN, MODE_HEAT, MODE_DRY, MODE_COOL = 0b0001, 0b0010, 0b0100, 0b1000
FAN_HIGH, FAN_MED, FAN_LOW, FAN_AUTO = 0b001, 0b010, 0b100, 0b111
MIN_TEMP, MAX_TEMP = 16, 30
SWINGH_NOCHANGE = 0b0011001

MODES = {"cool": MODE_COOL, "heat": MODE_HEAT, "dry": MODE_DRY, "fan": MODE_FAN}
FANS = {"auto": FAN_AUTO, "low": FAN_LOW, "medium": FAN_MED, "high": FAN_HIGH}


def build_state(power=True, mode="cool", temp=24, fan="auto",
                swing_v=True, turbo=False, sleep=False, econo=False,
                light=True):
    """Build the 10 byte Voltas state."""
    if not MIN_TEMP <= temp <= MAX_TEMP:
        raise ValueError(f"temp {temp} outside {MIN_TEMP}-{MAX_TEMP}")

    b = [0] * STATE_LEN

    # byte 0 -- no horizontal swing on this unit
    b[0] = (SWINGH_NOCHANGE << 1) & 0xFF

    # byte 1
    b[1] = (MODES[mode] & 0x0F) | ((FANS[fan] & 0x07) << 5)

    # byte 2
    swing_v_bits = 0b111 if swing_v else 0b000
    b[2] = ((swing_v_bits & 0x07)
            | (0 << 3)                      # Wifi
            | ((1 if turbo else 0) << 5)
            | ((1 if sleep else 0) << 6)
            | ((1 if power else 0) << 7))

    # byte 3 -- bits 4-5 are documented as "typically 0b01"
    b[3] = (((temp - MIN_TEMP) & 0x0F)
            | (0b01 << 4)
            | ((1 if econo else 0) << 6)
            | (1 << 7))                     # TempSet

    # bytes 4,5,7,8 -- timers off
    b[6] = 0x3B
    b[8] = ((1 if light else 0) << 5)

    b[9] = (~sum(b[:9])) & 0xFF
    return bytes(b)


def state_to_pulses(state):
    """Turn the state into a microsecond pulse train, MSB first, no header."""
    pulses = []
    for byte in state:
        for i in range(7, -1, -1):
            pulses.append(BIT_MARK)
            pulses.append(ONE_SPACE if (byte >> i) & 1 else ZERO_SPACE)
    pulses.append(BIT_MARK)           # footer mark
    return pulses


def pulses_to_tuya_b64(pulses):
    """Encode a pulse train in the Tuya IR format.

    Each duration is a little-endian uint16 and that is the whole format --
    there is NO compression. This mirrors tinytuya's own encoder exactly:

        fmt = '<' + str(len(pulses)) + 'H'
        return base64.b64encode(struct.pack(fmt, *pulses)).decode('ascii')

    An earlier version of this function wrapped the data in FastLZ-style
    literal-run headers (a length byte before every 32 byte chunk). The blaster
    accepted the payload and HA reported success, but the AC ignored it,
    because every inserted header byte shifted the timing stream by one byte
    and turned it into noise. A silent appliance is the only symptom, so do not
    "improve" this by adding compression.
    """
    return base64.b64encode(
        struct.pack("<" + str(len(pulses)) + "H",
                    *(min(p, 0xFFFF) for p in pulses))
    ).decode("ascii")


# --- sending -----------------------------------------------------------------
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
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--power", default="on", choices=["on", "off"])
    ap.add_argument("--mode", default="cool", choices=list(MODES))
    ap.add_argument("--temp", type=int, default=24)
    ap.add_argument("--fan", default="auto", choices=list(FANS))
    ap.add_argument("--swing", default="on", choices=["on", "off"])
    ap.add_argument("--turbo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    state = build_state(power=(a.power == "on"), mode=a.mode, temp=a.temp,
                        fan=a.fan, swing_v=(a.swing == "on"), turbo=a.turbo)
    pulses = state_to_pulses(state)
    code = pulses_to_tuya_b64(pulses)

    print("state  :", state.hex(" "))
    print("checksum ok:", state[9] == ((~sum(state[:9])) & 0xFF))
    print("pulses :", len(pulses), "edges")
    print("b64    :", code[:80] + ("..." if len(code) > 80 else ""))

    if a.dry_run:
        return
    print("sending...")
    print(send(code, ha_token()))
    print("sent")


if __name__ == "__main__":
    main()
