"""Read-only benchmark round 10: the shipping candidate.

Round 9 regressed badly: the comfort intents fell from 3/3 to 0/3 after edits
that were close to semantically neutral (adding "AIR CONDITIONER ONLY, not the
lights or the fan" to the AC tools, and reordering the tool list alphabetically
so the feeling_* tools moved from last to the middle). That swing is itself a
finding and is reported as one.

This round reverts to the round-8 config V wording, which measured best, and
changes exactly two things:

  1. good_night gains 'sone ja raha hoon' and 'so raha hoon'.
  2. The light group carries ALIASES in the static context. Home Assistant joins
     entity aliases into the `names` field it hands the model, so an alias is the
     supported way to teach it the word "batti". Both V and round 9 failed
     'batti bandh karo' 0/3 with no alias present, so this tests the fix.

Run twice to measure run-to-run variance, because rounds 8 and 9 differed more
than their content changes justified.

Nothing is written; Home Assistant is not reconfigured.
"""

import json
import time
import urllib.request
from collections import Counter

OLLAMA = "http://jarvis-ollama:11434/api/chat"
MODEL = "qwen2.5:3b"
REPS = 3

# Aliases appear inside `names`, exactly as async_get_entity_aliases builds it.
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

# Round 8 config V wording, verbatim, except good_night.
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

# Kept LAST in the list, as in config V.
FEELINGS = [
    tool("feeling_hot", "TOO HOT. Cools the room down: AC to 22 with the fan at speed 4. The word garmi means hot. Use for 'I'm hot', \"I'm feeling hot\", 'too warm', 'I'm sweating', \"it's boiling\", 'garmi lag rahi hai', 'mujhe garmi lag rahi hai', 'bahut garmi hai'.", NONE_P),
    tool("feeling_cold", "TOO COLD. Warms the room up: AC to 27 with the fan at speed 2. The word thand means cold. Use for 'I'm cold', 'too cold', \"it's freezing\", 'I'm shivering', 'thand lag rahi hai', 'mujhe thand lag rahi hai', 'bahut thand hai'.", NONE_P),
    tool("feeling_stuffy", "HUMID OR STUFFY. Switches the air conditioner to dry mode to pull moisture out. Use for \"it's stuffy in here\", 'the room feels humid', 'the room is humid', 'it feels muggy', \"it's damp\", \"it's sticky\", 'humid lag raha hai'.", NONE_P),
]

TOOLS = HA_TOOLS + REST + FEELINGS

FAN_ANY = {"fan_set", "fan_boost"}
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


def run(system, user):
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": TOOLS, "stream": False, "options": {"num_ctx": 4096, "temperature": 0}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r), (time.time() - t0) * 1000


def pass_once(label):
    system = PROMPT + "\n" + BOILER
    print(f"\n===== {label} ({len(TOOLS)} tools, {REPS} reps) =====")
    always = flaky = never = leaks = silence = 0
    lat = []
    ptoks = None
    rows = []
    for utt, expected in TESTS:
        hits = 0
        seen = Counter()
        lk = sil = 0
        for _ in range(REPS):
            res, ms = run(system, utt)
            msg = res.get("message", {})
            calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()
            ptoks = res.get("prompt_eval_count")
            lat.append(ms)
            got = {c["function"]["name"] for c in calls}
            for g in got:
                seen[g] += 1
            if not got:
                if content:
                    seen["(spoke only)"] += 1
                else:
                    seen["(SILENCE)"] += 1
                    sil += 1
            if (not expected and not got) or bool(got & expected):
                hits += 1
            if ("{" in content) or any(t["function"]["name"] in content for t in TOOLS):
                lk += 1
        leaks += lk
        silence += sil
        always += hits == REPS
        flaky += 0 < hits < REPS
        never += hits == 0
        verdict = "OK   " if hits == REPS else ("FLAKY" if hits else "BAD  ")
        rows.append((verdict, utt, hits, dict(seen), lk))
    for verdict, utt, hits, seen, lk in rows:
        print(f"  {verdict} {utt!r:28} {hits}/{REPS} {seen}" + (f" LEAK x{lk}" if lk else ""))
    s = sorted(lat)
    print(f"  ---- always {always}/{len(TESTS)}, flaky {flaky}, never {never}"
          f" | {leaks} leaked, {silence} silent of {len(TESTS)*REPS}"
          f" | {ptoks} tokens | median {round(s[len(s)//2])} ms, p90 {round(s[int(len(s)*0.9)])} ms")
    return always, flaky, never, leaks, silence


if __name__ == "__main__":
    a = pass_once("X. SHIPPING CANDIDATE, pass 1")
    b = pass_once("X. SHIPPING CANDIDATE, pass 2 (variance check)")
    print("\n########## SUMMARY ##########")
    print(f"  pass 1: always={a[0]}/{len(TESTS)} flaky={a[1]} never={a[2]} leaks={a[3]} silent={a[4]}")
    print(f"  pass 2: always={b[0]}/{len(TESTS)} flaky={b[1]} never={b[2]} leaks={b[3]} silent={b[4]}")
