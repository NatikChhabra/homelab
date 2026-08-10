#!/usr/bin/env python3
"""
JARVIS voice pipeline backend.

Bridges a browser microphone to the Wyoming voice services and Home Assistant:

    browser mic --ws--> [openwakeword] --> [whisper] --> HA conversation --> [piper] --ws--> browser

WHY A BRIDGE EXISTS AT ALL: Wyoming is a raw TCP protocol. A browser cannot
open a TCP socket, so it physically cannot talk to whisper/piper/openwakeword
directly. Something server-side has to translate, and that something is also
the right place to keep the Home Assistant token so it never reaches the page.

Intent recognition and device control are NOT done here -- they are delegated to
Home Assistant's conversation agent (POST /api/conversation/process), which is
where the Ollama agent and the real entity registry live. The previous api.py
tried to parse commands with substring matching and got it wrong: `'on' in
"turn on the fan"` is true, so fan commands were handled by the light branch.

What IS done here is the last gate before speech: every reply is checked against
what Home Assistant actually did (verify/closed_loop.py) before Piper is allowed
to say it. The model will otherwise confirm actions it never took -- measured on
this build, "AC set to 22. Fan speed is now 3." with no fan tool called at all.
A false confirmation is undetectable by the person listening, so it is treated
as the most serious failure in the pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import sys
import time
from typing import Any

import aiohttp
from aiohttp import web, WSMsgType

import wyoming_client as wy
import tts_engine
import hud
from router import IntentRouter, RouteResult

# verify/ is a sibling package directory; it holds the closed-loop check and its
# tests. Imported by path so the module keeps working whether the server is run
# from the repo or from /app in the container.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify"))
try:
    from closed_loop import ClosedLoopVerifier, Verdict  # type: ignore
except ImportError:  # pragma: no cover - verification is optional, speech is not
    ClosedLoopVerifier = None  # type: ignore
    Verdict = None  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("jarvis")

HASS_URL = os.getenv("HASS_URL", "http://jarvis-hass:8123").rstrip("/")
# Accept either name: the repo's secrets.env already uses HA_API_TOKEN.
HASS_TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")

WHISPER = (os.getenv("WHISPER_HOST", "jarvis-whisper"), int(os.getenv("WHISPER_PORT", "10300")))
PIPER = (os.getenv("PIPER_HOST", "jarvis-piper"), int(os.getenv("PIPER_PORT", "10200")))
WAKE = (os.getenv("WAKE_HOST", "jarvis-wake-word"), int(os.getenv("WAKE_PORT", "10400")))

WAKE_WORD = os.getenv("WAKE_WORD", "hey_jarvis")
# Voice selection now lives in tts_engine.py, which owns the Kokoro/Piper
# choice. PIPER stays defined above because /api/status still probes it as a
# service -- it is the fallback engine, so its liveness is worth reporting even
# when Kokoro is the one actually speaking.
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_GB-alan-medium")

# HA's conversation agent. Set to the Ollama-backed agent so commands reach the
# LLM; leaving it unset falls back to HA's built-in matcher, which only handles
# exact phrasings and makes the whole thing feel like a 2010 voice remote.
CONVERSATION_AGENT = os.getenv("CONVERSATION_AGENT", "conversation.jarvis")

# Closed-loop verification. On by default: the failure it prevents (a spoken
# confirmation for an action that never happened) is silent and undetectable by
# the listener, so it must not be opt-in. Set VERIFY_REPLIES=0 to measure the
# pipeline without it.
VERIFY_REPLIES = os.getenv("VERIFY_REPLIES", "1").lower() not in ("0", "false", "no")

# Host telemetry, already running for the rest of the server.
GLANCES_URL = os.getenv("GLANCES_URL", "http://host.docker.internal:61208").rstrip("/")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")

# --- Utterance segmentation ------------------------------------------------
# Energy-based, deliberately not a neural VAD: it runs per 64 ms batch on the
# same CPU as Whisper, so it has to be nearly free.
SILENCE_RMS = 0.015          # below this counts as silence
SILENCE_HANG_S = 0.5         # trailing silence that ends an utterance
MIN_UTTERANCE_S = 0.3        # ignore a lone click/cough
MAX_UTTERANCE_S = 12.0       # hard stop so one connection cannot record forever
NO_SPEECH_TIMEOUT_S = 6.0    # woke but nothing was ever said -> abandon

BYTES_PER_SEC = wy.RATE * wy.WIDTH * wy.CHANNELS


def _rms_int16(pcm: bytes) -> float:
    """RMS of little-endian int16 PCM, normalised to 0..1."""
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[: count * 2])
    total = 0
    for s in samples:
        total += s * s
    return (total / count) ** 0.5 / 32768.0


class WakeStream:
    """
    Persistent connection to the Wyoming wake-word service.

    Kept open across the whole browser session rather than reconnecting per
    utterance: openWakeWord needs continuous context, and reconnecting drops
    the audio history that makes detection reliable.
    """

    def __init__(self, host: str, port: int, name: str) -> None:
        self._host, self._port, self._name = host, port, name
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self._host, self._port)
        await wy.write_event(self._writer, wy.Event("detect", {"names": [self._name]}))
        await wy.write_event(self._writer, wy.Event("audio-start", {
            "rate": wy.RATE, "width": wy.WIDTH, "channels": wy.CHANNELS, "timestamp": 0,
        }))

    async def feed(self, pcm: bytes) -> None:
        if not self._writer:
            return
        await wy.write_event(self._writer, wy.Event(
            "audio-chunk",
            {"rate": wy.RATE, "width": wy.WIDTH, "channels": wy.CHANNELS},
            pcm,
        ))

    async def next_detection(self) -> str | None:
        """Block until the service reports a detection. None if stream ended."""
        if not self._reader:
            return None
        while True:
            event = await wy.read_event(self._reader)
            if event is None:
                return None
            if event.type == "detection":
                return event.data.get("name") or self._name

    async def close(self) -> None:
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
        self._reader = self._writer = None


async def ask_home_assistant(
    session: aiohttp.ClientSession, text: str, conversation_id: str | None
) -> tuple[str, str | None]:
    """
    Legacy single-agent path, kept for callers that predate the router.

    Prefer `route_utterance` below: it tries HA's built-in parser first and only
    pays for the model when that genuinely fails. Measured 2026-08-09 over the
    non-actuating benchmark set, 12 of 15 utterances resolve locally at a median
    18.3 ms against 1472 ms through the model -- an 80x difference on the
    commands that make up most of daily use.
    """
    if not HASS_TOKEN:
        return ("Home Assistant token is not configured, so I cannot act on that.", None)

    payload: dict[str, Any] = {"text": text, "language": "en"}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    # Without an explicit agent_id HA uses its built-in matcher, which only
    # understands exact phrasings. The whole point of the Ollama agent is that
    # "make it a bit cooler in here" works, so name it.
    if CONVERSATION_AGENT:
        payload["agent_id"] = CONVERSATION_AGENT

    try:
        async with session.post(
            f"{HASS_URL}/api/conversation/process",
            json=payload,
            headers={"Authorization": f"Bearer {HASS_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            if resp.status == 401:
                return ("Home Assistant rejected my token.", None)
            if resp.status != 200:
                body = (await resp.text())[:200]
                log.warning("HA conversation returned %s: %s", resp.status, body)
                return (f"Home Assistant returned an error, status {resp.status}.", None)
            data = await resp.json()
    except asyncio.TimeoutError:
        return ("Home Assistant took too long to answer.", None)
    except aiohttp.ClientError as exc:
        log.warning("HA conversation unreachable: %s", exc)
        return ("I could not reach Home Assistant.", None)

    new_id = data.get("conversation_id") or conversation_id
    # Response shape: {"response": {"speech": {"plain": {"speech": "..."}}}}
    speech = (
        data.get("response", {})
        .get("speech", {})
        .get("plain", {})
        .get("speech")
    )
    return (speech or "At your service, sir.", new_id)


# --- routing ---------------------------------------------------------------
# One router for the process. Stateless apart from counters, so sessions share
# it; per-connection conversation ids live in each Session's `route_state`.
ROUTER = IntentRouter(
    HASS_URL,
    HASS_TOKEN,
    local_agent=os.getenv("LOCAL_AGENT", "conversation.home_assistant"),
    llm_agent=CONVERSATION_AGENT,
    enabled=os.getenv("LOCAL_FIRST", "1").lower() not in ("0", "false", "no"),
)


async def route_utterance(
    session: aiohttp.ClientSession, text: str, state: dict[str, str | None]
) -> tuple[str, RouteResult]:
    """Route one utterance and return (reply, result). See router.py."""
    result = await ROUTER.process(session, text, state)
    if result.path == "local":
        log.info("routed LOCAL in %.1f ms%s", result.total_ms,
                 f" (rewritten: {result.rewritten!r})" if result.rewritten else "")
    else:
        log.info("routed %s in %.1f ms (local rejected: %s)",
                 result.path.upper(), result.total_ms, result.fallback_reason or "-")
    return result.speech, result


class Session:
    """One browser connection: owns its wake stream and utterance state."""

    def __init__(
        self,
        ws: web.WebSocketResponse,
        http: aiohttp.ClientSession,
        verifier: Any = None,
        app: web.Application | None = None,
    ) -> None:
        self.ws = ws
        self.http = http
        self.verifier = verifier
        # Held only so a display directive can be broadcast to every connected
        # HUD, not just the one that spoke -- the phone should follow the
        # desktop. Optional so nothing in the voice path depends on it.
        self.app = app
        self.wake = WakeStream(*WAKE, WAKE_WORD)
        self.state = "wake"          # wake | listening | busy
        self.frames: list[bytes] = []
        self.speech_started = False
        self.silence_s = 0.0
        self.listen_started = 0.0
        self.conversation_id: str | None = None
        # Per-connection conversation ids, one per agent. The two agents keep
        # SEPARATE histories -- handing an Ollama-minted id to the built-in
        # matcher makes HA resolve follow-ups against a history that does not
        # exist. router.IntentRouter owns the keys in here.
        self.route_state: dict[str, str | None] = {}
        self._detect_task: asyncio.Task | None = None

    async def send_json(self, **payload: Any) -> None:
        if not self.ws.closed:
            await self.ws.send_json(payload)

    async def show(self, text: str) -> None:
        """
        Answer a spoken request with a VISUAL, if it asked for one.

        Deliberately fire-and-forget and deliberately placed off the critical
        path: the matcher is a compiled regex over the transcript, no I/O and no
        model, and any failure is swallowed. Nothing here may cost the voice
        loop a millisecond or take it down.
        """
        try:
            directive = hud.directive_for(text)
            if not directive:
                return
            directive["reason"] = "utterance"
            if self.app is not None:
                await hud.broadcast_display(self.app, directive)
            else:
                await self.send_json(type="display", **directive)
        except Exception:  # never let a panel hint break speech
            log.exception("display directive failed (voice loop unaffected)")

    async def start(self) -> None:
        await self.wake.connect()
        self._detect_task = asyncio.create_task(self._watch_detections())

    async def _watch_detections(self) -> None:
        """Flip to listening when the wake word fires."""
        try:
            while True:
                name = await self.wake.next_detection()
                if name is None:
                    return
                if self.state != "wake":
                    # Already mid-utterance; ignore repeat hits.
                    continue
                log.info("wake word detected: %s", name)
                self._begin_listening()
                await self.send_json(type="wake", name=name)
        except asyncio.CancelledError:
            raise
        except (OSError, json.JSONDecodeError, asyncio.IncompleteReadError) as exc:
            log.warning("wake stream ended: %s", exc)
            await self.send_json(type="error", message="Wake word service disconnected.")

    def _begin_listening(self) -> None:
        self.state = "listening"
        self.frames = []
        self.speech_started = False
        self.silence_s = 0.0
        self.listen_started = time.monotonic()
        if self.app is not None:
            self.app["voice_state"]["state"] = "listening"
            self.app["voice_state"]["level"] = 0.0

    async def on_audio(self, pcm: bytes) -> None:
        if self.state == "busy":
            return

        if self.state == "wake":
            await self.wake.feed(pcm)
            return

        # state == listening
        self.frames.append(pcm)
        duration = len(pcm) / BYTES_PER_SEC
        rms = _rms_int16(pcm)
        loud = rms >= SILENCE_RMS
        # Feed the live envelope into the global voice state so the HUD can
        # drive a pulse ring without polling a separate source.
        if self.app is not None:
            self.app["voice_state"]["state"] = "listening"
            self.app["voice_state"]["level"] = min(1.0, rms / 8000.0)

        if loud:
            self.speech_started = True
            self.silence_s = 0.0
        elif self.speech_started:
            self.silence_s += duration

        elapsed = time.monotonic() - self.listen_started
        captured_s = sum(len(f) for f in self.frames) / BYTES_PER_SEC

        if not self.speech_started and elapsed >= NO_SPEECH_TIMEOUT_S:
            log.info("woke but heard nothing; returning to wake state")
            self.state = "wake"
            self.frames = []
            if self.app is not None:
                self.app["voice_state"]["state"] = "idle"
                self.app["voice_state"]["level"] = 0.0
            await self.send_json(type="idle", reason="no-speech")
            return

        done = (
            (self.speech_started and self.silence_s >= SILENCE_HANG_S
             and captured_s >= MIN_UTTERANCE_S)
            or captured_s >= MAX_UTTERANCE_S
        )
        if done:
            await self._handle_utterance()

    async def _handle_utterance(self) -> None:
        self.state = "busy"
        if self.app is not None:
            self.app["voice_state"]["state"] = "speaking"
            self.app["voice_state"]["level"] = 0.0
        pcm = b"".join(self.frames)
        self.frames = []

        try:
            await self.send_json(type="thinking", stage="stt")
            t0 = time.monotonic()
            text = await wy.transcribe(*WHISPER, pcm)
            stt_ms = int((time.monotonic() - t0) * 1000)

            if not text:
                await self.send_json(type="idle", reason="empty-transcript")
                return

            log.info("transcript (%d ms): %s", stt_ms, text)
            await self.send_json(type="transcript", text=text, stt_ms=stt_ms)
            # Fired here, BEFORE the model runs, on purpose: the panel is on
            # screen while JARVIS is still thinking, so the visual answer beats
            # the spoken one instead of trailing it.
            await self.show(text)

            await self.send_json(type="thinking", stage="intent")
            t1 = time.monotonic()
            # Wall-clock, not monotonic: it is compared against Home Assistant's
            # own last_updated timestamps to spot a read-back that predates the
            # command. The two containers share the host clock -- MEASURED skew
            # is 2 ms, so the comparison is safe.
            issued_at = time.time()
            reply, route = await route_utterance(self.http, text, self.route_state)
            intent_ms = int((time.monotonic() - t1) * 1000)
            log.info("reply (%d ms, %s): %s", intent_ms, route.path, reply)

            # THE GATE. Nothing reaches Piper until it has been checked against
            # what actually happened.
            reply, verdict = await self._verify(reply, issued_at)
            await self.send_json(
                type="reply", text=reply, intent_ms=intent_ms,
                route=route.path, local_ms=route.local_ms,
                **({"verification": verdict.as_payload()} if verdict else {}),
            )

            t2 = time.monotonic()
            spoke = await self._stream_speech(reply, t0, t2)
            if not spoke:
                await self.send_json(type="idle", reason="tts-empty")

        except (OSError, asyncio.IncompleteReadError, json.JSONDecodeError) as exc:
            log.exception("pipeline failure")
            await self.send_json(type="error", message=f"Voice pipeline failed: {exc}")
        finally:
            # Always return to wake, otherwise one failure wedges the session.
            self.state = "wake"
            if self.app is not None:
                self.app["voice_state"]["state"] = "idle"
                self.app["voice_state"]["level"] = 0.0

    async def _stream_speech(
        self, text: str, t0: float | None = None, t2: float | None = None
    ) -> bool:
        """
        Synthesise and stream to the browser chunk by chunk.

        Returns True if any audio was sent. The browser is told the stream is
        starting BEFORE the first chunk arrives, so it can begin playback on
        chunk one instead of waiting for the whole buffer -- that is the
        difference between ~2.1 s of dead air and audio starting almost at once.

        The sample rate is taken from the chunk stream rather than assumed:
        tts_engine may answer with Kokoro at 24 kHz or fall back to Piper at
        16 kHz, and `speak-start` has to declare whichever one actually spoke.
        Hardcoding 16 kHz here would play Kokoro audio at two-thirds speed --
        which sounds exactly like a deep, slow voice, i.e. plausible enough to
        ship by mistake.
        """
        t2 = t2 if t2 is not None else time.monotonic()
        started = False
        total = 0
        first_ms = None
        rate = wy.RATE

        async for chunk_rate, chunk in tts_engine.stream_speech(self.http, text):
            if self.ws.closed:
                break
            if not started:
                started = True
                rate = chunk_rate
                first_ms = int((time.monotonic() - t2) * 1000)
                await self.send_json(
                    type="speak-start",
                    rate=rate, width=wy.WIDTH, channels=wy.CHANNELS,
                    first_chunk_ms=first_ms,
                    # Time from end of speech to first audible sound -- the
                    # number a person actually experiences as "how fast is it".
                    ttfa_ms=int((time.monotonic() - t0) * 1000) if t0 else None,
                )
            total += len(chunk)
            await self.ws.send_bytes(chunk)

        if started:
            await self.send_json(
                type="speak-end",
                bytes=total,
                tts_ms=int((time.monotonic() - t2) * 1000),
                total_ms=int((time.monotonic() - t0) * 1000) if t0 else None,
            )
            log.info(
                "tts streamed %d bytes at %d Hz, first chunk %d ms, full %d ms",
                total, rate, first_ms or -1, int((time.monotonic() - t2) * 1000),
            )
        return started

    async def _verify(self, reply: str, issued_at: float):
        """
        Check the reply against reality. Returns (speech_to_use, verdict).

        Where the model's claim and Home Assistant disagree, the correction
        wins. A short honest failure is better than a fluent false success,
        because the listener cannot tell the two apart by ear.

        Verification never blocks speech: if the verifier is missing, times out,
        or throws, the model's own reply goes through unchanged and the turn is
        logged as unverified rather than silently trusted. Refusing to speak
        would be a worse failure than speaking something unchecked.
        """
        if self.verifier is None:
            return reply, None

        verdict = await self.verifier.verify(reply, self.conversation_id, issued_at)

        if verdict.corrected:
            log.warning(
                "VERIFICATION %s in %d ms | model said %r -> speaking %r | %s | tools=%s",
                verdict.status.upper(), verdict.verify_ms, reply,
                verdict.speech, verdict.detail, verdict.tool_calls,
            )
        else:
            log.info(
                "verification %s in %d ms (%s) | %s | tools=%s",
                verdict.status, verdict.verify_ms, verdict.confidence,
                verdict.detail, verdict.tool_calls,
            )
        return verdict.speech, verdict

    async def close(self) -> None:
        if self._detect_task:
            self._detect_task.cancel()
            try:
                await self._detect_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.wake.close()


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=8 * 1024 * 1024)
    await ws.prepare(request)

    session = Session(ws, request.app["http"], request.app.get("verifier"), request.app)
    # Registered so a directive reaches every open HUD -- desktop, phone and the
    # Rainmeter region stay on the same panel.
    request.app.setdefault("clients", set()).add(ws)
    try:
        await session.start()
    except OSError as exc:
        log.error("cannot reach wake word service: %s", exc)
        await ws.send_json({"type": "error", "message": "Wake word service unavailable."})
        await ws.close()
        request.app["clients"].discard(ws)
        return ws

    await ws.send_json({"type": "ready", "wake_word": WAKE_WORD})

    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                await session.on_audio(msg.data)
            elif msg.type == WSMsgType.TEXT:
                try:
                    cmd = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if cmd.get("type") == "push_to_talk":
                    # Manual trigger: same path as a wake-word hit, so the
                    # pipeline is testable without saying anything.
                    if session.state == "wake":
                        session._begin_listening()
                        await session.send_json(type="wake", name="push-to-talk")
                elif cmd.get("type") == "text":
                    # Typed command: skips STT, still exercises intent + TTS.
                    # It goes through the SAME verification gate as speech --
                    # this path is what the test harness drives, so letting it
                    # skip the check would mean testing a different program.
                    if session.state == "wake":
                        session.state = "busy"
                        issued_at = time.time()
                        await session.show(cmd.get("text", ""))
                        reply, route = await route_utterance(
                            session.http, cmd.get("text", ""), session.route_state
                        )
                        reply, verdict = await session._verify(reply, issued_at)
                        await session.send_json(
                            type="reply", text=reply,
                            route=route.path, local_ms=route.local_ms,
                            **({"verification": verdict.as_payload()} if verdict else {}),
                        )
                        await session._stream_speech(reply)
                        session.state = "wake"
                        if session.app is not None:
                            session.app["voice_state"]["state"] = "idle"
                            session.app["voice_state"]["level"] = 0.0
            elif msg.type == WSMsgType.ERROR:
                log.warning("websocket error: %s", ws.exception())
    finally:
        request.app["clients"].discard(ws)
        await session.close()

    return ws


async def healthz(request: web.Request) -> web.Response:
    """Liveness for the container healthcheck. Always 200 if the process is up."""
    return web.json_response({"status": "ok"})


async def status(request: web.Request) -> web.Response:
    """
    Real readiness. Probes each Wyoming service with a `describe` handshake
    rather than an HTTP GET -- these are TCP services and an HTTP request
    against them always fails, which is what made the old status endpoint
    report everything as down.
    """
    whisper_info, piper_info, wake_info = await asyncio.gather(
        wy.describe(*WHISPER), wy.describe(*PIPER), wy.describe(*WAKE)
    )

    hass_state = "error"
    try:
        headers = {"Authorization": f"Bearer {HASS_TOKEN}"} if HASS_TOKEN else {}
        async with request.app["http"].get(
            f"{HASS_URL}/api/", headers=headers,
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            hass_state = "ok" if resp.status == 200 else f"http-{resp.status}"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        hass_state = "unreachable"

    ollama_state = "error"
    try:
        async with request.app["http"].get(
            f"{os.getenv('OLLAMA_URL', 'http://jarvis-ollama:11434')}/api/tags",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            ollama_state = "ok" if resp.status == 200 else f"http-{resp.status}"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        ollama_state = "unreachable"

    return web.json_response({
        "hass": hass_state,
        "hass_token_configured": bool(HASS_TOKEN),
        "ollama": ollama_state,
        "whisper": "ok" if whisper_info else "unreachable",
        "piper": "ok" if piper_info else "unreachable",
        "wake_word": "ok" if wake_info else "unreachable",
        # Which engine is really speaking. Reported because the Kokoro->Piper
        # fallback is deliberately silent to the listener: JARVIS keeps talking,
        # he just stops sounding right. Without this the only evidence is a log
        # line nobody reads.
        "tts": await tts_engine.probe(request.app["http"]),
        # Live routing split. Worth surfacing rather than burying in logs: if a
        # config change or an HA upgrade breaks the local parser, the symptom is
        # not an error, it is local_share quietly falling to 0 while everything
        # still works a second slower. This is the number that catches that.
        "router": ROUTER.stats.snapshot(),
    })


async def verify_status(request: web.Request) -> web.Response:
    """
    Is the closed-loop check actually live, and can it read the chat log?

    Worth its own endpoint because verification failing OPEN is invisible from
    the outside -- the assistant keeps talking, it just stops being checked. The
    weekly audit can hit this and notice.
    """
    verifier = request.app.get("verifier")
    if verifier is None:
        return web.json_response({
            "enabled": False,
            "reason": "disabled by VERIFY_REPLIES"
                      if not VERIFY_REPLIES else "verifier not constructed",
        })
    ws_ok = await verifier.ping()
    return web.json_response({
        "enabled": True,
        "hass_websocket": "ok" if ws_ok else "unreachable",
    })


async def index(request: web.Request) -> web.StreamResponse:
    return web.FileResponse(os.path.join(STATIC_DIR, "index.html"))


async def vitals(request: web.Request) -> web.Response:
    """
    Host telemetry from the glances container that already runs on this server.

    Proxied rather than fetched directly by the page for two reasons: glances is
    on the host's published port, which a browser on the tailnet can reach only
    if it happens to be on the LAN too, and going through here keeps the UI a
    single origin so no CORS setup is needed.
    """
    async def grab(path: str) -> Any:
        try:
            async with request.app["http"].get(
                f"{GLANCES_URL}/api/4/{path}",
                timeout=aiohttp.ClientTimeout(total=4),
            ) as resp:
                if resp.status != 200:
                    return None
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    cpu, mem, load = await asyncio.gather(grab("cpu"), grab("mem"), grab("load"))

    out: dict[str, Any] = {}
    if cpu:
        out["cpu_percent"] = round(cpu.get("total", 0), 1)
        out["cpu_cores"] = cpu.get("cpucore")
    if mem:
        out["mem_percent"] = round(mem.get("percent", 0), 1)
        # Bytes to GB, so the page does not have to know the unit.
        out["mem_used_gb"] = round(mem.get("used", 0) / 1_073_741_824, 1)
        out["mem_total_gb"] = round(mem.get("total", 0) / 1_073_741_824, 1)
    if load:
        out["load_1m"] = load.get("min1")

    out["available"] = bool(out)
    return web.json_response(out)


# Only these domains are exposed as tiles. An allowlist rather than a filter:
# this UI is opened on a phone and sits on the desktop, so a stray tile that
# unlocks a door or disarms an alarm is a genuinely bad outcome.
TILE_DOMAINS = ("light", "switch", "fan", "climate")


async def devices(request: web.Request) -> web.Response:
    """Current state of the controllable entities, for the UI's tiles."""
    if not HASS_TOKEN:
        return web.json_response({"entities": [], "error": "no-token"})

    try:
        async with request.app["http"].get(
            f"{HASS_URL}/api/states",
            headers={"Authorization": f"Bearer {HASS_TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return web.json_response(
                    {"entities": [], "error": f"http-{resp.status}"}
                )
            states = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"entities": [], "error": "unreachable"})

    entities = []
    for s in states:
        eid = s.get("entity_id", "")
        if not eid.startswith(TILE_DOMAINS):
            continue
        attrs = s.get("attributes", {})
        entities.append({
            "entity_id": eid,
            "domain": eid.split(".", 1)[0],
            "name": attrs.get("friendly_name", eid),
            "state": s.get("state"),
            "brightness": attrs.get("brightness"),
            "temperature": attrs.get("temperature"),
            "current_temperature": attrs.get("current_temperature"),
            "percentage": attrs.get("percentage"),
        })
    entities.sort(key=lambda e: (e["domain"], e["name"]))
    return web.json_response({"entities": entities})


async def toggle(request: web.Request) -> web.Response:
    """
    Toggle one entity from a UI tile.

    Restricted to TILE_DOMAINS and to `toggle` specifically -- this endpoint is
    reachable by anything that can load the page, so it must not become a
    general-purpose proxy for arbitrary HA service calls.
    """
    if not HASS_TOKEN:
        return web.json_response({"ok": False, "error": "no-token"}, status=503)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response({"ok": False, "error": "bad-json"}, status=400)

    eid = str(body.get("entity_id", ""))
    domain = eid.split(".", 1)[0] if "." in eid else ""
    if domain not in TILE_DOMAINS:
        return web.json_response(
            {"ok": False, "error": "domain-not-allowed"}, status=403
        )

    try:
        async with request.app["http"].post(
            f"{HASS_URL}/api/services/{domain}/toggle",
            headers={"Authorization": f"Bearer {HASS_TOKEN}"},
            json={"entity_id": eid},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            ok = resp.status == 200
            return web.json_response({"ok": ok, "status": resp.status})
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return web.json_response({"ok": False, "error": "unreachable"}, status=502)


async def _prewarm_hermes(session: aiohttp.ClientSession) -> None:
    """Warm Ollama and Hermes before the first real request.

    The first Ollama chat call after any gap hits the model-load path and can
    take 10-15s. Subsequent calls finish in ~600ms. The 8s turn_timeout was
    killing every question ("what is the weather", "play music", "wake the pc")
    on first use because the cold-call penalty ate the whole budget.

    We probe once at startup (and once more after any long silence) so the
    very first real request lands on the fast path.
    """
    if not ROUTER._hermes:
        return
    try:
        t0 = time.perf_counter()
        await ROUTER._hermes.ask(session, "ping", specialist="chat")
        dt = (time.perf_counter() - t0) * 1000
        log.info("Hermes warmup: %.0f ms", dt)
    except Exception as exc:  # noqa: BLE001 - warmup is best-effort
        log.warning("Hermes warmup failed (%s); first user request will pay the cold start", exc)


# --- Ollama keep-alive background task ------------------------------------
# The RTX 3050 has only 4 GB VRAM shared between CUDA Whisper (~1.5 GB) and
# Ollama qwen2.5:3b (~2.1 GB). Despite OLLAMA_KEEP_ALIVE=-1, the GPU driver
# reclaims VRAM after ~5 min idle and evicts the model.
#
# Strategy: check /api/ps first. If model is already loaded, skip. If evicted,
# do a warmup with 30s timeout (cold-start is ~15s). The warmup itself uses
# the Hermes chat endpoint with num_predict=1 for minimal GPU time.
_OLLAMA_KEEPALIVE_INTERVAL_S = 150  # 2.5 minutes (under the ~5 min eviction window)


async def _ollama_keepalive_forever(session: aiohttp.ClientSession) -> None:
    """Periodically ping Ollama to prevent VRAM eviction.

    Without this, the model is evicted after ~5 min idle and the first user
    request pays a 5-8 s cold-start penalty. With it, the model stays warm
    and Hermes responds in 500-2000 ms every time.

    NOTE: /api/ps is unreliable on this hardware — it returns empty models
    list even when qwen2.5:3b is resident and answering. So we ALWAYS send
    a lightweight ping rather than skipping on the ps check.
    """
    if not ROUTER._hermes:
        return
    ollama_url = ROUTER._hermes.ollama_url
    while True:
        await asyncio.sleep(_OLLAMA_KEEPALIVE_INTERVAL_S)
        try:
            # Send a real ping to keep the model warm. This is the only
            # reliable way — /api/ps lies about eviction on WDDM.
            t0 = time.perf_counter()
            await ROUTER._hermes.ask(session, "ping", specialist="chat")
            dt = (time.perf_counter() - t0) * 1000
            log.debug("Ollama keep-alive: %.0f ms", dt)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ollama keep-alive failed: %s", exc)


async def on_startup(app: web.Application) -> None:
    app["http"] = aiohttp.ClientSession()
    # Global voice state for the HUD: the aggregate of all connected sessions.
    # Rainmeter reads this once per second; the browser HUD reads it in real
    # time over the same channel as everything else.
    app["voice_state"] = {"state": "idle", "level": 0.0}
    if not HASS_TOKEN:
        log.warning(
            "No HA token set (JARVIS_HASS_TOKEN or HA_API_TOKEN). "
            "Speech will transcribe but commands cannot be executed."
        )

    app["verifier"] = None
    if not VERIFY_REPLIES:
        log.warning(
            "VERIFY_REPLIES is off. Replies will be spoken exactly as the model "
            "produces them, INCLUDING confirmations for actions it never took."
        )
    elif ClosedLoopVerifier is None:
        log.error(
            "verify/closed_loop.py could not be imported -- replies will NOT be "
            "checked against Home Assistant."
        )
    elif not HASS_TOKEN:
        log.warning("No HA token, so replies cannot be verified.")
    else:
        app["verifier"] = ClosedLoopVerifier(HASS_URL, HASS_TOKEN, app["http"])
        log.info("closed-loop verification enabled")

    # Pre-warm Ollama/Hermes so the first user request never hits the cold
    # model-load penalty (10-15s) that was killing the 8s turn timeout.
    await _prewarm_hermes(app["http"])

    # Background keep-alive: ping Ollama every 3 min to prevent VRAM eviction
    # on the 4 GB RTX 3050 where CUDA Whisper shares the same memory pool.
    app["keepalive_task"] = asyncio.create_task(
        _ollama_keepalive_forever(app["http"])
    )


async def on_cleanup(app: web.Application) -> None:
    # Stop the keep-alive background task.
    ka = app.get("keepalive_task")
    if ka is not None:
        ka.cancel()
        try:
            await ka
        except asyncio.CancelledError:
            pass
    verifier = app.get("verifier")
    if verifier is not None:
        await verifier.close()
    await app["http"].close()



# HUD skin download -- one click for the Rainmeter package.
# The skin zip lives in ui/ alongside install-hud.ps1. Mounting the whole
# ui/ dir as static would expose install scripts and source, so we serve
# only the zip under /download/hud.
async def download_hud(request):
    hud_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ui')
    zip_path = os.path.join(hud_dir, 'jarvis-skin.zip')
    if not os.path.exists(zip_path):
        return web.HTTPNotFound()
    resp = web.FileResponse(zip_path)
    resp.content_type = 'application/zip'
    resp.headers['Content-Disposition'] = 'attachment; filename="JARVIS-HUD.rmskin"'
    return resp


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.get("/", index),
        web.get("/healthz", healthz),
        web.get("/api/status", status),
        web.get("/api/verify", verify_status),
        web.get("/api/vitals", vitals),
        web.get("/api/devices", devices),
        web.post("/api/toggle", toggle),
        web.get("/ws", ws_handler),
        web.static("/static", STATIC_DIR),
    ])
    # Panels, the display-directive channel, and the allowlisted actuation
    # endpoint. Registered last and entirely additively: every route it adds is
    # under /api/hud/, so nothing above can change behaviour.
    app.add_routes([web.get("/download/hud", download_hud)])
    hud.register(app)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=int(os.getenv("PORT", "3100")))
