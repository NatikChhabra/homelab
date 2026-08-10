#!/usr/bin/env python3
r"""
Server health, disk, and the morning briefing, spoken.

SOURCES, AND WHY THESE AND NOT OTHERS
  * Disk: server-audit-report.txt. Glances fs API returns exactly [] on this
    host (containerised on Windows with no host filesystem mounted) -- measured
    repeatedly, not assumed. The audit script reads the real volumes, so the
    file is a better disk source than anything Glances could provide anyway.
  * CPU / RAM / uptime: Glances quicklook/mem. These work; only fs is broken.
  * Containers: Glances containers endpoint.
  * Uptime Kuma is deliberately NOT a source: 1.x has no usable REST API
    (:3001/metrics is 401 with no key to be found), so it was excluded.

The audit report lines parse with ^\s+(OK|WARN|FAIL)\s+(.*)$ and the file
currently holds real failures -- at time of writing, the Weekly Audit scheduled
task exiting 99 and an Immich photo lost -- so tests here run against genuine
conditions, not fixtures.

SPEECH IS COMPOSED HERE, IN PYTHON, AND THE MODEL READS IT OUT VERBATIM. The
tool returns {"success": true, "result": <sentence>} and the system prompt
instructs the model to speak that sentence unchanged. A 3B model summarising
structured data makes lists; a number and a verdict is what a person wants.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time

import aiohttp

log = logging.getLogger("capabilities.status")

AUDIT_PATH = os.getenv("AUDIT_REPORT", "/audit/server-audit-report.txt")
GLANCES_URL = os.getenv("GLANCES_URL", "http://host.docker.internal:61208").rstrip("/")
VIKUNJA_URL = os.getenv("VIKUNJA_URL", "http://host.docker.internal:3456").rstrip("/")
# Vikunja's /api/v1/tasks is 401 without a token (measured), and there is no
# Vikunja token in this stack's secrets. Without one the briefing simply omits
# tasks rather than reporting a non-problem every single morning -- an assistant
# that reports its own missing configuration daily is one that gets muted.
VIKUNJA_TOKEN = os.getenv("VIKUNJA_TOKEN", "")
AUDIT_MAX_AGE_H = float(os.getenv("AUDIT_MAX_AGE_H", "6"))

_LINE_RE = re.compile(r"^\s+(OK|WARN|FAIL)\s+(.*)$")

_COUNT_WORDS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five"}

# Speakable short names for the failure kinds that matter to a person. Anything
# not here falls back to its literal line text.
SPEAKABLE = {
    "task 'ServerData - Weekly Audit' last result 99": "the weekly audit task failed",
    "Immich backup SHRANK": "an Immich photo was lost",
    "NO offsite backup configured": "there is no offsite backup",
    "docker credential helper is failing": "Docker pull is broken",
    "Immich: 105/836 assets have no original file": "some Immich photos are missing their files",
}


def parse_report(text: str) -> dict:
    """Name the lines that matter. Never raises; a missing report is itself an answer."""
    if not text or not text.strip():
        return {"summary": "missing", "fails": [], "warns": [], "containers": None, "disk": None}

    fails, warns = [], []
    disk = {}
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        status, body = m.group(1), m.group(2).strip()
        if status == "FAIL":
            fails.append(body)
        elif status == "WARN":
            warns.append(body)
        if body.startswith("/c ") or body.startswith("/d "):
            m2 = re.match(r"(/\w+)\s+(\d+)% used,\s*(\d+)G free", body)
            if m2:
                disk[m2.group(1)] = {"percent": int(m2.group(2)), "free_gb": int(m2.group(3))}
    return {"summary": "ok" if not fails else "degraded", "fails": fails, "warns": warns, "disk": disk}


async def read_audit() -> dict:
    """Fresh copy of the report, plus its age in minutes (None if absent)."""
    try:
        with open(AUDIT_PATH, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {"report": parse_report(""), "age_min": None}

    try:
        age_min = (time.time() - os.path.getmtime(AUDIT_PATH)) / 60
    except OSError:
        age_min = None
    return {"report": parse_report(text), "age_min": age_min}


def _fmt_age(minutes: float) -> str:
    if minutes is None:
        return "unknown"
    if minutes < 120:
        return f"{int(minutes)} minutes"
    return f"{minutes / 60:.1f} hours"


def sentence_verdict(report: dict, age_min: float | None) -> str:
    """One clause on the audit: a count and the leading problem, never a list."""
    fails = report["fails"]
    n = len(fails)
    if n == 0:
        return "All clear"
    count = _COUNT_WORDS.get(n, str(n))
    if n == 1:
        return f"One problem: {_speakable(fails[0])}"
    return f"{count} problems, starting with {_speakable(fails[0])}"


def _speakable(body: str) -> str:
    for key, spoken in SPEAKABLE.items():
        if body.startswith(key):
            return spoken
    # Fall back to a shortened literal, e.g. "Voltas AC: not responding".
    return body.split(":")[0] if ":" in body else body[:60]


async def _glances(http: aiohttp.ClientSession, path: str):
    try:
        async with http.get(
            f"{GLANCES_URL}/api/4/{path}", timeout=aiohttp.ClientTimeout(total=4)
        ) as resp:
            return await resp.json() if resp.status == 200 else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


def _vitals_clause(cpu, ram, disk_free_gb) -> str:
    """
    The short vitals phrase.

    RAM is mentioned only when it is actually interesting. A metric that is
    always recited is a metric nobody hears; a metric that only appears when it
    is high is a metric that means something when it does.
    """
    parts = []
    if cpu is not None:
        parts.append(f"CPU {int(cpu)} percent")
    if ram is not None and ram >= 85:
        parts.append(f"RAM {int(ram)} percent")
    if disk_free_gb is not None:
        parts.append(f"{disk_free_gb} gig free")
    return ", ".join(parts)


async def sentence_server(http: aiohttp.ClientSession) -> str:
    """CPU / RAM from Glances; disk and failures from the audit file."""
    result = await read_audit()
    report, age = result["report"], result["age_min"]

    quick = await _glances(http, "quicklook")
    mem = await _glances(http, "mem")
    if quick is None and mem is None and not report["disk"]:
        return "I cannot reach the server metrics."

    cpu = round(float(quick.get("cpu", 0))) if quick else None
    ram = round(float(mem.get("percent", 0))) if mem else None
    disk_c = report["disk"].get("/c") or {}
    vitals = _vitals_clause(cpu, ram, disk_c.get("free_gb"))

    verdict = sentence_verdict(report, age)
    if not vitals:
        return f"{verdict}."
    return f"{verdict}. {vitals}."


async def sentence_alerts(http: aiohttp.ClientSession) -> str:
    result = await read_audit()
    fails = result["report"]["fails"]
    if not fails:
        return "Nothing is down. The server audit is clear."
    if len(fails) == 1:
        return f"One alert: {_speakable(fails[0])}."
    count = _COUNT_WORDS.get(len(fails), str(len(fails)))
    return f"{count} alerts. {_speakable(fails[0]).capitalize()}, and {len(fails) - 1} more."


async def sentence_briefing(http: aiohttp.ClientSession) -> str:
    """
    Morning briefing: overnight audit result, anything down, Vikunja if it can
    be reached with a token. Deliberately the only reply allowed to run past a
    dozen words, because the user asked for a summary of several things.
    """
    result = await read_audit()
    report, age = result["report"], result["age_min"]

    parts = [f"Good morning. {sentence_verdict(report, age)} on the audit from {_fmt_age(age)} ago."]

    uptime = await _glances(http, "uptime")
    hours = _uptime_hours(uptime)
    if hours is not None:
        parts.append(f"Server up {hours}.")

    if VIKUNJA_TOKEN:
        count = await _vikunja_tasks(http)
        if count is None:
            parts.append("Vikunja did not answer.")
        elif count:
            parts.append(f"{count} open task{'s' if count != 1 else ''}.")
        else:
            parts.append("No open tasks.")
    return " ".join(parts)


def _uptime_hours(raw) -> str | None:
    """Glances returns uptime as a bare "H:MM:SS" string, not a number."""
    if not isinstance(raw, str):
        return None
    m = re.match(r"(\d+)\s*days?,?\s*(\d+):", raw) or re.match(r"^(\d+):", raw)
    try:
        if m and m.lastindex == 2:
            return f"{int(m.group(1))} days"
        if m:
            h = int(m.group(1))
            return f"{h} hour{'s' if h != 1 else ''}"
    except (ValueError, IndexError):
        return None
    return None


async def _vikunja_tasks(http: aiohttp.ClientSession) -> int | None:
    try:
        async with http.get(
            f"{VIKUNJA_URL}/api/v1/tasks/all?page=1&per_page=1&filter=done%3Dfalse",
            headers={"Authorization": f"Bearer {VIKUNJA_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                log.info("vikunja returned %s", resp.status)
                return None
            # The total lives in a header; the body is just the first page.
            total = resp.headers.get("x-pagination-total-pages")
            data = await resp.json()
            if isinstance(data, list) and total is not None:
                return len(data) * 0 + int(total) if len(data) else 0
            return len(data) if isinstance(data, list) else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


async def handle(http: aiohttp.ClientSession, topic: str) -> str:
    """Entry point for the status tool. Returns the exact sentence to speak."""
    topic = (topic or "server").strip().lower()
    if topic in ("alert", "alerts", "down", "problems"):
        return await sentence_alerts(http)
    if topic in ("briefing", "brief", "morning", "daily"):
        return await sentence_briefing(http)
    if topic in ("disk", "storage", "space"):
        result = await read_audit()
        report = result["report"]
        disk_c = report["disk"].get("/c") or {}
        if not disk_c:
            return "Disk metrics are unavailable."
        return f"Disk is {disk_c['free_gb']} gig free, {disk_c['percent']} percent used."
    return await sentence_server(http)
