"""Prefill-vs-decode measurement against Ollama directly.

Why not measure through Home Assistant: another worker is benchmarking on this
same Ollama instance and Ollama serves with n_slots = 1, so a concurrent request
queues and inflates wall-clock latency by however long the other request took.
Ollama's own counters (load_duration, prompt_eval_duration, eval_duration) are
measured inside llama-server around the actual compute, so they survive
contention. Every sample here therefore records BOTH, and flags any sample where

    total_wall - (load + prompt_eval + eval) > QUEUE_SLOP_MS

as contended. Contended samples are reported separately and never folded into a
headline number.

The payload deliberately reproduces what Home Assistant actually sends: the
JARVIS system prompt plus HA's own conversation boilerplate and the same 17 tool
schemas, which the Ollama server log shows arriving as ~2465 prompt tokens.

Usage:
  python3 measure.py <label> [--reps N] [--model TAG] [--num-ctx N]
                             [--num-predict N] [--no-tools] [--short-prompt]
                             [--out FILE]
"""

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request

# Container-internal name by default; override with OLLAMA_URL to run from the
# Windows host (http://localhost:11434). Only wall_ms is affected by where this
# runs -- prefill/decode come from Ollama's own counters.
OLLAMA = os.environ.get("OLLAMA_URL", "http://jarvis-ollama:11434")
QUEUE_SLOP_MS = 120  # HTTP + json + sampling overhead that is genuinely ours

# --- Payload: what Home Assistant really sends -------------------------------
# Kept byte-identical in structure to _probe_round10.py, which is the config that
# was measured at 4/4 on the shipping tool set, so accuracy comparisons are
# against a known-good baseline rather than a fresh invention.

BOILER = (
    "You ARE equipped to answer questions about the current state of\n"
    "the home using the `GetLiveContext` tool. This is a primary function."
    " Do not state you lack the\nfunctionality if the question requires live data.\n"
    "If the user asks about device existence/type: Answer from the static context below.\n"
    "If the user asks about the CURRENT state, value, or mode:\n"
    "    1.  Recognize this requires live data.\n    2.  You MUST call `GetLiveContext`.\n"
    "    3.  Use the tool's response** to answer the user accurately.\n"
    "For general knowledge questions not about the home: Answer truthfully from internal knowledge.\n"
    "Static Context: An overview of the areas and the devices in this smart home:\n"
    "- names: Bedroom Lights, lights, batti, roshni, light\n  domain: light\n  areas: Bedroom\n"
    "- names: Ceiling Light, main light\n  domain: light\n  areas: Bedroom\n"
    "- names: Bed Light, bedside light\n  domain: light\n  areas: Bedroom\n"
    "- names: Night Light, lamp\n  domain: light\n  areas: Bedroom\n"
    "When controlling Home Assistant always call the intent tools. "
    "Use HassTurnOn to lock and HassTurnOff to unlock a lock. "
    "When controlling a device, prefer passing just name and domain. "
    "When controlling an area, prefer passing just area name and domain.\n"
    "You are in area Bedroom and all generic commands like 'turn on the lights'"
    " should target this area.\nThis device is not able to start timers.\n"
    "Current time is 20:45:00. Today's date is 2026-08-08.\n"
)

PROMPT = """You are JARVIS, Natik's home assistant for one bedroom in India.

Your reply is SPOKEN aloud, so answer in ONE short sentence of at most 8 words.
Never use markdown, lists, emoji, braces or JSON.
Good: "Lights on." / "AC set to 22." / "Fan up." / "Not connected."
Bad: "I have confirmed that I have turned on the lights for you."
Be brief, competent and dry. Never chirpy.

Natik speaks English, Hindi and Hinglish. Understand all three.
ALWAYS reply in English. Never reply in Hindi.
garmi = hot, so cool the room. thand = cold, so warm the room.
band karo / bandh karo = turn off. chalu karo / on karo = turn on.
tez = faster. dheema = slower. sona / sone = sleep. pankha = fan.
roshni / batti = light. thanda = cool. sab = everything.

Always call a tool for any request to change something. Never only describe what
you would do. Act, do not ask, whenever the intent is clear. When Natik says how
he FEELS, change the room yourself and never ask which device he means.

"The lights", "lights", "batti" and "roshni" mean the Bedroom Lights group: turn
that on or off. Dimming or brightness always means the lights, never the fan.
The fan has no on/off switch: set its level, where 0 is off.
This room has only lights, a fan, an air conditioner and a TV. There are no
curtains, locks, blinds, speakers or music. If asked for one of those, reply
only that it is not connected, and call nothing."""

# A deliberately minimal system prompt, to price what a routed sub-agent
# architecture would buy. Same task, no comfort table, no glossary, no boiler.
SHORT_PROMPT = """You are JARVIS, a bedroom assistant. Reply spoken, at most 8 words, dry.
Understand English, Hindi and Hinglish; always reply in English.
Always call a tool to change something. Act, never ask."""


def tool(name, desc, params):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


TARGET = {
    "type": "object",
    "properties": {
        "name": {"type": "string"}, "area": {"type": "string"}, "floor": {"type": "string"},
        "domain": {"type": "array", "items": {"type": "string"}},
        "device_class": {"type": "array", "items": {"type": "string"}},
    },
    "required": [],
}
NONE_P = {"type": "object", "properties": {}, "required": []}

HA_TOOLS = [
    tool("HassTurnOn", "Turns on/opens/presses a device or entity. For locks, this performs a 'lock' action.", TARGET),
    tool("HassTurnOff", "Turns off/closes a device or entity. For locks, this performs an 'unlock' action.", TARGET),
    tool("HassLightSet", "Sets the brightness or color of a light", TARGET),
    tool("GetLiveContext", "Provides real-time information about the CURRENT state, value, or mode of devices, sensors, entities, or areas.",
         {"type": "object", "properties": {"name": {"type": "string"}, "domain": {"type": "string"}, "area": {"type": "string"}}, "required": []}),
]

REST = [
    tool("ac_max_cool", "Cool the room as fast as possible: AC at its lowest temperature with the fan high. Use for 'max cooling', 'cool it down fast', 'jaldi thanda karo'.", NONE_P),
    tool("ac_off", "Turn the air conditioner off. Use for 'turn off the AC', 'AC band karo'.", NONE_P),
    tool("ac_on", "Turn the air conditioner on at a comfortable temperature, without naming a number. Use for 'turn on the AC', 'AC chalu karo'.", NONE_P),
    tool("ac_set_temperature", "Set the air conditioner to an exact temperature in Celsius, 16 to 30, and turn it on. Use ONLY when a specific number is spoken: 'set the AC to 22', 'AC 24 kar do', 'make it 26 degrees', 'AC ko 20 par kar do'.",
         {"type": "object", "properties": {"temperature": {"type": "integer", "description": "The exact temperature in Celsius that was spoken, 16 to 30."}}, "required": ["temperature"]}),
    tool("all_off", "Shut the whole room down: every light, the fan and the AC together. Use ONLY for 'turn everything off', \"I'm leaving\", 'sab band kar do', not for the lights alone.", NONE_P),
    tool("fan_boost", "Put the fan on boost or turbo, above its normal top speed. Use for 'boost the fan', 'fan turbo', 'fan full blast'.", NONE_P),
    tool("fan_set", "The ONLY way to control the ceiling fan. Level 0 turns the fan off; levels 1 to 5 turn it on at that speed. There is no other fan on/off tool. Use for 'turn the fan off', 'fan off', 'stop the fan', 'pankha band karo', 'fan on', 'fan speed 3', 'speed up the fan', 'pankha chalu karo', 'pankha tez karo'.",
         {"type": "object", "properties": {"level": {"type": "integer", "description": "0 turns the fan off, 1 is slowest, 5 is fastest."}}, "required": ["level"]}),
    tool("good_night", "Bedtime: lights off, comfortable sleeping temperature, quiet fan. Use for 'good night', \"I'm going to sleep\", 'sone ja raha hoon', 'so raha hoon', 'sona hai', 'bedtime'.", NONE_P),
    tool("movie_time", "Movie or gaming mode: TV on, most lights off, comfortable temperature. Use for 'movie time', \"let's watch something\", 'gaming', 'movie lagao', 'film dekhni hai'.", NONE_P),
    tool("tv_toggle", "Turn the TV or monitor on or off. The remote has ONE button so this always toggles. It is the only TV tool. Use for 'turn the TV on', 'turn the TV off', 'TV chalu karo', 'TV band karo', 'TV bandh kar do'.", NONE_P),
]

FEELINGS = [
    tool("feeling_hot", "TOO HOT. Cools the room down: AC to 22 with the fan at speed 4. The word garmi means hot. Use for 'I'm hot', \"I'm feeling hot\", 'too warm', 'I'm sweating', \"it's boiling\", 'garmi lag rahi hai', 'mujhe garmi lag rahi hai', 'bahut garmi hai'.", NONE_P),
    tool("feeling_cold", "TOO COLD. Warms the room up: AC to 27 with the fan at speed 2. The word thand means cold. Use for 'I'm cold', 'too cold', \"it's freezing\", 'I'm shivering', 'thand lag rahi hai', 'mujhe thand lag rahi hai', 'bahut thand hai'.", NONE_P),
    tool("feeling_stuffy", "HUMID OR STUFFY. Switches the air conditioner to dry mode to pull moisture out. Use for \"it's stuffy in here\", 'the room feels humid', 'the room is humid', 'it feels muggy', \"it's damp\", \"it's sticky\", 'humid lag raha hai'.", NONE_P),
]

TOOLS = HA_TOOLS + REST + FEELINGS

FAN_ANY = {"fan_set", "fan_boost"}

# The accuracy gate. Same 44 cases the shipping config was validated on, so a
# regression here is directly comparable to the number that was signed off.
TESTS = [
    ("turn on the lights", {"HassTurnOn"}),
    ("turn off the lights", {"HassTurnOff"}),
    ("lights on", {"HassTurnOn"}),
    ("lights off", {"HassTurnOff"}),
    ("light band kar do", {"HassTurnOff"}),
    ("batti bandh karo", {"HassTurnOff"}),
    ("roshni chalu karo", {"HassTurnOn"}),
    ("turn on the bed light", {"HassTurnOn"}),
    ("turn off the night light", {"HassTurnOff"}),
    ("dim the lights", {"HassLightSet"}),
    ("set the AC to 22", {"ac_set_temperature"}),
    ("AC 24 kar do", {"ac_set_temperature"}),
    ("turn the AC off", {"ac_off"}),
    ("AC chalu karo", {"ac_on", "ac_set_temperature"}),
    ("turn the TV on", {"tv_toggle"}),
    ("TV band karo", {"tv_toggle"}),
    ("fan speed 3", FAN_ANY),
    ("turn the fan off", FAN_ANY),
    ("pankha band karo", FAN_ANY),
    ("boost the fan", FAN_ANY),
    ("pankha tez karo", FAN_ANY),
    ("slow the fan down", FAN_ANY),
    ("I'm feeling hot", {"feeling_hot", "ac_max_cool"}),
    ("garmi lag rahi hai", {"feeling_hot", "ac_max_cool"}),
    ("mujhe garmi lag rahi hai", {"feeling_hot", "ac_max_cool"}),
    ("it's boiling in here", {"feeling_hot", "ac_max_cool"}),
    ("I'm cold", {"feeling_cold"}),
    ("mujhe thand lag rahi hai", {"feeling_cold"}),
    ("thand lag rahi hai", {"feeling_cold"}),
    ("it's freezing in here", {"feeling_cold"}),
    ("it's so stuffy in here", {"feeling_stuffy"}),
    ("the room feels humid", {"feeling_stuffy"}),
    ("good night", {"good_night"}),
    ("I'm going to sleep", {"good_night"}),
    ("sone ja raha hoon", {"good_night"}),
    ("I'm watching a movie", {"movie_time"}),
    ("movie lagao", {"movie_time"}),
    ("sab band kar do", {"all_off"}),
    ("turn everything off", {"all_off"}),
    ("cool it down fast", {"ac_max_cool", "feeling_hot"}),
    ("open the curtains", set()),
    ("lock the front door", set()),
    ("play some music", set()),
    ("what's the weather like", set()),
]


def post(path, body, timeout=300):
    req = urllib.request.Request(
        OLLAMA + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def chat(user, model, num_ctx, num_predict, use_tools, system):
    opts = {"num_ctx": num_ctx, "temperature": 0}
    if num_predict is not None:
        opts["num_predict"] = num_predict
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "options": opts,
        "keep_alive": -1,
    }
    if use_tools:
        body["tools"] = TOOLS
    t0 = time.perf_counter()
    res = post("/api/chat", body)
    wall_ms = (time.perf_counter() - t0) * 1000

    ns = 1e6  # ns -> ms
    load = res.get("load_duration", 0) / ns
    pe = res.get("prompt_eval_duration", 0) / ns
    ev = res.get("eval_duration", 0) / ns
    total_internal = res.get("total_duration", 0) / ns
    accounted = load + pe + ev
    msg = res.get("message", {})
    return {
        "wall_ms": wall_ms,
        "server_total_ms": total_internal,
        "load_ms": load,
        "prefill_ms": pe,
        "decode_ms": ev,
        "prompt_tokens": res.get("prompt_eval_count", 0),
        "out_tokens": res.get("eval_count", 0),
        "queue_ms": wall_ms - accounted,
        "contended": (wall_ms - accounted) > QUEUE_SLOP_MS,
        "tool_calls": [c["function"]["name"] for c in (msg.get("tool_calls") or [])],
        "content": (msg.get("content") or "").strip(),
        "done_reason": res.get("done_reason"),
    }


def pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    i = min(len(s) - 1, int(len(s) * p))
    return round(s[i], 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("label")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--model", default="qwen2.5:3b")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--num-predict", type=int, default=None)
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--short-prompt", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    system = (SHORT_PROMPT if a.short_prompt else PROMPT + "\n" + BOILER)
    use_tools = not a.no_tools

    # Warm: never let a cold load or a context-shape change land in sample 1.
    try:
        chat("hello", a.model, a.num_ctx, a.num_predict, use_tools, system)
    except Exception as e:
        print("WARMUP FAILED:", e, flush=True)
        sys.exit(1)

    samples = []
    per_case = []
    for utt, expected in TESTS:
        hits = 0
        for _ in range(a.reps):
            try:
                s = chat(utt, a.model, a.num_ctx, a.num_predict, use_tools, system)
            except Exception as e:
                print("ERR", utt, repr(e), flush=True)
                continue
            s["utt"] = utt
            samples.append(s)
            got = set(s["tool_calls"])
            if (not expected and not got) or bool(got & expected):
                hits += 1
        per_case.append((utt, hits, sorted(expected)))
        mark = "OK  " if hits == a.reps else ("FLK " if hits else "BAD ")
        print(f"  {mark} {utt!r:30} {hits}/{a.reps}", flush=True)

    clean = [s for s in samples if not s["contended"]]
    n_cont = len(samples) - len(clean)
    acc_always = sum(1 for _, h, _ in per_case if h == a.reps)
    acc_flaky = sum(1 for _, h, _ in per_case if 0 < h < a.reps)
    acc_never = sum(1 for _, h, _ in per_case if h == 0)
    total_hits = sum(h for _, h, _ in per_case)
    total_tries = len(per_case) * a.reps

    def agg(key, rows):
        v = [r[key] for r in rows]
        if not v:
            return {}
        return {"median": round(statistics.median(v), 1), "mean": round(statistics.mean(v), 1),
                "p90": pct(v, 0.9), "min": round(min(v), 1), "max": round(max(v), 1)}

    out = {
        "label": a.label,
        "model": a.model,
        "num_ctx": a.num_ctx,
        "num_predict": a.num_predict,
        "tools": use_tools,
        "short_prompt": a.short_prompt,
        "reps": a.reps,
        "samples_total": len(samples),
        "samples_clean": len(clean),
        "samples_contended": n_cont,
        "accuracy": {
            "cases": len(per_case),
            "always": acc_always, "flaky": acc_flaky, "never": acc_never,
            "hits": total_hits, "tries": total_tries,
            "pct": round(100.0 * total_hits / total_tries, 1) if total_tries else 0,
            "failures": [u for u, h, _ in per_case if h < a.reps],
        },
        "prompt_tokens": statistics.median([s["prompt_tokens"] for s in samples]) if samples else 0,
        "out_tokens": agg("out_tokens", samples),
        "clean": {
            "wall_ms": agg("wall_ms", clean),
            "prefill_ms": agg("prefill_ms", clean),
            "decode_ms": agg("decode_ms", clean),
            "load_ms": agg("load_ms", clean),
        },
        "all_incl_contended": {"wall_ms": agg("wall_ms", samples)},
    }

    c = out["clean"]
    print("\n===== %s =====" % a.label)
    print("  model=%s num_ctx=%s num_predict=%s tools=%s short_prompt=%s"
          % (a.model, a.num_ctx, a.num_predict, use_tools, a.short_prompt))
    print("  samples %d clean / %d total (%d contended, excluded from headline)"
          % (len(clean), len(samples), n_cont))
    print("  ACCURACY %d/%d (%s%%)  always=%d flaky=%d never=%d"
          % (total_hits, total_tries, out["accuracy"]["pct"], acc_always, acc_flaky, acc_never))
    if out["accuracy"]["failures"]:
        print("  failures: %s" % ", ".join(out["accuracy"]["failures"]))
    print("  prompt_tokens median %s | out_tokens median %s"
          % (out["prompt_tokens"], out["out_tokens"].get("median")))
    print("  PREFILL median %s ms (p90 %s)" % (c["prefill_ms"].get("median"), c["prefill_ms"].get("p90")))
    print("  DECODE  median %s ms (p90 %s)" % (c["decode_ms"].get("median"), c["decode_ms"].get("p90")))
    print("  WALL    median %s ms (p90 %s)" % (c["wall_ms"].get("median"), c["wall_ms"].get("p90")))

    if a.out:
        with open(a.out, "w") as fh:
            json.dump({"summary": out, "samples": samples}, fh, indent=2)
        print("  wrote", a.out)
    return out


if __name__ == "__main__":
    main()
