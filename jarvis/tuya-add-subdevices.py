#!/usr/bin/env python3
"""
Add Tuya DIY sub-devices (AC, TV, fan) to Home Assistant via tuya-local.

Why this script exists
----------------------
DIY remotes on a Tuya IR blaster are NOT independent devices. They have no IP of
their own, so tuya-local's config flow refuses them with `needs_hub` until you
name the gateway they hang off. The gateway is the blaster itself -- its Tuya
category is `wnykq`, which IS in tuya-local's HUB_CATEGORIES, so it does show up
in the hub dropdown.

Reaching that dropdown requires a Tuya cloud login, which is QR-based: you hand
the flow a User Code from the SmartLife app, it returns a token, you scan the
token as a QR in the app, and only then can it list your devices. There is no
password path.

Doing that in a browser means navigating five UI screens. This drives the same
REST API the UI drives, and renders the QR as ASCII in the terminal so the whole
thing happens in one place.

Usage
-----
    python tuya-add-subdevices.py <USER_CODE>

Get the User Code from: SmartLife app > Settings > Account and Security > User
Code. It is case-sensitive.

The QR token is single-use and expires in minutes, so scan promptly. If it
expires, just run the script again.

What it does NOT do
-------------------
It does not fetch the IR codes for each button. Tuya keeps the learned-remote
database cloud-side and does not expose it locally, so after a sub-device is
added its buttons still need capturing with `remote.learn_command` against the
physical remote. Adding the sub-device gives you the entity; learning gives you
the codes.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HA = "http://localhost:8123"

# Long-lived HA token. Read from the running container rather than duplicated
# here -- the repo rule is that no credential is ever hardcoded in a script,
# because scripts get swept into the nightly backup.


def ha_token() -> str:
    out = subprocess.run(
        ["docker", "exec", "jarvis-voice", "printenv", "JARVIS_HASS_TOKEN"],
        capture_output=True, text=True, timeout=30,
    )
    tok = out.stdout.strip()
    if not tok:
        sys.exit("Could not read JARVIS_HASS_TOKEN from the jarvis-voice container.")
    return tok


TOKEN = ha_token()


def call(path: str, payload=None, timeout: int = 120):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        HA + path, data=data,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.loads(f.read() or b"null")


def show_qr(token: str, ascii_art: bool = True) -> str:
    """Render the login QR.

    The payload format is tuya-local's own, from async_step_scan:
        tuyaSmart--qrLogin?token=<token>
    Anything else will scan but the app will reject it.

    Written to PNG rather than drawn in the terminal: the compact terminal
    renderer emits half-block characters, and the Windows console defaults to
    cp1252, which cannot encode them -- it dies with UnicodeEncodeError before
    printing anything. A file also survives scrollback and can be reopened if
    the first scan attempt misses.
    """
    import segno
    qr = segno.make(f"tuyaSmart--qrLogin?token={token}", error="q")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "tuya-login-qr.png")
    qr.save(out, scale=8, border=4)
    print(f"\nQR written to: {out}")

    # ASCII fallback, pure 7-bit so no console codepage can reject it.
    if ascii_art:
        try:
            matrix = qr.matrix
            print()
            for row in matrix:
                print("".join("##" if px else "  " for px in row))
            print()
        except Exception:
            pass
    return out


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    user_code = sys.argv[1].strip()

    print("Starting tuya_local config flow...")
    flow = call("/api/config/config_entries/flow",
                {"handler": "tuya_local", "show_advanced_options": True})
    fid = flow["flow_id"]

    r = call(f"/api/config/config_entries/flow/{fid}", {"setup_mode": "cloud"})
    step = r.get("step_id")

    # If a previous run already authenticated, the flow skips straight past
    # login to device selection.
    if step == "cloud":
        r = call(f"/api/config/config_entries/flow/{fid}",
                 {"user_code": user_code})
        step = r.get("step_id")
        if step == "cloud":
            sys.exit(f"Login rejected. Check the User Code.\n"
                     f"{json.dumps(r.get('description_placeholders'), indent=1)}")

    if step == "scan":
        # The token is not in the JSON response -- it is embedded in the QR
        # selector inside data_schema, which is where the UI reads it from too.
        token = None
        for field in r.get("data_schema") or []:
            sel = field.get("selector", {}).get("qr_code", {})
            if sel.get("data", "").startswith("tuyaSmart--qrLogin?token="):
                token = sel["data"].split("token=", 1)[1]
        if not token:
            sys.exit("Flow reached the scan step but exposed no QR token:\n"
                     + json.dumps(r, indent=1)[:1500])

        show_qr(token)
        print("Scan the code above in SmartLife: Me tab > scan icon, top right.")
        print("Polling for login...", flush=True)

        # Poll rather than waiting on stdin: this runs unattended as often as
        # not, and blocking on input() would burn the token window. Tuya's QR
        # token expires server-side in a few minutes, so on expiry request a
        # fresh one and redraw rather than giving up -- the scan often happens
        # long after the script starts.
        deadline = time.time() + 1800
        refresh_at = time.time() + 150
        step = "scan"
        while time.time() < deadline:
            time.sleep(5)
            r = call(f"/api/config/config_entries/flow/{fid}", {})
            step = r.get("step_id")
            if step != "scan":
                print("Logged in.", flush=True)
                break
            if time.time() >= refresh_at:
                # async_step_scan re-issues a token on failed login; pull the
                # new one out of the redrawn form and repaint the PNG.
                for field in r.get("data_schema") or []:
                    sel = field.get("selector", {}).get("qr_code", {})
                    data = sel.get("data", "")
                    if data.startswith("tuyaSmart--qrLogin?token="):
                        newtok = data.split("token=", 1)[1]
                        if newtok != token:
                            token = newtok
                            print("Token expired; new QR written.", flush=True)
                        show_qr(token, ascii_art=False)
                refresh_at = time.time() + 150
        if step == "scan":
            sys.exit("Login did not complete within 30 minutes.")

    if step != "choose_device":
        sys.exit(f"Unexpected step {step!r}:\n{json.dumps(r, indent=1)[:1500]}")

    # Device and hub lists both come from the same schema.
    devices, hubs = [], []
    for field in r.get("data_schema") or []:
        opts = field.get("selector", {}).get("select", {}).get("options", [])
        if field.get("name") == "device_id":
            devices = opts
        elif field.get("name") == "hub_id":
            hubs = opts

    print(f"\nAddable devices ({len(devices)}):")
    for i, o in enumerate(devices):
        print(f"  [{i}] {o['label']}")
    print(f"\nGateways ({len(hubs)}):")
    for o in hubs:
        print(f"      {o['label']}  ({o['value']})")

    if not devices:
        print("\nNothing left to add -- every cloud device is already in HA.")
        return
    if len(hubs) <= 1:
        print("\nWARNING: no gateway offered. A DIY remote cannot be added "
              "without one. Learn its codes locally instead:\n"
              "  service: remote.learn_command  ->  entity: remote.smart_ir")

    print(f"\nFlow is parked at choose_device. Flow id: {fid}")
    print("Add one device with:")
    print(f"  curl -X POST {HA}/api/config/config_entries/flow/{fid} \\")
    print("    -H 'Authorization: Bearer $TOKEN' -H 'Content-Type: application/json' \\")
    print("    -d '{\"device_id\":\"<from list>\",\"hub_id\":\"<Smart IR value>\"}'")
    print("\nA flow left open holds the unique id and makes every later attempt")
    print("abort with already_in_progress, so finish it or restart HA.")


if __name__ == "__main__":
    main()
