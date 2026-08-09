#!/usr/bin/env python3
"""
Closed-loop verification: never speak a confirmation that is not true.

THE DEFECT THIS EXISTS TO KILL
------------------------------
qwen2.5:3b will produce a fluent, confident confirmation for an action it never
took. Measured on this build, 2026-08-08, verbatim:

    utterance : "set the air conditioner to 22"
    reply     : "AC set to 22. Fan speed is now 3."
    tool calls: ac_set_temperature{"temperature": 22}      <- and nothing else

The AC half was true. The fan half was invented: no fan tool ran and
input_number.fan_speed did not move. Nothing downstream noticed, so the
sentence was spoken as fact. A user has no way to detect that, which makes it
the worst possible bug in a voice assistant. Speaking a failure honestly is
strictly better than speaking a success falsely.

HOW IT IS CAUGHT
----------------
`POST /api/conversation/process` is useless for this. It answers
`response_type: "action_done"` with empty `success`/`failed` arrays whether the
model drove a device or merely claimed to. (MEASURED: for built-in intents
`data.success` is populated, but for SCRIPT tools -- the fan, the AC, the TV,
every comfort scene -- it comes back empty even on success. So it cannot be
used as the signal.)

Home Assistant 2026.8 keeps a per-conversation chat log holding the real
`tool_calls` and their `tool_result`s. It is reachable over the HA websocket API
with `conversation/chat_log/subscribe`. MEASURED cost: 2-17 ms. That is ground
truth -- what the model actually invoked and what came back -- rather than
anything inferred from the prose.

On top of that, entities with a real feedback path are read back and compared.

WHAT "VERIFIED" IS ALLOWED TO MEAN
---------------------------------
This is the honest part, and it differs per device:

  * LIGHTS -- Tuya protocol 3.5, genuinely bidirectional. The switch reports its
    own relay state, so a read-back is real evidence. Confidence: CONFIRMED.

  * FAN, AC, TV -- one-way infrared through the Zoook blaster. There is no
    sensor anywhere in the room that observes them. Nothing reports whether the
    AC received a frame, whether it was pointed at the unit, or whether someone
    is holding a newspaper in front of it. The input_number/input_boolean
    helpers are a record of what we COMMANDED, not what the appliance is doing.
    So the strongest claim available is "the send succeeded and HA's own state
    updated". Confidence: SENT. It is never upgraded to CONFIRMED, because that
    would be a lie told by the layer whose entire job is not lying.

    This is a property of infrared, not a gap to engineer around. The only fix
    is hardware -- a power-clamp sensor on the AC, or an ESP32 with an IR
    receiver watching the room.

STALE READS ARE NOT MISMATCHES
------------------------------
A read-back racing a write would report a mismatch that never happened, and a
verifier that cries wolf gets ignored, which puts us back where we started. So a
read whose `last_updated` predates the tool call is never treated as
contradiction -- it is retried briefly and then downgraded to UNVERIFIED.
MEASURED settling for the lights is ~130 ms from the service call, while the
LLM's reply takes 1.2-7.4 s longer than that to come back, so in practice the
state has settled long before verification even starts.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import aiohttp

log = logging.getLogger("jarvis.verify")

# --- Tuning, all derived from measurement (see verify/measure_settling.py) ---

# A Tuya light reports its new state ~127-156 ms after the service call is
# ISSUED, and the call itself returns in 21-38 ms. Verification runs after the
# LLM has finished, which MEASURED at 1.2-7.4 s later, so the state is normally
# settled before we look. This budget only covers the pathological case of a
# very fast reply landing on top of a slow device.
SETTLE_RETRY_MS = 240
SETTLE_POLL_MS = 40

# Ceiling on the whole verification step. Past this we speak the model's reply
# unchanged rather than delay speech -- the loop is 1720-2079 ms against a
# 3000 ms target, and silence is worse than an unverified reply.
VERIFY_BUDGET_MS = 400


class Feedback:
    """How much a device is physically able to tell us."""

    REAL = "real"          # bidirectional; a read-back is evidence
    OPEN_LOOP = "open"     # one-way IR; we only know the send left the building


@dataclass(frozen=True)
class DeviceSpec:
    key: str
    spoken: str                    # how it is referred to out loud
    feedback: str
    entities: tuple[str, ...] = ()
    # Entity holding the numeric setting, if the device has one.
    value_entity: str | None = None
    power_entity: str | None = None


# The four things JARVIS controls. Kept explicit rather than discovered, because
# the feedback classification is a physical fact about each device and must not
# be guessed at runtime.
DEVICES: dict[str, DeviceSpec] = {
    "light": DeviceSpec(
        key="light",
        spoken="lights",
        feedback=Feedback.REAL,
        entities=(
            "light.node_switch_gang_1",
            "light.node_switch_gang_2",
            "light.node_switch_gang_3",
        ),
    ),
    "fan": DeviceSpec(
        key="fan",
        spoken="fan",
        feedback=Feedback.OPEN_LOOP,
        entities=("fan.fan", "input_number.fan_speed", "input_boolean.fan_power"),
        value_entity="input_number.fan_speed",
        power_entity="input_boolean.fan_power",
    ),
    "ac": DeviceSpec(
        key="ac",
        spoken="AC",
        feedback=Feedback.OPEN_LOOP,
        entities=("input_number.ac_temperature", "input_boolean.ac_power"),
        value_entity="input_number.ac_temperature",
        power_entity="input_boolean.ac_power",
    ),
    "tv": DeviceSpec(key="tv", spoken="TV", feedback=Feedback.OPEN_LOOP),
}

# Which device a tool call touches. Built-in intents carry a `domain` argument;
# the scripts are named after what they drive.
_SCRIPT_DEVICES: dict[str, tuple[str, ...]] = {
    "fan_set": ("fan",),
    "fan_boost": ("fan",),
    "ac_set_temperature": ("ac",),
    "ac_on": ("ac",),
    "ac_off": ("ac",),
    "ac_dry": ("ac",),
    "ac_max_cool": ("ac",),
    "ac_apply": ("ac",),
    "tv_toggle": ("tv",),
    "feeling_hot": ("ac", "fan"),
    "feeling_cold": ("ac", "fan"),
    "good_night": ("light", "ac", "fan"),
    "movie_time": ("tv", "light", "ac"),
    "all_off": ("light", "fan", "ac"),
}

_DOMAIN_DEVICE = {"light": "light", "fan": "fan", "climate": "ac"}


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None
    # A tool that was called but whose result never arrived is NOT a success.
    # It is exactly the ambiguous case that must not be spoken as one.
    responded: bool = False

    @property
    def failed(self) -> bool:
        if not self.responded or self.result is None:
            return True
        if "error" in self.result:
            return True
        if self.result.get("success") is False:
            return True
        # Built-in intents report per-target outcomes.
        data = self.result.get("data")
        if isinstance(data, dict) and data.get("failed"):
            return True
        return False

    @property
    def error_text(self) -> str:
        if self.result is None:
            return "no result returned"
        return str(self.result.get("error") or self.result.get("error_text") or "")

    def devices(self) -> set[str]:
        """Which of the four devices this call is aimed at."""
        out: set[str] = set()
        short = self.name.split(".")[-1]
        for key, devs in _SCRIPT_DEVICES.items():
            if short == key or short.endswith("_" + key):
                out.update(devs)
        if out:
            return out
        # Built-in intents: HassTurnOn / HassTurnOff / HassLightSet /
        # HassFanSetSpeed / HassSetTemperature ...
        domains = self.args.get("domain")
        if isinstance(domains, str):
            domains = [domains]
        for d in domains or []:
            if d in _DOMAIN_DEVICE:
                out.add(_DOMAIN_DEVICE[d])
        lowered = self.name.lower()
        if "light" in lowered:
            out.add("light")
        if "fan" in lowered:
            out.add("fan")
        if "temperature" in lowered or "climate" in lowered or "hvac" in lowered:
            out.add("ac")
        # A bare HassTurnOn with only a name/area, no domain: fall back to the
        # spoken name if it matches a device we know.
        name = str(self.args.get("name", "")).lower()
        for key, spec in DEVICES.items():
            if name and (key in name or spec.spoken.lower() in name):
                out.add(key)
        return out


# --- Claim extraction ------------------------------------------------------
# Deliberately conservative. A false correction is itself a trust bug, so a
# pattern only fires when the reply is unambiguous. Anything not matched here
# simply is not checked as a numeric claim -- the zero-tool-call and
# failed-tool checks still apply, and those are the ones that matter most.

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20, "twentyone": 21, "twentytwo": 22, "twentythree": 23,
    "twentyfour": 24, "twentyfive": 25, "twentysix": 26, "twentyseven": 27,
    "twentyeight": 28, "twentynine": 29, "thirty": 30,
}
_SPOKEN_NUMBER = {v: k for k, v in _NUMBER_WORDS.items() if v <= 6}

_AC_WORDS = r"(?:a\.?c\.?|air\s*con(?:ditioner)?|cooling)"
_FAN_WORDS = r"fan"
_LIGHT_WORDS = r"(?:light|lights|lamp|lamps)"


def _num(token: str) -> int | None:
    token = token.strip().lower().replace("-", "").replace(" ", "")
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


@dataclass
class Claim:
    device: str
    kind: str          # "value" | "power"
    value: Any
    source: str        # the fragment of the reply that produced it


def extract_claims(reply: str) -> list[Claim]:
    """Pull checkable assertions out of the spoken reply."""
    text = " " + reply.lower().strip() + " "
    claims: list[Claim] = []

    num = r"(\d{1,2}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"

    # "AC at 22", "AC set to 22", "air conditioner is now 24 degrees"
    for m in re.finditer(
        _AC_WORDS + r"[^.,;!?]{0,24}?\b" + num + r"\b", text
    ):
        v = _num(m.group(1))
        if v is not None and 16 <= v <= 30:
            claims.append(Claim("ac", "value", v, m.group(0).strip()))

    # "fan speed is now 3", "fan on four", "fan at 2"
    for m in re.finditer(
        _FAN_WORDS + r"[^.,;!?]{0,24}?\b" + num + r"\b", text
    ):
        v = _num(m.group(1))
        if v is not None and 0 <= v <= 6:
            claims.append(Claim("fan", "value", v, m.group(0).strip()))

    # Power claims. "lights on", "lights are off", "AC off", "fan off".
    valued = {c.device for c in claims if c.kind == "value" and c.value}
    for device, words in (
        ("light", _LIGHT_WORDS), ("fan", _FAN_WORDS), ("ac", _AC_WORDS)
    ):
        for m in re.finditer(
            words + r"\s*(?:are|is|now|turned|switched|\s)*\s*\b(on|off)\b", text
        ):
            # "Fan on four." already yields fan=4, and a non-zero speed IS the
            # power state. Adding a separate "fan is on" claim would double-count
            # one assertion, so a mismatch would be reported twice for a single
            # fabrication. Only "off" adds information here.
            if m.group(1) == "on" and device in valued:
                continue
            claims.append(Claim(device, "power", m.group(1), m.group(0).strip()))

    return claims


# Does the reply assert that something was DONE? Used to decide whether a
# zero-tool-call turn is a false confirmation or an honest refusal/answer.
_ACTION_ASSERTION = re.compile(
    r"\b("
    r"done|set|turned|switched|adjusted|dimmed|brightened|started|stopped|"
    r"activated|enabled|disabled|running|now\s+(?:on|off|at)|"
    r"(?:lights?|fan|a\.?c\.?|air\s*con\w*|tv)\s+(?:is|are)?\s*(?:on|off)"
    r")\b",
    re.IGNORECASE,
)
# Phrases that make a sentence NOT a confirmation, so an honest "I can't do
# that" is never rewritten into a failure notice.
_DISCLAIMER = re.compile(
    r"\b(cannot|can't|couldn't|could not|unable|don't|do not|no\s+\w+\s+(?:exists|found)|"
    r"not\s+(?:able|possible|available|found|supported)|there\s+is\s+no|"
    r"sorry|failed|didn't|did not|nothing)\b",
    re.IGNORECASE,
)


def claims_action(reply: str) -> bool:
    """True if the reply reads as a confirmation that something happened."""
    if not reply.strip():
        return False
    if _DISCLAIMER.search(reply):
        return False
    return bool(_ACTION_ASSERTION.search(reply))


# --- Verdict ---------------------------------------------------------------

@dataclass
class Verdict:
    status: str                     # ok | no_action | failed | mismatch | unverified
    speech: str                     # what should actually be spoken
    corrected: bool = False
    confidence: str = "unknown"     # confirmed | sent | unknown
    detail: str = ""
    tool_calls: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    verify_ms: int = 0

    def as_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "corrected": self.corrected,
            "confidence": self.confidence,
            "detail": self.detail,
            "tool_calls": self.tool_calls,
            "checks": self.checks,
            "verify_ms": self.verify_ms,
        }


class ClosedLoopVerifier:
    """
    Checks a spoken reply against what Home Assistant actually did.

    Holds one long-lived websocket to HA. Reconnecting per utterance would cost
    an auth round trip inside the speech path; a persistent socket makes the
    chat-log read 2-3 ms instead of ~17 ms.
    """

    def __init__(self, hass_url: str, token: str, session: aiohttp.ClientSession):
        self._url = hass_url.rstrip("/")
        self._token = token
        self._http = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._msg_id = 0
        self._lock = asyncio.Lock()

    # -- HA websocket ------------------------------------------------------

    async def _ensure_ws(self) -> aiohttp.ClientWebSocketResponse | None:
        if self._ws is not None and not self._ws.closed:
            return self._ws
        try:
            ws = await self._http.ws_connect(
                f"{self._url}/api/websocket",
                timeout=aiohttp.ClientTimeout(total=5),
                heartbeat=30,
            )
            await ws.receive_json(timeout=5)          # auth_required
            await ws.send_json({"type": "auth", "access_token": self._token})
            auth = await ws.receive_json(timeout=5)
            if auth.get("type") != "auth_ok":
                log.warning("verifier: HA websocket auth failed: %s", auth)
                await ws.close()
                return None
            self._ws = ws
            self._msg_id = 0
            return ws
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            log.warning("verifier: cannot open HA websocket: %s", exc)
            self._ws = None
            return None

    async def fetch_tool_calls(self, conversation_id: str) -> list[ToolCall] | None:
        """
        The real tool calls for the LAST turn of this conversation.

        None means "could not tell" -- which is deliberately different from an
        empty list ("the model called nothing"). Confusing those two would let
        an HA outage masquerade as a hallucination, or worse, the reverse.
        """
        if not conversation_id:
            return None
        async with self._lock:
            ws = await self._ensure_ws()
            if ws is None:
                return None
            self._msg_id += 1
            mid = self._msg_id
            try:
                await ws.send_json({
                    "id": mid,
                    "type": "conversation/chat_log/subscribe",
                    "conversation_id": conversation_id,
                })
                content = await self._await_initial_state(ws, mid)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                log.warning("verifier: chat log read failed: %s", exc)
                self._ws = None
                return None
            finally:
                try:
                    self._msg_id += 1
                    await ws.send_json({
                        "id": self._msg_id,
                        "type": "unsubscribe_events",
                        "subscription": mid,
                    })
                except (aiohttp.ClientError, ValueError):
                    pass
        if content is None:
            return None
        return _calls_from_last_turn(content)

    async def _await_initial_state(
        self, ws: aiohttp.ClientWebSocketResponse, mid: int
    ) -> list[dict] | None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            msg = await asyncio.wait_for(ws.receive_json(), timeout=remaining)
            if msg.get("id") != mid:
                continue
            if msg.get("type") == "result" and not msg.get("success", True):
                log.warning("verifier: subscribe rejected: %s", msg.get("error"))
                return None
            if msg.get("type") == "event":
                data = msg.get("event", {}).get("data", {})
                content = data.get("content")
                if isinstance(content, list):
                    return content
                if isinstance(data, list):
                    return data
        return None

    async def ping(self) -> bool:
        """True if the HA websocket can be opened and authenticated."""
        async with self._lock:
            return await self._ensure_ws() is not None

    async def close(self) -> None:
        """Drop the persistent websocket. Safe to call more than once."""
        ws, self._ws = self._ws, None
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass

    # -- entity read-back --------------------------------------------------

    async def _read_states(self, entities: Iterable[str]) -> dict[str, dict]:
        """Read several entities concurrently; missing ones are simply absent."""
        entities = list(entities)
        if not entities:
            return {}

        async def one(eid: str) -> tuple[str, dict | None]:
            try:
                async with self._http.get(
                    f"{self._url}/api/states/{eid}",
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as r:
                    return eid, (await r.json()) if r.status == 200 else None
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return eid, None

        pairs = await asyncio.gather(*(one(e) for e in entities))
        return {eid: st for eid, st in pairs if st is not None}

    @staticmethod
    def _claim_entities(spec: DeviceSpec, claim: Claim) -> tuple[str, ...]:
        """The entities that actually back a specific claim."""
        if claim.kind == "value":
            return (spec.value_entity,) if spec.value_entity else ()
        if spec.key == "light":
            return spec.entities
        return (spec.power_entity,) if spec.power_entity else ()

    async def _recheck(
        self, spec: DeviceSpec, claim: Claim, not_before: float
    ) -> tuple[Any, bool]:
        """
        Re-read a claim that appears contradicted, in case we caught a write
        in flight. Returns (actual_value, is_fresh).

        `is_fresh` False means the backing entity has not been updated since the
        command was issued, so the read cannot be used to call the claim false.
        Only reached on an APPARENT contradiction -- a claim that already agrees
        with the state never pays this cost, which is what keeps the common path
        at single-digit milliseconds.
        """
        eids = self._claim_entities(spec, claim)
        if not eids:
            return None, True
        deadline = time.monotonic() + SETTLE_RETRY_MS / 1000
        actual, fresh = None, False
        while True:
            states = await self._read_states(eids)
            actual = _actual_value(spec, claim, states)
            stamps = [
                _last_updated(s) for s in states.values() if _last_updated(s)
            ]
            fresh = bool(stamps) and max(stamps) >= not_before
            if (actual is not None and _matches(claim, actual)) or fresh:
                return actual, fresh
            if time.monotonic() >= deadline:
                return actual, fresh
            await asyncio.sleep(SETTLE_POLL_MS / 1000)

    # -- the actual check --------------------------------------------------

    async def verify(
        self, reply: str, conversation_id: str | None, issued_at: float
    ) -> Verdict:
        """
        Compare `reply` against what really happened. Returns what to speak.

        `issued_at` is the unix timestamp from just before the conversation
        request went out, used to reject stale read-backs.
        """
        t0 = time.monotonic()
        try:
            verdict = await asyncio.wait_for(
                self._verify_inner(reply, conversation_id, issued_at),
                timeout=VERIFY_BUDGET_MS / 1000,
            )
        except asyncio.TimeoutError:
            # Out of budget. Speak the model's words rather than stall; the turn
            # is logged as unverified so it is still visible.
            verdict = Verdict(
                status="unverified",
                speech=reply,
                detail=f"verification exceeded {VERIFY_BUDGET_MS} ms budget",
            )
        except Exception as exc:  # noqa: BLE001 - must never break the pipeline
            log.exception("verifier: unexpected failure")
            verdict = Verdict(
                status="unverified", speech=reply, detail=f"verifier error: {exc!r}"
            )
        verdict.verify_ms = int((time.monotonic() - t0) * 1000)
        return verdict

    async def _verify_inner(
        self, reply: str, conversation_id: str | None, issued_at: float
    ) -> Verdict:
        calls = await self.fetch_tool_calls(conversation_id or "")

        if calls is None:
            return Verdict(
                status="unverified",
                speech=reply,
                detail="chat log unavailable; cannot tell what ran",
            )

        names = [c.name for c in calls]

        # ---- Case 1: nothing was called at all --------------------------
        # The headline defect. A confident sentence with no action behind it.
        if not calls:
            if claims_action(reply):
                return Verdict(
                    status="no_action",
                    speech="I did not actually do that.",
                    corrected=True,
                    detail=f"reply asserted an action but zero tools ran: {reply!r}",
                    tool_calls=[],
                )
            # No tools and no claim: an answer or an honest refusal. Fine.
            return Verdict(
                status="ok", speech=reply, detail="no action claimed, no tools ran"
            )

        succeeded = [c for c in calls if not c.failed]
        failed = [c for c in calls if c.failed]

        # ---- Case 2: everything the model tried failed -------------------
        if not succeeded:
            devices = sorted({d for c in failed for d in c.devices()})
            return Verdict(
                status="failed",
                speech=_failure_speech(devices, failed),
                corrected=True,
                detail="; ".join(
                    f"{c.name} -> {c.error_text[:120]}" for c in failed
                ),
                tool_calls=names,
            )

        # ---- Case 3: did any successful call touch a DEVICE at all? -------
        # A query tool (GetDateTime, GetLiveContext) is not an action. The model
        # calling one and then announcing "AC set to 22" is the headline defect
        # wearing a disguise: tool calls were made, so a naive check sees
        # activity, but nothing in the room moved. MEASURED: asking the time and
        # then claiming an action lands here, not in the zero-call branch.
        touched: set[str] = set()
        for c in succeeded:
            touched.update(c.devices())

        claims = extract_claims(reply)

        if not touched:
            if claims or claims_action(reply):
                return Verdict(
                    status="no_action",
                    speech="I did not actually do that.",
                    corrected=True,
                    detail=(
                        "reply asserted an action but no tool touched any device; "
                        f"calls were {names}"
                    ),
                    tool_calls=names,
                )
            return Verdict(
                status="ok", speech=reply, detail="query answered, no action claimed",
                tool_calls=names,
            )

        # ---- Case 4: compare each claim against what really happened ------
        checks: list[dict[str, Any]] = []
        contradictions: list[Claim] = []
        unverifiable = False

        # One concurrent read of everything a successful call touched. A claim
        # that AGREES with this snapshot is done -- no retry, no delay. Only an
        # apparent contradiction pays for a re-read, because only then does the
        # difference between "wrong" and "not settled yet" actually matter.
        entities: list[str] = []
        for dev in touched:
            spec = DEVICES.get(dev)
            if spec:
                entities.extend(spec.entities)
        states = await self._read_states(set(entities)) if entities else {}

        for claim in claims:
            spec = DEVICES.get(claim.device)
            if spec is None:
                continue
            # A claim about a device no successful tool touched is the partial
            # hallucination: "AC set to 22. Fan speed is now 3." with only the
            # AC tool called. No read-back needed to know it is false.
            if claim.device not in touched:
                checks.append({
                    "device": claim.device, "claim": claim.source,
                    "result": "no-tool-touched-this-device",
                })
                contradictions.append(claim)
                continue

            actual = _actual_value(spec, claim, states)
            if actual is not None and _matches(claim, actual):
                checks.append({
                    "device": claim.device, "claim": claim.source,
                    "result": "match", "actual": actual,
                    "confidence": (
                        "confirmed" if spec.feedback == Feedback.REAL else "sent"
                    ),
                })
                continue

            # Looks wrong. Before contradicting the model -- which is a strong,
            # user-visible action -- give a device that may still be settling a
            # bounded second chance.
            actual, fresh = await self._recheck(spec, claim, issued_at)
            if actual is None:
                unverifiable = True
                checks.append({
                    "device": claim.device, "claim": claim.source,
                    "result": "no-readable-state",
                })
            elif _matches(claim, actual):
                checks.append({
                    "device": claim.device, "claim": claim.source,
                    "result": "match-after-settle", "actual": actual,
                    "confidence": (
                        "confirmed" if spec.feedback == Feedback.REAL else "sent"
                    ),
                })
            elif not fresh:
                # The backing entity never updated after the command was issued,
                # so this read describes the world BEFORE the command. It cannot
                # be used to call the claim false. Degrade, do not accuse.
                unverifiable = True
                checks.append({
                    "device": claim.device, "claim": claim.source,
                    "result": "stale-read-skipped", "actual": actual,
                })
            else:
                checks.append({
                    "device": claim.device, "claim": claim.source,
                    "result": "mismatch", "actual": actual,
                })
                contradictions.append(claim)

        if contradictions:
            return Verdict(
                status="mismatch",
                speech=_correction_speech(succeeded, contradictions, states),
                corrected=True,
                confidence=_confidence(touched),
                detail="contradicted: "
                       + "; ".join(f"{c.device}:{c.source!r}" for c in contradictions),
                tool_calls=names,
                checks=checks,
            )

        detail = f"{len(succeeded)} call(s) ok"
        if failed:
            detail += f", {len(failed)} failed but the rest carried the request"
        if unverifiable:
            detail += ", some claims not independently readable"
        return Verdict(
            status="ok",
            speech=reply,
            confidence=_confidence(touched),
            detail=detail,
            tool_calls=names,
            checks=checks,
        )


# --- helpers ---------------------------------------------------------------

def _last_updated(state: dict) -> float | None:
    raw = state.get("last_updated") or state.get("last_changed")
    if not raw:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _calls_from_last_turn(content: list[dict]) -> list[ToolCall]:
    """
    Tool calls belonging to the most recent user turn only.

    The chat log carries history (max_history=2 on this build), so scanning the
    whole thing would credit THIS turn with the previous turn's tool calls and
    happily verify a hallucination. Scope to everything after the last
    UserContent.
    """
    start = 0
    for i, item in enumerate(content):
        if item.get("role") == "user":
            start = i
    turn = content[start + 1:]

    calls: list[ToolCall] = []
    by_id: dict[str, ToolCall] = {}
    for item in turn:
        role = item.get("role")
        if role == "assistant":
            for tc in item.get("tool_calls") or []:
                call = ToolCall(
                    name=str(tc.get("tool_name") or ""),
                    args=dict(tc.get("tool_args") or {}),
                )
                calls.append(call)
                if tc.get("id"):
                    by_id[str(tc["id"])] = call
        elif role == "tool_result":
            result = item.get("tool_result")
            result = result if isinstance(result, dict) else {"result": result}
            target = by_id.get(str(item.get("tool_call_id")))
            if target is None:
                # Match by name against the first call still awaiting a result.
                target = next(
                    (c for c in calls
                     if c.name == item.get("tool_name") and not c.responded),
                    None,
                )
            if target is not None:
                target.result = result
                target.responded = True
    return calls


def _actual_value(spec: DeviceSpec, claim: Claim, states: dict[str, dict]) -> Any:
    if claim.kind == "value":
        if not spec.value_entity:
            return None
        s = states.get(spec.value_entity)
        if not s:
            return None
        try:
            return int(float(s["state"]))
        except (ValueError, TypeError, KeyError):
            return None
    # power
    if spec.key == "light":
        vals = [states[e]["state"] for e in spec.entities if e in states]
        if not vals:
            return None
        return "on" if any(v == "on" for v in vals) else "off"
    if spec.power_entity and spec.power_entity in states:
        return states[spec.power_entity]["state"]
    return None


def _matches(claim: Claim, actual: Any) -> bool:
    if claim.kind == "power":
        return str(actual).lower() == str(claim.value).lower()
    if actual is None:
        return False
    try:
        want = int(claim.value)
    except (TypeError, ValueError):
        return False
    if claim.device == "fan":
        # Boost (6) and speed 5 are indistinguishable in the mirror helper --
        # it clamps to 5 -- so treat that pair as agreeing rather than invent a
        # mismatch out of a known representational limit.
        return int(actual) == want or (want == 6 and int(actual) == 5)
    return int(actual) == want


def _confidence(devices: set[str]) -> str:
    """
    CONFIRMED only if every device touched can actually report back.

    A turn that moved the lights AND the AC is only as trustworthy as its
    weakest link, and the AC link is infrared with nothing listening.
    """
    if not devices:
        return "unknown"
    specs = [DEVICES[d] for d in devices if d in DEVICES]
    if specs and all(s.feedback == Feedback.REAL for s in specs):
        return "confirmed"
    return "sent"


def _failure_speech(devices: list[str], failed: list[ToolCall]) -> str:
    """Short, spoken, honest. Never longer than about ten words."""
    known = [DEVICES[d].spoken for d in devices if d in DEVICES]
    joined = _join(known)
    text = (failed[0].error_text or "").lower()
    if "matchfailed" in text or "not found" in text or "no_match" in text:
        if joined:
            return f"I could not find the {joined}."
        return "I could not find that device."
    if "unavailable" in text or "not currently available" in text:
        return f"The {joined or 'device'} is not responding."
    if joined:
        return f"That failed. The {joined} did not change."
    return "That failed, sir. Nothing changed."


def _correction_speech(
    succeeded: list[ToolCall], bad: list[Claim], states: dict[str, dict]
) -> str:
    """
    Say what really happened, then name what did not.

    Built from ground truth rather than by editing the model's sentence: the
    reply is the thing under suspicion, so reusing its wording would carry the
    fabrication through. Persona takes second place to accuracy here.
    """
    done: set[str] = set()
    for c in succeeded:
        done.update(c.devices())
    bad_devices = sorted({c.device for c in bad})
    done -= set(bad_devices)

    parts: list[str] = []
    for dev in sorted(done):
        spec = DEVICES.get(dev)
        if not spec:
            continue
        frag = _state_phrase(spec, states)
        if frag:
            parts.append(frag)

    missed = _join([DEVICES[d].spoken for d in bad_devices if d in DEVICES])
    tail = f"the {missed} did not change" if missed else "the rest did not"
    if parts:
        return f"{'. '.join(parts)}. But {tail}."
    # Nothing survived verification. Do not dress that up as partial success --
    # "only part of that worked" implies some of it did, which would be a
    # smaller version of the exact lie this module exists to stop.
    return "I did not actually do that."


def _state_phrase(spec: DeviceSpec, states: dict[str, dict]) -> str:
    if spec.key == "ac":
        s = states.get(spec.value_entity or "")
        p = states.get(spec.power_entity or "")
        if p and p.get("state") == "off":
            return "AC off"
        if s:
            try:
                return f"AC at {int(float(s['state']))}"
            except (ValueError, TypeError):
                return ""
        return ""
    if spec.key == "fan":
        s = states.get(spec.value_entity or "")
        if s:
            try:
                v = int(float(s["state"]))
            except (ValueError, TypeError):
                return ""
            if v == 0:
                return "Fan off"
            return f"Fan on {_SPOKEN_NUMBER.get(v, str(v))}"
        return ""
    if spec.key == "light":
        vals = [states[e]["state"] for e in spec.entities if e in states]
        if not vals:
            return ""
        return "Lights on" if any(v == "on" for v in vals) else "Lights off"
    return ""


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]
