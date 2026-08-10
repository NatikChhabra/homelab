"""Read-only benchmark round 8: entity naming, light groups, description openers.

Round 7 failures clustered almost entirely on light commands. "turn off the
lights" and "lights on" produced SILENCE, while the single-entity Hindi phrase
"batti bandh karo" worked 3/3. Three light entities exist and none is called
"lights", so the plural has no single target and the model stalls instead of
falling back to area targeting.

This round tests three things that all feed the design directly:

  T. The live registry naming (Node Switch Gang 1/2/3) -- what is deployed today.
  U. Friendly names (Ceiling Light / Bed Light / Night Light) + a light group
     entity named "Bedroom Lights", so the plural has exactly one target.
  V. U, plus sharply distinct opening words on the competing comfort tools.

V exists because round 7 regressed "mujhe thand lag rahi hai" from 3/3 to 0/3
against round 6. The only change was rewriting both feeling tools to open with
the same words, "Natik feels too warm/cold and names no temperature". Making two
competing descriptions start alike appears to destroy discrimination, so V puts
the distinguishing word first instead.

Nothing is written; Home Assistant is not reconfigured.
"""

import json
import time
import urllib.request
from collections import Counter

OLLAMA = "http://jarvis-ollama:11434/api/chat"
MODEL = "qwen2.5:3b"
REPS = 3

BOILER_HEAD = (
    "You ARE equipped to answer questions about the current state of\n"
    "the home using the `GetLiveContext` tool. This is a primary function."
    " Do not state you lack the\nfunctionality if the question requires live data.\n"
    "If the user asks about device existence/type: Answer from the static context below.\n"
    "If the user asks about the CURRENT state, value, or mode:\n"
    "    1.  Recognize this requires live data.\n    2.  You MUST call `GetLiveContext`.\n"
    "    3.  Use the tool's response** to answer the user accurately.\n"
    "For general knowledge questions not about the home: Answer truthfully from internal knowledge.\n"
    "Static Context: An overview of the areas and the devices in this smart home:\n"
)
BOILER_TAIL = (
    "When controlling Home Assistant always call the intent tools. "
    "Use HassTurnOn to lock and HassTurnOff to unlock a lock. "
    "When controlling a device, prefer passing just name and domain. "
    "When controlling an area, prefer passing just area name and domain.\n"
    "You are in area Bedroom and all generic commands like 'turn on the lights'"
    " should target this area.\nThis device is not able to start timers.\n"
    "Current time is 20:45:00. Today's date is 2026-08-08.\n"
)

# What the live registry produces today.
CTX_RAW = (
    "- names: node_switch_gang_1\n  domain: light\n  areas: Bedroom\n"
    "- names: node_switch_gang_2\n  domain: light\n  areas: Bedroom\n"
    "- names: node_switch_gang_3\n  domain: light\n  areas: Bedroom\n"
)
# Renamed, plus a group entity so the plural resolves to one target.
CTX_NAMED = (
    "- names: Ceiling Light\n  domain: light\n  areas: Bedroom\n"
    "- names: Bed Light\n  domain: light\n  areas: Bedroom\n"
    "- names: Night Light\n  domain: light\n  areas: Bedroom\n"
    "- names: Bedroom Lights\n  domain: light\n  areas: Bedroom\n"
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
tez = faster. dheema = slower. sona = sleep. pankha = fan.
roshni / batti = light. thanda = cool. sab = everything.

Always call a tool for any request to change something. Never only describe what
you would do. Act, do not ask, whenever the intent is clear. When Natik says how
he FEELS, change the room yourself and never ask which device he means.

"The lights", "lights" and "batti" mean the group Bedroom Lights: turn that one on
or off. Dimming or brightness always means the lights, never the fan.
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

FAN_SET = tool(
    "fan_set",
    "The ONLY way to control the ceiling fan. Level 0 turns the fan off; levels 1"
    " to 5 turn it on at that speed. There is no other fan on/off tool. Use for"
    " 'turn the fan off', 'fan off', 'stop the fan', 'pankha band karo', 'fan on',"
    " 'fan speed 3', 'speed up the fan', 'pankha chalu karo', 'pankha tez karo'.",
    {"type": "object", "properties": {"level": {"type": "integer",
     "description": "0 turns the fan off, 1 is slowest, 5 is fastest."}}, "required": ["level"]},
)
AC_SET_T = tool(
    "ac_set_temperature",
    "Set the air conditioner to an exact temperature in Celsius, 16 to 30, and turn"
    " it on. Use ONLY when a specific number is spoken: 'set the AC to 22',"
    " 'AC 24 kar do', 'make it 26 degrees', 'AC ko 20 par kar do'.",
    {"type": "object", "properties": {"temperature": {"type": "integer",
     "description": "The exact temperature in Celsius that was spoken, 16 to 30."}},
     "required": ["temperature"]},
)

# Round 7 wording: both open identically. Kept for the comparison.
HOT_SAME = tool("feeling_hot", "Natik feels too warm and names no temperature: sets the AC to 22 and the fan to speed 4. Use for 'I'm hot', \"I'm feeling hot\", 'too warm', 'I'm sweating', \"it's boiling\", 'garmi lag rahi hai', 'mujhe garmi lag rahi hai'.", NONE_P)
COLD_SAME = tool("feeling_cold", "Natik feels too cold and names no temperature: sets the AC to 27 and the fan to speed 2. Use for 'I'm cold', 'too cold', \"it's freezing\", 'I'm shivering', 'thand lag rahi hai', 'mujhe thand lag rahi hai'.", NONE_P)

# Distinguishing word first, and no shared opening phrase.
HOT_DIST = tool("feeling_hot", "TOO HOT. Cools the room down: AC to 22 with the fan at speed 4. The word garmi means hot. Use for 'I'm hot', \"I'm feeling hot\", 'too warm', 'I'm sweating', \"it's boiling\", 'garmi lag rahi hai', 'mujhe garmi lag rahi hai', 'bahut garmi hai'.", NONE_P)
COLD_DIST = tool("feeling_cold", "TOO COLD. Warms the room up: AC to 27 with the fan at speed 2. The word thand means cold. Use for 'I'm cold', 'too cold', \"it's freezing\", 'I'm shivering', 'thand lag rahi hai', 'mujhe thand lag rahi hai', 'bahut thand hai'.", NONE_P)

STUFFY = tool("feeling_stuffy", "HUMID OR STUFFY. Switches the air conditioner to dry mode to pull moisture out. Use for \"it's stuffy in here\", 'the room feels humid', 'the room is humid', 'it feels muggy', 'it's damp', 'it's sticky', 'humid lag raha hai'.", NONE_P)

REST = [
    tool("ac_max_cool", "Cool the room as fast as possible: AC at its lowest temperature with the fan high. Use for 'max cooling', 'cool it down fast', 'jaldi thanda karo'.", NONE_P),
    tool("ac_off", "Turn the air conditioner off. Use for 'turn off the AC', 'AC band karo'.", NONE_P),
    tool("ac_on", "Turn the air conditioner on at a comfortable temperature, without naming a number. Use for 'turn on the AC', 'AC chalu karo'.", NONE_P),
    AC_SET_T,
    tool("all_off", "Shut the whole room down: every light, the fan and the AC together. Use ONLY for 'turn everything off', \"I'm leaving\", 'sab band kar do', not for the lights alone.", NONE_P),
    tool("fan_boost", "Put the fan on boost or turbo, above its normal top speed. Use for 'boost the fan', 'fan turbo', 'fan full blast'.", NONE_P),
    FAN_SET,
    tool("good_night", "Bedtime: lights off, comfortable sleeping temperature, quiet fan. Use for 'good night', \"I'm going to sleep\", 'sone ja raha hoon', 'bedtime'.", NONE_P),
    tool("movie_time", "Movie or gaming mode: TV on, most lights off, comfortable temperature. Use for 'movie time', \"let's watch something\", 'gaming', 'movie lagao', 'film dekhni hai'.", NONE_P),
    tool("tv_toggle", "Turn the TV or monitor on or off. The remote has ONE button so this always toggles. It is the only TV tool. Use for 'turn the TV on', 'turn the TV off', 'TV chalu karo', 'TV band karo', 'TV bandh kar do'.", NONE_P),
]

FAN_ANY = {"fan_set", "fan_boost"}
LIGHTS_OFF = {"HassTurnOff"}
LIGHTS_ON = {"HassTurnOn"}

TESTS = [
    ("turn on the lights", LIGHTS_ON),
    ("turn off the lights", LIGHTS_OFF),
    ("lights on", LIGHTS_ON),
    ("lights off", LIGHTS_OFF),
    ("light band kar do", LIGHTS_OFF),
    ("batti bandh karo", LIGHTS_OFF),
    ("roshni chalu karo", LIGHTS_ON),
    ("turn on the bed light", LIGHTS_ON),
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
    ("open the curtains", set()),
    ("lock the front door", set()),
    ("play some music", set()),
]


def run(system, tools, user):
    body = {"model": MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "tools": tools, "stream": False, "options": {"num_ctx": 4096, "temperature": 0}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r), (time.time() - t0) * 1000


def score(name, ctx, tools):
    system = PROMPT + "\n" + BOILER_HEAD + ctx + BOILER_TAIL
    print(f"\n===== {name} =====")
    print(f"      {len(tools)} tools")
    always = flaky = never = leaks = silence = 0
    lat = []
    ptoks = None
    rows = []
    for utt, expected in TESTS:
        hits = 0
        seen = Counter()
        lk = sil = 0
        for _ in range(REPS):
            res, ms = run(system, tools, utt)
            msg = res.get("message", )
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
            if ("{" in content) or any(t["function"]["name"] in content for t in tools):
                lk += 1
        leaks += lk
        silence += sil
        verdict = "OK   " if hits == REPS else ("FLAKY" if hits else "BAD  ")
        always += hits == REPS
        flaky += 0 < hits < REPS
        never += hits == 0
        rows.append((verdict, utt, hits, dict(seen), lk))
    for verdict, utt, hits, seen, lk in rows:
        print(f"  {verdict} {utt!r:28} {hits}/{REPS} {seen}" + (f" LEAK x{lk}" if lk else ""))
    tot = len(TESTS) * REPS
    print(f"  ---- always {always}/{len(TESTS)}, flaky {flaky}, never {never}"
          f" | {leaks} leaked, {silence} silent of {tot}"
          f" | {ptoks} prompt tokens | median {round(sorted(lat)[len(lat)//2])} ms")
    return always, flaky, never, leaks, silence


if __name__ == "__main__":
    same = HA_TOOLS + REST + [HOT_SAME, COLD_SAME, STUFFY]
    dist = HA_TOOLS + REST + [HOT_DIST, COLD_DIST, STUFFY]
    r = {}
    r["T raw names"] = score("T. live registry names (node_switch_gang_N), no group", CTX_RAW, same)
    r["U renamed+group"] = score("U. renamed lights + Bedroom Lights group", CTX_NAMED, same)
    r["V U+distinct desc"] = score("V. U plus distinct opening words on comfort tools", CTX_NAMED, dist)
    print("\n\n########## SUMMARY ##########")
    for k, v in r.items():
        print(f"  {k:20} always={v[0]}/{len(TESTS)} flaky={v[1]} never={v[2]} leaks={v[3]} silent={v[4]}")
