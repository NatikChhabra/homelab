#!/usr/bin/env python3
"""
Pull the IR library codes for Natik's DIY remotes out of the Tuya cloud.

WHY THIS EXISTS
---------------
The Zoook blaster cannot LEARN an air-conditioner frame. Capturing the AC power
button returned 51 bits of an 80-bit Voltas frame: the study buffer truncates
anything that long, and a truncated frame is useless. See
[[zoook-ir-blaster-dp-scheme]].

The Tuya app does not learn AC codes either. For air conditioners it uses Tuya's
IR LIBRARY -- a `head` (codeset id) plus a short `key` per button, which the
blaster expands into the full frame on its own. That is why the app works while
local capture fails, and why these pairs are worth fetching: once we have them
they send LOCALLY forever after, via DP 3 = head, DP 4 = key_code, DP 1 =
"send_ir". tinytuya wraps that as send_key(head, key). No cloud on the command
path.

The blaster caches none of it -- a status query returns only {'1': 'send_ir'} --
so the cloud is the only source.

SESSION CACHING
---------------
Tuya login is QR-only; there is no password path. Worse, each QR token is
single-use, so every failed run used to cost another scan. This script therefore
SAVES the session to tuya-session.json and reuses it. Scan once, re-run freely.

Getting a QR right, learned the hard way:
  - user code lives in SmartLife: Settings > Account and Security > User Code
  - scan in SmartLife: Me tab > scan icon (top right)
  - then TAP CONFIRM. Scanning alone leaves the poll returning E0020003
    "Login failed, please scan and try again!" -- byte-identical to the
    never-scanned error, so it is indistinguishable from a failure.
  - mint the token ONCE per run. An earlier version regenerated it every 150s,
    which meant the user was scanning tokens that had already been retired.

Usage:
    python tuya_ir_codes.py [USER_CODE]      # user code only needed on first run
"""

import json
import os
import sys
import time

BLASTER_ID = "853521608cce4edb3d49"

# Same client id/schema as Home Assistant core's Tuya integration. Works with
# both "Tuya Smart" and "SmartLife" accounts.
CLIENT_ID = "HA_3y9q4ak7g4ephrvke"
SCHEMA = "haauthorize"

SESSION = "/work/tuya-session.json"
OUT = "/work/tuya-ir-library.json"
QR = "/work/tuya-cloud-qr.png"


def qr_login(user_code):
    from tuya_sharing import LoginControl
    lc = LoginControl()
    resp = lc.qr_code(CLIENT_ID, SCHEMA, user_code)
    if not resp.get("success"):
        sys.exit(f"could not mint QR: {resp}")
    token = resp["result"]["qrcode"]

    try:
        import segno
        segno.make(f"tuyaSmart--qrLogin?token={token}", error="q").save(
            QR, scale=8, border=4)
        print(f"QR written -> {QR}", flush=True)
    except Exception as e:
        print(f"(no PNG: {e}) payload: tuyaSmart--qrLogin?token={token}",
              flush=True)
    print("Scan in SmartLife (Me > scan icon), then TAP CONFIRM.", flush=True)

    deadline = time.time() + 240
    while time.time() < deadline:
        time.sleep(6)
        ok, info = lc.login_result(token, CLIENT_ID, user_code)
        if ok:
            info["user_code"] = user_code
            with open(SESSION, "w") as f:
                json.dump(info, f)
            print(f"LOGGED IN (session cached -> {SESSION})", flush=True)
            return info
        print(f"  ...{int(deadline - time.time())}s left "
              f"({info.get('msg', info)})", flush=True)
    sys.exit("login never completed")


def get_session():
    if os.path.exists(SESSION):
        info = json.load(open(SESSION))
        print("reusing cached session", flush=True)
        return info
    if len(sys.argv) < 2:
        sys.exit("no cached session; pass the SmartLife user code")
    return qr_login(sys.argv[1].strip())


def make_manager(info):
    from tuya_sharing import Manager

    class NullListener:
        def update_token(self, t):
            pass

    return Manager(
        CLIENT_ID, info["user_code"], info["terminal_id"], info["endpoint"],
        {"t": info["t"], "uid": info["uid"],
         "expire_time": info["expire_time"],
         "access_token": info["access_token"],
         "refresh_token": info["refresh_token"]},
        NullListener(),
    )


def probe(api, path):
    try:
        r = api.get(path)
    except Exception as e:
        return None, f"EXC {type(e).__name__}: {str(e)[:80]}"
    if isinstance(r, dict) and r.get("success"):
        return r.get("result"), None
    return None, (r.get("msg") if isinstance(r, dict) else str(r)[:80])


def main():
    mgr = make_manager(get_session())
    api = mgr.customer_api
    out = {"devices": [], "probes": {}}

    # Every DIY remote shows up in the app's device list as a sub-device of the
    # blaster, so the device cache is the reliable way to enumerate them --
    # more so than guessing at IR endpoint paths.
    try:
        mgr.update_device_cache()
        for did, dev in mgr.device_map.items():
            entry = {"id": did, "name": getattr(dev, "name", None),
                     "category": getattr(dev, "category", None),
                     "product_id": getattr(dev, "product_id", None),
                     "status": dict(getattr(dev, "status", {}) or {})}
            out["devices"].append(entry)
            print(f"  {did}  {entry['category']:<12} {entry['name']}",
                  flush=True)
    except Exception as e:
        print(f"device cache failed: {type(e).__name__}: {e}", flush=True)

    ir_subs = [d for d in out["devices"]
               if (d["category"] or "").startswith(("infrared_", "qt", "wnykq"))
               or d["id"] != BLASTER_ID]

    # Customer-API IR namespaces, in descending likelihood. The /v2.0/infrareds
    # industry paths are NOT valid for a SmartLife token -- they need an IoT
    # project token, which is why an earlier run got nothing.
    cands = []
    for b in (f"/v1.0/m/ir/{BLASTER_ID}", f"/v1.0/ir/{BLASTER_ID}",
              f"/v1.0/m/infrared/{BLASTER_ID}",
              f"/v2.0/m/infrareds/{BLASTER_ID}"):
        cands += [f"{b}/remotes", f"{b}/keys", b]
    cands += [f"/v1.0/m/devices/{BLASTER_ID}/sub-devices",
              f"/v1.1/m/life/{BLASTER_ID}/specifications",
              f"/v1.0/m/life/{BLASTER_ID}/specification"]
    for d in ir_subs[:6]:
        cands += [f"/v1.1/m/life/{d['id']}/specifications",
                  f"/v1.0/m/ir/{BLASTER_ID}/remotes/{d['id']}/keys",
                  f"/v1.0/m/ir/{BLASTER_ID}/remotes/{d['id']}/learned-keys"]

    print("\nprobing IR endpoints:", flush=True)
    for p in cands:
        res, err = probe(api, p)
        out["probes"][p] = res if res is not None else f"ERR {err}"
        if res is not None:
            print(f"  OK  {p}\n      {json.dumps(res)[:500]}", flush=True)

    with open(OUT, "w") as f:
        json.dump(out, f, indent=2)
    hits = sum(1 for v in out["probes"].values()
               if not (isinstance(v, str) and v.startswith("ERR")))
    print(f"\n{len(out['devices'])} devices, {hits} endpoints answered "
          f"-> {OUT}", flush=True)


if __name__ == "__main__":
    main()
