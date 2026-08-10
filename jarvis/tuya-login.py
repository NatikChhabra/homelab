#!/usr/bin/env python3
"""
Tuya cloud QR login, one token, no regeneration.

Split out from tuya-add-subdevices.py after a real failure: that script minted a
fresh QR token every 150 s and wrote it over the SAME png path. If the user had
the image already open, they scanned a token the script had already retired, and
Tuya answered every poll with

    E0020003  Login failed, please scan and try again!

which reads exactly like "you have not scanned yet" -- indistinguishable from the
real not-yet-scanned case. One token, held until it genuinely expires, removes
that whole class of confusion. If it does expire, rerun this and scan the new
file; do not silently swap the token underneath a displayed image.

Usage:
    python tuya-login.py <USER_CODE>

Writes the QR to tuya-login-qr.png with a timestamp printed, polls until Tuya
accepts the scan, then prints the flow id parked at choose_device.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HA = "http://localhost:8123"
HERE = os.path.dirname(os.path.abspath(__file__))


def ha_token() -> str:
    out = subprocess.run(
        ["docker", "exec", "jarvis-voice", "printenv", "JARVIS_HASS_TOKEN"],
        capture_output=True, text=True, timeout=30,
    )
    tok = out.stdout.strip()
    if not tok:
        sys.exit("Could not read JARVIS_HASS_TOKEN from jarvis-voice.")
    return tok


TOKEN = ha_token()


def call(path, payload=None, timeout=120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        HA + path, data=data,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.loads(f.read() or b"null")


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    user_code = sys.argv[1].strip()

    flow = call("/api/config/config_entries/flow",
                {"handler": "tuya_local", "show_advanced_options": True})
    fid = flow["flow_id"]
    r = call(f"/api/config/config_entries/flow/{fid}", {"setup_mode": "cloud"})

    if r.get("step_id") == "choose_device":
        print("Already authenticated.")
        print("FLOWID", fid)
        return

    if r.get("step_id") == "cloud":
        r = call(f"/api/config/config_entries/flow/{fid}",
                 {"user_code": user_code})
        if r.get("step_id") == "cloud":
            sys.exit("User code rejected: "
                     + json.dumps(r.get("description_placeholders")))

    if r.get("step_id") != "scan":
        sys.exit(f"Unexpected step {r.get('step_id')!r}: "
                 + json.dumps(r)[:800])

    token = None
    for field in r.get("data_schema") or []:
        data = field.get("selector", {}).get("qr_code", {}).get("data", "")
        if data.startswith("tuyaSmart--qrLogin?token="):
            token = data.split("token=", 1)[1]
    if not token:
        sys.exit("No QR token in scan step: " + json.dumps(r)[:800])

    import segno
    out = os.path.join(HERE, "tuya-login-qr.png")
    segno.make(f"tuyaSmart--qrLogin?token={token}", error="q").save(
        out, scale=8, border=4)

    print(f"QR written {time.strftime('%H:%M:%S')} -> {out}")
    print(f"token tail ...{token[-12:]}")
    print("Scan in SmartLife: Me tab > scan icon (top right).")
    print("This token is NOT regenerated. Polling...", flush=True)

    deadline = time.time() + 900
    last = ""
    while time.time() < deadline:
        time.sleep(6)
        r = call(f"/api/config/config_entries/flow/{fid}", {})
        step = r.get("step_id")
        if step != "scan":
            print(f"LOGGED IN at {time.strftime('%H:%M:%S')}", flush=True)
            print("step:", step)
            print("FLOWID", fid)
            return
        msg = json.dumps(r.get("description_placeholders") or {})
        if msg != last:
            print(f"  {time.strftime('%H:%M:%S')} {msg[:160]}", flush=True)
            last = msg

    sys.exit("Token expired without a successful scan. Rerun for a new one.")


if __name__ == "__main__":
    main()
