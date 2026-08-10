"""Read-only benchmark round 4.

Two things to settle before recommending anything:

  1. Does num_ctx 2048 actually overflow once a tool RESULT comes back? The
     first turn measured 1760-2150 tokens, so it fits -- but GetLiveContext
     returns a YAML dump of every exposed entity, and that lands on the second
     turn. Overflow silently truncates from the front, which is where the
     system prompt lives.
  2. Round 3 regressed "turn on the lights" to an empty reply. Find which line
     of the prompt did it, because a prompt that breaks the commonest command
     in the house is not shippable.

Nothing is written; Home Assistant is not reconfigured.
"""

import json
import time
import urllib.request

OLLAMA = "http://jarvis-ollama:11434/api/chat"
MODEL = "qwen2.5:3b"

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
    "- names: Fan\n  domain: fan\n  areas: Bedroom\n"
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

STYLE = """Your reply is SPOKEN aloud. Answer in ONE short sentence, at most 8 words.
Never use markdown, lists, emoji, braces or JSON.
Good: "Lights on." / "AC set to 22." / "Fan up."
Bad: "I have confirmed that I have turned on the lights for you."
Be brief, competent and dry. Never chirpy."""

GLOSSARY = """garmi = hot. thand = cold. band karo = turn off. chalu karo / on karo = turn on.
tez = faster or higher. dheema = slower or lower. sona = sleep. pankha = fan.
roshni / light = light. thanda = cool. sab = everything. kar do / kardo = please do it."""

LANG = """Natik speaks English, Hindi and Hinglish. Understand all three.
ALWAYS reply in English. Never reply in Hindi."""

ACT = """Act, do not ask, whenever the intent is clear. Ask only when a real choice is
missing. When Natik says how he FEELS, change the room yourself and never ask
which device to use."""

NOINVENT = """Only a fan, an air conditioner, a TV and three lights exist here. If asked for
anything else, say plainly that it is not connected. Never invent a device."""

# Variant 1: exactly what round 3 shipped, minus nothing (the regression).
P_R3 = f"""You are JARVIS, Natik's assistant. You control one bedroom, in India.

Your reply is SPOKEN aloud. Answer in ONE short sentence, at most 8 words.
Never use markdown, lists, emoji, braces, JSON, or any internal tool or entity name.
Good: "Lights on." / "AC set to 22." / "Fan up." / "The TV is not connected."
Bad: "I have confirmed that I have turned on the lights for you."

Be brief, competent and dry. Never chirpy, never apologetic, never enthusiastic.

{LANG}

{ACT}

{NOINVENT}"""

# Variant 2: round 3 minus "or any internal tool or entity name".
P_NOENTITYBAN = f"""You are JARVIS, Natik's assistant. You control one bedroom, in India.

{STYLE}

{LANG}

{ACT}

{NOINVENT}"""

# Variant 3: variant 2 plus the Hinglish glossary that round 2 had.
P_GLOSSARY = f"""You are JARVIS, Natik's assistant. You control one bedroom, in India.

{STYLE}

{LANG}
{GLOSSARY}

{ACT}

{NOINVENT}"""

# Variant 4: shortest thing that could work -- style + language + act only.
P_MINIMAL = f"""You are JARVIS, Natik's assistant for one bedroom in India.

{STYLE}

{LANG}
{GLOSSARY}

{ACT}"""


def tool(name, desc, params):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


TARGET = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "area": {"type": "string"},
        "floor": {"type": "string"},
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
    tool("GetLiveContext", "Provides real-time information about the CURRENT state, value, or mode of devices, sensors, entities, or areas.", {"type": "object", "properties": {"name": {"type": "string"}, "domain": {"type": "string"}, "area": {"type": "string"}}, "required": []}),
    tool("HassFanSetSpeed", "Sets a fan's speed by percentage", dict(TARGET, properties=dict(TARGET["properties"], percentage={"type": "integer", "description": "The speed percentage of the fan"}), required=["percentage"])),
    tool("ac_dry", "Switch the air conditioner to dry or dehumidify mode. Use when the room feels stuffy, damp, humid, muggy, sticky, airless, 'humid lag raha hai', or during the monsoon.", NONE_P),
    tool("ac_max_cool", "Cool the room as fast as possible: AC at its lowest temperature with the fan high. Use for 'it's boiling', 'I'm burning up', 'bahut garmi hai', 'jaldi thanda karo', 'max cooling'.", NONE_P),
    tool("ac_off", "Turn the air conditioner off. Use for 'turn off the AC', 'AC band karo'.", NONE_P),
    tool("ac_on", "Turn the air conditioner on at a comfortable temperature. Use for 'turn on the AC', 'AC chalu karo'.", NONE_P),
    tool("ac_set_temperature", "Set the air conditioner temperature in Celsius, 16 to 30, and turn the AC on. Use whenever Natik is warm or cold and names no device: 'I'm hot', 'I'm cold', 'garmi lag rahi hai', 'thand lag rahi hai', 'make it warmer', 'make it cooler', 'AC 22 kar do'.", {"type": "object", "properties": {"temperature": {"type": "integer", "description": "Target temperature in Celsius, 16 to 30. Use 22 when he is hot, 27 when he is cold."}}, "required": ["temperature"]}),
    tool("all_off", "Shut the whole room down: every light, the fan and the AC. Use for 'turn everything off', \"I'm leaving\", 'sab band kar do'.", NONE_P),
    tool("fan_boost", "Put the fan on boost or turbo, the highest setting above speed 5. Use for 'boost the fan', 'fan turbo', 'fan full blast'.", NONE_P),
    tool("good_night", "Bedtime: lights off, comfortable sleeping temperature, quiet fan. Use for 'good night', \"I'm going to sleep\", 'sone ja raha hoon', 'bedtime'.", NONE_P),
    tool("movie_time", "Movie or gaming mode: TV on, most lights off, comfortable temperature. Use for 'movie time', \"let's watch something\", 'gaming', 'film dekhni hai'.", NONE_P),
    tool("tv_toggle", "Turn the TV or monitor on or off. The remote has ONE button so this always toggles. Use for 'turn the TV on', 'turn the TV off', 'TV chalu karo'.", NONE_P),
]

FAN_OK = {"HassFanSetSpeed", "fan_boost"}
TESTS = [
    ("turn on the lights", {"HassTurnOn"}),
    ("turn off the lights", {"HassTurnOff"}),
    ("lights on", {"HassTurnOn"}),
    ("set the AC to 22", {"ac_set_temperature"}),
    ("fan speed 3", FAN_OK),
    ("turn the TV on", {"tv_toggle"}),
    ("boost the fan", FAN_OK),
    ("turn the AC off", {"ac_off"}),
    ("I'm feeling hot", {"ac_set_temperature", "ac_max_cool", "ac_on"} | FAN_OK),
    ("garmi lag rahi hai", {"ac_set_temperature", "ac_max_cool", "ac_on"} | FAN_OK),
    ("mujhe thand lag rahi hai", {"ac_set_temperature", "ac_off"} | FAN_OK),
    ("good night", {"good_night", "HassTurnOff", "ac_set_temperature"}),
    ("light band kar do", {"HassTurnOff"}),
    ("it's so stuffy in here", {"ac_dry", "ac_set_temperature"} | FAN_OK),
    ("sab band kar do", {"all_off", "HassTurnOff"}),
    ("pankha tez karo", FAN_OK),
    ("I'm going to sleep", {"good_night", "HassTurnOff"}),
    ("I'm watching a movie", {"movie_time"}),
    ("it's freezing in here", {"ac_set_temperature", "ac_off"} | FAN_OK),
    ("open the curtains", set()),
]


def run(msgs, tools, num_ctx=4096):
    body = {"model": MODEL, "messages": msgs, "tools": tools, "stream": False,
            "options": {"num_ctx": num_ctx, "temperature": 0}}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r), (time.time() - t0) * 1000


def score(name, prompt):
    system = prompt + "\n" + BOILER
    ok = leaked = empty = 0
    lat = []
    ptoks = None
    fails = []
    for utt, expected in TESTS:
        res, ms = run([{"role": "system", "content": system}, {"role": "user", "content": utt}], TOOLS)
        msg = res.get("message", {})
        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        ptoks = res.get("prompt_eval_count")
        lat.append(ms)
        got = {c["function"]["name"] for c in calls}
        hit = (not expected and not got) or bool(got & expected)
        leak = ("{" in content) or any(t["function"]["name"] in content for t in TOOLS)
        if not got and not content:
            empty += 1
        ok += hit
        leaked += leak
        if not hit or leak:
            fails.append((utt, sorted(got), content[:60]))
    print(f"\n--- {name}")
    print(f"    {ok}/{len(TESTS)} correct | {leaked} leaked | {empty} empty replies"
          f" | {ptoks} prompt tokens | median {round(sorted(lat)[len(lat)//2])} ms")
    for utt, got, said in fails:
        print(f"      MISS {utt!r:28} calls={got or '(none)'} said={said!r}")
    return ok


def overflow_test():
    """Second turn, after GetLiveContext returns. Does it still fit in 2048?"""
    print("\n\n########## num_ctx overflow check ##########")
    system = P_GLOSSARY + "\n" + BOILER
    live_result = json.dumps({
        "success": True,
        "result": "Live Context: An overview of the areas and the devices in this smart home:\n"
        "- names: Fan\n  domain: fan\n  state: 'on'\n  areas: Bedroom\n  attributes:\n    percentage: 60\n"
        "- names: Ceiling Light\n  domain: light\n  state: 'on'\n  areas: Bedroom\n  attributes:\n    brightness: 255\n"
        "- names: Bed Light\n  domain: light\n  state: 'off'\n  areas: Bedroom\n"
        "- names: Night Light\n  domain: light\n  state: 'off'\n  areas: Bedroom\n",
    })
    msgs = [
        {"role": "system", "content": system},
        {"role": "user", "content": "is the fan on?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "GetLiveContext", "arguments": {"domain": "fan"}}}]},
        {"role": "tool", "content": live_result},
    ]
    for ctx in (2048, 4096):
        res, ms = run(msgs, TOOLS, num_ctx=ctx)
        print(f"  num_ctx={ctx}: prompt_eval_count={res.get('prompt_eval_count')}"
              f"  reply={(res.get('message', {}).get('content') or '')[:70]!r}  {round(ms)}ms")
    print("  (a prompt_eval_count at or above num_ctx means the front of the")
    print("   conversation -- the system prompt -- was silently discarded)")


if __name__ == "__main__":
    print("########## prompt variant diagnosis ##########")
    score("1. round-3 prompt, bans 'entity name' (the regression)", P_R3)
    score("2. round-3 minus the entity-name ban", P_NOENTITYBAN)
    score("3. variant 2 + Hinglish glossary", P_GLOSSARY)
    score("4. minimal: style + language + glossary + act", P_MINIMAL)
    overflow_test()
