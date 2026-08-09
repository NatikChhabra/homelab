"""Measure the two conversation paths against each other, same utterances.

Path A: conversation.home_assistant -- Home Assistant's built-in intent parser.
        Pure local string/template matching. The LLM is never invoked.
Path B: conversation.jarvis -- the Ollama agent (qwen2.5:3b).

For each utterance this records latency and, critically, whether the path
actually SUCCEEDED. A local intent match that returns an error response is not a
win, it is a failure that happens to be fast, so responses are classified:

  action_done / query_answer -> handled
  error                      -> not handled (the parser did not understand)

Only utterances the local parser genuinely handles can be routed away from the
LLM, so the headline number is latency on the handled subset.

Usage: python3 intent_path.py [--reps N] [--out FILE]
"""

import argparse
import json
import statistics
import os
import time
import urllib.request

HASS = os.environ.get("HASS_URL", "http://localhost:8123")
# Windows path: this is run by the host Python, not from inside a container.
TOKEN_FILE = os.environ.get(
    "HASS_TOKEN_FILE", r"C:\ServerData\Stacks\jarvis\.bench-token.txt")

with open(TOKEN_FILE) as fh:
    TOKEN = fh.read().strip()

# The utterance set spans what the brief and the routing worker care about:
# plain device commands the built-in parser should cover, the scripted actions
# that probably need the LLM, and the Hindi/Hinglish and comfort phrasings that
# are the reason the LLM is in the loop at all.
UTTERANCES = [
    # --- plain device commands: the candidates for local-first routing
    ("device", "turn on the lights"),
    ("device", "turn off the lights"),
    ("device", "lights on"),
    ("device", "lights off"),
    ("device", "turn on the bed light"),
    ("device", "turn off the night light"),
    ("device", "turn on the ceiling light"),
    ("device", "set the fan to speed 3"),
    ("device", "turn off the fan"),
    ("device", "turn on the fan"),
    # --- scripted actions: expected to need the LLM
    ("script", "set the AC to 22"),
    ("script", "turn the AC off"),
    ("script", "good night"),
    ("script", "turn everything off"),
    ("script", "boost the fan"),
    ("script", "movie time"),
    # --- comfort reasoning: definitely needs the LLM
    ("comfort", "it's boiling in here"),
    ("comfort", "I'm cold"),
    ("comfort", "it's so stuffy in here"),
    # --- Hindi / Hinglish
    ("hindi", "batti bandh karo"),
    ("hindi", "roshni chalu karo"),
    ("hindi", "pankha band karo"),
    ("hindi", "garmi lag rahi hai"),
    ("hindi", "sab band kar do"),
]


def process(text, agent):
    body = {"text": text, "language": "en", "agent_id": agent}
    req = urllib.request.Request(
        HASS + "/api/conversation/process",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"},
        method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as r:
        res = json.load(r)
    ms = (time.perf_counter() - t0) * 1000
    resp = res.get("response", {})
    rtype = resp.get("response_type")
    try:
        speech = resp["speech"]["plain"]["speech"]
    except Exception:
        speech = ""
    targets = []
    try:
        for t in resp.get("data", {}).get("success", []) or []:
            targets.append(t.get("id") or t.get("name"))
    except Exception:
        pass
    return {
        "ms": ms,
        "response_type": rtype,
        "handled": rtype in ("action_done", "query_answer"),
        "speech": speech,
        "targets": targets,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    # Warm both agents so neither pays a first-call cost in the samples.
    for agent in ("conversation.home_assistant", "conversation.jarvis"):
        try:
            process("turn on the lights", agent)
        except Exception as e:
            print("warmup", agent, "failed:", e, flush=True)
    time.sleep(1)

    rows = []
    for cat, utt in UTTERANCES:
        rec = {"cat": cat, "utt": utt}
        for agent, key in (("conversation.home_assistant", "local"),
                           ("conversation.jarvis", "llm")):
            lats, handled, speech, rtype = [], 0, "", ""
            for _ in range(a.reps):
                try:
                    r = process(utt, agent)
                except Exception as e:
                    print("ERR", utt, agent, repr(e), flush=True)
                    continue
                lats.append(r["ms"])
                handled += 1 if r["handled"] else 0
                speech = r["speech"]
                rtype = r["response_type"]
                time.sleep(0.25)
            rec[key] = {
                "median_ms": round(statistics.median(lats), 1) if lats else None,
                "min_ms": round(min(lats), 1) if lats else None,
                "handled": handled,
                "reps": len(lats),
                "response_type": rtype,
                "speech": speech[:70],
            }
        rows.append(rec)
        lo, hi = rec["local"], rec["llm"]
        speed = ""
        if lo["median_ms"] and hi["median_ms"] and lo["handled"] == a.reps:
            speed = "  %.1fx faster" % (hi["median_ms"] / lo["median_ms"])
        print("  %-9s %-28s local %6s ms %s | llm %6s ms %s%s" % (
            cat, utt,
            lo["median_ms"], "OK " if lo["handled"] == a.reps else "MISS",
            hi["median_ms"], "OK " if hi["handled"] == a.reps else "MISS",
            speed), flush=True)

    local_ok = [r for r in rows if r["local"]["handled"] == r["local"]["reps"]]
    local_no = [r for r in rows if r["local"]["handled"] < r["local"]["reps"]]

    def med(rs, k):
        v = [r[k]["median_ms"] for r in rs if r[k]["median_ms"]]
        return round(statistics.median(v), 1) if v else None

    print("\n===== SUMMARY =====")
    print("  utterances the LOCAL parser handles: %d / %d" % (len(local_ok), len(rows)))
    print("    by category: %s" % json.dumps(
        {c: sum(1 for r in local_ok if r["cat"] == c) for c in
         sorted({r["cat"] for r in rows})}))
    print("  on that handled subset:")
    print("    local median %s ms" % med(local_ok, "local"))
    print("    llm   median %s ms" % med(local_ok, "llm"))
    if med(local_ok, "local") and med(local_ok, "llm"):
        print("    speedup %.1fx, saving %.0f ms per command" % (
            med(local_ok, "llm") / med(local_ok, "local"),
            med(local_ok, "llm") - med(local_ok, "local")))
    print("  local parser MISSES (must stay on the LLM): %s"
          % ", ".join(r["utt"] for r in local_no))

    if a.out:
        with open(a.out, "w") as fh:
            json.dump(rows, fh, indent=2)
        print("  wrote", a.out)


if __name__ == "__main__":
    main()
