#!/usr/bin/env python3
"""
JARVIS capability service: persistent timers, spoken server status, and
proactive announcements.

WHY A SEPARATE SERVICE RATHER THAN MORE HOME ASSISTANT YAML
Timers need to survive a restart and fire on their own schedule, and HA's own
timer intents cannot be reached from this pipeline at all (see timers.py for the
two independent reasons). Announcements need to originate here, unprompted, with
no tool call involved. Neither is expressible in a template.

TOOL BUDGET: EXACTLY TWO. A 3B model degrades badly past roughly a dozen tools,
and 12 already exist. So the whole of this service is exposed to the LLM as two
grouped HA scripts -- script.jarvis_timer and script.jarvis_status -- and
announcements are exposed as nothing at all, because push costs zero.

THE REPLY IS COMPOSED HERE, NOT BY THE MODEL. Every endpoint returns
{"speech": "<exact sentence>"}, the HA script hands that back as its response,
and the system prompt tells the model to say it verbatim. This is what makes a
3B model reliable on a status report: it is not summarising anything.

Endpoints
  POST /api/timer   {action, duration_or_time, label}  -> {"speech": ...}
  POST /api/status  {topic}                            -> {"speech": ...}
  GET  /ws/announce                                    -> live PCM sink
  GET  /api/announce/pending                           -> queued wavs for a host player
  POST /api/announce/test  {text}                      -> force one, for testing
  GET  /api/debug/state                                -> timers, gates, history
  GET  /healthz
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime

import aiohttp
from aiohttp import web, WSMsgType

import announce as announce_mod
import parsing
import status as status_mod
import watcher as watcher_mod
from announce import Announcement, Announcer
from timers import TimerStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("capabilities")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


async def timer_endpoint(request: web.Request) -> web.Response:
    store: TimerStore = request.app["timers"]
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}

    action = str(body.get("action") or "set").strip().lower()
    text = str(body.get("duration_or_time") or "").strip()
    label = str(body.get("label") or "").strip()

    # The model sometimes puts the whole phrase in `label` and leaves
    # duration_or_time empty. Recovering here is cheaper than retraining a 3B.
    if action in ("set", "start", "create", "add", "new") and not text and label:
        try:
            parsing.resolve(label, datetime.now())
            text, label = label, ""
        except parsing.ParseError:
            pass

    if action in ("cancel", "stop", "delete", "remove", "clear"):
        killed = store.cancel(label)
        return web.json_response({"speech": store.reply_cancel(killed, label),
                                  "cancelled": len(killed)})

    if action in ("list", "show", "status", "check", "remaining"):
        return web.json_response({"speech": store.reply_list(),
                                  "active": len(store.active())})

    try:
        timer, described = store.add(text, label)
    except parsing.ParseError as exc:
        log.info("timer parse failed: %s", exc)
        return web.json_response({
            "speech": "How long should I set it for?",
            "error": str(exc),
        })

    log.info("timer set: %r fires at %s (%s)", timer.label or timer.kind,
             datetime.fromtimestamp(timer.fire_at).isoformat(timespec="seconds"), described)
    return web.json_response({
        "speech": store.reply_set(timer, described),
        "id": timer.id,
        "fires_at": datetime.fromtimestamp(timer.fire_at).isoformat(timespec="seconds"),
    })


async def status_endpoint(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    topic = str(body.get("topic") or "server")
    speech = await status_mod.handle(request.app["http"], topic)
    log.info("status(%s) -> %r", topic, speech)
    return web.json_response({"speech": speech})


async def announce_ws(request: web.Request) -> web.WebSocketResponse:
    """
    Live announcement sink. Mirrors voice_server.py's browser frames -- a JSON
    header, then raw PCM, then an end frame -- so the existing UI can subscribe
    without inventing a second audio protocol.
    """
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    announcer: Announcer = request.app["announcer"]
    queue = announcer.subscribe()
    log.info("announce sink connected (%d total)", announcer.subscriber_count)

    async def pump() -> None:
        while True:
            item = await queue.get()
            if isinstance(item, bytes):
                await ws.send_bytes(item)
            else:
                await ws.send_json(item)

    pump_task = asyncio.create_task(pump())
    try:
        await ws.send_json({"type": "ready", "rate": 16000, "width": 2, "channels": 1})
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
    finally:
        pump_task.cancel()
        announcer.unsubscribe(queue)
        log.info("announce sink disconnected (%d left)", announcer.subscriber_count)
    return ws


async def announce_pending(request: web.Request) -> web.Response:
    """
    Queued .wav files for the host-side player.

    A container on Windows has no audio device, so this is the path that
    actually reaches a speaker. The player GETs this, plays each file, then
    DELETEs it.
    """
    names = []
    try:
        for name in sorted(os.listdir(announce_mod.AUDIO_DIR)):
            if name.endswith(".wav"):
                names.append(name)
    except OSError:
        pass
    return web.json_response({"pending": names, "dir": announce_mod.AUDIO_DIR})


async def announce_file(request: web.Request) -> web.StreamResponse:
    name = os.path.basename(request.match_info["name"])
    path = os.path.join(announce_mod.AUDIO_DIR, name)
    if not name.endswith(".wav") or not os.path.exists(path):
        raise web.HTTPNotFound()
    if request.method == "DELETE":
        try:
            os.remove(path)
        except OSError:
            pass
        return web.json_response({"ok": True})
    return web.FileResponse(path)


async def announce_test(request: web.Request) -> web.Response:
    """Force an announcement. `kind` selects which gates apply."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    text = str(body.get("text") or "This is a test announcement.")
    kind = str(body.get("kind") or "test")
    result = await request.app["announcer"].speak(Announcement(text=text, kind=kind))
    return web.json_response(result)


async def watch_now(request: web.Request) -> web.Response:
    """Run one audit-watch pass immediately instead of waiting for the poll."""
    result = await request.app["watcher"].check_once()
    return web.json_response(result)


async def debug_state(request: web.Request) -> web.Response:
    store: TimerStore = request.app["timers"]
    watch: watcher_mod.AuditWatcher = request.app["watcher"]
    announcer: Announcer = request.app["announcer"]
    now = datetime.now()
    return web.json_response({
        "timers": [
            {
                "id": t.id, "label": t.label, "kind": t.kind,
                "fires_at": datetime.fromtimestamp(t.fire_at).isoformat(timespec="seconds"),
                "remaining_s": round(t.remaining, 1),
            }
            for t in store.active()
        ],
        "announcer": {
            "ws_sinks": announcer.subscriber_count,
            "in_quiet_hours": announce_mod.in_quiet_hours(now),
            "quiet_window": f"{announce_mod.QUIET_START}-{announce_mod.QUIET_END}",
            "today_count": announcer._day_count,
            "daily_cap": announce_mod.MAX_PER_DAY,
            "history": announcer.history[-10:],
        },
        "watcher": {
            "known_failures": len(watch.seen),
            "held": watch.held,
            "checks": watch.checks,
            "last_check": watch.last_check,
        },
    })


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "timers": len(request.app["timers"].active())})


async def announcer_page(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(os.path.join(STATIC_DIR, "announcer.html"))


async def on_startup(app: web.Application) -> None:
    app["http"] = aiohttp.ClientSession()
    announcer = Announcer()
    app["announcer"] = announcer

    store = TimerStore(announcer)
    app["timers"] = store
    await store.start()

    watch = watcher_mod.AuditWatcher(announcer)
    app["watcher"] = watch
    await watch.start()

    log.info("capability service up: %d timer(s) restored", len(store.active()))


async def on_cleanup(app: web.Application) -> None:
    await app["watcher"].stop()
    await app["timers"].stop()
    await app["http"].close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/", announcer_page),
        web.get("/healthz", healthz),
        web.post("/api/timer", timer_endpoint),
        web.post("/api/status", status_endpoint),
        web.get("/ws/announce", announce_ws),
        web.get("/api/announce/pending", announce_pending),
        web.get("/api/announce/file/{name}", announce_file),
        web.delete("/api/announce/file/{name}", announce_file),
        web.post("/api/announce/test", announce_test),
        web.post("/api/watch/now", watch_now),
        web.get("/api/debug/state", debug_state),
    ])
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "3101")))
