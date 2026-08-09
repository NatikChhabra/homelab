"""Probe HA's built-in intent parser directly.

Written 2026-08-09 because HANDOFF.md claimed "18 of 25 commands are handled
locally" while perf/results-intent-path.json -- the file that measurement came
from -- shows only 4 of 24 handled. The two numbers can only be reconciled if
the 18 was measured against EXACT friendly names and the 4 against the natural
phrasings a person actually says. This script settles which.
"""
import json
import time
import urllib.request

TOKEN = open(r"C:\ServerData\Stacks\jarvis\.bench-token.txt").read().strip()
HASS = "http://localhost:8123"


def conv(text, agent="conversation.home_assistant"):
    payload = {"text": text, "language": "en", "agent_id": agent}
    req = urllib.request.Request(
        HASS + "/api/conversation/process",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    ms = (time.perf_counter() - t) * 1000
    resp = d.get("response", {})
    speech = (resp.get("speech", {}).get("plain", {}).get("speech") or "")[:70]
    return ms, resp.get("response_type"), speech


# Natural phrasing vs the exact friendly name, same underlying device.
TESTS = [
    ("natural", "turn on the lights"),
    ("exact", "turn on Node Switch Gang 1"),
    ("natural", "turn on gang 1"),
    ("natural", "turn on the ceiling light"),
    ("natural", "turn off the fan"),
    ("natural", "turn on the fan"),
    ("natural", "set the fan to speed 3"),
    ("exact", "good night"),
    ("exact", "turn everything off"),
    ("exact", "toggle the tv"),
    ("exact", "maximum cooling"),
    ("exact", "AC dry mode"),
    ("exact", "turn on the AC"),
    ("exact", "turn off the AC"),
    ("exact", "fan boost"),
    ("exact", "I am feeling hot"),
    ("exact", "I am feeling cold"),
    ("exact", "movie time"),
    ("natural", "set the AC to 22"),
]

if __name__ == "__main__":
    handled = 0
    for kind, text in TESTS:
        ms, rt, sp = conv(text)
        ok = rt in ("action_done", "query_answer")
        handled += ok
        print(f"{'OK ' if ok else 'NO '} {kind:8} {ms:7.1f}ms {str(rt):12} | {text:30} | {sp}")
    print(f"\nhandled {handled}/{len(TESTS)}")
