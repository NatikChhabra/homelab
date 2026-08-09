#!/usr/bin/env python3
"""
Adversarial tests for closed-loop verification, against the LIVE stack.

The point of this file is that a verifier which has only been tested on the
happy path is worthless. The bug it exists to catch is by definition the case
where the model sounds right and is wrong, so the tests have to manufacture
exactly that and prove the gate holds.

Cases covered, each printed with the verbatim exchange:

  1  GENUINE      a command that really works, verified end to end
  2  NO ACTION    a confident sentence with zero tool calls  <- the headline bug
  3  UNREACHABLE  a command aimed at something that is not there
  4  INFRARED     an IR command, checked for overclaiming physical state
  5  RAPID        back-to-back commands, checking a stale read is not a mismatch
  6  HONEST NO    a refusal must NOT be rewritten into a failure notice

Cases 2 and 3 are INJECTED rather than waited for. Injection is the honest way
to test a rare fault: it pins the exact condition instead of hoping the model
misbehaves while someone is watching. Case 2 injects by taking a real
conversation whose last turn called nothing (a question) and asking the verifier
to check a fabricated confirmation against it -- which is bit for bit the
situation that produced "AC set to 22. Fan speed is now 3.".

SAFETY: snapshots every helper and light it can disturb and restores them at the
end, including on exception. Sends no IR "off" it did not first send an "on" for.
Never opens a tinytuya session -- a Tuya device permits ONE local connection and
Home Assistant already holds it.

Run:
    docker cp verify/ jarvis-voice:/tmp/verify/
    docker exec jarvis-voice python3 /tmp/verify/adversarial_test.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from closed_loop import (  # noqa: E402
    ClosedLoopVerifier, DEVICES, Feedback, ToolCall, _correction_speech,
    _failure_speech,
)

TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")
BASE = os.getenv("HASS_URL", "http://jarvis-hass:8123").rstrip("/")
AGENT = os.getenv("CONVERSATION_AGENT", "conversation.jarvis")
AUTH = {"Authorization": f"Bearer {TOKEN}"}

RESTORE = [
    "light.node_switch_gang_1",
    "light.node_switch_gang_2",
    "light.node_switch_gang_3",
    "input_number.fan_speed",
    "input_number.fan_last_speed",
    "input_boolean.fan_power",
    "input_number.ac_temperature",
    "input_boolean.ac_power",
]

results: list[tuple[str, bool, str]] = []
verify_times: list[int] = []


def record(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"\n  >>> {'PASS' if ok else 'FAIL'}  {name}" + (f" -- {note}" if note else ""))


async def snapshot(s: aiohttp.ClientSession) -> dict:
    async with s.get(f"{BASE}/api/states", headers=AUTH) as r:
        all_states = {x["entity_id"]: x["state"] for x in await r.json()}
    return {e: all_states[e] for e in RESTORE if e in all_states}


async def restore(s: aiohttp.ClientSession, snap: dict) -> None:
    print("\n" + "=" * 74)
    print("RESTORING PRE-TEST STATE")
    async with s.get(f"{BASE}/api/states", headers=AUTH) as r:
        now = {x["entity_id"]: x["state"] for x in await r.json()}
    for eid, want in snap.items():
        have = now.get(eid)
        if have == want:
            continue
        print(f"  {eid}: {have} -> {want}")
        domain = eid.split(".")[0]
        try:
            if domain == "light":
                await s.post(f"{BASE}/api/services/light/turn_{want}",
                             json={"entity_id": eid}, headers=AUTH)
            elif domain == "input_number":
                await s.post(f"{BASE}/api/services/input_number/set_value",
                             json={"entity_id": eid, "value": float(want)},
                             headers=AUTH)
            elif domain == "input_boolean":
                await s.post(f"{BASE}/api/services/input_boolean/turn_{want}",
                             json={"entity_id": eid}, headers=AUTH)
        except aiohttp.ClientError as exc:
            print(f"    restore failed: {exc}")
        await asyncio.sleep(0.25)
    print("  done")


async def say(s: aiohttp.ClientSession, text: str, cid: str | None = None):
    """One turn through the real conversation agent. Returns (reply, cid, ms, issued_at)."""
    payload = {"text": text, "language": "en", "agent_id": AGENT}
    if cid:
        payload["conversation_id"] = cid
    issued = time.time()
    t = time.monotonic()
    async with s.post(f"{BASE}/api/conversation/process", json=payload,
                      headers=AUTH,
                      timeout=aiohttp.ClientTimeout(total=180)) as r:
        data = await r.json()
    ms = int((time.monotonic() - t) * 1000)
    reply = data.get("response", {}).get("speech", {}).get("plain", {}).get("speech", "")
    return reply, data.get("conversation_id"), ms, issued


def show(utterance: str, reply: str, verdict, llm_ms: int | None = None) -> None:
    print(f"\n  utterance      : {utterance!r}")
    if llm_ms is not None:
        print(f"  LLM reply      : {reply!r}   ({llm_ms} ms)")
    else:
        print(f"  model claimed  : {reply!r}")
    print(f"  tool calls     : {verdict.tool_calls}")
    print(f"  status         : {verdict.status}   confidence={verdict.confidence}")
    print(f"  SPOKEN         : {verdict.speech!r}")
    print(f"  corrected      : {verdict.corrected}")
    print(f"  detail         : {verdict.detail}")
    if verdict.checks:
        for c in verdict.checks:
            print(f"    check: {json.dumps(c)}")
    print(f"  verify cost    : {verdict.verify_ms} ms")
    verify_times.append(verdict.verify_ms)


# ---------------------------------------------------------------- case 1
async def case_genuine(s, v) -> None:
    print("\n" + "=" * 74)
    print("CASE 1 -- GENUINE COMMAND, verified end to end")
    print("The lights are the only device with a real feedback path, so this is")
    print("the only case where 'confirmed' is an honest word.")
    reply, cid, ms, issued = await say(s, "turn on the lights")
    verdict = await v.verify(reply, cid, issued)
    show("turn on the lights", reply, verdict, ms)

    async with s.get(f"{BASE}/api/states/light.node_switch_gang_1", headers=AUTH) as r:
        actual = (await r.json())["state"]
    print(f"  ground truth   : light.node_switch_gang_1 = {actual}")

    ok = verdict.status in ("ok", "failed", "mismatch")
    if verdict.status == "ok":
        ok = not verdict.corrected and actual == "on"
        record("genuine command passes verification unchanged", ok,
               f"spoke the model's own words, lights really are {actual}")
    else:
        # Lights genuinely unreachable right now -> honest failure is CORRECT.
        record("genuine command: device unreachable, reported honestly",
               verdict.corrected,
               f"status={verdict.status}, spoke {verdict.speech!r}")


# ---------------------------------------------------------------- case 2
async def case_no_tool_calls(s, v) -> None:
    print("\n" + "=" * 74)
    print("CASE 2 -- ZERO TOOL CALLS, CONFIDENT REPLY   <<< the headline defect")
    print("A question turn calls no device tool. We then ask the verifier to")
    print("check a FABRICATED confirmation against that turn -- byte for byte")
    print("the condition that produced 'AC set to 22. Fan speed is now 3.'")

    reply, cid, ms, issued = await say(s, "what time is it?")
    print(f"\n  (setup turn {ms} ms, real reply {reply!r})")

    for fabricated in (
        "AC set to 22. Fan speed is now 3.",
        "Lights on.",
        "Done. Fan on four.",
        "AC at 22, sir.",
    ):
        verdict = await v.verify(fabricated, cid, issued)
        show("what time is it?", fabricated, verdict)
        # The requirement is behavioural, not a particular status code: the
        # fabrication must not reach the speaker, and what replaces it must not
        # imply the action happened. GetDateTime counts as a tool call, so this
        # legitimately arrives as either no_action or mismatch.
        suppressed = fabricated not in verdict.speech and verdict.corrected
        no_partial_credit = "part of that worked" not in verdict.speech.lower()
        record(f"fabricated {fabricated!r} is not spoken as fact",
               suppressed and no_partial_credit
               and verdict.status in ("no_action", "mismatch"),
               f"status={verdict.status}, spoke {verdict.speech!r}")


    # The measured defect in its exact original form: a REAL, SUCCESSFUL AC call
    # with a fabricated fan clause bolted on. The AC half must survive, the fan
    # half must be contradicted. This is the single most important assertion in
    # the file -- it is the bug, reproduced.
    print("\n  --- the original defect, reproduced exactly ---")
    reply, cid2, ms, issued2 = await say(s, "set the air conditioner to 22")
    print(f"  real turn: {reply!r} ({ms} ms)")
    verdict = await v.verify("AC set to 22. Fan speed is now 3.", cid2, issued2)
    show("set the air conditioner to 22",
         "AC set to 22. Fan speed is now 3.", verdict)
    ac_kept = "22" in verdict.speech
    fan_flagged = "fan" in verdict.speech.lower() and "did not" in verdict.speech.lower()
    record("partial hallucination: true half kept, invented half contradicted",
           verdict.corrected and ac_kept and fan_flagged,
           f"spoke {verdict.speech!r}")


# ---------------------------------------------------------------- case 3
async def case_unreachable(s, v) -> None:
    print("\n" + "=" * 74)
    print("CASE 3 -- TARGET THAT IS NOT THERE")
    print("No hardware is unplugged and nothing is re-paired: the command simply")
    print("names a device that does not exist in this room.")

    for utterance in ("turn on the kettle", "set the bedroom heater to 30"):
        reply, cid, ms, issued = await say(s, utterance)
        verdict = await v.verify(reply, cid, issued)
        show(utterance, reply, verdict, ms)
        # Two acceptable outcomes, and both are honest: the model refuses
        # (no tools, no claim) or it tries and the tool errors (failed).
        ok = (
            verdict.status == "failed"
            or (verdict.status == "ok" and not verdict.corrected
                and verdict.tool_calls == [])
            or verdict.status == "no_action"
        )
        record(f"{utterance!r} does not produce a false confirmation", ok,
               f"status={verdict.status}, spoke {verdict.speech!r}")

    # Deterministic proof of the all-failed speech path, independent of what
    # the model happens to do on the day.
    failed = [ToolCall("HassFanSetSpeed", {"name": "Node Switch Gang 3"},
                       {"error": "MatchFailedError",
                        "error_text": "no_match_reason=NAME"}, True)]
    speech = _failure_speech(["fan"], failed)
    print(f"\n  synthetic all-failed speech: {speech!r}  ({len(speech.split())} words)")
    record("failure speech is short and honest",
           "could not find" in speech.lower() and len(speech.split()) <= 12, speech)


# ---------------------------------------------------------------- case 4
async def case_infrared(s, v) -> None:
    print("\n" + "=" * 74)
    print("CASE 4 -- INFRARED, WHICH CANNOT BE CONFIRMED")
    print("The AC and fan are one-way IR through the Zoook blaster. Nothing in")
    print("the room observes them. The strongest true statement is 'the send")
    print("succeeded and HA's own state updated' -- never 'the AC is at 22'.")

    reply, cid, ms, issued = await say(s, "set the air conditioner to 24")
    verdict = await v.verify(reply, cid, issued)
    show("set the air conditioner to 24", reply, verdict, ms)

    print(f"\n  DEVICES['ac'].feedback = {DEVICES['ac'].feedback!r} "
          f"(Feedback.OPEN_LOOP = {Feedback.OPEN_LOOP!r})")
    record("AC is classified as open-loop infrared",
           DEVICES["ac"].feedback == Feedback.OPEN_LOOP)
    record("fan is classified as open-loop infrared",
           DEVICES["fan"].feedback == Feedback.OPEN_LOOP)
    record("lights are classified as having real feedback",
           DEVICES["light"].feedback == Feedback.REAL)

    if verdict.status == "ok" and verdict.tool_calls:
        record("an IR success is labelled 'sent', never 'confirmed'",
               verdict.confidence == "sent",
               f"confidence={verdict.confidence!r}")
    else:
        record("IR turn did not produce a false confirmation",
               verdict.corrected or verdict.status == "ok",
               f"status={verdict.status}")


# ---------------------------------------------------------------- case 5
async def case_rapid(s, v) -> None:
    print("\n" + "=" * 74)
    print("CASE 5 -- RAPID REPEATED COMMANDS")
    print("A read-back that races a write must never be reported as a mismatch.")
    print("A verifier that cries wolf gets ignored, which puts us back at the")
    print("start. Stale reads degrade to 'unverified', never to 'mismatch'.")

    cid = None
    for i, utterance in enumerate(
        ("fan speed 2", "fan speed 4", "fan speed 1"), start=1
    ):
        reply, cid, ms, issued = await say(s, utterance, cid)
        verdict = await v.verify(reply, cid, issued)
        show(f"[rapid {i}] {utterance}", reply, verdict, ms)
        stale = [c for c in verdict.checks if c.get("result") == "stale-read-skipped"]
        bad = (
            verdict.status == "mismatch"
            and any(c.get("result") == "mismatch" for c in verdict.checks)
            and stale
        )
        record(f"rapid command {i}: no stale-read false alarm", not bad,
               f"status={verdict.status}, stale_skips={len(stale)}")

    print("\n  Now the same thing CONCURRENTLY -- three turns launched together.")
    utterances = ["fan speed 3", "fan speed 5", "turn the fan off"]
    async def one(u: str):
        reply, c, ms, issued = await say(s, u)
        return u, reply, await v.verify(reply, c, issued), ms
    out = await asyncio.gather(*(one(u) for u in utterances),
                               return_exceptions=True)
    false_alarm = False
    for item in out:
        if isinstance(item, Exception):
            print(f"  concurrent turn raised: {item!r}")
            continue
        u, reply, verdict, ms = item
        show(f"[concurrent] {u}", reply, verdict, ms)
        if verdict.status == "mismatch" and any(
            c.get("result") == "stale-read-skipped" for c in verdict.checks
        ):
            false_alarm = True
    record("concurrent turns produce no stale-read mismatch", not false_alarm)


# ---------------------------------------------------------------- case 6
async def case_honest_refusal(s, v) -> None:
    print("\n" + "=" * 74)
    print("CASE 6 -- AN HONEST 'NO' MUST SURVIVE UNTOUCHED")
    print("Rewriting a truthful refusal into a failure notice would be its own")
    print("kind of lying, and would make the correction meaningless.")

    reply, cid, ms, issued = await say(s, "what is the temperature in here?")
    verdict = await v.verify(reply, cid, issued)
    show("what is the temperature in here?", reply, verdict, ms)
    record("answer/refusal is spoken unchanged",
           not verdict.corrected and verdict.speech == reply,
           f"status={verdict.status}")


async def main() -> None:
    async with aiohttp.ClientSession() as s:
        v = ClosedLoopVerifier(BASE, TOKEN, s)
        print("HA websocket reachable:", await v.ping())
        snap = await snapshot(s)
        print("\npre-test snapshot:", json.dumps(snap, indent=2))
        try:
            await case_genuine(s, v)
            await case_no_tool_calls(s, v)
            await case_unreachable(s, v)
            await case_infrared(s, v)
            await case_rapid(s, v)
            await case_honest_refusal(s, v)
        finally:
            await restore(s, snap)
            await v.close()

    print("\n" + "=" * 74)
    print("RESULTS")
    for name, ok, note in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if note:
            print(f"        {note}")
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n  {passed}/{len(results)} passed")
    if verify_times:
        print(f"\n  VERIFICATION COST, measured over {len(verify_times)} turns:")
        print(f"    min {min(verify_times)} ms | max {max(verify_times)} ms | "
              f"mean {sum(verify_times) / len(verify_times):.1f} ms")
    sys.exit(0 if passed == len(results) else 1)


asyncio.run(main())
