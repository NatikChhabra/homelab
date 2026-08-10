#!/usr/bin/env python3
"""
Full IR control of the AC, TV and fan via Tuya's cloud API.

WHY THE CLOUD, AND WHY THIS IS NOT A COMPROMISE
-----------------------------------------------
Local control of these remotes is impossible on this hardware, established by
test rather than assumption:

  * The AC frame cannot be LEARNED. A capture of the power button gave 51 bits
    of an 80-bit Voltas frame. Re-decoded including the 52000us gap -- which
    sits at edge index 103, the very end, so there is no hidden second half --
    it is still 51 bits. The blaster's study buffer truncates frames this long.

  * SUB-DEVICE (cid) access does not work. Probed with Home Assistant stopped
    so nothing else held the socket:
        parent blaster     -> {'dps': {'1': 'study_key'}}  responds
        sub-device 4b83... -> 904 Unexpected Payload        never responds
    The blaster is not a real gateway. The DIY remotes are cloud-side
    definitions it expands only when told to, so tuya-local's sub-device form
    cannot connect with ANY key or protocol version.

  * SYNTHESISED Voltas frames do not work. 96 candidates covering every
    combination of the unverified fields produced no response from the unit.

  * HA core's Tuya integration marks the IR remotes "(unsupported)" -- it has
    no IR remote platform, so AirCon/TV/fan get no entities of their own.

What works is the API the phone app itself uses. The AC exposes real commands
(PowerOn/PowerOff, T 16-30, M 0-4, F 0-3) AND readable status (power, temp,
mode, wind), so JARVIS can know the AC's state rather than just firing at it.
That is strictly more capable than the scene-per-preset approach.

SESSION
-------
Borrowed read-only from the official Tuya integration's config entry. Unlike
tuya-local's session -- memory-only, discarded on every restart, which cost
several QR scans -- this one is persisted to disk and survives reboots. No QR
scan is ever needed again.

The fan is deliberately NOT driven through here: it works locally over NEC
(ir_blaster.py), which is faster and needs no internet.
"""

import json
import subprocess
import sys

CLIENT_ID = "HA_3y9q4ak7g4ephrvke"

AC = "d7f817a1bbcfc953dct8kh"
TV = "d7683c9323ce2e9d3d8uwl"

# Verified against /v1.1/m/life/<id>/specifications
AC_MODES = {"cool": 0, "heat": 1, "auto": 2, "fan": 3, "dry": 4}
AC_FAN = {"auto": 0, "low": 1, "medium": 2, "high": 3}


def session():
    raw = subprocess.run(
        ["docker", "exec", "jarvis-hass", "cat",
         "/config/.storage/core.config_entries"],
        capture_output=True, text=True, timeout=60).stdout
    entries = json.loads(raw)["data"]["entries"]
    s = [e["data"] for e in entries if e["domain"] == "tuya"]
    if not s:
        sys.exit("no Tuya session; add the Tuya integration in HA first")
    return s[-1]


def api():
    from tuya_sharing import Manager

    class NullListener:
        def update_token(self, t):
            pass

    s = session()
    return Manager(CLIENT_ID, s["user_code"], s["terminal_id"], s["endpoint"],
                   s["token_info"], NullListener()).customer_api


def send(device_id, code, value):
    a = api()
    r = a.post(f"/v1.1/m/thing/{device_id}/commands",
               {"commands": [{"code": code, "value": value}]})
    ok = isinstance(r, dict) and r.get("success")
    return ok, r


def status(device_id):
    a = api()
    r = a.get(f"/v1.0/m/life/devices/{device_id}/status")
    return r.get("result") if isinstance(r, dict) and r.get("success") else r


def main():
    if len(sys.argv) < 2:
        sys.exit(
            "usage:\n"
            "  ac on|off\n"
            "  ac temp <16-30>\n"
            "  ac mode <cool|heat|auto|fan|dry>\n"
            "  ac fan <auto|low|medium|high>\n"
            "  ac status\n"
            "  tv <Power|Volume+|Volume-|Channel+|Home|Menu|OK|Up|Down|Left|Right|Back|0-9>\n"
        )

    what = sys.argv[1]

    if what == "ac":
        sub = sys.argv[2]
        if sub == "status":
            print(json.dumps(status(AC), indent=2))
            return
        if sub == "on":
            ok, r = send(AC, "PowerOn", "PowerOn")
        elif sub == "off":
            ok, r = send(AC, "PowerOff", "PowerOff")
        elif sub == "temp":
            ok, r = send(AC, "T", int(sys.argv[3]))
        elif sub == "mode":
            ok, r = send(AC, "M", AC_MODES[sys.argv[3]])
        elif sub == "fan":
            ok, r = send(AC, "F", AC_FAN[sys.argv[3]])
        else:
            sys.exit(f"unknown ac command {sub!r}")
        print(("OK " if ok else "FAILED ") + json.dumps(r)[:200])

    elif what == "tv":
        key = sys.argv[2]
        ok, r = send(TV, key, key)
        print(("OK " if ok else "FAILED ") + json.dumps(r)[:200])

    else:
        sys.exit(f"unknown target {what!r}")


if __name__ == "__main__":
    main()
