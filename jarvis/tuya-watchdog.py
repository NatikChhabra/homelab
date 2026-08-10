#!/usr/bin/env python3
"""Keep Home Assistant's local control of the Tuya devices alive across IP changes.

WHY THIS EXISTS
---------------
tuya-local binds a config entry to a fixed IP address. There is no mDNS, no
hostname, no rediscovery: if the device comes back on a different address the
entry keeps dialling the old one and every entity goes `unavailable` with no
error anyone will notice until they ask for the lights and nothing happens. On
2026-08-08 the AZIOT switch dropped off WiFi twice. Both times it came back on
the same address, so nothing broke -- but only by luck.

The real fix is the DHCP reservation Natik is setting at the router. This is the
safety net for when that reservation is *lost*: router reboot, factory reset, or
a replacement router. It should normally do nothing at all.

THE ONE RULE THAT MATTERS: DO NOT PROBE THE DEVICE
--------------------------------------------------
A Tuya device accepts exactly ONE local connection at a time, and Home Assistant
is holding it. Measured on this host:

    TCP  192.168.29.108:55670 -> 192.168.29.236:6668  ESTABLISHED  pid 15280
    TCP  192.168.29.108:62679 -> 192.168.29.12:6668   ESTABLISHED  pid 15280

So a `tinytuya` status query from out here returns 914, and a TCP connect to 6668
gets refused or hangs. Those are the symptoms of a HEALTHY device that is already
under control. A watchdog that read them as "device down" would flap on every
single run and would be strictly worse than no watchdog -- and worse still, a
connect attempt can knock Home Assistant's own session off the device.

This script therefore NEVER opens a Tuya session and NEVER touches port 6668.
Every signal it uses is either passive or comes from Home Assistant itself:

    ARP / neighbour cache   layer 2. A host cannot refuse to answer ARP without
                            leaving the network, so this works even when the
                            device firewalls everything above it.
    ICMP                    reachability only, and only as corroboration.
    HA REST API             entity availability -- HA's own opinion of whether
                            it is talking to the device.
    /proc/net/tcp in the
    container               whether HA actually holds a socket to that IP.

REDISCOVERY: MAC -> IP
----------------------
The Zoook blaster's device ID embeds its MAC:

    853521608cce4edb3d49  ->  8c:ce:4e:db:3d:49   MEASURED: matches ARP for .236

The AZIOT switch's does NOT. Its ID is d79ea7cee4fd01107c99uz; the last 12 hex
characters are e4fd01107c99, and that MAC appears nowhere in the ARP table, while
.12's real MAC is 30:48:7d:36:e4:74. So "the device ID embeds the MAC" is true of
one device here and false of the other, and a watchdog that assumed it for both
would silently fail to find the switch -- the one device this was built for.

So the ID-derived MAC is treated as a CANDIDATE that has to be confirmed against
observation, never as truth. The authoritative MAC for each device is LEARNED:
whenever a device is confirmed present at its configured address, its MAC is
cached in the state file, and that cached MAC is what later finds it if it moves.

Tuya UDP discovery broadcasts (6666/6667/7000) would map device ID to IP
directly, with no MAC needed. MEASURED on this host 2026-08-08: zero broadcasts
in 25 seconds on all three ports, so it is not usable here as a primary path. It
is available behind --broadcast if the network ever starts carrying them.

REPAIR
------
Via Home Assistant's own options flow over REST, which is exactly what the UI
does when you click Configure:

    POST /api/config/config_entries/options/flow      {"handler": <entry_id>}
    POST /api/config/config_entries/options/flow/<id> {host: <new>, ...}

Three reasons that beats editing .storage by hand:
  * HA rewrites .storage from memory on shutdown, so a hand edit to a running
    instance is simply discarded. Editing it safely means stopping HA first.
  * tuya-local's async_test_connection PAUSES its existing device object before
    testing the new address, so the validation does not fight its own session.
  * On success the update listener calls async_schedule_reload. No HA restart.

The flow hands back the current values as form defaults, so repair echoes every
field back unchanged and overrides only `host`. The local key and the
protocol version are carried through untouched -- which matters, because this
switch answers ONLY on 3.5 and "auto" reports "unable to connect".

A local key is never logged, never printed, and never written to the state file.

Usage:
    tuya-watchdog.py                        # --check, read-only
    tuya-watchdog.py --check
    tuya-watchdog.py --repair               # dry run: says what it WOULD do
    tuya-watchdog.py --repair --commit      # actually applies it
    tuya-watchdog.py --check --expect switch=192.168.29.99    # simulate a move

Exit codes (the installer's task depends on these being distinct):
    0  everything consistent, or a repair succeeded
    1  a device has MOVED -- actionable, repair can fix it
    2  a device could not be found on the LAN at all -- offline, NOT repairable,
       and deliberately NOT treated the same as a move
    3  environment/config error: HA unreachable, entries unreadable
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "tuya-watchdog-state.json")
LOG_FILE = os.path.join(HERE, "tuya-watchdog.log")
LOG_MAX_BYTES = 1_000_000

# Fields the repair flow may log. Everything else is redacted. Allowlist
# deliberately small: none of these are secrets.
SAFE_TO_LOG = ("host", "protocol_version", "poll_only")

# secrets.env lives beside this script; the HA token is in the parent stack file.
SECRETS = [os.path.join(os.path.dirname(HERE), "secrets.env"),
           os.path.join(HERE, "secrets.env")]

HASS_URL = os.environ.get("HASS_URL", "http://localhost:8123")
HASS_CONTAINER = os.environ.get("HASS_CONTAINER", "jarvis-hass")
TUYA_PORT = 6668

_LOG_LINES = []


# --------------------------------------------------------------- logging ----

def now():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def say(msg):
    """Print to the console. Not persisted -- console is for humans."""
    print(msg, flush=True)


def log(msg):
    """Buffer a line for the log file. Flushed once, at exit.

    --check runs every 15 minutes forever, so a heartbeat line per run would be
    ~35k lines a year of "everything is fine". Only non-OK results, repairs and
    MAC-cache changes are logged, which keeps the file worth reading.
    """
    _LOG_LINES.append(f"{now()} {msg}")


def flush_log():
    if not _LOG_LINES:
        return
    try:
        if (os.path.exists(LOG_FILE)
                and os.path.getsize(LOG_FILE) > LOG_MAX_BYTES):
            os.replace(LOG_FILE, LOG_FILE + ".1")
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write("\n".join(_LOG_LINES) + "\n")
    except OSError as exc:
        say(f"  (could not write {LOG_FILE}: {exc})")


# --------------------------------------------------------------- secrets ----

def load_secrets():
    """Read the env-style secrets files. Values are never logged."""
    env = {}
    for path in SECRETS:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        except OSError as exc:
            say(f"  (could not read {path}: {exc})")
    return env


# ------------------------------------------------------------- HA client ----

class Hass:
    def __init__(self, url, token):
        self.url = url.rstrip("/")
        self.token = token

    def _call(self, path, payload=None, method=None, timeout=30):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
        return json.loads(body) if body else None

    def ping(self):
        try:
            self._call("/api/", timeout=10)
            return True
        except Exception:
            return False

    def entries(self):
        return self._call("/api/config/config_entries/entry") or []

    def state(self, entity_id):
        try:
            return self._call(f"/api/states/{entity_id}", timeout=10)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    # -- options flow ------------------------------------------------------
    def options_start(self, entry_id):
        return self._call("/api/config/config_entries/options/flow",
                          {"handler": entry_id})

    def options_submit(self, flow_id, payload):
        # 60s: tuya-local pauses the live device and sleeps 5s before testing
        # the new address, then has to complete a 3.5 handshake.
        return self._call(f"/api/config/config_entries/options/flow/{flow_id}",
                          payload, timeout=60)

    def options_abort(self, flow_id):
        try:
            self._call(f"/api/config/config_entries/options/flow/{flow_id}",
                       method="DELETE", timeout=10)
            return True
        except Exception:
            return False

    def reload_entry(self, entry_id):
        return self._call(
            f"/api/config/config_entries/entry/{entry_id}/reload", {},
            timeout=60)


def schema_defaults(form):
    """Pull {field: default} out of a serialised voluptuous schema.

    HA renders each field as {"name":..., "default":..., "type":...}. The
    defaults are the device's CURRENT settings, which is what makes a
    change-only-the-host repair possible without reading the local key from
    anywhere.
    """
    out = {}
    for field in form.get("data_schema") or []:
        name = field.get("name")
        if name is not None and "default" in field:
            out[name] = field["default"]
    return out


# ------------------------------------------------------ layer 2 discovery ---

MAC_RE = re.compile(
    r"\s+(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:[-:][0-9a-fA-F]{2}){5})\s+(\w+)")


def norm_mac(value):
    if not value:
        return None
    hexes = re.sub(r"[^0-9a-fA-F]", "", value).lower()
    if len(hexes) != 12:
        return None
    return ":".join(hexes[i:i + 2] for i in range(0, 12, 2))


def arp_table():
    """{ip: (mac, type)} from the OS neighbour cache. Read-only, no packets."""
    try:
        out = subprocess.run(["arp", "-a"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        say(f"  (arp -a failed: {exc})")
        return {}
    table = {}
    for line in out.splitlines():
        m = MAC_RE.match(line)
        if not m:
            continue
        mac = norm_mac(m.group(2))
        if mac and mac != "ff:ff:ff:ff:ff:ff":
            table[m.group(1)] = (mac, m.group(3))
    return table


def arp_sweep(subnet, settle=3.0):
    """Force the OS to resolve every host on the /24, then let it settle.

    Sending a datagram at an on-link address makes the stack emit an ARP request
    before it can transmit; whether anything is listening on the port is
    irrelevant, because the resolution happens a layer below. 254 sends take
    well under a second. Nothing is sent to port 6668, so no Tuya session is
    ever opened by this.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sent = 0
    for host in range(1, 255):
        try:
            sock.sendto(b"\x00", (f"{subnet}.{host}", 9))  # 9 = discard
            sent += 1
        except OSError:
            pass
    sock.close()
    time.sleep(settle)
    return sent


def ping(ip, timeout_ms=800):
    """Corroboration only. A silent device is NOT evidence of absence here."""
    if os.name == "nt":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, timeout_ms // 1000)), ip]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def subnet_of(ip):
    return ip.rsplit(".", 1)[0]


def tuya_broadcast_listen(seconds):
    """Passive listen for Tuya discovery broadcasts. Optional, off by default.

    MEASURED 2026-08-08 on this host: zero broadcasters in 25s across
    udp/6666, udp/6667 and udp/7000, so this finds nothing here. Kept because it
    is the only zero-ambiguity device-ID-to-IP mapping that exists, and costs
    nothing when not asked for. Returns {device_id: ip}.
    """
    import hashlib
    found = {}
    socks = []
    for port in (6666, 6667, 7000):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            s.setblocking(False)
            socks.append(s)
        except OSError:
            s.close()
    if not socks:
        return found

    key = hashlib.md5(b"yGAdlopoPVldABfn").digest()

    def decode(payload):
        try:
            return json.loads(payload.decode("utf8"))
        except Exception:
            pass
        try:
            from cryptography.hazmat.primitives.ciphers import (
                Cipher, algorithms, modes)
            dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
            raw = dec.update(payload) + dec.finalize()
            return json.loads(raw[:-raw[-1]].decode("utf8"))
        except Exception:
            return {}

    import select
    deadline = time.time() + seconds
    while time.time() < deadline:
        ready, _, _ = select.select(socks, [], [], 1.0)
        for s in ready:
            try:
                data, addr = s.recvfrom(4096)
            except OSError:
                continue
            body = data[20:-8] if len(data) > 28 else data
            msg = decode(body)
            dev = msg.get("gwId") or msg.get("devId")
            if dev:
                found[dev] = msg.get("ip") or addr[0]
    for s in socks:
        s.close()
    return found


# ----------------------------------------------------------- HA config in ---

READ_ENTRIES = r"""
import json
out = {"entries": [], "entities": {}}
try:
    d = json.load(open('/config/.storage/core.config_entries'))
    for e in d['data']['entries']:
        if e['domain'] != 'tuya_local':
            continue
        # The local key is dropped HERE, inside the container, so it never
        # crosses into the watchdog process at all on this path.
        out['entries'].append({
            'entry_id': e['entry_id'],
            'title': e.get('title'),
            'device_id': e['data'].get('device_id'),
            'host': (e.get('options') or {}).get('host') or e['data'].get('host'),
            'protocol_version': ((e.get('options') or {}).get('protocol_version')
                                 or e['data'].get('protocol_version')),
            'type': e['data'].get('type'),
        })
except Exception as exc:
    out['error'] = '%s: %s' % (type(exc).__name__, exc)
try:
    r = json.load(open('/config/.storage/core.entity_registry'))
    for ent in r['data']['entities']:
        cid = ent.get('config_entry_id')
        if cid:
            out['entities'].setdefault(cid, []).append(ent['entity_id'])
except Exception:
    pass
print(json.dumps(out))
"""


def read_entries_via_docker():
    """Read tuya-local entries out of .storage. READ ONLY.

    Reading is safe at any time. WRITING is not: HA holds .storage in memory and
    rewrites it on shutdown, so a hand edit to a live instance is silently
    discarded. That is why repair goes through the REST options flow instead.
    """
    try:
        res = subprocess.run(
            ["docker", "exec", HASS_CONTAINER, "python", "-c", READ_ENTRIES],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"docker exec failed: {exc}"
    if res.returncode != 0:
        return None, f"docker exec exit {res.returncode}: {res.stderr.strip()[:200]}"
    try:
        return json.loads(res.stdout.strip()), None
    except json.JSONDecodeError as exc:
        return None, f"unparseable output: {exc}"


def entries_from_secrets(env):
    """Fallback when Docker is unavailable. No entry_id, so repair cannot run."""
    out = []
    for prefix, title in (("TUYA_NODE_SWITCH", "Node Switch"),
                          ("TUYA_SMART_IR", "Smart IR")):
        dev = env.get(prefix + "_DEVICE_ID")
        host = env.get(prefix + "_HOST")
        if dev and host:
            out.append({"entry_id": None, "title": title, "device_id": dev,
                        "host": host,
                        "protocol_version": env.get(prefix + "_PROTOCOL"),
                        "type": None})
    return out


def ha_sockets():
    """IPs that HA currently holds an ESTABLISHED socket to on port 6668.

    Read out of the container's own /proc/net/tcp. This is the strongest
    evidence that local control is genuinely working, and it costs nothing --
    we are reading HA's connection, not making one.
    """
    port_hex = f"{TUYA_PORT:04X}"
    try:
        res = subprocess.run(
            ["docker", "exec", HASS_CONTAINER, "cat", "/proc/net/tcp"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    if res.returncode != 0:
        return set()
    live = set()
    for line in res.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4 or parts[3] != "01":       # 01 = ESTABLISHED
            continue
        rem = parts[2]
        if not rem.endswith(":" + port_hex):
            continue
        hexip = rem.split(":")[0]
        octets = [int(hexip[i:i + 2], 16) for i in (6, 4, 2, 0)]
        live.add(".".join(str(o) for o in octets))
    return live


# ------------------------------------------------------------------ state ---

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"devices": {}}
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
        state.setdefault("devices", {})
        return state
    except (OSError, json.JSONDecodeError):
        return {"devices": {}}


def save_state(state):
    state["updated"] = now()
    state["note"] = ("MACs and IPs only -- no local keys are ever written here. "
                     "Safe to delete: it is rebuilt on the next successful "
                     "--check while each device is at its configured address.")
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        say(f"  (could not write {STATE_FILE}: {exc})")


def mac_from_device_id(device_id):
    """Last 12 chars of the ID, IF they are hex. A CANDIDATE, never truth.

    True for the Zoook blaster (853521608cce4edb3d49 -> 8c:ce:4e:db:3d:49).
    False for the AZIOT switch, whose ID ends 'uz' and whose real MAC shares no
    digits with its ID. Callers must confirm against observation.
    """
    if not device_id or len(device_id) < 12:
        return None
    return norm_mac(device_id[-12:])


# ------------------------------------------------------------- the check ----

OK, MOVED, LOST, UNKNOWN = "OK", "MOVED", "LOST", "UNKNOWN"


def assess(entry, expected, arp, state, live_sockets, entities, hass,
           broadcast_map, allow_sweep=True):
    """Work out where a device actually is. Returns a result dict."""
    dev_id = entry["device_id"]
    known = state["devices"].get(dev_id, {})
    cached_mac = norm_mac(known.get("mac"))
    id_mac = mac_from_device_id(dev_id)

    res = {"title": entry.get("title") or dev_id, "device_id": dev_id,
           "entry_id": entry.get("entry_id"), "expected": expected,
           "protocol": entry.get("protocol_version"), "current": None,
           "mac": None, "mac_source": None, "status": UNKNOWN, "notes": [],
           "learned": False, "entities_total": 0, "entities_unavailable": []}

    # HA's own opinion, which is the thing we actually care about preserving.
    for ent_id in entities.get(entry.get("entry_id") or "", []):
        st = None
        try:
            st = hass.state(ent_id) if hass else None
        except Exception:
            st = None
        if st is None:
            continue
        res["entities_total"] += 1
        if st.get("state") in ("unavailable", "unknown"):
            res["entities_unavailable"].append(ent_id)

    res["ha_socket"] = expected in live_sockets

    # A broadcast, if we ever see one, is unambiguous: it carries the device ID.
    if dev_id in broadcast_map:
        res["current"] = broadcast_map[dev_id]
        res["mac_source"] = "tuya-broadcast"

    observed = arp.get(expected)
    observed_mac = observed[0] if observed else None

    # Confirmed-present test. Deliberately NOT "can I connect to 6668" -- that
    # would fight HA for the single connection slot the device allows.
    confirmed = bool(observed_mac) and (
        res["ha_socket"]
        or (res["entities_total"] > 0 and not res["entities_unavailable"])
        or ping(expected))

    if confirmed:
        res["current"] = expected
        res["mac"] = observed_mac
        res["mac_source"] = "arp@expected"
        res["status"] = OK
        if cached_mac != observed_mac:
            res["learned"] = True
            res["notes"].append(
                f"cached MAC {cached_mac or '(none)'} -> {observed_mac}")
        state["devices"][dev_id] = {
            "mac": observed_mac, "last_good_ip": expected, "last_good": now(),
            "id_mac_matches": bool(id_mac and id_mac == observed_mac),
            "title": res["title"]}
        if id_mac and id_mac != observed_mac:
            res["notes"].append(
                f"device ID implies {id_mac}, real MAC is {observed_mac} "
                "-- ID does NOT embed the MAC for this device")
        return res

    # Not at the expected address. Now we need its MAC to go find it.
    candidates = []
    if cached_mac:
        candidates.append((cached_mac, "cached (verified when last seen)"))
    if id_mac and id_mac != cached_mac:
        candidates.append((id_mac, "derived from device ID (UNCONFIRMED)"))

    if not candidates:
        res["status"] = LOST
        res["notes"].append(
            "no MAC known for this device and its ID does not embed one, so it "
            "cannot be rediscovered. Run --check once while it is at its "
            "configured address to learn the MAC.")
        return res

    def find(table):
        for mac, source in candidates:
            hits = [ip for ip, (m, _t) in table.items()
                    if m == mac and subnet_of(ip) == subnet_of(expected)]
            if hits:
                return hits, mac, source
        return [], None, None

    hits, mac, source = find(arp)
    if not hits and allow_sweep:
        res["notes"].append("not in ARP cache; sweeping the /24")
        sent = arp_sweep(subnet_of(expected))
        res["notes"].append(f"sent {sent} ARP-forcing datagrams")
        arp.clear()
        arp.update(arp_table())
        hits, mac, source = find(arp)

    if not hits:
        res["status"] = LOST
        res["notes"].append(
            "MAC not present on the LAN -- device is powered off or off WiFi. "
            "This is NOT an IP change and will NOT be repaired.")
        return res

    if len(hits) > 1:
        reachable = [ip for ip in hits if ping(ip)]
        res["notes"].append(
            f"MAC seen at multiple addresses {hits}; "
            f"reachable: {reachable or 'none'}")
        hits = reachable or hits

    res["mac"] = mac
    res["mac_source"] = source
    res["current"] = hits[0]
    res["status"] = MOVED if hits[0] != expected else OK
    return res


def render(res):
    tag = {OK: "OK    ", MOVED: "MOVED ", LOST: "LOST  ",
           UNKNOWN: "??    "}[res["status"]]
    say(f"  [{tag}] {res['title']}")
    say(f"           device id : {res['device_id']}")
    say(f"           expected  : {res['expected']}  (protocol "
        f"{res['protocol']})")
    cur = res["current"] or "not found on LAN"
    say(f"           current   : {cur}")
    if res["mac"]:
        say(f"           mac       : {res['mac']}  [{res['mac_source']}]")
    if res["entities_total"]:
        bad = len(res["entities_unavailable"])
        say(f"           entities  : {res['entities_total'] - bad}/"
            f"{res['entities_total']} available")
        for ent in res["entities_unavailable"][:5]:
            say(f"                       unavailable: {ent}")
    say(f"           ha socket : "
        f"{'yes, HA holds a live session' if res['ha_socket'] else 'no'}")
    for note in res["notes"]:
        say(f"           note      : {note}")


# ------------------------------------------------------------- the repair ---

def repair(res, hass, commit):
    """Point the config entry at the new address. Idempotent by construction."""
    title = res["title"]
    if res["status"] != MOVED:
        return True, "nothing to do"
    if not res["entry_id"]:
        return False, ("no HA entry id (entries came from secrets.env, not from "
                       "Home Assistant) -- cannot repair")

    target = res["current"]
    try:
        form = hass.options_start(res["entry_id"])
    except Exception as exc:
        return False, f"could not open options flow: {type(exc).__name__}: {exc}"

    flow_id = form.get("flow_id")
    if form.get("type") != "form" or not flow_id:
        return False, f"unexpected flow response: {form.get('type')}"

    defaults = schema_defaults(form)
    current_host = defaults.get("host")

    # Idempotency. If HA already points at the target there is nothing to write,
    # and writing anyway would trigger a pointless integration reload.
    if current_host == target:
        hass.options_abort(flow_id)
        return True, f"entry already points at {target}; no change made"

    # Echo every field back untouched except host. The local key and the
    # protocol version ride through as opaque values -- never inspected, never
    # logged. protocol_version in particular MUST survive: this switch answers
    # only on 3.5 and would break on "auto".
    payload = dict(defaults)
    payload["host"] = target
    if "protocol_version" in payload:
        payload["protocol_version"] = str(payload["protocol_version"])

    # Redaction is an ALLOWLIST, not a "does the name look secret" test. If
    # tuya-local ever adds or renames a field, the failure mode is a redacted
    # log line, not a leaked credential.
    safe = {k: (v if k in SAFE_TO_LOG else "<redacted>")
            for k, v in payload.items()}
    log(f"[repair] {title}: host {current_host} -> {target}; "
        f"submitting {json.dumps(safe)}")
    say(f"           would set host: {current_host} -> {target}")
    say(f"           payload       : {json.dumps(safe)}")

    if not commit:
        hass.options_abort(flow_id)
        say("           DRY RUN -- flow aborted, nothing written")
        log(f"[repair] {title}: dry run, flow aborted, nothing written")
        return False, "dry run"

    try:
        result = hass.options_submit(flow_id, payload)
    except Exception as exc:
        hass.options_abort(flow_id)
        msg = f"submit failed: {type(exc).__name__}: {exc}"
        log(f"[repair] {title}: {msg}")
        return False, msg

    if result.get("type") == "create_entry":
        log(f"[repair] {title}: entry updated to {target}, "
            "tuya-local reload scheduled")
        # async_update_entry -> async_schedule_reload. Give it a moment, then
        # confirm rather than assume.
        time.sleep(8)
        state = None
        for entry in hass.entries():
            if entry.get("entry_id") == res["entry_id"]:
                state = entry.get("state")
        log(f"[repair] {title}: entry state after reload = {state}")
        if state != "loaded":
            try:
                hass.reload_entry(res["entry_id"])
                log(f"[repair] {title}: forced an explicit reload")
            except Exception as exc:
                log(f"[repair] {title}: forced reload failed: {exc}")
        return True, f"host updated to {target}; entry state={state}"

    errors = result.get("errors") or {}
    if errors.get("base") == "connection":
        msg = ("tuya-local refused the new address: it could not complete a "
               f"protocol {res['protocol']} handshake with {target}. That "
               "usually means the MAC now belongs to a different host, or the "
               "device was re-paired and the local key rotated. NOT retrying.")
    else:
        msg = f"flow returned {result.get('type')} errors={errors}"
    hass.options_abort(flow_id)
    log(f"[repair] {title}: FAILED -- {msg}")
    return False, msg


# -------------------------------------------------------------------- main --

def main():
    ap = argparse.ArgumentParser(
        description="Keep tuya-local pointed at the right IP addresses.",
        epilog="Never opens a Tuya session: HA holds the only one each device "
               "allows.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true",
                     help="read-only consistency check (default)")
    mode.add_argument("--repair", action="store_true",
                     help="fix a moved device. DRY RUN unless --commit.")
    ap.add_argument("--commit", action="store_true",
                    help="with --repair, actually write the change")
    ap.add_argument("--dry-run", action="store_true",
                    help="explicit no-op; this is already the default")
    ap.add_argument("--expect", action="append", default=[], metavar="DEV=IP",
                    help="TEST ONLY: override the IP the config entry is "
                         "treated as holding, e.g. --expect switch=192.168.29.99. "
                         "Matches on device id or a substring of the entry "
                         "title. Exercises DETECTION.")
    ap.add_argument("--simulate-current", action="append", default=[],
                    metavar="DEV=IP",
                    help="TEST ONLY: pretend rediscovery found the device at "
                         "this IP. Exercises the REPAIR payload path without a "
                         "real DHCP change. Refused with --commit, because "
                         "committing an invented address would point Home "
                         "Assistant at a host that may not be the device.")
    ap.add_argument("--broadcast", type=int, default=0, metavar="SECS",
                    help="also listen for Tuya UDP discovery broadcasts "
                         "(measured: none on this LAN)")
    ap.add_argument("--no-sweep", action="store_true",
                    help="do not ARP-sweep the /24 when a device is missing")
    ap.add_argument("--hass-url", default=HASS_URL)
    args = ap.parse_args()

    if args.dry_run and args.commit:
        say("--dry-run and --commit contradict each other. Refusing.")
        return 3
    if args.simulate_current and args.commit:
        say("--simulate-current is a dry-run testing aid and cannot be "
            "combined with --commit.")
        say("Committing an invented address would point Home Assistant at a "
            "host that may not be the device.")
        return 3
    doing_repair = args.repair
    commit = args.commit and not args.dry_run

    say(f"tuya-watchdog  {now()}")
    say(f"mode: {'repair' if doing_repair else 'check'}"
        f"{'' if not doing_repair else (' (COMMIT)' if commit else ' (dry run)')}")
    say("")

    env = load_secrets()
    token = os.environ.get("JARVIS_HASS_TOKEN") or env.get("JARVIS_HASS_TOKEN")
    hass = Hass(args.hass_url, token) if token else None
    if hass and not hass.ping():
        say(f"Home Assistant at {args.hass_url} did not answer. "
            "Entity state and repair are unavailable.")
        log(f"[check] HA unreachable at {args.hass_url}")
        if doing_repair:
            flush_log()
            return 3
        hass = None
    elif not hass:
        say("No JARVIS_HASS_TOKEN found; continuing without HA (network checks "
            "only, repair unavailable).")
        if doing_repair:
            flush_log()
            return 3

    data, err = read_entries_via_docker()
    if data and not data.get("error") and data.get("entries"):
        entries = data["entries"]
        entities = data.get("entities", {})
        source = f"{HASS_CONTAINER}:/config/.storage/core.config_entries"
    else:
        entries = entries_from_secrets(env)
        entities = {}
        source = "secrets.env (fallback)"
        say(f"could not read HA config entries ({err or data.get('error')}); "
            "falling back to secrets.env")
        log(f"[check] entry read failed: {err or (data or {}).get('error')}")

    if not entries:
        say("No tuya-local devices found. Nothing to watch.")
        log("[check] no tuya_local entries found")
        flush_log()
        return 3

    say(f"devices from: {source}")

    def parse_selectors(items, flag):
        out = {}
        for item in items:
            if "=" not in item:
                say(f"bad {flag} '{item}', want DEV=IP")
                return None
            sel, ip = item.split("=", 1)
            out[sel.strip().lower()] = ip.strip()
        return out

    def matches(entry, sel):
        return (sel == (entry.get("device_id") or "").lower()
                or sel in (entry.get("title") or "").lower())

    overrides = parse_selectors(args.expect, "--expect")
    sim_current = parse_selectors(args.simulate_current, "--simulate-current")
    if overrides is None or sim_current is None:
        return 3

    broadcast_map = {}
    if args.broadcast:
        say(f"listening {args.broadcast}s for Tuya discovery broadcasts...")
        broadcast_map = tuya_broadcast_listen(args.broadcast)
        say(f"  {len(broadcast_map)} broadcaster(s) heard")

    arp = arp_table()
    live_sockets = ha_sockets()
    state = load_state()
    say(f"arp entries: {len(arp)}   HA sockets on {TUYA_PORT}: "
        f"{sorted(live_sockets) if live_sockets else 'none'}")
    say("")

    results = []
    for entry in entries:
        expected = entry["host"]
        for sel, ip in overrides.items():
            if matches(entry, sel):
                say(f"  (--expect override: {entry['title']} expected IP forced "
                    f"to {ip}, real config entry says {entry['host']})")
                expected = ip
        res = assess(entry, expected, arp, state, live_sockets,
                     entities, hass, broadcast_map,
                     allow_sweep=not args.no_sweep)
        for sel, ip in sim_current.items():
            if matches(entry, sel):
                say(f"  (--simulate-current: pretending {entry['title']} was "
                    f"rediscovered at {ip}; it is really at "
                    f"{res['current'] or 'nowhere'})")
                res["current"] = ip
                res["status"] = MOVED if ip != expected else OK
                res["notes"].append("SIMULATED current IP -- not a real "
                                    "observation")
        results.append(res)

    for res in results:
        render(res)
        say("")

    save_state(state)

    moved = [r for r in results if r["status"] == MOVED]
    lost = [r for r in results if r["status"] in (LOST, UNKNOWN)]
    for res in results:
        if res["status"] != OK:
            log(f"[check] {res['title']}: {res['status']} expected="
                f"{res['expected']} current={res['current']} "
                f"mac={res['mac']} ({res['mac_source']})")
        elif res["learned"]:
            log(f"[check] {res['title']}: learned mac {res['mac']} at "
                f"{res['expected']}")

    if doing_repair:
        if not moved:
            say("Nothing has moved. No changes to make.")
            say("")
        else:
            say("--- repair ---")
            failed = 0
            for res in moved:
                say(f"  {res['title']}:")
                ok, msg = repair(res, hass, commit)
                say(f"           result        : {msg}")
                say("")
                if not ok:
                    failed += 1
            if failed:
                flush_log()
                return 1

    say("--- summary ---")
    for res in results:
        say(f"  {res['status']:<7} {res['title']:<14} expected "
            f"{res['expected']:<15} current {res['current'] or '-'}")

    if moved and not (doing_repair and commit):
        say("")
        say(f"{len(moved)} device(s) have MOVED. Fix with:")
        say(f"  python \"{os.path.abspath(__file__)}\" --repair --commit")
        flush_log()
        return 1
    if moved:
        flush_log()
        return 0
    if lost:
        say("")
        say(f"{len(lost)} device(s) not found on the LAN. This is an outage, "
            "not an IP change --")
        say("repairing would be wrong, so nothing was changed.")
        flush_log()
        return 2
    say("")
    say("All devices are where Home Assistant thinks they are.")
    flush_log()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
