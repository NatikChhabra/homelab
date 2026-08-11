#!/usr/bin/env python3
"""
Host-side call bridge wrapper.

The real call_bridge.py needs numpy, sounddevice, and Bluetooth HFP — none of
which work inside Docker. This thin wrapper runs on the HOST and exposes a
simple HTTP API that the container can call via host.docker.internal.

Endpoints:
  POST /call/make      {"contact": "mum"}
  POST /call/answer    {}
  POST /call/reject    {}
  GET  /call/state     {}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis.call_bridge")

HERE = Path(__file__).resolve().parent
CALL_BRIDGE = HERE.parent / "calls" / "call_bridge.py"
PORT = int(os.getenv("CALL_BRIDGE_PORT", "8098"))


async def handle_make(request: web.Request) -> web.Response:
    body = await request.json()
    contact = body.get("contact", "")
    if not contact:
        return web.json_response({"ok": False, "error": "no contact"}, status=400)
    cmd = [sys.executable, str(CALL_BRIDGE), "--mode", "announce",
           "--contact", contact,
           "--message", "Hello, this is JARVIS calling on behalf of Natik."]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        # call_bridge.py logs to stdout, not stderr — capture both
        # stdout/stderr are bytes; decode safely
        if isinstance(stderr, bytes):
            err = stderr.decode("utf-8", errors="replace")[:200]
        else:
            err = str(stderr or "")[:200]
        if isinstance(stdout, bytes):
            out = stdout.decode("utf-8", errors="replace")[:200]
        else:
            out = str(stdout or "")[:200]
        if proc.returncode != 0:
            reason = err or out or f"exit code {proc.returncode}"
            return web.json_response({"ok": False, "error": reason}, status=500)
        try:
            return web.json_response(json.loads(stdout) if stdout else {"ok": True})
        except json.JSONDecodeError:
            return web.json_response({"ok": True, "raw": stdout.decode("utf-8", errors="replace")})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_answer(request: web.Request) -> web.Response:
    cmd = [sys.executable, str(CALL_BRIDGE), "--mode", "answer"]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        # capture both streams (call_bridge.py logs to stdout)
        if isinstance(stderr, bytes):
            err = stderr.decode("utf-8", errors="replace")[:200]
        else:
            err = str(stderr or "")[:200]
        if isinstance(stdout, bytes):
            out = stdout.decode("utf-8", errors="replace")[:200]
        else:
            out = str(stdout or "")[:200]
        if proc.returncode != 0:
            return web.json_response({"ok": False, "error": err or out}, status=500)
        return web.json_response(json.loads(stdout) if stdout else {"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_reject(request: web.Request) -> web.Response:
    cmd = [sys.executable, str(CALL_BRIDGE), "--mode", "reject"]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        # capture both streams
        if isinstance(stderr, bytes):
            err = stderr.decode("utf-8", errors="replace")[:200]
        else:
            err = str(stderr or "")[:200]
        if isinstance(stdout, bytes):
            out = stdout.decode("utf-8", errors="replace")[:200]
        else:
            out = str(stdout or "")[:200]
        if proc.returncode != 0:
            return web.json_response({"ok": False, "error": err or out}, status=500)
        return web.json_response(json.loads(stdout) if stdout else {"ok": True})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


async def handle_state(request: web.Request) -> web.Response:
    cmd = [sys.executable, str(CALL_BRIDGE), "--mode", "state"]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
        # call_bridge.py outputs JSON to stdout even on error
        if isinstance(stdout, bytes):
            out = stdout.decode("utf-8", errors="replace")
        else:
            out = str(stdout or "")
        if isinstance(stderr, bytes):
            err = stderr.decode("utf-8", errors="replace")
        else:
            err = str(stderr or "")
        # Try to parse JSON response first
        try:
            data = json.loads(out.strip()) if out.strip() else {}
            if proc.returncode != 0:
                reason = data.get("reason", err or out[:200])
                return web.json_response({"ok": False, "error": reason}, status=500)
            return web.json_response(data)
        except json.JSONDecodeError:
            # Fallback to plain text error
            if proc.returncode != 0:
                return web.json_response({"ok": False, "error": err or out[:200]}, status=500)
            return web.json_response({"ok": True, "raw": out[:200]})
    except Exception as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=500)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/call/make", handle_make)
    app.router.add_post("/call/answer", handle_answer)
    app.router.add_post("/call/reject", handle_reject)
    app.router.add_get("/call/state", handle_state)
    return app


if __name__ == "__main__":
    if not CALL_BRIDGE.exists():
        log.error("call_bridge.py not found at %s", CALL_BRIDGE)
        sys.exit(1)
    web.run_app(build_app(), host="127.0.0.1", port=PORT)
