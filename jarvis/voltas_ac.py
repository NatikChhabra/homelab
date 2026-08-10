#!/usr/bin/env python3
"""
Voltas AC control over the Zoook blaster -- fully local, no cloud.

VERIFIED WORKING 2026-08-08: the OFF frame below physically turned the AC off
when Tuya's cloud scene had stopped delivering. This is the only AC path that
does not depend on the internet.

WHY THE FRAMES ARE SYNTHESISED RATHER THAN LEARNED
The blaster cannot learn an AC frame. Capturing the power button returned 104
edges / 51 bits; a Voltas frame is 161 edges / 80 bits. The trailing 52000us
gap sits at edge index 103 -- the very end -- so nothing was cut mid-frame; the
study buffer is simply too small. Any "learned" AC code in ir-codes.json is
truncated and will never work.

So the frames are built from the protocol spec (IRremoteESP8266 ir_Voltas.h /
ir_Voltas.cpp) instead:

    timing   no header; bit mark 1026us; 1 = 2553us space, 0 = 554us space;
             footer mark 1026us; 38 kHz; 10 bytes = 80 bits, MSB first
    checksum byte 9 = (~sum(bytes[0..8])) & 0xFF

An earlier attempt failed because four bytes were wrong -- zeros where the
reference has 3B 3B in the timer bytes, 0x00 instead of 0x11 in byte 7, and a
stray TempSet bit:

    wrong:  32 e8 87 98 00 00 3b 00 20 6b
    right:  33 e8 87 18 3b 3b 3b 11 00 83

96 brute-forced variants could never have found that, because they never varied
those fields. Do not "simplify" the constants below.

BYTE LAYOUT (C bitfields pack LSB-first within each byte)
    byte 0  SwingH:1  SwingHChange:7      0x33 = no horizontal swing
    byte 1  Mode:4  unused:1  FanSpeed:3
    byte 2  SwingV:3  Wifi:1  _:1  Turbo:1  Sleep:1  Power:1
    byte 3  Temp:4  fixed 0b01:2  Econo:1  TempSet:1
    byte 4  OnTimerMins:6  _:1  OnTimer12Hr:1     0x3B when unused
    byte 5  OffTimerMins:6  _:1  OffTimer12Hr:1   0x3B when unused
    byte 6  fixed 0x3B
    byte 7  OnTimerHrs:4  OffTimerHrs:4           0x11 when unused
    byte 8  _:5  Light:1  OffTimerEnable:1  OnTimerEnable:1
    byte 9  checksum

Temperature is encoded as (degrees - 16) in the low nibble of byte 3, giving the
documented 16-30C range.

TRANSPORT
The blaster is a control_type=2 device (DP 1-13, no DP 201/202). A raw base64
code goes out with DP 1 = "study_key" and the code on DP 7; sending it with
"send_ir" is accepted and does nothing. tinytuya's send_button() handles this.
Tuya allows ONE local connection, so Home Assistant must be stopped first --
its tuya-local entry holds the socket.
"""

import base64
import os
import struct
import subprocess
import sys
import time


def _load_secrets():
    """Read secrets.env from this directory or the parent stack directory.

    The local key used to be hardcoded here. It is a credential, this repo is
    staged for public release, and CLAUDE.md forbids hardcoding one -- they get
    swept into the nightly backup. Values are never logged.
    """
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
    return env


_ENV = _load_secrets()

DEVICE_ID = _ENV.get("TUYA_SMART_IR_DEVICE_ID", "853521608cce4edb3d49")
DEVICE_IP = _ENV.get("TUYA_SMART_IR_HOST", "192.168.29.236")
LOCAL_KEY = _ENV.get("TUYA_SMART_IR_LOCAL_KEY", "")
VERSION = float(_ENV.get("TUYA_SMART_IR_PROTOCOL", "3.3"))

if not LOCAL_KEY:
    sys.exit("TUYA_SMART_IR_LOCAL_KEY not found in secrets.env -- cannot reach "
             "the blaster. Check Stacks/secrets.env or Stacks/jarvis/secrets.env.")

BIT_MARK, ONE_SPACE, ZERO_SPACE = 1026, 2553, 554

MODES = {"cool": 0b1000, "heat": 0b0010, "dry": 0b0100, "fan": 0b0001}
FANS = {"high": 0b001, "medium": 0b010, "low": 0b100, "auto": 0b111}


def build(power=True, mode="cool", temp=24, fan="auto",
          swing_v=True, turbo=False, sleep=False, econo=False, light=False,
          wifi=True):
    """Build a 10-byte Voltas frame.

    wifi: byte 2 bit 3. Every frame captured from a real remote has it SET, and
    it is the only difference between this builder and the reference turbo/dry
    states (0xa0 vs 0xa8, 0x80 vs 0x88). The AC accepts frames either way -- the
    OFF frame that physically worked here had it clear -- so it looks like a
    status flag the unit reports rather than one it acts on. Defaulted to 1 to
    match real remote traffic exactly.
    """
    if not 16 <= temp <= 30:
        raise ValueError(f"temp {temp} outside 16-30")
    b = [0] * 10
    b[0] = 0x33                                   # no horizontal swing
    b[1] = (MODES[mode] & 0x0F) | ((FANS[fan] & 0x07) << 5)
    b[2] = (((0b111 if swing_v else 0) & 0x07)
            | ((1 if wifi else 0) << 3)
            | ((1 if turbo else 0) << 5)
            | ((1 if sleep else 0) << 6)
            | ((1 if power else 0) << 7))
    # TempSet (bit 7) and Light (byte 8 bit 5) are BOTH 0 in every reference
    # state and in the frame that physically worked. Setting them produced
    # 0x98 / 0x20 instead of 0x18 / 0x00 and the AC ignored it. Leave them off.
    b[3] = (((temp - 16) & 0x0F) | (0b01 << 4)
            | ((1 if econo else 0) << 6)
            | ((1 if False else 0) << 7))
    b[4] = 0x3B                                   # timers unused
    b[5] = 0x3B
    b[6] = 0x3B                                   # fixed
    b[7] = 0x11                                   # timer hours unused
    b[8] = (1 if light else 0) << 5
    b[9] = (~sum(b[:9])) & 0xFF
    return bytes(b)


def to_b64(state):
    p = []
    for byte in state:
        for i in range(7, -1, -1):
            p.append(BIT_MARK)
            p.append(ONE_SPACE if (byte >> i) & 1 else ZERO_SPACE)
    p.append(BIT_MARK)
    return base64.b64encode(
        struct.pack("<" + str(len(p)) + "H", *p)).decode("ascii")


def send(code_b64, repeat=2, gap=1.5):
    from tinytuya.Contrib.IRRemoteControlDevice import IRRemoteControlDevice
    ir = IRRemoteControlDevice(DEVICE_ID, DEVICE_IP, LOCAL_KEY, version=VERSION)
    ir.set_socketTimeout(6)
    ir.control_type = 2          # detect_control_type() probes DP 201 and fails
    for i in range(repeat):
        ir.send_button(code_b64)
        if i + 1 < repeat:
            time.sleep(gap)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Local Voltas AC control")
    ap.add_argument("power", choices=["on", "off"])
    ap.add_argument("--temp", type=int, default=24)
    ap.add_argument("--mode", default="cool", choices=list(MODES))
    ap.add_argument("--fan", default="auto", choices=list(FANS))
    ap.add_argument("--no-swing", action="store_true")
    ap.add_argument("--turbo", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    st = build(power=(a.power == "on"), mode=a.mode, temp=a.temp, fan=a.fan,
               swing_v=not a.no_swing, turbo=a.turbo)
    print("frame   :", st.hex(" "))
    print("checksum:", "ok" if st[9] == ((~sum(st[:9])) & 0xFF) else "BAD")
    if a.dry_run:
        print("b64     :", to_b64(st))
        return
    send(to_b64(st))
    print(f"sent    : power={a.power} {a.temp}C {a.mode} fan={a.fan}")


if __name__ == "__main__":
    main()
