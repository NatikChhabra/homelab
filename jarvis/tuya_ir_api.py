#!/usr/bin/env python3
"""
Full IR control of the AC/TV/fan by reusing Home Assistant's stored Tuya session.

WHY THIS APPROACH
-----------------
Everything else was tried and ruled out by evidence:

  * LEARNING the AC locally is impossible. A capture of the power button
    returned 51 bits of an 80-bit Voltas frame. Re-decoded including the
    52000us gap (which sits at index 103, the very end -- there is no hidden
    second half), it is still 51 bits. The blaster's study buffer truncates
    anything that long.

  * SUB-DEVICE (cid) access is impossible. Queried with Home Assistant stopped
    so nothing held the socket:
        parent blaster      -> {'dps': {'1': 'study_key'}}   responds
        sub-device 4b83...  -> 904 Unexpected Payload         never responds
    The blaster is not a real gateway; the DIY remotes are cloud-side
    definitions it expands only when the cloud tells it to. tuya-local's
    sub-device form therefore cannot connect with ANY key or protocol.

  * SYNTHESISING Voltas frames failed. 96 candidate frames covering every
    combination of the unverified fields (temp offset, TempSet bit, byte 6,
    byte 8, V-swing, H-swing sentinel) produced no response.

  * HA core's Tuya integration lists the IR remotes as "(unsupported)" -- it
    has no IR remote platform, so AirCon/TV/fan get no entities.

What DOES work is Tuya's cloud IR API, which is what the phone app uses. And
the official Tuya integration persists a full session to
`.storage/core.config_entries` -- endpoint, terminal_id, user_code and
token_info. Unlike tuya-local's session (memory-only, lost on every restart,
which cost several QR scans) this one survives reboots. So: borrow it, and
never ask for a QR again.

Read-only with respect to that entry -- it only reads the token, never writes.
"""

import json
import subprocess
import sys
import time

BLASTER_ID = "853521608cce4edb3d49"
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"

REMOTES = {
    "ac":  "d7f817a1bbcfc953dct8kh",
    "tv":  "d7683c9323ce2e9d3d8uwl",
    "fan": "d7cae672c058f6224azgcp",
    "diy": "d73c40cfe2b3c8b2f8nzj1",
}


def ha_tuya_session():
    """Pull the stored Tuya session out of Home Assistant's config entries."""
    raw = subprocess.run(
        ["docker", "exec", "jarvis-hass", "cat",
         "/config/.storage/core.config_entries"],
        capture_output=True, text=True, timeout=60,
    ).stdout
    entries = json.loads(raw)["data"]["entries"]
    sessions = [e["data"] for e in entries if e["domain"] == "tuya"]
    if not sessions:
        sys.exit("no Tuya integration session found -- add the Tuya "
                 "integration in Home Assistant first")
    # The integration got added twice; either session works, so take the last.
    return sessions[-1]


def manager(sess):
    from tuya_sharing import Manager

    class NullListener:
        def update_token(self, t):
            pass

    return Manager(CLIENT_ID, sess["user_code"], sess["terminal_id"],
                   sess["endpoint"], sess["token_info"], NullListener())


def probe(api, path, method="GET", body=None):
    try:
        r = api.get(path) if method == "GET" else api.post(path, body)
    except Exception as e:
        return None, f"EXC {type(e).__name__}: {str(e)[:100]}"
    if isinstance(r, dict) and r.get("success"):
        return r.get("result"), None
    return None, (r.get("msg") if isinstance(r, dict) else str(r)[:100])


def main():
    sess = ha_tuya_session()
    print(f"using session: endpoint={sess['endpoint']} "
          f"terminal={sess['terminal_id'][:14]}", flush=True)
    api = manager(sess).customer_api

    found = {}
    print("\n--- probing IR endpoints ---", flush=True)
    for name, rid in REMOTES.items():
        for tmpl in (
            f"/v2.0/infrareds/{BLASTER_ID}/remotes/{rid}/keys",
            f"/v1.0/infrareds/{BLASTER_ID}/remotes/{rid}/keys",
            f"/v2.0/infrareds/{BLASTER_ID}/remotes/{rid}/learning-codes",
            f"/v1.0/m/ir/{BLASTER_ID}/remotes/{rid}/keys",
            f"/v1.0/devices/{rid}/functions",
            f"/v1.0/devices/{rid}/specifications",
            f"/v1.1/m/life/{rid}/specifications",
        ):
            res, err = probe(api, tmpl)
            if res is not None:
                print(f"  OK  {name}: {tmpl}", flush=True)
                print(f"      {json.dumps(res)[:600]}", flush=True)
                found.setdefault(name, {})[tmpl] = res
                break
            print(f"  --  {name}: {tmpl}  ({err})", flush=True)

    with open("tuya-ir-api.json", "w") as f:
        json.dump(found, f, indent=2)
    print(f"\nwrote tuya-ir-api.json  ({len(found)} remotes answered)",
          flush=True)


if __name__ == "__main__":
    main()
