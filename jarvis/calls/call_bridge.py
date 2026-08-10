#!/usr/bin/env python3
"""
call_bridge.py -- put JARVIS *on* a phone call.

This is the piece that makes calling useful rather than a dial button. When a
call is routed to this PC, Windows exposes the call audio as two ordinary WASAPI
endpoints named "realme C63 5G Hands-Free HF Audio" -- one capture, one render.
That means the EXISTING voice pipeline works unchanged:

    caller speech -> HF capture endpoint -> Whisper (jarvis-whisper :10300)
    JARVIS speech <- HF render  endpoint <- Piper   (jarvis-piper   :10200)

No new audio plumbing, no cloud, no paid API. Only the routing is new.

--------------------------------------------------------------------- SCOPE
The call itself travels over Airtel's network. "No cloud" cannot mean "no
carrier" here. What holds is: no paid API, no SIP provider, no third-party
service in the middle. Windows is the Hands-Free unit; the phone is the Audio
Gateway and dials on its own SIM.

-------------------------------------------------------------------- SAFETY
This module NEVER handles a phone number. It passes a contact NAME to
jarvis_call.ps1, which validates the name against contacts.json and refuses
anything not on the list. Numbers never enter this process, so they cannot end
up in a transcript, a log line, or a model prompt. The allowlist is enforced
twice: once here (fail fast, clearer message) and once in PowerShell (the real
boundary). Do not "optimise" the second check away.

------------------------------------------------------------------- MODES
  devices    list audio endpoints and show which ones would be used  [no phone]
  selftest   exercise the Whisper/Piper legs end to end               [no phone]
  state      print current call state as JSON
  watch      announce incoming calls out loud ("Mum is calling")
  answer     answer, greet, record the caller, transcribe, report back
  announce   call an approved contact and read out a short message

--------------------------------------------------------------- VERIFICATION
As of 2026-08-08 the phone was out of Bluetooth range for the whole build
(BluetoothDevice.FromBluetoothAddressAsync -> 0x800710DF device not ready), so:
  TESTED : devices, selftest, contact-name safety, Whisper leg, Piper leg,
           narrowband (8 kHz) transcription, endpoint matching against a
           recorded fixture of the real endpoint names.
  UNTESTED: everything that requires a live SCO link -- answer, announce,
           watch, and the actual HF endpoint capture/render.
See calls/NOTES.md for exactly what remains unproven.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import wyoming_client as wy  # noqa: E402  (path juggling is deliberate)

# --------------------------------------------------------------------- config
WHISPER_HOST = os.getenv("WHISPER_HOST", "127.0.0.1")
WHISPER_PORT = int(os.getenv("WHISPER_PORT", "10300"))
PIPER_HOST = os.getenv("PIPER_HOST", "127.0.0.1")
PIPER_PORT = int(os.getenv("PIPER_PORT", "10200"))
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_GB-alan-medium")

PS = os.getenv("POWERSHELL", r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")
CALL_PS1 = HERE / "jarvis_call.ps1"
CONTACTS = HERE / "contacts.json"
MESSAGES_DIR = HERE / "messages"

# Wyoming services in this stack are all 16 kHz / 16-bit / mono.
ASR_RATE = 16000

# The HF endpoints are named after the phone. Matching on "hands-free" is what
# distinguishes the CALL endpoints from the A2DP music endpoint of the same
# phone -- picking the A2DP one would give JARVIS music-quality silence and no
# microphone at all, because A2DP is one-directional.
HF_HINTS = ("hands-free", "hands free", "handsfree")

# What a caller hears if nobody configures anything. Deliberately does not claim
# to be a person and does not promise a call back on Natik's behalf.
DEFAULT_GREETING = (
    "Hello. Natik is not available to take this call right now. "
    "This is his assistant. Please say your message after the tone, "
    "and I will pass it on."
)

# Recording limits. A voicemail is short by design; an unbounded recorder that
# silently runs for ten minutes is how you fill a disk and lose a message.
MAX_MESSAGE_SEC = 60.0
SILENCE_HANGUP_SEC = 3.5
MIN_MESSAGE_SEC = 1.0


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ PowerShell
def call_ps(action: str, **kwargs: Any) -> dict:
    """
    Invoke jarvis_call.ps1 and parse its single JSON object.

    Every argument is passed as a separate argv element, never interpolated into
    a command string, so a contact name can never become a second command.
    """
    args = [PS, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(CALL_PS1),
            "-Action", action]
    for k, v in kwargs.items():
        if v is None:
            continue
        if isinstance(v, bool):
            if v:
                args.append(f"-{k}")
        else:
            args += [f"-{k}", str(v)]

    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        return {"ok": False, "action": action, "reason": "call control timed out"}

    out = (proc.stdout or "").strip()
    # Take the last JSON-looking line; PowerShell may emit warnings before it.
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {
        "ok": False,
        "action": action,
        "reason": f"call control produced no JSON (exit {proc.returncode})",
        "stdout": out[:400],
        "stderr": (proc.stderr or "")[:400],
    }


def approved_contacts() -> list[str]:
    res = call_ps("contacts")
    if not res.get("ok"):
        return []
    return list(res.get("contacts") or [])


# --------------------------------------------------------------------- audio
@dataclass
class Endpoints:
    capture_index: int | None
    capture_name: str
    capture_rate: int
    render_index: int | None
    render_name: str
    render_rate: int

    def ok(self) -> bool:
        return self.capture_index is not None and self.render_index is not None


def _sd():
    import sounddevice as sd
    return sd


def refresh_devices() -> None:
    """
    Force PortAudio to re-enumerate.

    This matters more than it looks: the HF endpoints do not exist until the
    phone connects and, on many stacks, only go ACTIVE while a call is up.
    PortAudio caches the device list at import, so without this the bridge would
    look for the endpoint using a snapshot taken before the call existed and
    conclude the phone was absent.
    """
    sd = _sd()
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:  # pragma: no cover - depends on PortAudio build
        log(f"warning: could not re-enumerate audio devices ({exc})")


def find_endpoints(phone_hint: str = "") -> Endpoints:
    sd = _sd()
    hint = phone_hint.lower().strip()
    cap_i = ren_i = None
    cap_n = ren_n = ""
    cap_r = ren_r = ASR_RATE

    for idx, dev in enumerate(sd.query_devices()):
        name = str(dev["name"])
        low = name.lower()
        if not any(h in low for h in HF_HINTS):
            continue
        if hint and hint not in low:
            continue
        host = sd.query_hostapis(dev["hostapi"])["name"]
        # Prefer WASAPI; the same endpoint is also exposed via MME/DirectSound
        # with worse latency and, for MME, a 31-character truncated name.
        if dev["max_input_channels"] > 0 and (cap_i is None or "WASAPI" in host):
            cap_i, cap_n, cap_r = idx, name, int(dev["default_samplerate"])
        if dev["max_output_channels"] > 0 and (ren_i is None or "WASAPI" in host):
            ren_i, ren_n, ren_r = idx, name, int(dev["default_samplerate"])

    return Endpoints(cap_i, cap_n, cap_r, ren_i, ren_n, ren_r)


def resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Linear resample of mono int16. Good enough for 8k<->16k speech."""
    if src == dst or pcm.size == 0:
        return pcm
    n_out = int(round(pcm.size * dst / src))
    x_old = np.linspace(0.0, 1.0, pcm.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
    return np.interp(x_new, x_old, pcm.astype(np.float32)).astype(np.int16)


def rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(frame.astype(np.float32)))))


def record_until_silence(
    device: int,
    rate: int,
    max_sec: float = MAX_MESSAGE_SEC,
    silence_sec: float = SILENCE_HANGUP_SEC,
    on_gone=None,
) -> np.ndarray:
    """
    Record from the HF capture endpoint until the caller stops talking.

    Endpointing is energy-based against a noise floor measured from the first
    300 ms of the stream, because a phone line's floor varies far more than a
    room microphone's. A fixed threshold either clips the first word on a noisy
    line or never triggers on a quiet one.
    """
    sd = _sd()
    block = int(rate * 0.05)  # 50 ms
    chunks: list[np.ndarray] = []
    floor: float | None = None
    floor_frames: list[float] = []
    silent_for = 0.0
    voiced_total = 0.0
    started = time.time()

    with sd.InputStream(device=device, channels=1, samplerate=rate,
                        dtype="int16", blocksize=block) as stream:
        while True:
            if time.time() - started > max_sec:
                log("recording hit the maximum length")
                break
            if on_gone is not None and on_gone():
                log("caller hung up")
                break

            data, overflowed = stream.read(block)
            if overflowed:
                log("warning: capture overflow (dropped audio)")
            frame = np.asarray(data, dtype=np.int16).reshape(-1)
            chunks.append(frame)
            level = rms(frame)

            if floor is None:
                floor_frames.append(level)
                if len(floor_frames) >= 6:  # 300 ms
                    floor = max(float(np.median(floor_frames)), 30.0)
                    log(f"noise floor {floor:.0f}")
                continue

            threshold = max(floor * 3.0, 90.0)
            if level > threshold:
                silent_for = 0.0
                voiced_total += block / rate
            else:
                silent_for += block / rate
                # Only treat silence as the end once the caller has actually said
                # something; otherwise a slow starter is cut off mid-greeting.
                if voiced_total >= MIN_MESSAGE_SEC and silent_for >= silence_sec:
                    log("caller stopped speaking")
                    break

    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks)


def play(pcm16k: bytes, device: int, rate: int) -> None:
    """Play 16 kHz mono PCM into an endpoint, resampling to its native rate."""
    sd = _sd()
    audio = np.frombuffer(pcm16k, dtype=np.int16)
    audio = resample(audio, ASR_RATE, rate)
    sd.play(audio, samplerate=rate, device=device, blocking=True)


def tone(device: int, rate: int, freq: float = 880.0, secs: float = 0.35) -> None:
    sd = _sd()
    t = np.linspace(0, secs, int(rate * secs), endpoint=False)
    wave = (np.sin(2 * np.pi * freq * t) * 8000).astype(np.int16)
    sd.play(wave, samplerate=rate, device=device, blocking=True)


# ------------------------------------------------------------------- pipeline
async def say_to(text: str, device: int, rate: int) -> None:
    pcm = await wy.synthesize(PIPER_HOST, PIPER_PORT, text, PIPER_VOICE)
    if not pcm:
        log("Piper returned no audio")
        return
    play(pcm, device, rate)


async def say_local(text: str) -> None:
    """Speak on the PC's normal speakers -- this is JARVIS talking to Natik."""
    pcm = await wy.synthesize(PIPER_HOST, PIPER_PORT, text, PIPER_VOICE)
    if not pcm:
        log(f"(Piper unavailable) {text}")
        return
    sd = _sd()
    sd.play(np.frombuffer(pcm, dtype=np.int16), samplerate=ASR_RATE, blocking=True)


async def transcribe(pcm: np.ndarray, rate: int) -> str:
    if pcm.size == 0:
        return ""
    audio = resample(pcm, rate, ASR_RATE)
    return await wy.transcribe(WHISPER_HOST, WHISPER_PORT, audio.tobytes())


def save_message(caller: str, text: str, pcm: np.ndarray, rate: int) -> Path:
    MESSAGES_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c for c in (caller or "unknown") if c.isalnum() or c in "-_") or "unknown"
    base = MESSAGES_DIR / f"{stamp}-{safe}"

    import wave
    with wave.open(str(base.with_suffix(".wav")), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())

    base.with_suffix(".json").write_text(json.dumps({
        "caller": caller,
        "text": text,
        "at": dt.datetime.now().isoformat(),
        "audio": base.with_suffix(".wav").name,
        "seconds": round(pcm.size / rate, 2),
    }, indent=2), encoding="utf-8")
    return base.with_suffix(".json")


# ---------------------------------------------------------------- call state
def current_call() -> dict:
    return call_ps("state")


def describe_caller(call: dict) -> str:
    """Best available human label, preferring the allowlist name."""
    known = (call.get("known_contact") or "").strip()
    if known:
        return known
    disp = (call.get("display_name") or "").strip()
    if disp:
        return disp
    if call.get("number_known"):
        return "an unknown number"
    return "someone"


# -------------------------------------------------------------------- modes
def mode_devices(args) -> int:
    sd = _sd()
    refresh_devices()
    print("--- all audio devices ---")
    for idx, dev in enumerate(sd.query_devices()):
        host = sd.query_hostapis(dev["hostapi"])["name"]
        print(f"  {idx:3d}  in={dev['max_input_channels']} out={dev['max_output_channels']} "
              f"{host:20s} {dev['name']}")
    ep = find_endpoints(args.phone)
    print("\n--- endpoints the bridge would use ---")
    print(json.dumps(asdict(ep), indent=2))
    if not ep.ok():
        print("\nNo Hands-Free endpoints. Expected while no call is routed to this PC.")
        print("They appear once the phone is connected AND the call audio is routed here.")
        return 1
    return 0


async def mode_selftest(args) -> int:
    """
    Prove the parts that do not need a phone: Whisper, Piper, the resampler,
    endpoint matching, and the contact allowlist.
    """
    ok = True

    print("=== 1. Wyoming services ===")
    for name, host, port in (("piper", PIPER_HOST, PIPER_PORT), ("whisper", WHISPER_HOST, WHISPER_PORT)):
        info = await wy.describe(host, port)
        print(f"  {name:8s} {'OK' if info else 'UNREACHABLE'}")
        ok &= bool(info)

    print("\n=== 2. Piper -> Whisper round trip ===")
    phrase = "Hello, this is Ravi. Please call me back about the meeting tomorrow."
    t0 = time.time()
    pcm = await wy.synthesize(PIPER_HOST, PIPER_PORT, phrase, PIPER_VOICE)
    t1 = time.time()
    print(f"  synth {len(pcm)} bytes ({len(pcm)/2/ASR_RATE:.2f}s audio) in {t1-t0:.2f}s")
    text = await wy.transcribe(WHISPER_HOST, WHISPER_PORT, pcm)
    print(f"  transcribe in {time.time()-t1:.2f}s -> {text!r}")
    ok &= bool(text)

    print("\n=== 3. narrowband realism (HFP CVSD is 8 kHz) ===")
    a = np.frombuffer(pcm, dtype=np.int16)
    nb = resample(resample(a, ASR_RATE, 8000), 8000, ASR_RATE)
    t2 = time.time()
    text_nb = await wy.transcribe(WHISPER_HOST, WHISPER_PORT, nb.tobytes())
    print(f"  8k-degraded transcribe in {time.time()-t2:.2f}s -> {text_nb!r}")
    ok &= bool(text_nb)

    print("\n=== 4. endpoint discovery ===")
    refresh_devices()
    ep = find_endpoints(args.phone)
    if ep.ok():
        print(f"  capture: [{ep.capture_index}] {ep.capture_name} @ {ep.capture_rate}")
        print(f"  render : [{ep.render_index}] {ep.render_name} @ {ep.render_rate}")
    else:
        print("  no Hands-Free endpoints right now (expected with no call routed here)")

    print("\n=== 5. contact allowlist ===")
    names = approved_contacts()
    print(f"  approved names: {names}")
    refusal = call_ps("dial", Contact="definitely-not-a-contact")
    refused = (not refusal.get("ok")) and "not on the approved call list" in refusal.get("reason", "")
    print(f"  unapproved name refused: {refused}")
    ok &= refused

    print("\n=== 6. message persistence ===")
    demo = np.zeros(ASR_RATE // 2, dtype=np.int16)
    p = save_message("selftest", text or "(empty)", demo, ASR_RATE)
    print(f"  wrote {p}")
    ok &= p.exists()

    print("\nSELFTEST", "PASS" if ok else "FAIL")
    return 0 if ok else 1


async def mode_state(args) -> int:
    st = current_call()
    print(json.dumps(st, indent=2))
    return 0 if st.get("ok") else 1


async def mode_watch(args) -> int:
    """Announce ringing calls on the PC speakers. Never answers by itself."""
    log("watching for incoming calls (Ctrl-C to stop)")
    announced: set[str] = set()
    while True:
        st = current_call()
        if st.get("ok"):
            for call in st.get("calls") or []:
                cid = call.get("call_id") or ""
                status = call.get("status") or ""
                if status == "Incoming" and cid not in announced:
                    announced.add(cid)
                    who = describe_caller(call)
                    log(f"INCOMING from {who}")
                    await say_local(f"{who} is calling.")
            live = {c.get("call_id") for c in (st.get("calls") or [])}
            announced &= {c for c in announced if c in live}
        await asyncio.sleep(args.poll)


async def mode_answer(args) -> int:
    """
    Priority 1: answer, greet, record the caller, transcribe, report to Natik.
    """
    st = current_call()
    if not st.get("ok"):
        log(f"cannot read call state: {st.get('reason')}")
        return 1
    incoming = [c for c in (st.get("calls") or []) if (c.get("status") == "Incoming")]
    if not incoming:
        log("no incoming call to answer")
        return 1

    who = describe_caller(incoming[0])
    log(f"answering call from {who}")

    res = call_ps("answer")
    if not res.get("ok"):
        log(f"answer failed: {res.get('reason')}")
        return 1

    # Routing the audio to this PC is what actually puts JARVIS on the call.
    routed = call_ps("route", To="pc")
    if not routed.get("ok"):
        log(f"could not route call audio to the PC: {routed.get('reason')}")
        log("hanging up rather than leaving the caller connected to silence")
        call_ps("hangup")
        return 1

    # The endpoints only materialise once the SCO link is up, so re-enumerate.
    time.sleep(1.0)
    refresh_devices()
    ep = find_endpoints(args.phone)
    if not ep.ok():
        log("call audio endpoints did not appear; hanging up")
        call_ps("hangup")
        return 1
    log(f"on the call: capture[{ep.capture_index}]@{ep.capture_rate} "
        f"render[{ep.render_index}]@{ep.render_rate}")

    greeting = args.greeting or DEFAULT_GREETING
    await say_to(greeting, ep.render_index, ep.render_rate)
    tone(ep.render_index, ep.render_rate)

    def gone() -> bool:
        s = current_call()
        if not s.get("ok"):
            return True
        return not any(c.get("status") in ("Talking", "Incoming", "Held")
                       for c in (s.get("calls") or []))

    log("recording the caller")
    pcm = record_until_silence(ep.capture_index, ep.capture_rate,
                               max_sec=args.max_seconds, on_gone=None)

    await say_to("Thank you. I will pass that on. Goodbye.", ep.render_index, ep.render_rate)
    call_ps("hangup")

    log("transcribing")
    text = await transcribe(pcm, ep.capture_rate)
    path = save_message(who, text, pcm, ep.capture_rate)
    log(f"saved {path}")

    if text:
        summary = f"{who} called and said: {text}"
    else:
        summary = f"{who} called but did not leave a message I could make out."
    print(summary)
    await say_local(summary)
    return 0


async def mode_announce(args) -> int:
    """
    Priority 2: call an approved contact and read a short message.

    The contact NAME is all that crosses this boundary. No number is ever
    constructed, read, or logged here.
    """
    if not args.contact:
        log("--contact is required")
        return 1
    names = approved_contacts()
    if names and args.contact.lower() not in names:
        # Fail fast with a helpful message. jarvis_call.ps1 enforces this again.
        log(f"'{args.contact}' is not on the approved call list. Approved: {names}")
        return 1
    if not args.message:
        log("--message is required")
        return 1

    log(f"dialling {args.contact}")
    res = call_ps("dial", Contact=args.contact)
    if not res.get("ok"):
        log(f"dial failed: {res.get('reason')}")
        return 1

    # Wait for the callee to actually pick up. Speaking into a ringing line just
    # talks to a ringback tone and the message is lost.
    deadline = time.time() + args.ring_timeout
    talking = False
    while time.time() < deadline:
        st = current_call()
        calls = st.get("calls") or []
        if st.get("ok") and any(c.get("status") == "Talking" for c in calls):
            talking = True
            break
        if st.get("ok") and not calls:
            log("call ended before it was answered")
            return 1
        await asyncio.sleep(1.0)

    if not talking:
        log("nobody answered within the ring timeout; hanging up")
        call_ps("hangup")
        return 1

    routed = call_ps("route", To="pc")
    if not routed.get("ok"):
        log(f"could not route audio to the PC: {routed.get('reason')}")
        call_ps("hangup")
        return 1

    time.sleep(1.0)
    refresh_devices()
    ep = find_endpoints(args.phone)
    if not ep.ok():
        log("call audio endpoints did not appear; hanging up")
        call_ps("hangup")
        return 1

    body = f"Hello. This is Natik's assistant with a message. {args.message}"
    # Said twice: the first second of a mobile call is routinely clipped while
    # the codec settles, and a one-shot message that loses its opening words is
    # worse than useless.
    await say_to(body, ep.render_index, ep.render_rate)
    await asyncio.sleep(0.4)
    await say_to(f"Again. {args.message}. Goodbye.", ep.render_index, ep.render_rate)

    reply_text = ""
    if args.listen:
        log("listening for a reply")
        pcm = record_until_silence(ep.capture_index, ep.capture_rate, max_sec=args.max_seconds)
        reply_text = await transcribe(pcm, ep.capture_rate)
        if pcm.size:
            save_message(args.contact, reply_text, pcm, ep.capture_rate)

    call_ps("hangup")
    if reply_text:
        line = f"{args.contact} replied: {reply_text}"
        print(line)
        await say_local(line)
    else:
        log("message delivered")
    return 0


# --------------------------------------------------------------------- entry
def main() -> int:
    ap = argparse.ArgumentParser(description="Put JARVIS on a phone call.")
    ap.add_argument("--mode", required=True,
                    choices=["devices", "selftest", "state", "watch", "answer", "announce"])
    ap.add_argument("--contact", default="", help="approved contact NAME (never a number)")
    ap.add_argument("--message", default="", help="what to read out on the call")
    ap.add_argument("--greeting", default="", help="override the answering greeting")
    ap.add_argument("--phone", default="", help="substring of the phone name, to disambiguate")
    ap.add_argument("--poll", type=float, default=2.0, help="watch poll interval, seconds")
    ap.add_argument("--ring-timeout", type=float, default=45.0)
    ap.add_argument("--max-seconds", type=float, default=MAX_MESSAGE_SEC)
    ap.add_argument("--listen", action="store_true", help="also record a reply in announce mode")
    args = ap.parse_args()

    if args.mode == "devices":
        return mode_devices(args)
    fn = {
        "selftest": mode_selftest,
        "state": mode_state,
        "watch": mode_watch,
        "answer": mode_answer,
        "announce": mode_announce,
    }[args.mode]
    try:
        return asyncio.run(fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
