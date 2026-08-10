"""Read-only benchmark round 9: final validation of the config to be shipped.

Round 8 left four failures, and three share one cause: the AC tools open with
"Turn the air conditioner on/off", so a bare "turn on the lights" or "batti
bandh karo" matched ac_on / ac_off. The fix is to make every device-specific
tool name its device in the FIRST WORDS and explicitly disclaim the others.

Fixes under test here:
  - ac_on / ac_off / ac_max_cool lead with "AIR CONDITIONER ONLY" and say they
    are not for lights or the fan.
  - good_night picks up 'sone ja raha hoon' and 'so raha hoon'.
  - all_off says it is not for the lights alone.

Also reports the latency of the winning configuration, since every extra token
of tool schema is prefill on a 3050.

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
    "- names: Bedroom Lights\n  domain: light\n  areas: Bedroom\n"
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

TOOLS = [
    tool("HassTurnOn", "Turns on/opens/presses a device or entity. For locks, this performs a 'lock' action.", TARGET),
    tool("HassTurnOff", "Turns off/closes a device or entity. For locks, this performs an 'unlock' action.", TARGET),
    tool("HassLightSet", "Sets the brightness or color of a light", TARGET),
    tool("GetLiveContext", "Provides real-time information about the CURRENT state, value, or mode of devices, sensors, entities, or areas.",
         {"type": "object", "properties": {"name": {"type": "string"}, "domain": {"type": "string"}, "area": {"type": "string"}}, "required": []}),

    tool("ac_max_cool", "AIR CONDITIONER ONLY, not the lights or the fan. Cools the room as fast as possible, AC at its lowest temperature. Use for 'max cooling', 'cool it down fast', 'jaldi thanda karo'.", NONE_P),
    tool("ac_off", "AIR CONDITIONER ONLY, not the lights or the fan. Switches the air conditioner off. The words AC or air conditioner must be spoken. Use for 'turn off the AC', 'AC band karo', 'AC bandh kar do'.", NONE_P),
    tool("ac_on", "AIR CONDITIONER ONLY, not the lights or the fan. Switches the air conditioner on at a comfortable temperature. The words AC or air conditioner must be spoken. Use for 'turn on the AC', 'AC chalu karo'.", NONE_P),
    tool("ac_set_temperature", "AIR CONDITIONER ONLY. Sets the air conditioner to an exact temperature in Celsius, 16 to 30, and turns it on. Use ONLY when a specific number is spoken: 'set the AC to 22', 'AC 24 kar do', 'make it 26 degrees'.",
         {"type": "object", "properties": {"temperature": {"type": "integer", "description": "The exact temperature in Celsius that was spoken, 16 to 30."}}, "required": ["temperature"]}),
    tool("all_off", "EVERYTHING AT ONCE: every light, the fan and the AC together. Use ONLY for 'turn everything off', 'shut it all down', \"I'm leaving\", 'sab band kar do'. Never use this for the lights alone.", NONE_P),
    tool("fan_boost", "CEILING FAN ONLY. Puts the fan on boost or turbo, above its normal top speed. Use for 'boost the fan', 'fan turbo', 'fan full blast'.", NONE_P),
    tool("fan_set", "CEILING FAN ONLY, not the lights or the AC. The one way to control the fan: level 0 switches it off, levels 1 to 5 switch it on at that speed. Use for 'turn the fan off', 'fan off', 'stop the fan', 'pankha band karo', 'fan on', 'fan speed 3', 'speed up the fan', 'pankha tez karo', 'pankha dheema karo'.",
         {"type": "object", "properties": {"level": {"type": "integer", "description": "0 turns the fan off, 1 is slowest, 5 is fastest."}}, "required": ["level"]}),
    tool("feeling_cold", "TOO COLD. Warms the room: AC to 27 with the fan at speed 2. The word thand means cold. Use for 'I'm cold', 'too cold', \"it's freezing\", 'I'm shivering', 'thand lag rahi hai', 'mujhe thand lag rahi hai'.", NONE_P),
    tool("feeling_hot", "TOO HOT. Cools the room: AC to 22 with the fan at speed 4. The word garmi means hot. Use for 'I'm hot', \"I'm feeling hot\", 'too warm', 'I'm sweating', \"it's boiling\", 'garmi lag rahi hai', 'mujhe garmi lag rahi hai'.", NONE_P),
    tool("feeling_stuffy", "HUMID OR STUFFY. Switches the air conditioner to dry mode to pull moisture out. Use for \"it's stuffy in here\", 'the room feels humid', 'it feels muggy', \"it's damp\", \"it's sticky\", 'humid lag raha hai'.", NONE_P),
    tool("good_night", "BEDTIME. Lights off, comfortable sleeping temperature, quiet fan. Use for 'good night', \"I'm going to sleep\", 'I'm off to bed', 'bedtime', 'sone ja raha hoon', 'so raha hoon', 'sona hai', 'good night jarvis'.", NONE_P),
    tool("movie_time", "MOVIE OR GAMING. TV on, most lights off, comfortable temperature. Use for 'movie time', \"let's watch something\", \"I'm watching a movie\", 'gaming', 'movie lagao', 'film dekhni hai'.", NONE_P),
    tool("tv_toggle", "TELEVISION ONLY. The remote has ONE button so this always toggles the TV on or off. Use for 'turn the TV on', 'turn the TV off', 'TV chalu karo', 'TV band karo'.", NONE_P),
]

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


if __name__ == "__main__":
    system = PROMPT + "\n" + BOILER
    print(f"===== W. FINAL CANDIDATE ({len(TOOLS)} tools, {REPS} reps) =====")
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
    tot = len(TESTS) * REPS
    s = sorted(lat)
    print(f"\n  always {always}/{len(TESTS)}, flaky {flaky}, never {never}")
    print(f"  {leaks} leaked, {silence} silent, of {tot} replies")
    print(f"  prompt tokens {ptoks}")
    print(f"  LLM latency: median {round(s[len(s)//2])} ms, p90 {round(s[int(len(s)*0.9)])} ms,"
          f" min {round(s[0])} ms, max {round(s[-1])} ms")
