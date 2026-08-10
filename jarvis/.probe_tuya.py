import json
import os
import sys
import urllib.request

# Read-only. Deliberately does NOT open a Tuya protocol connection: a Tuya
# device accepts exactly ONE local connection at a time and Home Assistant is
# holding it, so a competing probe would either fail spuriously (error 914) or
# knock HA off its own device.
TOK = os.environ.get("TOK", "")

try:
    entries = json.load(open("/config/.storage/core.config_entries"))["data"]["entries"]
except Exception as exc:
    print("error|config_entries|%s" % type(exc).__name__)
    sys.exit()

entry_type = {}
for e in entries:
    if e.get("domain") != "tuya_local":
        continue
    d = e.get("data") or {}
    h = d.get("host")
    t = d.get("type") or "?"
    if h:
        print("host|%s|%s|%s" % (t, h, d.get("protocol_version")))
        entry_type[e.get("entry_id")] = t

try:
    reg = json.load(open("/config/.storage/core.entity_registry"))["data"]["entities"]
except Exception as exc:
    print("error|entity_registry|%s" % type(exc).__name__)
    sys.exit()

want = {}
for e in reg:
    if e.get("platform") != "tuya_local":
        continue
    want[e["entity_id"]] = entry_type.get(e.get("config_entry_id"), "?")

if not TOK:
    sys.exit()

req = urllib.request.Request(
    "http://localhost:8123/api/states", headers={"Authorization": "Bearer " + TOK}
)
try:
    with urllib.request.urlopen(req, timeout=30) as r:
        states = json.loads(r.read())
except Exception as exc:
    print("error|states|%s" % type(exc).__name__)
    sys.exit()

# An entity carrying attributes.restored is a stale registry row that the
# integration is no longer providing at all -- a different fault from "the
# device stopped answering", and it must not be counted as one.
dead = {}
restored = {}
tot = {}
for st in states:
    eid = st["entity_id"]
    if eid not in want:
        continue
    t = want[eid]
    tot[t] = tot.get(t, 0) + 1
    if st.get("state") in ("unavailable", "unknown"):
        if (st.get("attributes") or {}).get("restored"):
            restored[t] = restored.get(t, 0) + 1
        else:
            dead[t] = dead.get(t, 0) + 1

for t in sorted(tot):
    print("avail|%s|%d|%d|%d" % (t, tot[t], dead.get(t, 0), restored.get(t, 0)))
