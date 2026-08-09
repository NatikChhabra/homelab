"""Model selection benchmark for a ROUTED multi-agent JARVIS.

Adapted from _probe_round10.py (same Ollama-direct, read-only approach and the
same production-accurate tool schemas, lifted verbatim). Two changes matter:

  1. Tools are split into SPECIALIST subsets of 3-6 tools. That is the regime a
     routed design actually runs in, and it is the regime round10 never tested
     -- round10 always offered all 17 tools at once.
  2. ARGUMENT CORRECTNESS is checked, not just the tool name. round10 only ever
     compared tool names, so "set the AC to 22" scored a hit even if the model
     passed temperature="22" as a string, or 24 as the value. For a routed build
     the argument is the whole point, so it is graded here.

Talks straight to Ollama on localhost:11434. Home Assistant is never contacted
and nothing is written, so this cannot collide with the worker mutating exposed
entities on the live HA instance.

Usage: python bench_specialist.py <model-tag> [reps]
"""

import json
import statistics
import sys
import time
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
NUM_CTX = 8192  # production value for the incumbent
REPS = 3


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
LIVE_P = {"type": "object", "properties": {"name": {"type": "string"}, "domain": {"type": "string"},
                                           "area": {"type": "string"}}, "required": []}

T = {
    "HassTurnOn": tool("HassTurnOn", "Turns on/opens/presses a device or entity. For locks, this performs a 'lock' action.", TARGET),
    "HassTurnOff": tool("HassTurnOff", "Turns off/closes a device or entity. For locks, this performs an 'unlock' action.", TARGET),
    "HassLightSet": tool("HassLightSet", "Sets the brightness or color of a light", TARGET),
    "GetLiveContext": tool("GetLiveContext", "Provides real-time information about the CURRENT state, value, or mode of devices, sensors, entities, or areas.", LIVE_P),
    "ac_max_cool": tool("ac_max_cool", "Cool the room as fast as possible: AC at its lowest temperature with the fan high. Use for 'max cooling', 'cool it down fast', 'jaldi thanda karo'.", NONE_P),
    "ac_off": tool("ac_off", "Turn the air conditioner off. Use for 'turn off the AC', 'AC band karo'.", NONE_P),
    "ac_on": tool("ac_on", "Turn the air conditioner on at a comfortable temperature, without naming a number. Use for 'turn on the AC', 'AC chalu karo'.", NONE_P),
    "ac_set_temperature": tool("ac_set_temperature", "Set the air conditioner to an exact temperature in Celsius, 16 to 30, and turn it on. Use ONLY when a specific number is spoken: 'set the AC to 22', 'AC 24 kar do', 'make it 26 degrees', 'AC ko 20 par kar do'.",
                               {"type": "object", "properties": {"temperature": {"type": "integer", "description": "The exact temperature in Celsius that was spoken, 16 to 30."}}, "required": ["temperature"]}),
    "all_off": tool("all_off", "Shut the whole room down: every light, the fan and the AC together. Use ONLY for 'turn everything off', \"I'm leaving\", 'sab band kar do', not for the lights alone.", NONE_P),
    "fan_boost": tool("fan_boost", "Put the fan on boost or turbo, above its normal top speed. Use for 'boost the fan', 'fan turbo', 'fan full blast'.", NONE_P),
    "fan_set": tool("fan_set", "The ONLY way to control the ceiling fan. Level 0 turns the fan off; levels 1 to 5 turn it on at that speed. There is no other fan on/off tool. Use for 'turn the fan off', 'fan off', 'stop the fan', 'pankha band karo', 'fan on', 'fan speed 3', 'speed up the fan', 'pankha chalu karo', 'pankha tez karo'.",
                     {"type": "object", "properties": {"level": {"type": "integer", "description": "0 turns the fan off, 1 is slowest, 5 is fastest."}}, "required": ["level"]}),
    "good_night": tool("good_night", "Bedtime: lights off, comfortable sleeping temperature, quiet fan. Use for 'good night', \"I'm going to sleep\", 'sone ja raha hoon', 'so raha hoon', 'sona hai', 'bedtime'.", NONE_P),
    "movie_time": tool("movie_time", "Movie or gaming mode: TV on, most lights off, comfortable temperature. Use for 'movie time', \"let's watch something\", 'gaming', 'movie lagao', 'film dekhni hai'.", NONE_P),
    "tv_toggle": tool("tv_toggle", "Turn the TV or monitor on or off. The remote has ONE button so this always toggles. It is the only TV tool. Use for 'turn the TV on', 'turn the TV off', 'TV chalu karo', 'TV band karo', 'TV bandh kar do'.", NONE_P),
    "feeling_hot": tool("feeling_hot", "TOO HOT. Cools the room down: AC to 22 with the fan at speed 4. The word garmi means hot. Use for 'I'm hot', \"I'm feeling hot\", 'too warm', 'I'm sweating', \"it's boiling\", 'garmi lag rahi hai', 'mujhe garmi lag rahi hai', 'bahut garmi hai'.", NONE_P),
    "feeling_cold": tool("feeling_cold", "TOO COLD. Warms the room up: AC to 27 with the fan at speed 2. The word thand means cold. Use for 'I'm cold', 'too cold', \"it's freezing\", 'I'm shivering', 'thand lag rahi hai', 'mujhe thand lag rahi hai', 'bahut thand hai'.", NONE_P),
    "feeling_stuffy": tool("feeling_stuffy", "HUMID OR STUFFY. Switches the air conditioner to dry mode to pull moisture out. Use for \"it's stuffy in here\", 'the room feels humid', 'the room is humid', 'it feels muggy', \"it's damp\", \"it's sticky\", 'humid lag raha hai'.", NONE_P),
}

BASE_PROMPT = """You are JARVIS, Natik's home assistant for one bedroom in India.

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

FAN_ANY = {"fan_set", "fan_boost"}

SPECIALISTS = {
    "lights": {
        "tools": ["HassTurnOn", "HassTurnOff", "HassLightSet", "GetLiveContext"],
        "role": "You handle only the bedroom LIGHTS.",
        "cases": [
            ("turn on the lights", {"HassTurnOn"}, None),
            ("turn off the lights", {"HassTurnOff"}, None),
            ("lights on", {"HassTurnOn"}, None),
            ("light band kar do", {"HassTurnOff"}, None),
            ("batti bandh karo", {"HassTurnOff"}, None),
            ("roshni chalu karo", {"HassTurnOn"}, None),
            ("dim the lights", {"HassLightSet"}, None),
            ("turn on the bed light", {"HassTurnOn"}, None),
        ],
    },
    "climate": {
        "tools": ["ac_set_temperature", "ac_on", "ac_off", "ac_max_cool", "feeling_stuffy"],
        "role": "You handle only the AIR CONDITIONER.",
        "cases": [
            ("set the AC to 22", {"ac_set_temperature"}, {"temperature": 22}),
            ("AC 24 kar do", {"ac_set_temperature"}, {"temperature": 24}),
            ("make it 26 degrees", {"ac_set_temperature"}, {"temperature": 26}),
            ("AC ko 20 par kar do", {"ac_set_temperature"}, {"temperature": 20}),
            ("turn the AC off", {"ac_off"}, None),
            ("AC chalu karo", {"ac_on", "ac_set_temperature"}, None),
            ("cool it down fast", {"ac_max_cool"}, None),
            ("the room feels humid", {"feeling_stuffy"}, None),
        ],
    },
    "fan": {
        "tools": ["fan_set", "fan_boost", "GetLiveContext"],
        "role": "You handle only the CEILING FAN.",
        "cases": [
            ("fan speed 3", {"fan_set"}, {"level": 3}),
            ("set the fan to 5", {"fan_set"}, {"level": 5}),
            ("turn the fan off", {"fan_set"}, {"level": 0}),
            ("pankha band karo", {"fan_set"}, {"level": 0}),
            ("boost the fan", FAN_ANY, None),
            ("pankha tez karo", FAN_ANY, None),
        ],
    },
    "comfort": {
        "tools": ["feeling_hot", "feeling_cold", "feeling_stuffy", "ac_max_cool"],
        "role": "You handle only how the room FEELS.",
        "cases": [
            ("I'm feeling hot", {"feeling_hot", "ac_max_cool"}, None),
            ("garmi lag rahi hai", {"feeling_hot", "ac_max_cool"}, None),
            ("it's boiling in here", {"feeling_hot", "ac_max_cool"}, None),
            ("I'm cold", {"feeling_cold"}, None),
            ("mujhe thand lag rahi hai", {"feeling_cold"}, None),
            ("it's so stuffy in here", {"feeling_stuffy"}, None),
        ],
    },
    "media": {
        "tools": ["tv_toggle", "movie_time", "GetLiveContext"],
        "role": "You handle only the TV and movie mode.",
        "cases": [
            ("turn the TV on", {"tv_toggle"}, None),
            ("TV band karo", {"tv_toggle"}, None),
            ("I'm watching a movie", {"movie_time"}, None),
            ("movie lagao", {"movie_time"}, None),
            ("play some music", set(), None),
        ],
    },
    "scenes": {
        "tools": ["good_night", "all_off", "movie_time"],
        "role": "You handle only whole-room scenes.",
        "cases": [
            ("good night", {"good_night"}, None),
            ("I'm going to sleep", {"good_night"}, None),
            ("sone ja raha hoon", {"good_night"}, None),
            ("turn everything off", {"all_off"}, None),
            ("sab band kar do", {"all_off"}, None),
            ("open the curtains", set(), None),
        ],
    },
}


def chat(model, system, user, tools, num_ctx=NUM_CTX):
    body = {"model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False, "options": {"num_ctx": num_ctx, "temperature": 0}}
    if tools is not None:
        body["tools"] = tools
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r), (time.time() - t0) * 1000


def args_ok(got_args, want):
    """Value AND type must both be right. A string "22" is a FAIL, not a pass."""
    if want is None:
        return True, ""
    if not isinstance(got_args, dict):
        return False, "args not a dict"
    for k, v in want.items():
        if k not in got_args:
            return False, "missing %s" % k
        g = got_args[k]
        if isinstance(g, bool) or not isinstance(g, int):
            return False, "%s=%r type %s, want int" % (k, g, type(g).__name__)
        if g != v:
            return False, "%s=%s, want %s" % (k, g, v)
    return True, ""


def run_specialists(model, reps):
    print("\n%s\nSPECIALIST TOOL-CALLING -- %s  (num_ctx=%d, temp=0, reps=%d)\n%s"
          % ("=" * 74, model, NUM_CTX, reps, "=" * 74))
    all_lat = []
    totals = {"cases": 0, "name_ok": 0, "full_ok": 0, "leaks": 0, "silent": 0, "malformed": 0}
    per_spec = {}
    for spec, cfg in SPECIALISTS.items():
        tools = [T[n] for n in cfg["tools"]]
        system = BASE_PROMPT + "\n\n" + cfg["role"]
        print("\n-- specialist '%s' (%d tools: %s)" % (spec, len(tools), ", ".join(cfg["tools"])))
        s_name = 0
        s_full = 0
        s_cases = 0
        s_lat = []
        for utt, want_names, want_args in cfg["cases"]:
            n_hits = 0
            f_hits = 0
            notes = []
            for _ in range(reps):
                try:
                    res, ms = chat(model, system, utt, tools)
                except Exception as e:
                    notes.append("ERR %r" % (e,))
                    totals["malformed"] += 1
                    continue
                s_lat.append(ms)
                all_lat.append(ms)
                msg = res.get("message", {}) or {}
                calls = msg.get("tool_calls") or []
                content = (msg.get("content") or "").strip()
                got = set(c.get("function", {}).get("name") for c in calls)
                if not got and not content:
                    totals["silent"] += 1
                if "{" in content or any(n in content for n in T):
                    totals["leaks"] += 1
                    notes.append("LEAK:" + content[:40])
                if not want_names:
                    if not got:
                        n_hits += 1
                        f_hits += 1
                    else:
                        notes.append("called " + ",".join(sorted(x for x in got if x)))
                    continue
                if got & want_names:
                    n_hits += 1
                    ca = None
                    for c in calls:
                        if c.get("function", {}).get("name") in want_names:
                            ca = c["function"].get("arguments")
                            break
                    ok, why = args_ok(ca, want_args)
                    if ok:
                        f_hits += 1
                    else:
                        notes.append(why)
                else:
                    notes.append("got " + (",".join(sorted(x for x in got if x)) or "(none)"))
            s_cases += 1
            s_name += n_hits == reps
            s_full += f_hits == reps
            v = "OK  " if f_hits == reps else ("NAME" if n_hits == reps else "BAD ")
            extra = ("  " + "; ".join(dict.fromkeys(notes))[:80]) if notes else ""
            print("   %s %-30r name %d/%d full %d/%d%s" % (v, utt, n_hits, reps, f_hits, reps, extra))
            totals["cases"] += 1
            totals["name_ok"] += n_hits == reps
            totals["full_ok"] += f_hits == reps
        med = round(statistics.median(s_lat)) if s_lat else 0
        per_spec[spec] = {"cases": s_cases, "name_ok": s_name, "full_ok": s_full,
                          "n_tools": len(tools), "median_ms": med}
        print("   -> %s: name %d/%d, full %d/%d, median %d ms" % (spec, s_name, s_cases, s_full, s_cases, med))
    return per_spec, totals, all_lat


ROUTER_PROMPT = """You are a router for a home assistant. Classify the user's request into exactly ONE domain.

Domains:
lights  - the bedroom lights, brightness, dimming, batti, roshni
climate - the air conditioner, temperature numbers, cooling, AC
fan     - the ceiling fan, fan speed, pankha
comfort - how the room FEELS: hot, cold, garmi, thand, stuffy, humid
media   - the TV, movies, gaming
scenes  - whole-room routines: good night, sleep, turn everything off, leaving

Reply with ONE word: the domain name. No punctuation, no explanation."""

ROUTING_CASES = [
    ("turn on the lights", "lights"), ("batti bandh karo", "lights"), ("dim the lights", "lights"),
    ("set the AC to 22", "climate"), ("turn the AC off", "climate"), ("AC 24 kar do", "climate"),
    ("fan speed 3", "fan"), ("pankha band karo", "fan"), ("boost the fan", "fan"),
    ("I'm feeling hot", "comfort"), ("garmi lag rahi hai", "comfort"), ("it's so stuffy in here", "comfort"),
    ("turn the TV on", "media"), ("movie lagao", "media"), ("I'm watching a movie", "media"),
    ("good night", "scenes"), ("turn everything off", "scenes"), ("sone ja raha hoon", "scenes"),
]
DOMAINS = set(["lights", "climate", "fan", "comfort", "media", "scenes"])


def run_routing(model, reps):
    print("\n%s\nROUTING ACCURACY -- %s  (6 domains, text classify, temp=0, reps=%d)\n%s"
          % ("=" * 74, model, reps, "=" * 74))
    lat = []
    correct = 0
    total = 0
    offvocab = 0
    for utt, want in ROUTING_CASES:
        hits = 0
        seen = []
        for _ in range(reps):
            try:
                res, ms = chat(model, ROUTER_PROMPT, utt, None, num_ctx=2048)
            except Exception as e:
                seen.append("ERR%r" % (e,))
                total += 1
                continue
            lat.append(ms)
            raw = (res.get("message", {}) or {}).get("content", "") or ""
            parts = raw.strip().strip(".").strip().lower().split()
            got = parts[0] if parts else ""
            seen.append(got)
            total += 1
            if got == want:
                hits += 1
                correct += 1
            if got not in DOMAINS:
                offvocab += 1
        v = "OK  " if hits == reps else ("PART" if hits else "BAD ")
        note = "" if hits == reps else "  got " + ",".join(dict.fromkeys(seen))[:40]
        print("   %s %-30r -> want %-8s %d/%d%s" % (v, utt, want, hits, reps, note))
    med = round(statistics.median(lat)) if lat else 0
    p90 = round(sorted(lat)[int(len(lat) * 0.9)]) if lat else 0
    print("   -> routing accuracy %d/%d = %.1f%%, off-vocabulary %d, median %d ms, p90 %d ms"
          % (correct, total, 100.0 * correct / total, offvocab, med, p90))
    return {"correct": correct, "total": total, "offvocab": offvocab, "median_ms": med, "p90_ms": p90}


def main():
    model = sys.argv[1]
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else REPS
    try:
        chat(model, "You are a test.", "hello", None, num_ctx=2048)
    except Exception as e:
        print("WARMUP FAILED:", e)
    per_spec, totals, lat = run_specialists(model, reps)
    routing = run_routing(model, reps)
    s = sorted(lat)
    summary = {
        "model": model, "reps": reps, "num_ctx": NUM_CTX,
        "specialists": per_spec, "totals": totals, "routing": routing,
        "tool_latency_median_ms": round(statistics.median(s)) if s else 0,
        "tool_latency_p90_ms": round(s[int(len(s) * 0.9)]) if s else 0,
        "tool_latency_max_ms": round(max(s)) if s else 0,
    }
    print("\n%s\nSUMMARY %s" % ("#" * 74, model))
    print("  specialist cases fully correct (name+args): %d/%d" % (totals["full_ok"], totals["cases"]))
    print("  specialist cases name-correct only        : %d/%d" % (totals["name_ok"], totals["cases"]))
    print("  JSON/tool-name leaks into speech          : %d" % totals["leaks"])
    print("  silent (no call, no text)                 : %d" % totals["silent"])
    print("  malformed/transport errors                : %d" % totals["malformed"])
    print("  tool-call latency median/p90/max ms       : %d/%d/%d"
          % (summary["tool_latency_median_ms"], summary["tool_latency_p90_ms"], summary["tool_latency_max_ms"]))
    print("  routing accuracy                          : %d/%d median %d ms"
          % (routing["correct"], routing["total"], routing["median_ms"]))
    out = "C:/ServerData/Stacks/jarvis/hermes/results-%s.json" % model.replace(":", "_").replace("/", "_")
    with open(out, "w") as fh:
        json.dump(summary, fh, indent=2)
    print("WROTE", out)


if __name__ == "__main__":
    main()
