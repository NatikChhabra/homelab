"""JARVIS tool-calling benchmark.

Runs a fixed prompt set against conversation.jarvis, three times each, and
records for every run:
  - the spoken reply text
  - end-to-end latency measured at the HA REST API
  - which tools the model actually called, with arguments (read from the
    proxy capture, which is ground truth from the wire)
  - which scripts actually fired (last_triggered deltas -- proof of execution,
    not just of intent)

Usage: python3 /tmp/bench.py <model-tag> <runs>
"""

import json
import os
import subprocess
import sys
import time
import urllib.request

HASS = "http://jarvis-hass:8123"
TOKEN = os.environ["JARVIS_HASS_TOKEN"]
CAPTURE = "/tmp/ollama-capture.jsonl"

PROMPTS = [
    ("1_ac22", "set the AC to 22"),
    ("2_fan_on", "turn on the fan"),
    ("3_boiling", "it's boiling in here"),
    ("4_hinglish", "yaar bahut garmi hai"),
    ("5_goodnight", "good night"),
    ("6_alloff", "turn everything off"),
    ("7_temp_q", "what's the temperature in here"),
]


def api(path, payload=None):
    req = urllib.request.Request(
        HASS + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
        },
        method="POST" if payload is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def states():
    out = {}
    for s in api("/api/states"):
        eid = s["entity_id"]
        if eid.startswith(("script.", "input_number.", "input_boolean.", "fan.", "light.")):
            out[eid] = {
                "state": s["state"],
                "last_triggered": s["attributes"].get("last_triggered"),
            }
    return out


def cap_size():
    try:
        return os.path.getsize(CAPTURE)
    except OSError:
        return 0


def cap_since(offset):
    recs = []
    if not os.path.exists(CAPTURE):
        return recs
    with open(CAPTURE) as fh:
        fh.seek(offset)
        for line in fh:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except Exception:
                    pass
    return recs


def service(domain, svc, payload):
    req = urllib.request.Request(
        HASS + "/api/services/%s/%s" % (domain, svc),
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + TOKEN,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def reset_room():
    """Put the room in a known, deliberately WRONG state.

    Every test then has an unambiguous signal: AC at 29 (too warm), fan off,
    lights on. If a model claims it cooled the room, the helpers prove it.
    """
    try:
        service("input_number", "set_value",
                {"entity_id": "input_number.ac_temperature", "value": 29})
        service("input_number", "set_value",
                {"entity_id": "input_number.fan_speed", "value": 0})
        service("input_boolean", "turn_off", {"entity_id": "input_boolean.ac_power"})
        service("input_boolean", "turn_off", {"entity_id": "input_boolean.fan_power"})
    except Exception as e:
        print("reset warning:", e, flush=True)
    time.sleep(0.4)


def run_one(key, text):
    reset_room()
    before = states()
    off = cap_size()
    t0 = time.time()
    err = None
    try:
        res = api(
            "/api/conversation/process",
            {"text": text, "language": "en", "agent_id": "conversation.jarvis"},
        )
    except Exception as e:
        res = {}
        err = repr(e)
    dt = time.time() - t0
    time.sleep(1.2)  # let scripts finish so last_triggered settles
    after = states()

    fired = []
    for eid, av in after.items():
        bv = before.get(eid, {})
        if eid.startswith("script.") and av.get("last_triggered") != bv.get("last_triggered"):
            fired.append(eid)

    changed = {}
    for eid, av in after.items():
        if eid.startswith("script."):
            continue
        bv = before.get(eid, {})
        if bv.get("state") != av.get("state"):
            changed[eid] = [bv.get("state"), av.get("state")]

    recs = cap_since(off)
    tool_calls = []
    reqs = []
    for r in recs:
        if r["kind"] == "response":
            for tc in r["payload"].get("tool_calls") or []:
                fn = tc.get("function", {})
                tool_calls.append({"name": fn.get("name"), "args": fn.get("arguments")})
        elif r["kind"] == "request":
            reqs.append(
                {
                    "n_tools": r["payload"]["n_tools"],
                    "tool_names": r["payload"]["tool_names"],
                    "options": r["payload"].get("options"),
                }
            )

    speech = ""
    try:
        speech = res["response"]["speech"]["plain"]["speech"]
    except Exception:
        pass

    return {
        "key": key,
        "prompt": text,
        "latency_s": round(dt, 2),
        "speech": speech,
        "response_type": res.get("response", {}).get("response_type"),
        "tool_calls": tool_calls,
        "scripts_fired": sorted(fired),
        "state_changes": changed,
        "n_llm_requests": len(reqs),
        "tools_offered": reqs[0]["n_tools"] if reqs else None,
        "tool_names_offered": reqs[0]["tool_names"] if reqs else None,
        "num_ctx_sent": (reqs[0]["options"] or {}).get("num_ctx") if reqs else None,
        "error": err,
    }


def main():
    model = sys.argv[1]
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    results = []

    # Warm the model so run 1 is not a cold-load outlier.
    try:
        api(
            "/api/conversation/process",
            {"text": "hello", "language": "en", "agent_id": "conversation.jarvis"},
        )
    except Exception as e:
        print("WARMUP FAILED:", e, flush=True)
    time.sleep(1)

    for r in range(runs):
        for key, text in PROMPTS:
            out = run_one(key, text)
            out["run"] = r + 1
            out["model"] = model
            results.append(out)
            print(
                json.dumps(
                    {
                        k: out[k]
                        for k in (
                            "run",
                            "key",
                            "latency_s",
                            "speech",
                            "tool_calls",
                            "scripts_fired",
                            "tools_offered",
                        )
                    }
                ),
                flush=True,
            )

    path = "/tmp/results-%s.json" % model.replace(":", "_").replace("/", "_")
    with open(path, "w") as fh:
        json.dump(results, fh, indent=2)
    print("WROTE", path, flush=True)


if __name__ == "__main__":
    main()
