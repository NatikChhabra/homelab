"""
HUD data plane: everything the visual panels need, and the display-directive
channel that lets a spoken command open a panel.

Why this is a separate module
-----------------------------
`voice_server.py` owns the 1720-2079 ms voice loop, which is the project's
headline number. Panel plumbing has no business living in the same file: a
mistake in a Jellyfin timeout must not be able to wedge speech. So everything
here is additive and isolated -- `voice_server.py` gains three small hooks and
nothing else, and every function below fails soft, returning a payload that
SAYS it failed rather than raising.

The honesty rule
----------------
Every endpoint returns `available: false` plus a human-readable `reason` when
its source is down. Nothing here invents a number. A panel that renders a
plausible fake reading is worse than a panel that renders an error, because the
error is visible and the fake is not -- and this project has already lost more
time to confident-but-false output than to anything else.

Credentials
-----------
The Home Assistant token and the Jellyfin key are read from the environment and
used only inside this process. Nothing that reaches the browser carries either
one; the poster proxy exists precisely so the page never needs the Jellyfin key
to render artwork.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any

import aiohttp
from aiohttp import web

log = logging.getLogger("jarvis.hud")

HASS_URL = os.getenv("HASS_URL", "http://jarvis-hass:8123").rstrip("/")
HASS_TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")

GLANCES_URL = os.getenv("GLANCES_URL", "http://host.docker.internal:61208").rstrip("/")

# Jellyfin and Traccar are on the host's published ports, not this compose
# network, so they are reached through host.docker.internal.
JELLYFIN_URL = os.getenv("JELLYFIN_URL", "http://host.docker.internal:8096").rstrip("/")
JELLYFIN_KEY = os.getenv("JELLYFIN_KEY", "")

# Traccar: probed, not assumed. MEASURED 2026-08-08 -- there is no traccar
# container, no traccar Windows service, and no install directory on this host;
# only a stale H2 database from 2026-07-05 under C:\ServerData\Traccar. So the
# map falls back to Home Assistant trackers and says why. If Traccar is ever
# started, set TRACCAR_URL and the map picks it up with no code change.
TRACCAR_URL = os.getenv("TRACCAR_URL", "http://host.docker.internal:8082").rstrip("/")
TRACCAR_USER = os.getenv("TRACCAR_USER", "")
TRACCAR_PASS = os.getenv("TRACCAR_PASS", "")

# Real disk usage. glances' fs plugin always returns [] here (see vitals_panel
# docstring), so this asks the pc-agent instead: it runs directly on the host,
# outside Docker Desktop's 9p mount translation, and can read the real NTFS
# volumes. Same token executor.py uses to reach pc-agent for pc_* tools.
PC_URL = os.getenv("JARVIS_PC_URL", "http://host.docker.internal:8099").rstrip("/")
PC_TOKEN = os.getenv("JARVIS_PC_TOKEN") or os.getenv("JARVIS_WOL_TOKEN") or ""

_HA_HEADERS = {"Authorization": f"Bearer {HASS_TOKEN}"} if HASS_TOKEN else {}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

async def _get_json(
    http: aiohttp.ClientSession, url: str, *, headers: dict | None = None,
    timeout: float = 5.0, params: dict | None = None,
) -> tuple[Any, str]:
    """
    GET and decode JSON. Returns (payload, error). Exactly one is truthy.

    Never raises. Callers here are rendering a dashboard; a dead dependency has
    to become a visible message on a panel, not a 500 that blanks the page.
    """
    try:
        async with http.get(
            url, headers=headers or {}, params=params,
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status != 200:
                return None, f"HTTP {resp.status}"
            return await resp.json(content_type=None), ""
    except asyncio.TimeoutError:
        return None, f"timed out after {timeout:.0f}s"
    except aiohttp.ClientError as exc:
        # The class name alone ("ClientConnectorError") is useless on screen;
        # the string carries the host and port, which is what identifies it.
        return None, f"{type(exc).__name__}: {exc}"
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"bad JSON: {exc}"


async def _ha_states(http: aiohttp.ClientSession) -> tuple[list[dict], str]:
    if not HASS_TOKEN:
        return [], "no Home Assistant token configured on the server"
    data, err = await _get_json(http, f"{HASS_URL}/api/states", headers=_HA_HEADERS, timeout=8)
    if err:
        return [], f"Home Assistant unreachable ({err})"
    return (data or []), ""


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# MAP
# --------------------------------------------------------------------------

async def _traccar_positions(http: aiohttp.ClientSession) -> tuple[list[dict], str]:
    """
    Traccar devices + latest positions, if Traccar is actually running.

    Kept on a short timeout: this endpoint is on the path of a voice command
    saying "show me the map", and a two-minute TCP wait on a host that is not
    listening would make the panel feel broken.
    """
    if not TRACCAR_URL:
        return [], "TRACCAR_URL not set"
    auth = aiohttp.BasicAuth(TRACCAR_USER, TRACCAR_PASS) if TRACCAR_USER else None
    try:
        async with http.get(
            f"{TRACCAR_URL}/api/devices", auth=auth,
            timeout=aiohttp.ClientTimeout(total=2.5),
        ) as resp:
            if resp.status in (401, 403):
                return [], f"Traccar refused the credentials (HTTP {resp.status})"
            if resp.status != 200:
                return [], f"Traccar returned HTTP {resp.status}"
            devices = await resp.json(content_type=None)
    except asyncio.TimeoutError:
        return [], ""  # Traccar not responding — silent, not an error
    except aiohttp.ClientError:
        return [], ""  # Traccar not running — silent, not an error
    except (ValueError, json.JSONDecodeError):
        return [], "Traccar returned a non-JSON device list"

    if not devices:
        return [], "Traccar is running but has no devices registered"

    positions, err = await _get_json(
        http, f"{TRACCAR_URL}/api/positions", timeout=2.5,
    )
    by_device = {}
    for p in (positions or []):
        by_device[p.get("deviceId")] = p

    out = []
    for d in devices:
        p = by_device.get(d.get("id"))
        if not p:
            continue
        out.append({
            "id": f"traccar:{d.get('id')}",
            "name": d.get("name") or f"device {d.get('id')}",
            "lat": p.get("latitude"),
            "lon": p.get("longitude"),
            "accuracy_m": p.get("accuracy"),
            "speed_kmh": round((p.get("speed") or 0) * 1.852, 1),
            "updated": p.get("deviceTime") or d.get("lastUpdate"),
            "source": "traccar",
            "state": d.get("status"),
        })
    if not out:
        return [], f"Traccar has {len(devices)} device(s) but no position fix for any of them"
    return out, err


def _ha_trackers(states: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Split HA person/device_tracker entities into (located, unlocated).

    Unlocated ones are returned rather than dropped: a person entity sitting at
    `unknown` with no coordinates is the actual state of this system today, and
    the map has to show that instead of an empty screen that reads as a bug.
    """
    located: list[dict] = []
    unlocated: list[dict] = []
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith(("person.", "device_tracker.")):
            continue
        a = s.get("attributes", {}) or {}
        lat, lon = _num(a.get("latitude")), _num(a.get("longitude"))
        entry = {
            "id": eid,
            "name": a.get("friendly_name") or eid,
            "state": s.get("state"),
            "source": "home-assistant",
            "updated": s.get("last_updated"),
        }
        if lat is not None and lon is not None:
            entry.update({
                "lat": lat, "lon": lon,
                "accuracy_m": _num(a.get("gps_accuracy")),
                "source_type": a.get("source_type"),
            })
            located.append(entry)
        else:
            entry["why"] = (
                f"{eid} reports '{s.get('state')}' and carries no GPS coordinates"
            )
            unlocated.append(entry)
    return located, unlocated


def _ha_zones(states: list[dict]) -> list[dict]:
    zones = []
    for s in states:
        if not s.get("entity_id", "").startswith("zone."):
            continue
        a = s.get("attributes", {}) or {}
        lat, lon = _num(a.get("latitude")), _num(a.get("longitude"))
        if lat is None or lon is None:
            continue
        zones.append({
            "id": s["entity_id"],
            "name": a.get("friendly_name") or s["entity_id"],
            "lat": lat, "lon": lon,
            "radius_m": _num(a.get("radius")) or 100,
        })
    return zones


async def map_panel(request: web.Request) -> web.Response:
    http = request.app["http"]
    states, ha_err = await _ha_states(http)

    traccar, traccar_err = await _traccar_positions(http)
    located, unlocated = _ha_trackers(states)
    zones = _ha_zones(states)

    home = next((z for z in zones if z["id"] == "zone.home"), None)
    if home is None:
        cfg, cfg_err = await _get_json(
            http, f"{HASS_URL}/api/config", headers=_HA_HEADERS, timeout=5
        )
        if cfg and _num(cfg.get("latitude")) is not None:
            home = {
                "id": "config.home", "name": cfg.get("location_name") or "Home",
                "lat": float(cfg["latitude"]), "lon": float(cfg["longitude"]),
                "radius_m": 100,
            }

    devices = traccar + located
    # Centre on the first real fix, else on home. Never on a hardcoded default:
    # a map silently centred on null island looks like a working map.
    centre = None
    if devices:
        centre = {"lat": devices[0]["lat"], "lon": devices[0]["lon"], "of": devices[0]["name"]}
    elif home:
        centre = {"lat": home["lat"], "lon": home["lon"], "of": home["name"]}

    return web.json_response({
        "available": centre is not None,
        "centre": centre,
        "home": home,
        "zones": zones,
        "devices": devices,
        "unlocated": unlocated,
        "sources": {
            "traccar": {"ok": bool(traccar), "detail": traccar_err or (f"{len(traccar)} device(s)" if traccar else "not configured")},
            "home_assistant": {
                "ok": not ha_err,
                "detail": ha_err or f"{len(located)} located, {len(unlocated)} without a fix",
            },
        },
        "reason": (
            "" if centre else
            ("No GPS source has a position: " + (traccar_err or "Traccar unavailable") + "; " if traccar_err else "")
            + ("Home Assistant " + (ha_err or "has no tracker with coordinates") if ha_err else "")
        ).rstrip("; "),
    })


# --------------------------------------------------------------------------
# VITALS
# --------------------------------------------------------------------------

async def vitals_panel(request: web.Request) -> web.Response:
    """
    Host telemetry from the glances container already running on :61208.

    Two plugins are structurally unavailable on this host and both are reported
    as such rather than omitted:

      * `fs` returns [] because Docker Desktop exposes C: and D: as 9p mounts
        and psutil.disk_partitions -- which the plugin is built on -- only lists
        physical filesystems. Documented in Stacks/server-room/docker-compose.yml.
      * `sensors` answers HTTP 400 here; there is no readable thermal source
        inside the container.

    Leaving either silently blank would read as "everything is fine".
    """
    http = request.app["http"]

    async def grab(path: str, timeout: float = 4.0):
        return await _get_json(http, f"{GLANCES_URL}/api/4/{path}", timeout=timeout)

    (cpu, cpu_e), (mem, mem_e), (load, load_e), (quick, quick_e), \
        (uptime, up_e), (containers, cont_e), (fs, fs_e), (sensors, sens_e) = \
        await asyncio.gather(
            grab("cpu"), grab("mem"), grab("load"), grab("quicklook"),
            grab("uptime"), grab("containers", 6.0), grab("fs"), grab("sensors"),
        )

    out: dict[str, Any] = {"available": bool(cpu or mem)}
    if not out["available"]:
        out["reason"] = f"glances unreachable at {GLANCES_URL} ({cpu_e or mem_e})"
        return web.json_response(out)

    if cpu:
        # The total and the per-core bars MUST come from one sample.
        #
        # glances caches each plugin independently, so /api/4/cpu and
        # /api/4/quicklook can answer from snapshots taken at different
        # moments. MEASURED: a render showed a 38% CPU ring above sixteen
        # per-core bars all pegged red at ~100%, which cannot both be true of
        # the same machine at the same instant. Whichever one was stale, a
        # panel that contradicts itself is worse than either number alone.
        #
        # quicklook carries both its own total and the per-core list, so it is
        # preferred for anything the bars are compared against. /api/4/cpu is
        # still used for the user/system/iowait split, which quicklook lacks.
        total = _num(cpu.get("total")) or 0.0
        source = "glances /api/4/cpu"
        if quick and isinstance(quick.get("percpu"), list) \
                and _num(quick.get("cpu")) is not None:
            total = _num(quick.get("cpu"))
            source = "glances /api/4/quicklook (same sample as the per-core bars)"
        out["cpu"] = {
            "percent": round(total, 1),
            "user": round(_num(cpu.get("user")) or 0, 1),
            "system": round(_num(cpu.get("system")) or 0, 1),
            "iowait": round(_num(cpu.get("iowait")) or 0, 1),
            "cores": cpu.get("cpucore"),
            "ctx_per_sec": cpu.get("ctx_switches_rate_per_sec"),
            "source": source,
        }
    if quick and isinstance(quick.get("percpu"), list):
        out["percpu"] = [round(_num(c.get("total")) or 0, 1) for c in quick["percpu"]]
        out["cpu_name"] = quick.get("cpu_name")
    if mem:
        gb = 1_073_741_824
        out["mem"] = {
            "percent": round(_num(mem.get("percent")) or 0, 1),
            "used_gb": round((_num(mem.get("used")) or 0) / gb, 2),
            "total_gb": round((_num(mem.get("total")) or 0) / gb, 2),
            "available_gb": round((_num(mem.get("available")) or 0) / gb, 2),
            "cached_gb": round((_num(mem.get("cached")) or 0) / gb, 2),
        }
    if load:
        out["load"] = {
            "min1": load.get("min1"), "min5": load.get("min5"),
            "min15": load.get("min15"), "cpucore": load.get("cpucore"),
        }
    if uptime:
        out["uptime"] = uptime.get("seconds") if isinstance(uptime, dict) else uptime

    if isinstance(containers, list):
        # MEASURED against the live glances 4.x API: `status` is the DOCKER
        # HEALTH state where a healthcheck exists and the run state where it
        # does not, so this host reports 17 "running", 13 "healthy" and 1
        # "unhealthy". Treating anything != "running" as broken flagged 13
        # perfectly healthy containers as down on the first render -- exactly
        # the confidently-wrong output this panel exists to avoid.
        healthy_states = ("running", "healthy")
        running = [c for c in containers if c.get("status") in healthy_states]
        top = sorted(
            running, key=lambda c: _num(c.get("cpu_percent")) or 0, reverse=True
        )[:6]
        out["containers"] = {
            "available": True,
            "total": len(containers),
            "running": len(running),
            "unhealthy": [
                f"{c.get('name')} ({c.get('status')})" for c in containers
                if c.get("status") not in healthy_states
            ][:8],
            "top": [{
                "name": c.get("name"),
                "cpu": round(_num(c.get("cpu_percent")) or 0, 1),
                "mem_mb": round((_num(c.get("memory_usage")) or 0) / 1_048_576),
            } for c in top],
        }
    else:
        out["containers"] = {"available": False, "reason": cont_e or "no container data"}

    out["disk"] = {
        "available": bool(fs),
        "reason": (
            "" if fs else
            "glances cannot read Windows disks: Docker Desktop exposes C: and D: "
            "as 9p mounts and the fs plugin only lists physical filesystems"
        ),
        "mounts": [{
            "mnt": f.get("mnt_point"), "percent": _num(f.get("percent")),
            "used_gb": round((_num(f.get("used")) or 0) / 1_073_741_824, 1),
            "size_gb": round((_num(f.get("size")) or 0) / 1_073_741_824, 1),
        } for f in (fs or [])],
    }

    temps = []
    for s in (sensors or []):
        if s.get("type") == "temperature_core" or "temp" in str(s.get("unit", "")).lower():
            temps.append({"label": s.get("label"), "value": _num(s.get("value")),
                          "unit": s.get("unit")})
    out["temps"] = {
        "available": bool(temps),
        "reason": "" if temps else (
            f"glances sensors plugin: {sens_e or 'no thermal sensors visible inside the container'}"
        ),
        "sensors": temps,
    }
    return web.json_response(out)


# --------------------------------------------------------------------------
# DEVICES
# --------------------------------------------------------------------------

# Everything the UI is allowed to actuate, declared server-side.
#
# An allowlist rather than a pass-through proxy: this page is reachable by
# anything that can open port 3100, including a phone on the tailnet, so the
# endpoint must not become a general-purpose way to call arbitrary Home
# Assistant services. Adding a capability is a deliberate edit here.
ALLOWED_SCRIPTS = {
    "ac_on", "ac_off", "ac_dry", "ac_max_cool", "ac_set_temperature",
    "fan_set", "fan_boost", "tv_toggle",
    "good_night", "all_off", "movie_time", "feeling_hot", "feeling_cold",
}
TOGGLE_DOMAINS = ("light", "switch", "fan")

GANGS = ("light.node_switch_gang_1", "light.node_switch_gang_2", "light.node_switch_gang_3")


async def devices_panel(request: web.Request) -> web.Response:
    states, err = await _ha_states(request.app["http"])
    if err:
        return web.json_response({"available": False, "reason": err})

    by_id = {s.get("entity_id"): s for s in states}

    def state_of(eid: str) -> str | None:
        s = by_id.get(eid)
        return s.get("state") if s else None

    def attrs_of(eid: str) -> dict:
        s = by_id.get(eid)
        return (s.get("attributes") or {}) if s else {}

    lights = []
    for eid in GANGS + ("light.node_switch_backlight",):
        s = by_id.get(eid)
        if not s:
            continue
        a = s.get("attributes") or {}
        lights.append({
            "entity_id": eid,
            "name": a.get("friendly_name") or eid,
            "state": s.get("state"),
            "brightness": a.get("brightness"),
            "controllable": s.get("state") in ("on", "off"),
        })

    switches = []
    for eid in ("switch.node_switch_main_switch", "switch.node_switch_node_4"):
        s = by_id.get(eid)
        if not s:
            continue
        switches.append({
            "entity_id": eid,
            "name": (s.get("attributes") or {}).get("friendly_name") or eid,
            "state": s.get("state"),
            "controllable": s.get("state") in ("on", "off"),
        })

    # Fan level: the fan entity reports a percentage in 20% steps, and the
    # helper input_number.fan_speed is what the scripts actually drive. Both are
    # shown because they can legitimately disagree while an IR send is in
    # flight, and the helper is the one the next command will act on.
    fan_pct = _num(attrs_of("fan.fan").get("percentage"))
    fan = {
        "entity_id": "fan.fan",
        "state": state_of("fan.fan"),
        "percentage": fan_pct,
        "level": int(round(fan_pct / 20)) if fan_pct is not None else None,
        "helper_level": _num(state_of("input_number.fan_speed")),
        "last_level": _num(state_of("input_number.fan_last_speed")),
        "power_helper": state_of("input_boolean.fan_power"),
        "available": state_of("fan.fan") is not None,
    }

    # The AC has no climate entity: it is an IR device driven by scripts, and
    # its state lives entirely in two helpers. Reading them is the only truthful
    # way to show what the AC is set to.
    ac = {
        "power": state_of("input_boolean.ac_power"),
        "temperature": _num(state_of("input_number.ac_temperature")),
        "blaster": state_of("remote.smart_ir"),
        "available": state_of("input_boolean.ac_power") is not None,
        "reason": "" if state_of("input_boolean.ac_power") is not None else
                  "input_boolean.ac_power is missing from Home Assistant",
    }

    scenes = []
    for name in ("good_night", "movie_time", "all_off", "feeling_hot",
                 "feeling_cold", "ac_dry", "ac_max_cool", "fan_boost", "tv_toggle"):
        s = by_id.get(f"script.{name}")
        if not s:
            continue
        scenes.append({
            "script": name,
            "name": (s.get("attributes") or {}).get("friendly_name") or name,
            "last_triggered": (s.get("attributes") or {}).get("last_triggered"),
        })

    # Entities that exist but are dead. These are the duplicate Tuya *cloud*
    # entities; they are shown, dimmed, rather than filtered, because silently
    # hiding an unavailable device is how a real outage goes unnoticed.
    offline = [{
        "entity_id": s["entity_id"],
        "name": (s.get("attributes") or {}).get("friendly_name") or s["entity_id"],
    } for s in states
        if s.get("state") == "unavailable"
        and s.get("entity_id", "").startswith(("light.", "switch.", "fan.", "climate.", "remote."))]

    return web.json_response({
        "available": True,
        "lights": lights, "switches": switches, "fan": fan, "ac": ac,
        "scenes": scenes, "offline": offline,
    })


async def action(request: web.Request) -> web.Response:
    """
    Actuate one allowlisted thing. The only write path the browser has.

    Every branch validates its own argument range server-side. The UI clamps
    too, but the UI is the untrusted side of this boundary.
    """
    if not HASS_TOKEN:
        return web.json_response({"ok": False, "error": "no Home Assistant token on the server"},
                                 status=503)
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        return web.json_response({"ok": False, "error": "bad JSON"}, status=400)

    kind = str(body.get("action", ""))
    http = request.app["http"]

    async def call(domain: str, service: str, data: dict) -> web.Response:
        t0 = time.monotonic()
        try:
            async with http.post(
                f"{HASS_URL}/api/services/{domain}/{service}",
                headers=_HA_HEADERS, json=data,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                ok = resp.status == 200
                return web.json_response({
                    "ok": ok, "status": resp.status,
                    "ms": int((time.monotonic() - t0) * 1000),
                    "error": "" if ok else f"Home Assistant returned HTTP {resp.status}",
                }, status=200 if ok else 502)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return web.json_response(
                {"ok": False, "error": f"Home Assistant unreachable: {type(exc).__name__}"},
                status=502,
            )

    if kind == "toggle":
        eid = str(body.get("entity_id", ""))
        domain = eid.split(".", 1)[0] if "." in eid else ""
        if domain not in TOGGLE_DOMAINS:
            return web.json_response({"ok": False, "error": f"domain '{domain}' not allowed"},
                                     status=403)
        return await call(domain, "toggle", {"entity_id": eid})

    if kind == "script":
        name = str(body.get("name", ""))
        if name not in ALLOWED_SCRIPTS:
            return web.json_response({"ok": False, "error": f"script '{name}' not allowed"},
                                     status=403)
        return await call("script", name, {})

    if kind == "fan_set":
        level = body.get("level")
        if not isinstance(level, int) or not 0 <= level <= 6:
            return web.json_response(
                {"ok": False, "error": "level must be an integer 0-6 (0=off, 6=boost)"},
                status=400)
        return await call("script", "fan_set", {"level": level})

    if kind == "ac_temp":
        temp = body.get("temperature")
        if not isinstance(temp, (int, float)) or not 16 <= temp <= 30:
            return web.json_response(
                {"ok": False, "error": "temperature must be 16-30"}, status=400)
        return await call("script", "ac_set_temperature", {"temperature": int(temp)})

    return web.json_response({"ok": False, "error": f"unknown action '{kind}'"}, status=400)


# --------------------------------------------------------------------------
# WEATHER
# --------------------------------------------------------------------------

async def weather_panel(request: web.Request) -> web.Response:
    """
    Weather, and an honest account of why there is none.

    `weather.forecast_home` is currently `unavailable`: met.no does not resolve
    from inside the Home Assistant container. The panel says exactly that. The
    sun timings alongside it are real Home Assistant data and keep the panel
    useful while the forecast is down.
    """
    states, err = await _ha_states(request.app["http"])
    if err:
        return web.json_response({"available": False, "reason": err})

    by_id = {s.get("entity_id"): s for s in states}
    w = by_id.get("weather.forecast_home")

    payload: dict[str, Any] = {"entity_id": "weather.forecast_home"}
    if w is None:
        payload.update({"available": False,
                        "reason": "weather.forecast_home does not exist in Home Assistant"})
    elif w.get("state") in (None, "unavailable", "unknown"):
        payload.update({
            "available": False,
            "state": w.get("state"),
            "reason": (
                f"weather.forecast_home reports '{w.get('state')}' — the met.no "
                f"integration cannot resolve DNS inside the Home Assistant container"
            ),
            "since": w.get("last_changed"),
        })
    else:
        a = w.get("attributes") or {}
        payload.update({
            "available": True,
            "state": w.get("state"),
            "temperature": a.get("temperature"),
            "unit": a.get("temperature_unit"),
            "humidity": a.get("humidity"),
            "pressure": a.get("pressure"),
            "wind_speed": a.get("wind_speed"),
            "wind_unit": a.get("wind_speed_unit"),
            "updated": w.get("last_updated"),
        })

    sun = by_id.get("sun.sun")
    payload["sun"] = {
        "available": sun is not None,
        "state": sun.get("state") if sun else None,
        "elevation": (sun.get("attributes") or {}).get("elevation") if sun else None,
        "azimuth": (sun.get("attributes") or {}).get("azimuth") if sun else None,
        "next_rising": (by_id.get("sensor.sun_next_rising") or {}).get("state"),
        "next_setting": (by_id.get("sensor.sun_next_setting") or {}).get("state"),
        "next_dawn": (by_id.get("sensor.sun_next_dawn") or {}).get("state"),
        "next_dusk": (by_id.get("sensor.sun_next_dusk") or {}).get("state"),
    }

    zone = by_id.get("zone.home")
    payload["location"] = (zone.get("attributes") or {}).get("friendly_name") if zone else None
    return web.json_response(payload)


# --------------------------------------------------------------------------
# MEDIA
# --------------------------------------------------------------------------

def _jf_headers() -> dict:
    return {"X-Emby-Token": JELLYFIN_KEY} if JELLYFIN_KEY else {}


async def media_panel(request: web.Request) -> web.Response:
    http = request.app["http"]
    if not JELLYFIN_KEY:
        return web.json_response({
            "available": False,
            "reason": "JELLYFIN_KEY is not set in the jarvis-voice environment",
        })

    sessions, s_err = await _get_json(
        http, f"{JELLYFIN_URL}/Sessions", headers=_jf_headers(), timeout=6)
    counts, c_err = await _get_json(
        http, f"{JELLYFIN_URL}/Items/Counts", headers=_jf_headers(), timeout=6)
    recent, r_err = await _get_json(
        http, f"{JELLYFIN_URL}/Items", headers=_jf_headers(), timeout=8,
        params={
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "SortBy": "DateCreated", "SortOrder": "Descending",
            "Limit": "8", "Fields": "ProductionYear,SeriesName",
        })

    if s_err and c_err and r_err:
        return web.json_response({
            "available": False,
            "reason": f"Jellyfin unreachable at {JELLYFIN_URL} ({s_err})",
        })

    playing = []
    for s in (sessions or []):
        item = s.get("NowPlayingItem")
        if not item:
            continue
        pos = s.get("PlayState", {}) or {}
        ticks = _num(item.get("RunTimeTicks")) or 0
        at = _num(pos.get("PositionTicks")) or 0
        playing.append({
            "user": s.get("UserName"),
            "device": s.get("DeviceName"),
            "client": s.get("Client"),
            "title": item.get("Name"),
            "series": item.get("SeriesName"),
            "type": item.get("Type"),
            "id": item.get("Id"),
            "paused": pos.get("IsPaused"),
            "percent": round(at / ticks * 100, 1) if ticks else None,
            "runtime_min": round(ticks / 600_000_000) if ticks else None,
        })

    return web.json_response({
        "available": True,
        "playing": playing,
        "idle_reason": "" if playing else (
            f"{len(sessions or [])} Jellyfin session(s) connected, none playing"
        ),
        "library": {
            "available": bool(counts),
            "reason": c_err,
            "movies": (counts or {}).get("MovieCount"),
            "series": (counts or {}).get("SeriesCount"),
            "episodes": (counts or {}).get("EpisodeCount"),
        },
        "recent": [{
            "id": i.get("Id"),
            "title": i.get("Name"),
            "series": i.get("SeriesName"),
            "type": i.get("Type"),
            "year": i.get("ProductionYear"),
        } for i in ((recent or {}).get("Items") or [])],
        "recent_error": r_err,
        "sessions_error": s_err,
    })


async def media_art(request: web.Request) -> web.StreamResponse:
    """
    Proxy Jellyfin artwork.

    Exists so the page never needs the Jellyfin key and never needs to reach
    port 8096 itself -- the phone on the tailnet can load posters through the
    one origin it already trusts.
    """
    item = request.query.get("id", "")
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", item):
        return web.Response(status=400, text="bad id")
    height = request.query.get("h", "220")
    if not height.isdigit() or not 16 <= int(height) <= 800:
        height = "220"

    url = f"{JELLYFIN_URL}/Items/{item}/Images/Primary"
    try:
        async with request.app["http"].get(
            url, headers=_jf_headers(), params={"maxHeight": height, "quality": "82"},
            timeout=aiohttp.ClientTimeout(total=8),
        ) as resp:
            if resp.status != 200:
                return web.Response(status=404, text="no artwork")
            body = await resp.read()
            return web.Response(
                body=body,
                content_type=resp.headers.get("Content-Type", "image/jpeg"),
                headers={"Cache-Control": "public, max-age=3600"},
            )
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.Response(status=502, text="jellyfin unreachable")


# --------------------------------------------------------------------------
# DISPLAY DIRECTIVES  --  "if I ask for map, give me map"
# --------------------------------------------------------------------------

# Ordered, first match wins. Order is the whole design: "show me the movie
# posters" must reach MEDIA before DEVICES sees the word "show", and "movie
# time" must NOT open MEDIA because it is a scene that dims the lights.
#
# Matching is on the user's own words, not on the model's reply. The reply is
# generated text and can drift; the utterance is what was asked for.
_RULES: list[tuple[str, re.Pattern]] = [
    ("map", re.compile(
        r"\b(map|where\s+(is|are|am|s)|whereabouts|location of|my location|"
        r"gps|track(ing|er)?|position of|show me where)\b", re.I)),
    ("media", re.compile(
        r"\b(jellyfin|now playing|what.?s playing|media|library|"
        r"recently added|new episodes?|what can i watch|watchlist|posters?)\b", re.I)),
    ("weather", re.compile(
        r"\b(weather|forecast|rain(ing)?|sunrise|sunset|dawn|dusk|"
        r"how (hot|cold) is it outside|outside temperature)\b", re.I)),
    ("vitals", re.compile(
        r"\b(vitals|cpu|ram|memory|load average|server (load|health|status)|"
        r"system (stats|status|health)|containers?|uptime|how.?s the server|"
        r"diagnostics|telemetry)\b", re.I)),
    ("devices", re.compile(
        r"\b(devices?|dashboard|controls?|what.?s on|which lights?|"
        r"show me the (lights?|fan|ac|switches))\b", re.I)),
]

# Guard phrases that must never trigger a panel even though they contain a
# keyword. "movie time" is a scene; "good night" turns things off.
_NEVER = re.compile(r"\b(movie time|good ?night|all off)\b", re.I)


def directive_for(text: str) -> dict | None:
    """
    Map an utterance to a panel. Pure, local, and microseconds -- it must never
    add measurable latency to the voice loop, so it does no I/O and no model
    call. Returns None when nothing matched, which is the common case.
    """
    if not text:
        return None
    if _NEVER.search(text):
        return None
    for panel, pattern in _RULES:
        m = pattern.search(text)
        if m:
            return {"panel": panel, "matched": m.group(0), "query": text}
    return None


async def broadcast_display(app: web.Application, directive: dict) -> int:
    """
    Push a directive to every connected HUD. Returns how many got it.

    Failures to send are swallowed per-client: one dead socket must not stop
    the others from receiving, and must never propagate into the voice loop.
    """
    payload = {"type": "display", **directive}
    sent = 0
    for ws in list(app.get("clients", ())):
        try:
            if not ws.closed:
                await ws.send_json(payload)
                sent += 1
        except (ConnectionResetError, RuntimeError, aiohttp.ClientError):
            pass
    return sent


async def display_push(request: web.Request) -> web.Response:
    """
    Open a panel on every connected HUD, from anything that can reach this port.

    Two jobs: it lets other services (an automation, a Rainmeter bang, a shell
    one-liner) drive the display, and it makes the directive channel testable
    end to end without saying a word.
    """
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        body = {}

    panel = str(body.get("panel", "") or "")
    if not panel and body.get("text"):
        d = directive_for(str(body["text"]))
        if not d:
            return web.json_response(
                {"ok": False, "error": "no panel matched that text", "clients": 0})
        panel = d["panel"]
        body = {**d, **{k: v for k, v in body.items() if k != "text"}}

    known = {"home", "map", "vitals", "devices", "weather", "media"}
    if panel not in known:
        return web.json_response(
            {"ok": False, "error": f"unknown panel '{panel}', expected one of {sorted(known)}"},
            status=400)

    directive = {"panel": panel, "reason": body.get("reason", "api"),
                 "query": body.get("query", "")}
    sent = await broadcast_display(request.app, directive)
    return web.json_response({"ok": True, "panel": panel, "clients": sent})


async def directive_probe(request: web.Request) -> web.Response:
    """Dry-run the matcher. Used by the verification pass; changes nothing."""
    text = request.query.get("text", "")
    return web.json_response({"text": text, "directive": directive_for(text)})


async def voice_state(request: web.Request) -> web.Response:
    """
    Live voice activity for the pulse indicators.

    The value is written by voice_server's Session as it flips between wake /
    listening / busy -- the same state machine that drives the browser orb. The
    Rainmeter skin polls this once a second; the browser HUD does not need it
    because it already owns the state locally.
    """
    return web.json_response(
        request.app.get("voice_state", {"state": "idle", "level": 0.0})
    )


# --------------------------------------------------------------------------
# SCREEN CAPTURE
# --------------------------------------------------------------------------

_SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(_SCREENSHOT_DIR, exist_ok=True)


async def screen_capture(request: web.Request) -> web.Response:
    """
    Serve the most recent desktop screenshot.

    Screenshots are written by the periodic host-side script
    (jarvis/capture_screen.py) and cached here as .png files. This endpoint
    returns the latest one with a Cache-Control header so Rainmeter can
    poll it as an Image meter without reloading the full image every tick.
    """
    files = sorted(
        (f for f in os.listdir(_SCREENSHOT_DIR) if f.endswith(".png")),
        key=lambda f: os.path.getmtime(os.path.join(_SCREENSHOT_DIR, f)),
        reverse=True,
    )
    if not files:
        return web.json_response(
            {"available": False, "reason": "no screenshots captured yet"},
        )
    latest = os.path.join(_SCREENSHOT_DIR, files[0])
    stat = os.stat(latest)
    return web.FileResponse(
        latest,
        headers={
            "Cache-Control": "no-cache",
            "Last-Modified": stat.st_mtime,
        },
    )


async def screen_capture_status(request: web.Request) -> web.Response:
    """Lightweight status: is the screenshot pipeline alive and what was the
    last capture time? Rainmeter uses this to decide whether to show a
    placeholder or the actual image."""
    files = sorted(
        (f for f in os.listdir(_SCREENSHOT_DIR) if f.endswith(".png")),
        key=lambda f: os.path.getmtime(os.path.join(_SCREENSHOT_DIR, f)),
        reverse=True,
    )
    if not files:
        return web.json_response({"available": False, "reason": "no screenshots yet"})
    latest = os.path.join(_SCREENSHOT_DIR, files[0])
    stat = os.stat(latest)
    return web.json_response({
        "available": True,
        "filename": files[0],
        "size_kb": round(stat.st_size / 1024),
        "age_seconds": round(time.time() - stat.st_mtime),
    })


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------

def register(app: web.Application) -> None:
    """Attach every HUD route. Called once from voice_server.build_app()."""
    app.setdefault("clients", set())
    app.add_routes([
        web.get("/api/hud/map", map_panel),
        web.get("/api/hud/vitals", vitals_panel),
        web.get("/api/hud/devices", devices_panel),
        web.get("/api/hud/weather", weather_panel),
        web.get("/api/hud/media", media_panel),
        web.get("/api/hud/media/art", media_art),
        web.get("/api/hud/voice", voice_state),
        web.post("/api/hud/action", action),
        web.post("/api/hud/display", display_push),
        web.get("/api/hud/directive", directive_probe),
        # Screen capture: the desktop image and its metadata.
        web.get("/api/hud/screen", screen_capture),
        web.get("/api/hud/screen/status", screen_capture_status),
    ])
