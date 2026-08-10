"""Read-only benchmark round 6: polish the winning config.

Round 5 config P scored 20/24 always-correct with zero leaked replies. Three
failures remain, and this round fixes and re-verifies each:

  1. "turn the fan off"    -> silence. The fan is not an exposed entity in this
     config, so HassTurnOff has nothing to match and the model gives up rather
     than falling back to the fan_set tool.
  2. "it's so stuffy"      -> spoke without acting. ac_dry is never selected.
  3. "open the curtains"   -> called HassTurnOn for a device that does not exist.

Nothing is written; Home Assistant is not reconfigured.
"""

import json
import time
import urllib.request
from collections import Counter

OLLAMA = "http://jarvis-ollama:11434/api/chat"
MODEL = "qwen2.5:3b"
REPS = 3

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
    "- names: Ceiling Light\n  domain: light\n  areas: Bedroom\n"
    "- names: Bed Light\n  domain: light\n  areas: Bedroom\n"
    "- names: Night Light\n  domain: light\n  areas: Bedroom\n"
    "When controlling Home Assistant always call the intent tools. "
    "Use HassTurnOn to lock and HassTurnOff to unlock a lock. "
    "When controlling a device, prefer passing just name and domain. "
    "When controlling an area, prefer passing just area name and domain.\n"
    "You are in area Bedroom and all generic commands like 'turn on the lights'"
    " should target this area.\nThis device is not able to start timers.\n"
    "Current time is 20:45:00. Today's date is 2026-08-08.\n"
)

# Round 5 config P prompt, unchanged.
PROMPT_P = """You are JARVIS, Natik's home assistant for one bedroom in India.

Your reply is SPOKEN aloud, so answer in ONE short sentence of at most 8 words.
Never use markdown, lists, emoji, braces or JSON.
Good: "Lights on." / "AC set to 22." / "Fan up." / "Not connected."
Bad: "I have confirmed that I have turned on the lights for you."
Be brief, competent and dry. Never chirpy.

Natik speaks English, Hindi and Hinglish. Understand all three.
ALWAYS reply in English. Never reply in Hindi.
garmi = hot. thand = cold. band karo = turn off. chalu karo / on karo = turn on.
tez = faster. dheema = slower. sona = sleep. pankha = fan. roshni = light.
thanda = cool. sab = everything. kar do / kardo = do it.

Act, do not ask, whenever the intent is clear. Ask only when a real choice is
missing. When Natik says how he FEELS, change the room yourself and never ask
which device he means."""

# Adds the two guard lines the failures call for.
PROMPT_Q = PROMPT_P + """

The fan has no separate on/off switch: set its level instead, where 0 is off.
This room has only lights, a fan, an air conditioner and a TV. There are no
curtains, locks, blinds or speakers. If asked for one of those, say it is not
connected and call nothing."""


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

CORE = [
    tool("HassTurnOn", "Turns on/opens/presses a device or entity. For locks, this performs a 'lock' action.", TARGET),
    tool("HassTurnOff", "Turns off/closes a device or entity. For locks, this performs an 'unlock' action.", TARGET),
    tool("HassLightSet", "Sets the brightness or color of a light", TARGET),
    tool("GetLiveContext", "Provides real-time information about the CURRENT state, value, or mode of devices, sensors, entities, or areas.",
         {"type": "object", "properties": {"name": {"type": "string"}, "domain": {"type": "string"}, "area": {"type": "string"}}, "required": []}),
]

# Round 5 wording.
FAN_SET_OLD = tool(
    "fan_set",
    "Control the ceiling fan: turn it on, turn it off, or set its speed. Use for"
    " 'fan on', 'fan off', 'turn the fan off', 'fan speed 3', 'slow the fan down',"
    " 'speed up the fan', 'pankha chalu karo', 'pankha band karo', 'pankha tez karo',"
    " 'pankha dheema karo', 'fan tez karo', 'fan dheema karo'.",
    {"type": "object", "properties": {"level": {"type": "integer",
     "description": "0 turns the fan off, 1 is slowest, 5 is fastest, 6 is boost."}},
     "required": ["level"]},
)

# Leads with OFF, and states plainly that it is the only way to stop the fan.
FAN_SET_NEW = tool(
    "fan_set",
    "The ONLY way to control the ceiling fan. Turns the fan off with level 0, or"
    " on at speeds 1 to 5. There is no other fan on/off tool, so use this one for"
    " 'turn the fan off', 'fan off', 'stop the fan', 'switch off the fan',"
    " 'pankha band karo', 'fan band kar do', and equally for 'fan on',"
    " 'fan speed 3', 'slow the fan down', 'speed up the fan', 'pankha chalu karo',"
    " 'pankha tez karo', 'pankha dheema karo'.",
    {"type": "object", "properties": {"level": {"type": "integer",
     "description": "0 turns the fan off, 1 is slowest, 5 is fastest, 6 is boost."}},
     "required": ["level"]},
)

AC_SET_T = tool(
    "ac_set_temperature",
    "Set the air conditioner temperature in Celsius, 16 to 30, and turn the AC on."
    " Use for 'set the AC to 22', 'AC 24 kar do', 'make it warmer', 'make it cooler'.",
    {"type": "object", "properties": {"temperature": {"type": "integer",
     "description": "Target temperature in Celsius, 16 to 30."}}, "required": ["temperature"]},
)

AC_DRY_OLD = tool("ac_dry", "Switch the air conditioner to dry or dehumidify mode. Use when the room feels stuffy, damp, humid, muggy, sticky or airless, 'humid lag raha hai', or during the monsoon.", NONE_P)
# Renamed to match how the feeling is actually spoken, like feeling_hot/feeling_cold.
FEELING_STUFFY = tool("feeling_stuffy", "The room feels stuffy, damp, humid, muggy, sticky, close or airless: switches the air conditioner to dry / dehumidify mode. Use for \"it's stuffy in here\", \"it's so stuffy\", 'the room is humid', 'it feels muggy', 'humid lag raha hai', 'chipchipa lag raha hai', and during the monsoon.", NONE_P)

REST = [
    tool("ac_max_cool", "Cool the room as fast as possible: AC at its lowest temperature with the fan high. Use for 'max cooling', 'cool it down fast', 'jaldi thanda karo'.", NONE_P),
    tool("ac_off", "Turn the air conditioner off. Use for 'turn off the AC', 'AC band karo'.", NONE_P),
    tool("ac_on", "Turn the air conditioner on at a comfortable temperature. Use for 'turn on the AC', 'AC chalu karo'.", NONE_P),
    AC_SET_T,
    tool("all_off", "Shut the whole room down: every light, the fan and the AC. Use for 'turn everything off', \"I'm leaving\", 'sab band kar do'.", NONE_P),
    tool("fan_boost", "Put the fan on boost or turbo, the highest setting above speed 5. Use for 'boost the fan', 'fan turbo', 'fan full blast'.", NONE_P),
    tool("good_night", "Bedtime: lights off, comfortable sleeping temperature, quiet fan. Use for 'good night', \"I'm going to sleep\", 'sone ja raha hoon', 'bedtime', 'sona hai'.", NONE_P),
    tool("movie_time", "Movie or gaming mode: TV on, most lights off, comfortable temperature. Use for 'movie time', \"let's watch something\", 'gaming', 'film dekhni hai'.", NONE_P),
    tool("tv_toggle", "Turn the TV or monitor on or off. The remote has ONE button so this always toggles. Use for 'turn the TV on', 'turn the TV off', 'TV chalu karo'.", NONE_P),
    tool("feeling_hot", "Natik is too warm: cools the room by setting the AC to 22 and the fan to speed 4. Use for 'I'm hot', \"I'm feeling hot\", 'too warm', 'I'm sweating', \"it's boiling\", 'garmi lag rahi hai', 'mujhe garmi lag rahi hai', 'thoda thanda karo'.", NONE_P),
    tool("feeling_cold", "Natik is too cold: warms the room by setting the AC to 27 and the fan to speed 2. Use for 'I'm cold', 'too cold', \"it's freezing\", 'I'm shivering', 'thand lag rahi hai', 'mujhe thand lag rahi hai'.", NONE_P),
]

FAN_ANY = {"fan_set", "fan_boost"}
HOT = {"feeling_hot", "ac_set_temperature", "ac_max_cool", "ac_on"} | FAN_ANY
COLD = {"feeling_cold", "ac_set_temperature", "ac_off"} | FAN_ANY
STUFFY = {"ac_dry", "feeling_stuffy"}

TESTS = [
    ("turn on the lights", {"HassTurnOn"}),
    ("turn off the lights", {"HassTurnOff"}),
    ("lights on", {"HassTurnOn"}),
    ("light band kar do", {"HassTurnOff"}),
    ("dim the lights", {"HassLightSet", "HassTurnOff"}),
    ("set the AC to 22", {"ac_set_temperature"}),
    ("turn the AC off", {"ac_off"}),
    ("AC chalu karo", {"ac_on", "ac_set_temperature"}),
    ("turn the TV on", {"tv_toggle"}),
    ("TV band karo", {"tv_toggle"}),
    ("fan speed 3", FAN_ANY),
    ("turn the fan off", FAN_ANY),
    ("pankha band karo", FAN_ANY),
    ("boost the fan", FAN_ANY),
    ("pankha tez karo", FAN_ANY),
    ("I'm feeling hot", HOT),
    ("garmi lag rahi hai", HOT),
    ("it's boiling in here", HOT),
    ("I'm cold", COLD),
    ("mujhe thand lag rahi hai", COLD),
    ("it's freezing in here", COLD),
    ("it's so stuffy in here", STUFFY),
    ("the room feels humid", STUFFY),
    ("good night", {"good_night", "HassTurnOff"}),
    ("I'm going to sleep", {"good_night", "HassTurnOff"}),
    ("sone ja raha hoon", {"good_night", "HassTurnOff"}),
    ("I'm watching a movie", {"movie_time"}),
    ("sab band kar do", {"all_off", "HassTurnOff"}),
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


def score(name, prompt, tools):
    system = prompt + "\n" + BOILER
    print(f"\n===== {name} =====")
    print(f"      {len(tools)} tools, {REPS} reps each")
    always = flaky = never = leaks = 0
    lat = []
    ptoks = None
    rows = []
    for utt, expected in TESTS:
        hits = 0
        seen = Counter()
        lk = 0
        for _ in range(REPS):
            res, ms = run(system, tools, utt)
            msg = res.get("message", {})
            calls = msg.get("tool_calls") or []
            content = (msg.get("content") or "").strip()
            ptoks = res.get("prompt_eval_count")
            lat.append(ms)
            got = {c["function"]["name"] for c in calls}
            for g in got:
                seen[g] += 1
            if not got:
                seen["(spoke only)" if content else "(silence)"] += 1
            if (not expected and not got) or bool(got & expected):
                hits += 1
            if ("{" in content) or any(t["function"]["name"] in content for t in tools):
                lk += 1
        leaks += lk
        verdict = "OK   " if hits == REPS else ("FLAKY" if hits else "BAD  ")
        always += hits == REPS
        flaky += 0 < hits < REPS
        never += hits == 0
        rows.append((verdict, utt, hits, dict(seen), lk))
    for verdict, utt, hits, seen, lk in rows:
        print(f"  {verdict} {utt!r:30} {hits}/{REPS} {seen}" + (f" LEAK x{lk}" if lk else ""))
    print(f"  ---- always {always}, flaky {flaky}, never {never}"
          f" | {leaks} leaked of {len(TESTS)*REPS} | {ptoks} prompt tokens"
          f" | median {round(sorted(lat)[len(lat)//2])} ms")
    return always, flaky, never, leaks


if __name__ == "__main__":
    base = CORE + REST
    r = {}
    r["P (round 5 winner)"] = score("P. round-5 winner, re-run on the wider test set",
                                    PROMPT_P, base + [FAN_SET_OLD, AC_DRY_OLD])
    r["Q (fixes applied)"] = score("Q. guard lines + fan_set reworded + feeling_stuffy",
                                   PROMPT_Q, base + [FAN_SET_NEW, FEELING_STUFFY])
    print("\n\n########## SUMMARY ##########")
    for k, v in r.items():
        print(f"  {k:22} always={v[0]}/{len(TESTS)} flaky={v[1]} never={v[2]} leaks={v[3]}")
