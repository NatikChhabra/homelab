"""
Execute the tool calls Hermes selects.

Why this file has to exist
--------------------------
`hermes.py` classifies an utterance to a specialist and asks qwen2.5:3b which
tool to call with which arguments. It stops there. Nothing dispatched the result,
so Hermes was a benchmark rather than a feature: it could score 20/21 on routing
and still leave the lights untouched.

Normally Home Assistant's own conversation agent executes the tools it offers.
Hermes deliberately bypasses that agent -- it talks to Ollama directly, because
that is the only way to control which tools the model sees, and controlling the
tool count is the whole point (17 tools live, 30+ broke the model outright, a
specialist offers 3-6). Bypassing the agent means inheriting its job.

Two dispatch paths, because the tools are two different kinds of thing
---------------------------------------------------------------------
1. INTENTS -- HassTurnOn, HassTurnOff, HassLightSet. Built into HA, they take a
   NAME and resolve it against entities, aliases and areas. Sent to
   `POST /api/intent/handle`, which is the same machinery
   `conversation.home_assistant` uses, so name resolution behaves identically on
   both routing paths. That matters: the local path and the Hermes path must not
   disagree about what "light one" means.

2. SCRIPTS -- ac_on, ac_set_temperature, fan_set, good_night and the rest. Every
   Hermes tool name maps 1:1 onto a real `script.<name>` entity, verified
   against the live instance. Sent to `POST /api/services/script/<name>`, with
   the model's arguments passed as script variables.

Arguments are validated here, not trusted
-----------------------------------------
The model emits arguments and models get them wrong. The specific failure
already recorded in this project is `set the AC to 22` returning `action=on`
with the temperature dropped entirely. Worse in the other direction: a string
"22" where an integer is required, or a temperature of 220.

So every argument is coerced and range-checked against the same bounds the HA
script declares (temperature 16-30, fan level 0-6). An out-of-range value is
CLAMPED and reported, never silently sent -- a rejected service call would make
JARVIS claim success while nothing moved, which is the phantom-action failure
this project has paid for repeatedly.

Unknown tool names are refused rather than forwarded. A hallucinated tool name
reaching HA would either 404 or, worse, match some unintended script.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncio
import aiohttp

log = logging.getLogger("jarvis.hermes.exec")

# Tools that are HA INTENTS rather than scripts. Everything else in hermes.TOOLS
# is a script of the same name.
INTENT_TOOLS = {"HassTurnOn", "HassTurnOff", "HassLightSet"}

# Read-only; handled locally without touching HA services.
CONTEXT_TOOLS = {"GetLiveContext"}

# Argument schemas, mirroring the bounds declared by the HA scripts themselves
# (/config/packages/jarvis.yaml). Kept here so a bad value is caught before the
# network call, and so the clamp is visible in one place.
#   tool -> {arg: (type, min, max)}
ARG_SPEC: dict[str, dict[str, tuple[type, int, int]]] = {
    "ac_set_temperature": {"temperature": (int, 16, 30)},
    "fan_set": {"level": (int, 0, 6)},
    "HassLightSet": {"brightness": (int, 0, 100)},
    "pc_set_volume": {"level": (int, 0, 100)},
}

# Schema `required` is model-facing, not a safety gate. If qwen drops a required
# argument, HA script defaults are dangerous: fan_set {} becomes level 0 (OFF),
# ac_set_temperature {} becomes 24 (ON). Refuse before dispatch instead.
REQUIRED_ARGS: dict[str, set[str]] = {
    "ac_set_temperature": {"temperature"},
    "fan_set": {"level"},
    "pc_set_volume": {"level"},
    "pc_app": {"action", "name"},
    "pc_media": {"action"},
    "pc_power": {"action"},
    "pc_type": {"text"},
    "wa_send": {"contact", "message"},
}


# --- the third dispatch path: plain HTTP services on the host ---------------
#
# WHY THIS EXISTS, AND THE BUG IT FIXES
# hermes.py has carried six pc_* tools and a "pc" specialist for some time, and
# the pc-agent on 8099 answers all of them correctly by curl. They were still
# completely dead by voice. Every tool that is not an INTENT and not a CONTEXT
# tool fell through to `POST /api/services/script/<tool>` -- so `pc_app` became
# a call to `script.pc_app`, which does not exist. Verified 2026-08-09: Home
# Assistant has 14 scripts and not one of them starts with `pc_`.
#
# The failure was invisible in the worst possible way. The executor reported
# exec_failed, Hermes fell through to the big agent, and the big agent -- which
# has no PC tools either -- answered from its own head. "Opening Spotify, sir"
# with nothing launched. That is the phantom-action defect this project has paid
# for more than any other, reappearing through a routing gap rather than a
# model gap.
#
# So HTTP_ROUTES is the explicit third path. A tool listed here is dispatched to
# a host service instead of Home Assistant, and anything NOT listed still goes
# to a script, so the existing behaviour is untouched.
#
# Both services bind 127.0.0.1 only. Docker Desktop presents container traffic
# to the host as loopback, which is how a container reaches them at all -- this
# was measured against the PC agent, not assumed.
PC_URL = os.getenv("JARVIS_PC_URL", "http://host.docker.internal:8099").rstrip("/")
WA_URL = os.getenv("JARVIS_WA_URL", "http://host.docker.internal:8097").rstrip("/")
# Web search service. The ONLY outbound internet path in this build.
# JARVIS_WEBSEARCH_ENABLED=0 disables all outbound traffic at the top of the
# handler, before DNS, before a socket.
SEARCH_URL = os.getenv("JARVIS_SEARCH_URL", "http://jarvis-websearch:3101").rstrip("/")
# Capabilities server on the host (Glances proxy + audit report).
CAPABILITIES_URL = os.getenv("JARVIS_CAPABILITIES_URL", "http://host.docker.internal:3102").rstrip("/")
# Call bridge HTTP API on the host. The real call_bridge.py needs numpy +
# audio hardware, so this wraps it in a thin HTTP server on port 8098.
CALL_URL = os.getenv("JARVIS_CALL_URL", "http://host.docker.internal:8098").rstrip("/")

# The PC agent accepts either token name, preferring the specific one. Falling
# back to the WOL token is not sloppiness: pc-agent.ps1 replaced wol-helper.ps1
# on the same port and deliberately kept accepting its token.
PC_TOKEN = os.getenv("JARVIS_PC_TOKEN") or os.getenv("JARVIS_WOL_TOKEN") or ""
WA_TOKEN = os.getenv("JARVIS_WA_TOKEN", "")

# Shutdown and restart are held to a longer delay than the agent's own default
# of 20 s. The agent already refuses without {"confirm": true} and exposes
# POST /pc/power/cancel; this widens the window in which a mistake can be taken
# back, because the thing setting `confirm` is a 3B model hearing a noisy room.
_POWER_DELAY_S = 30


def _pc_route(tool: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | str:
    """
    Map one pc_* tool call onto a real pc-agent route.

    Returns (method, path, body), or a string explaining the refusal. Routes are
    taken from the route table in host-helper/pc-agent.ps1, not from memory.
    """
    if tool == "pc_status":
        # NOT GET /pc/status. That route returns Get-PcStatus, whose only
        # power-ish field is `interactive` -- the agent's own session flag,
        # true whenever the agent is reachable, so it can never report the PC
        # asleep or off. The legacy GET /status route probes the target PC
        # (ICMP/TCP/ARP) and returns a real `state`. A read reports what it
        # measured, which is the grounded-read rule this file is built around.
        return ("GET", "/status", {})

    if tool == "pc_set_volume":
        if "level" not in args:
            return "no volume level given"
        return ("POST", "/pc/volume", {"level": args["level"]})

    if tool == "pc_type":
        text = str(args.get("text") or "")
        if not text:
            return "nothing to type"
        # The agent rejects over 500 characters outright; truncating here keeps a
        # long dictation useful instead of failing the whole turn.
        return ("POST", "/pc/input/type", {"text": text[:500]})

    if tool == "pc_app":
        action = str(args.get("action") or "").lower()
        name = str(args.get("name") or "")
        if action not in ("open", "close"):
            return f"unknown app action {action!r}"
        if not name:
            return "no app name given"
        return ("POST", f"/pc/app/{action}", {"name": name})

    if tool == "pc_media":
        action = str(args.get("action") or "").lower()
        # Mirrors the schema enum exactly. The model can only emit these, and
        # the pc-agent only implements these routes; accepting anything else
        # here would hand a 404 a name the agent never planned for.
        allowed = ("play_pause", "next", "previous", "stop", "mute",
                   "volume_up", "volume_down")
        if action not in allowed:
            return f"unknown media action {action!r}"
        return ("POST", f"/pc/media/{action}", {})

    if tool == "pc_power":
        action = str(args.get("action") or "").lower()
        if action == "wake":
            return ("POST", "/pc/power/wake", {})
        if action in ("sleep", "lock"):
            return ("POST", f"/pc/power/{action}", {})
        if action in ("shutdown", "restart"):
            # Never synthesised here. If the model did not ask for confirmation
            # the call is refused, exactly as the agent would refuse it -- the
            # gate is worth nothing if the client fills it in automatically.
            if args.get("confirm") is not True:
                return f"{action} needs explicit confirmation"
            return ("POST", f"/pc/power/{action}",
                    {"confirm": True, "delay_seconds": _POWER_DELAY_S})
        return f"unknown power action {action!r}"

    return f"no route for {tool!r}"


def _wa_route(tool: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | str:
    """Map a WhatsApp tool onto the bridge's API."""
    if tool == "wa_read":
        return ("GET", "/messages?limit=5", {})
    if tool == "wa_send":
        to = str(args.get("contact") or args.get("to") or args.get("name") or "")
        msg = str(args.get("message") or args.get("text") or "")
        if not to:
            return "no contact given"
        if not msg:
            return "no message text given"
        # The bridge caps at 1000 and refuses anything longer. Truncating would
        # silently send a half message to a real person, so this refuses instead
        # -- the one place in this file where refusing beats correcting.
        if len(msg) > 1000:
            return "message too long"
        return ("POST", "/send", {"to": to, "message": msg})
    return f"no route for {tool!r}"


def _search_route(tool: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | str:
    """Map a web search tool onto the websearch service."""
    if tool == "web_search":
        q = str(args.get("query") or "").strip()
        if not q:
            return "no search query given"
        if len(q) > 300:
            return "search query too long"
        return ("POST", "/search", {"q": q})
    if tool == "web_fetch":
        url = str(args.get("url") or "").strip()
        if not url:
            return "no URL given"
        if not url.startswith(("http://", "https://")):
            return f"invalid URL: {url!r}"
        return ("GET", f"/fetch?url={url}", {})
    return f"no route for {tool!r}"


def _capabilities_route(tool: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | str:
    """Map a server status tool onto the capabilities service."""
    if tool == "server_status":
        return ("POST", "/api/status", {})
    return f"no route for {tool!r}"


def _call_route(tool: str, args: dict[str, Any]) -> tuple[str, str, dict[str, Any]] | str:
    """Map a call tool onto call_bridge.py via subprocess."""
    if tool == "call_make":
        contact = str(args.get("contact") or "").strip()
        if not contact:
            return "no contact given"
        return ("CALL", "make", {"contact": contact})
    if tool == "call_answer":
        return ("CALL", "answer", {})
    if tool == "call_reject":
        return ("CALL", "reject", {})
    if tool == "call_state":
        return ("CALL", "state", {})
    return f"no route for {tool!r}"


# tool -> which host service dispatches it
HTTP_ROUTES: dict[str, str] = {
    "pc_power": "pc", "pc_media": "pc", "pc_set_volume": "pc",
    "pc_app": "pc", "pc_type": "pc", "pc_status": "pc",
    "wa_send": "wa", "wa_read": "wa",
    "web_search": "search", "web_fetch": "search",
    "server_status": "capabilities",
}
# Call tools use subprocess, not HTTP. Kept separate so they bypass the
# HTTP dispatch block and go straight to _run_call.
CALL_TOOLS = frozenset({"call_make", "call_answer", "call_reject", "call_state"})

# Actions that must not leave the building without explicit user confirmation.
# The 3B model sometimes hallucinates actuation calls from noisy audio; a
# refusal that returns a spoken explanation stops the lie before it starts.
CONFIRM_TOOLS = frozenset({"wa_send", "call_make"})

# Tool -> list of entity IDs whose state should change after the tool runs.
# Used by the closed-loop verifier: capture before, dispatch, re-query after,
# complain if nothing moved. Covers the most dangerous failure mode -- JARVIS
# confirming an actuation that the device silently ignored.
ACTUATION_ENTITIES: dict[str, list[str]] = {
    "fan_set": ["fan.fan", "input_number.fan_speed"],
    "ac_on": ["input_boolean.ac_power"],
    "ac_set_temperature": ["input_boolean.ac_power", "input_number.ac_temperature"],
    "good_night": ["light.node_switch_gang_1", "light.node_switch_gang_2",
                    "light.node_switch_gang_3", "fan.fan",
                    "input_boolean.ac_power"],
}
# Tools whose desired post-action state we can predict from the arguments.
ACTUATION_EXPECTED: dict[str, dict[str, str | None]] = {
    # entity_id -> expected state after the tool succeeds
    "fan_set": {"fan.fan": "on", "input_number.fan_speed": None},  # None = don't check
    "ac_on": {"input_boolean.ac_power": "on"},
    "ac_set_temperature": {"input_boolean.ac_power": "on"},
    # good_night turns everything off; checked by re-query alone.
}
ACTUATION_TOOLS = frozenset(ACTUATION_ENTITIES)


def _clip_text(text: str, limit: int = 120) -> str:
    """Collapse a message to one line and fit it for speech.

    Truncation happens on a word boundary, and any trailing period is dropped
    so the caller's own sentence punctuation is the only one -- otherwise a
    message that already ends in "." produces the double-period "hi.." this
    project has already been burned by.
    """
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text.rstrip(". ")
    return text[:limit].rsplit(" ", 1)[0].rstrip(". ") + "..."


def _describe(tool: str, payload: dict[str, Any] | None) -> str:
    """
    Turn a read-only tool's real response into something speakable.

    Only the READS produce a sentence here. Actuation tools return "" so the
    model's own short confirmation is spoken, which is both nicer and safe --
    the call either succeeded or the turn failed.

    Kept to roughly a dozen words, matching the spoken-reply budget in the
    Hermes prompt. A HUD can render the full payload; a voice line cannot.
    """
    if not isinstance(payload, dict):
        return ""

    if tool == "pc_status":
        bits: list[str] = []
        # `state` now comes from the agent's GET /status probe (awake, asleep,
        # off, unknown). The old fallback read `interactive` -- the agent's
        # session flag, true whenever it is reachable -- made every read
        # narrate "The PC is awake" from nothing that was measured. A read
        # speaks what it measured or it stays silent.
        state = payload.get("state")
        if state:
            state = {"asleep_or_hibernated": "asleep"}.get(state, state)
            bits.append(f"The PC is {state}")
        batt = payload.get("battery")
        if isinstance(batt, dict) and batt.get("percent") is not None:
            charging = " and charging" if batt.get("charging") else ""
            bits.append(f"battery {batt['percent']} percent{charging}")
        apps = payload.get("running_apps") or payload.get("running") or []
        if isinstance(apps, list) and apps:
            bits.append(f"{', '.join(str(a) for a in apps[:3])} running")
        return (", ".join(bits) + ".") if bits else ""

    if tool == "wa_read":
        msgs = payload.get("messages") or []
        if not isinstance(msgs, list) or not msgs:
            return "No new messages."

        # Group by sender so "who messaged me" gets a per-sender answer rather
        # than a wall of texts from one person. The bridge returns messages
        # newest-first, so the first sender seen is the one he last heard from.
        # pushName is absent for numbers that are not saved contacts; fall back
        # to the number, then to a neutral label rather than None.
        by_sender: dict[str, list[str]] = {}
        for m in msgs:
            if not isinstance(m, dict):
                continue
            who = str(m.get("pushName") or m.get("from")
                      or m.get("contact") or "").strip()
            who = who or "a contact"
            text = str(m.get("text") or "").strip()
            if text:
                by_sender.setdefault(who, []).append(text)
        if not by_sender:
            return "No new messages."

        senders = list(by_sender)
        newest, texts = senders[0], by_sender[senders[0]]
        body = _clip_text(texts[0]) or "an empty message"
        if len(texts) > 1:
            body += f" and {len(texts) - 1} more from {newest}"
        if len(senders) > 1:
            others = len(senders) - 1
            body += f"; {others} other sender{'s' if others > 1 else ''} messaged too"
        ending = "" if body.endswith(("?", "!", "...")) else "."
        return f"{newest} said: {body}{ending}"

    if tool == "web_search":
        if not isinstance(payload, dict):
            return ""
        answer = payload.get("answer") or ""
        if answer:
            return answer
        error = payload.get("error") or ""
        if error:
            return f"Search failed: {error}"
        return ""

    if tool == "web_fetch":
        if not isinstance(payload, dict):
            return ""
        answer = payload.get("answer") or ""
        if answer:
            return answer
        return ""

    if tool == "server_status":
        if not isinstance(payload, dict):
            return ""
        # Capabilities /api/status returns structured server health.
        # Build a grounded one-liner from the real data.
        containers = payload.get("containers") or {}
        total = containers.get("total") or 0
        running = containers.get("running") or 0
        unhealthy = containers.get("unhealthy") or []
        cpu = containers.get("cpu") or {}
        mem = containers.get("mem") or {}
        parts = []
        if total:
            parts.append(f"{running} of {total} containers running")
        if unhealthy:
            parts.append(f"{len(unhealthy)} unhealthy: {', '.join(str(u) for u in unhealthy[:2])}")
        if isinstance(cpu, dict) and cpu.get("percent"):
            parts.append(f"CPU at {cpu['percent']:.0f}%")
        if isinstance(mem, dict) and mem.get("percent"):
            parts.append(f"memory at {mem['percent']:.0f}%")
        if not parts:
            return "Server is up."
        return ". ".join(parts) + "."

    return ""


@dataclass
class ExecResult:
    """One dispatched tool call, with enough detail to explain a failure."""

    tool: str
    ok: bool
    detail: str = ""
    corrected: list[str] = field(default_factory=list)
    ms: float = 0.0
    # Speech built FROM THE SERVICE'S OWN RESPONSE, for read-only tools.
    #
    # Actuation tools leave this empty and the model's confirmation is spoken,
    # which is safe: the action provably happened or the call failed. A READ is
    # different. If pc_status or wa_read spoke the model's sentence, JARVIS
    # would report a battery level and an unread message it never looked at --
    # the model has the answer only if it invents one. So the router prefers
    # this string whenever it is set, and the model never gets to narrate a
    # fact it did not read.
    speech: str = ""


def _coerce(tool: str, args: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """
    Coerce and clamp arguments. Returns (clean_args, notes_about_corrections).

    Models emit "22" as often as 22, and occasionally 220. Both are fixed here
    rather than rejected, because refusing the call would leave JARVIS saying
    something happened while nothing did.
    """
    spec = ARG_SPEC.get(tool)
    if not spec:
        return dict(args), []

    clean, notes = dict(args), []
    for name, (typ, lo, hi) in spec.items():
        if name not in clean:
            continue
        raw = clean[name]
        if typ is int:
            try:
                # float() first so "22.0" and 22.0 both survive; int("22.0") raises.
                val = int(float(raw))
            except (TypeError, ValueError):
                notes.append(f"{name}={raw!r} is not a number; dropped")
                del clean[name]
                continue
            if not isinstance(raw, int):
                notes.append(f"{name} {raw!r} -> {val}")
            if val < lo or val > hi:
                notes.append(f"{name} {val} out of range {lo}-{hi}; clamped")
                val = max(lo, min(hi, val))
            clean[name] = val
    return clean, notes


def _describe_failure(tool: str, payload: dict[str, Any] | None, detail: str) -> str:
    """
    Explain a REFUSAL in words, so the turn can end honestly.

    Without this the failure path is genuinely dangerous. A refused wa_send
    makes the executor report exec_failed, Hermes falls through, and the big
    agent -- which holds no WhatsApp tool whatsoever -- answers the request from
    its own head. The most likely sentence a helpful model produces there is
    "I've sent that message, sir", for a message that was refused and never left
    the building. That lie is undetectable by ear.

    So a refusal that is EXPECTED and explainable returns a sentence here, and
    the router speaks it instead of consulting the model at all. Anything
    unrecognised still returns "" and falls through, because inventing an
    explanation for an unknown failure is the same sin pointed the other way.
    """
    err = ""
    if isinstance(payload, dict):
        err = str(payload.get("error") or "")

    if tool == "pc_power":
        # The executor's own confirm gate (see _pc_route): shutdown/restart
        # without {"confirm": true} never leaves the building. The big agent
        # holds pc_power too, so without a sentence here the refusal falls
        # through to a narrator who will answer "I've shut it down, sir" for a
        # shutdown that was refused -- and can then dispatch the shutdown
        # itself. Say the refusal; do not hand it to a narrator.
        action = (detail or "").split(" needs explicit confirmation", 1)[0].strip()
        if action in ("shutdown", "restart"):
            return f"To {action} the PC you must confirm it clearly, so I have not done it."

    if tool == "wa_send":
        if "needs explicit confirmation" in (detail or ""):
            return "I will not send that without you confirming it first."
        if err == "contact_not_allowed":
            return "That name is not in your WhatsApp contacts, so I did not send it."
        if err == "not_linked":
            return "WhatsApp is not linked yet, so I could not send that."
        if err in ("too_soon", "duplicate"):
            return "I sent them that a moment ago, so I have not sent it again."
        if err == "rate_limited":
            return "That is a great many messages this hour. I have stopped sending."
        if err == "message_too_long":
            return "That message is too long to send."
    if tool == "wa_read" and err == "not_linked":
        return "WhatsApp is not linked yet, so I cannot read your messages."
    if tool == "pc_app" and err in ("app_not_allowed", "not_allowed"):
        return "That application is not on the allowed list."

    if tool == "call_make":
        if "needs explicit confirmation" in (detail or ""):
            return "I will not make that call without you confirming it first."
        if "not available" in low:
            return "Calling is not available right now, Sir."

    if tool == "web_search" and err == "disabled":
        return "Internet search is not available right now."
    if tool == "web_fetch" and err == "disabled":
        return "Web fetching is not available right now."

    # Missing error cases from earlier audit
    if tool == "ha_call_service" and "cannot connect" in low:
        return "I could not reach the lights, Sir."
    if "did not understand" in (detail or ""):
        return "I did not understand that, Sir."

    # Missing error cases from earlier audit
    if tool == "ha_call_service" and "cannot connect" in low:
        return "I could not reach the lights, Sir."
    if tool == "conversation" and "did not understand" in low:
        return "I did not understand that, Sir."
    if "turn_timeout" in (detail or ""):
        return "That took too long to process, Sir."
    if tool.startswith("call_") and "not available" in low:
        return "Calling is not available right now, Sir."

    # Transport-level problems, common to both services. "unreachable" is worth
    # saying out loud: a host service being down is something Natik can fix, and
    # silence here previously read as the model simply ignoring him.
    low = (detail or "").lower()
    if "cannot connect" in low or "clientconnector" in low or "timeout" in low:
        return ("I could not reach the PC." if tool.startswith("pc_")
                else "I could not reach the WhatsApp bridge.")
    if "http 401" in low:
        return ("The PC agent rejected my token." if tool.startswith("pc_")
                else "The WhatsApp bridge rejected my token.")
    return ""


class Executor:
    """Dispatches Hermes tool calls against Home Assistant."""

    def __init__(self, hass_url: str, token: str, timeout_s: float = 15.0) -> None:
        self.hass_url = hass_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json"}

    async def _post(self, session: aiohttp.ClientSession, path: str,
                    body: dict) -> tuple[bool, str]:
        url = f"{self.hass_url}{path}"
        try:
            async with session.post(
                url, json=body, headers=self._headers,
                timeout=aiohttp.ClientTimeout(total=self.timeout_s),
            ) as resp:
                text = (await resp.text())[:300]
                if resp.status in (200, 201):
                    return True, text
                return False, f"http {resp.status}: {text}"
        except Exception as exc:  # noqa: BLE001 - a failed tool must not kill speech
            return False, f"{type(exc).__name__}: {exc}"

    async def _read_context(
        self, session: aiohttp.ClientSession, device: str,
    ) -> tuple[bool, str, str]:
        """Read the requested device state and build grounded speech.

        Returns ``(ok, detail, speech)``. A read failure is never successful:
        callers must not let the model narrate around an unavailable state read.
        """
        key = re.sub(r"\s+", " ", str(device or "").strip().lower())
        entity_groups = {
            "fan": ["fan.fan", "input_number.fan_speed"],
            "ac": ["input_boolean.ac_power", "input_number.ac_temperature"],
            "air conditioner": ["input_boolean.ac_power", "input_number.ac_temperature"],
            "light one": ["light.node_switch_gang_1"],
            "light two": ["light.node_switch_gang_2"],
            "light three": ["light.node_switch_gang_3"],
            "lights": ["light.node_switch_gang_1", "light.node_switch_gang_2",
                       "light.node_switch_gang_3"],
            "light": ["light.node_switch_gang_1", "light.node_switch_gang_2",
                      "light.node_switch_gang_3"],
            "room": ["light.node_switch_gang_1", "light.node_switch_gang_2",
                     "light.node_switch_gang_3", "fan.fan",
                     "input_number.fan_speed", "input_boolean.ac_power",
                     "input_number.ac_temperature"],
            "": ["light.node_switch_gang_1", "light.node_switch_gang_2",
                 "light.node_switch_gang_3", "fan.fan", "input_number.fan_speed",
                 "input_boolean.ac_power", "input_number.ac_temperature"],
        }
        entity_ids = entity_groups.get(key)
        if entity_ids is None:
            return False, f"unknown device {device!r}; refused", ""

        by_id: dict[str, str] = {}
        try:
            for entity_id in entity_ids:
                async with session.get(
                    f"{self.hass_url}/api/states/{entity_id}",
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_s),
                ) as resp:
                    if resp.status != 200:
                        return False, f"http {resp.status} reading {entity_id}", ""
                    state = await resp.json()
                    by_id[entity_id] = str(state.get("state") or "unknown")
        except Exception as exc:  # noqa: BLE001 - a failed read must not kill speech
            return False, f"{type(exc).__name__}: {exc}", ""

        if key == "fan":
            state = by_id.get("fan.fan")
            # The speed mirror (input_number.fan_speed) keeps the last set speed
            # even after the fan turns off. Reading it naively is how "the fan is
            # at speed 4" got spoken while nothing was spinning: the state said
            # off, the stale number said 4, and a human heard a contradiction.
            # Off means off -- report it and stop.
            if state == "off":
                speech = "The fan is off"
            else:
                try:
                    n = int(float(by_id.get("input_number.fan_speed") or 0))
                except (TypeError, ValueError):
                    n = 0
                speech = "The fan is on" if n <= 0 else f"The fan is on at speed {n}"
        elif key in ("ac", "air conditioner"):
            state = by_id.get("input_boolean.ac_power")
            temp = by_id.get("input_number.ac_temperature")
            speech = "The AC is on" if state == "on" else "The AC is off"
            if state == "on" and temp:
                speech += f" at {temp} degrees"
        elif key.startswith("light") and key not in ("lights", "light"):
            label = key.replace("light ", "Light ")
            speech = f"{label} is {by_id.get(entity_ids[0], 'unknown')}"
        elif key in ("lights", "light"):
            on = [str(i + 1) for i, entity_id in enumerate(entity_ids)
                  if by_id.get(entity_id) == "on"]
            speech = (f"Lights {', '.join(on)} are on" if on
                      else "All bedroom lights are off")
        else:
            on = [f"light {i + 1}" for i, entity_id in enumerate(entity_ids[:3])
                  if by_id.get(entity_id) == "on"]
            fan = by_id.get("fan.fan")
            ac = by_id.get("input_boolean.ac_power")
            parts = [f"{', '.join(on)} on" if on else "all lights off",
                     f"fan {fan or 'unknown'}", f"AC {'on' if ac == 'on' else 'off'}"]
            speech = ". ".join(parts)
        return True, "context read", speech + "."

    async def _read_live_context(self, session: aiohttp.ClientSession,
                                 device: str) -> tuple[bool, str, str]:
        """Compatibility alias for callers/tests using the descriptive name."""
        return await self._read_context(session, device)

    @staticmethod
    def _describe_live_context(device: str, states: dict[str, str]) -> str:
        """Format already-read states; kept as a small testable seam."""
        return f"{device} state: {', '.join(f'{k}={v}' for k, v in states.items())}."
    async def _call(self, session: aiohttp.ClientSession, method: str, base: str,
                    path: str, body: dict, token: str,
                    ) -> tuple[bool, str, dict[str, Any] | None]:
        """
        Call a host service. Returns (ok, detail, parsed_json_or_None).

        Separate from _post because these are NOT Home Assistant: different
        host, different bearer token, and the parsed body matters here -- a read
        tool's spoken answer is built from it, so it cannot be discarded as a
        300-character string the way an HA service result can be.
        """
        url = f"{base}{path}"
        try:
            async with session.request(
                method, url, json=(body or None) if method != "GET" else None,
                headers={"Authorization": f"Bearer {token}",
                         "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=self.timeout_s),
            ) as resp:
                text = await resp.text()
                data: dict[str, Any] | None
                try:
                    parsed = json.loads(text)
                    data = parsed if isinstance(parsed, dict) else None
                except ValueError:
                    data = None
                if resp.status in (200, 201):
                    return True, text[:300], data
                # These services answer with a JSON `detail` explaining the
                # refusal -- "contact_not_allowed", "confirmation_required".
                # Surfacing it is what lets a failure be diagnosed from the log
                # instead of just a status code.
                why = ""
                if data:
                    why = str(data.get("detail") or data.get("error") or "")
                return False, f"http {resp.status}: {why or text[:200]}", data
        except Exception as exc:  # noqa: BLE001 - a failed tool must not kill speech
            return False, f"{type(exc).__name__}: {exc}", None

    async def run_one(self, session: aiohttp.ClientSession,
                      call: dict[str, Any]) -> ExecResult:
        """Dispatch a single {'name':..., 'arguments':{...}} tool call."""
        started = time.perf_counter()
        tool = call.get("name") or ""
        args = call.get("arguments") or {}

        def done(ok: bool, detail: str, notes: list[str] | None = None) -> ExecResult:
            return ExecResult(tool, ok, detail, notes or [],
                              round((time.perf_counter() - started) * 1000, 1))

        # Import here rather than at module scope: executor.py is imported by
        # voice_server.py, and a circular import at startup would take the whole
        # voice loop down over a tool table.
        from . import hermes as H

        if tool not in H.TOOLS:
            # A hallucinated name must never reach HA. It would 404 at best and
            # match an unintended script at worst.
            return done(False, f"unknown tool {tool!r}; refused")

        if tool in CONTEXT_TOOLS:
            # A REAL read, not "answered by the caller" (that was the lie). The
            # router does not hold state; this does the /api/states read and
            # speaks what is actually on, so the model can never narrate state
            # it did not look at.
            device = str(args.get("device") or "").strip()
            ok, detail, speech = await self._read_context(session, device)
            return ExecResult(tool, ok, detail, [],
                              round((time.perf_counter() - started) * 1000, 1),
                              speech)

        clean, notes = _coerce(tool, args)

        # REQUIRED_ARGS is the safety net for HA scripts whose template defaults
        # are dangerous when an argument is missing: fan_set's `{{ level | int(0) }}`
        # turns the fan OFF, ac_set_temperature's default 24 turns the AC ON. A
        # hallucinated or dropped argument must refuse the dispatch, never reach
        # HA. None is treated as absent because the model emits nulls when it
        # has nothing to say.
        required = REQUIRED_ARGS.get(tool)
        if required:
            # None counts as absent even though the key may survive _coerce for
            # tools outside ARG_SPEC (wa_send, pc_*): the model emits nulls when
            # it has nothing to say, and a null is exactly as dangerous as a gap.
            present = {k for k, v in clean.items() if v is not None}
            missing = sorted(required - present)
            if missing:
                return done(
                    False,
                    f"{tool} needs {', '.join(missing)}; dispatch refused",
                    notes)

        # --- host HTTP services (PC agent, WhatsApp bridge, websearch, capabilities)
        # Confirmation gate: wa_send and call_make refuse without confirm:true.
        if tool in CONFIRM_TOOLS and args.get("confirm") is not True:
            return done(False, f"{tool} needs explicit confirmation", notes)

        if tool in HTTP_ROUTES:
            which = HTTP_ROUTES[tool]
            if which == "pc":
                base, token = PC_URL, PC_TOKEN
                route = _pc_route(tool, clean)
            elif which == "wa":
                base, token = WA_URL, WA_TOKEN
                route = _wa_route(tool, clean)
            elif which == "search":
                base, token = SEARCH_URL, ""
                route = _search_route(tool, clean)
            elif which == "capabilities":
                base, token = CAPABILITIES_URL, ""
                route = _capabilities_route(tool, clean)
            elif which == "call":
                base, token = "", ""
                route = _call_route(tool, clean)
            else:
                return done(False, f"unknown service {which!r}")
            if not token and which in ("pc", "wa"):
                # Fail loudly. A missing token used to surface as a 401 that the
                # model then narrated around, which is the failure this whole
                # file exists to prevent. Search and capabilities are internal
                # services on the Docker network, no token needed.
                return done(False, f"{which} token is not configured in this container")
            if isinstance(route, str):
                # A route-string is a REFUSAL, not an unexplained failure.
                return ExecResult(tool, False, route, notes,
                                  round((time.perf_counter() - started) * 1000, 1),
                                  _describe_failure(tool, None, route))
            method, path, body = route
            if tool == "pc_power" and clean.get("action") in ("shutdown", "restart"):
                log.warning("dispatching PC %s with %ss delay", clean.get("action"), _POWER_DELAY_S)
            ok, detail, payload = await self._call(session, method, base, path, body, token)
            spoken = (_describe(tool, payload) if ok
                      else _describe_failure(tool, payload, detail))
            return ExecResult(tool, ok, detail, notes,
                              round((time.perf_counter() - started) * 1000, 1), spoken)

        # --- call tools: HTTP to call bridge wrapper ------------------------
        if tool in CALL_TOOLS:
            return await self._run_call(session, tool, clean)

        if tool in INTENT_TOOLS:
            data = {k: v for k, v in clean.items() if v not in (None, "")}
            ok, detail = await self._post(
                session, "/api/intent/handle", {"name": tool, "data": data})
            return done(ok, detail, notes)

        # Everything else is a script of the same name. Arguments ride as script
        # variables; a script with no fields simply ignores an empty body.
        ok, detail = await self._post(
            session, f"/api/services/script/{tool}", clean)
        return done(ok, detail, notes)

    async def _verify_actuation(
        self, session: aiohttp.ClientSession, tool: str,
        args: dict[str, Any],
    ) -> str | None:
        """
        Re-query entities after an actuation and return a complaint if nothing
        changed. Returns None when the state looks correct (or we cannot tell).

        Must not add latency to the local path -- the caller speaks the
        acknowledgement first and calls this in the background. Here we just
        return the string; the router decides whether to append it.
        """
        entity_ids = ACTUATION_ENTITIES.get(tool)
        if not entity_ids:
            return None
        expected = ACTUATION_EXPECTED.get(tool, {})

        # Snapshot current states.
        before: dict[str, str] = {}
        try:
            for eid in entity_ids:
                async with session.get(
                    f"{self.hass_url}/api/states/{eid}",
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=3.0),
                ) as resp:
                    if resp.status == 200:
                        state = await resp.json()
                        before[eid] = str(state.get("state") or "unknown")
        except Exception:  # noqa: BLE001 - verification failure is not a tool failure
            return None

        # Wait for the device to settle.
        await asyncio.sleep(1.5)

        # Re-query.
        after: dict[str, str] = {}
        try:
            for eid in entity_ids:
                async with session.get(
                    f"{self.hass_url}/api/states/{eid}",
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=3.0),
                ) as resp:
                    if resp.status == 200:
                        state = await resp.json()
                        after[eid] = str(state.get("state") or "unknown")
        except Exception:  # noqa: BLE001
            return None

        # Compare.
        for eid, exp in expected.items():
            if exp is None:
                continue
            if after.get(eid) == exp:
                continue
            return f"That did not take, Sir. {eid.replace(chr(46), chr(95))} is {after.get(eid, chr(63))}, not {exp}."
        # For tools without a fixed expected state (good_night), check that
        # at least one entity changed.
        if not expected and all(after.get(eid) == before.get(eid) for eid in entity_ids
                                if eid in before and eid in after):
            return "That did not take, Sir. The devices did not change state."
        return None

    async def _run_call(self, session: aiohttp.ClientSession,
                        tool: str, args: dict[str, Any]) -> ExecResult:
        """Dispatch a call tool via the host call bridge HTTP API."""
        started = time.perf_counter()
        def done(ok, detail, speech="") -> ExecResult:
            return ExecResult(tool, ok, detail, [],
                              round((time.perf_counter() - started) * 1000, 1), speech)

        if tool == "call_make":
            contact = str(args.get("contact") or "").strip()
            if not contact:
                return done(False, "no contact given")
            path = "/call/make"
            body = {"contact": contact}
        elif tool == "call_answer":
            path = "/call/answer"
            body = {}
        elif tool == "call_reject":
            path = "/call/reject"
            body = {}
        elif tool == "call_state":
            path = "/call/state"
            body = {}
        else:
            return done(False, f"unknown call tool {tool!r}")

        try:
            async with session.post(
                f"{CALL_URL}{path}", json=body,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                text = await resp.text()
                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    payload = {"raw": text}
                ok = resp.status == 200 and payload.get("ok") is not True
                detail = payload.get("error") or (f"http {resp.status}" if resp.status != 200 else "ok")
                speech = payload.get("speech") or payload.get("state", "") or ""
                return ExecResult(tool, not ok, detail, [],
                                  round((time.perf_counter() - started) * 1000, 1), speech)
        except Exception as exc:  # noqa: BLE001
            return done(False, f"{type(exc).__name__}: {exc}")

    async def run(self, session: aiohttp.ClientSession,
                  calls: list[dict[str, Any]]) -> list[ExecResult]:
        """
        Dispatch calls IN ORDER, sequentially.

        Not gathered concurrently on purpose. These are IR and Tuya commands to
        physical devices, and a Tuya device accepts ONE local connection at a
        time -- firing two at once is how you get a device that answers neither.
        Order also carries meaning: "AC 22 and fan 4" should not race.
        """
        out: list[ExecResult] = []
        for c in calls:
            r = await self.run_one(session, c)
            out.append(r)
            if not r.ok:
                log.warning("tool %s failed: %s", r.tool, r.detail)
            elif r.corrected:
                log.info("tool %s corrected: %s", r.tool, "; ".join(r.corrected))
        return out
