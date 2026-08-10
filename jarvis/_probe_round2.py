"""Read-only benchmark round 2.

Round 1 produced two surprises worth isolating:

  1. Naming tools in the system prompt made things WORSE. The model echoed
     "ac_set_temperature 27" as spoken text instead of calling the tool.
  2. MORE comfort scripts scored better than fewer, because a script collapses
     a multi-step decision into one call.

Round 2 tests where routing knowledge belongs: the system prompt, or the tool
descriptions. Hinglish triggers are the weakest area, so they are the probe.
Nothing is written; Home Assistant is not reconfigured.
"""

import json
import time
import urllib.request

OLLAMA = "http://jarvis-ollama:11434/api/chat"
MODEL = "qwen2.5:3b"

HA_BOILERPLATE = (
    "You ARE equipped to answer questions about the current state of\n"
    "the home using the `GetLiveContext` tool. This is a primary function."
    " Do not state you lack the\nfunctionality if the question requires live data.\n"
    "If the user asks about device existence/type (e.g., \"Do I have lights in the"
    " bedroom?\"): Answer\nfrom the static context below.\n"
    "If the user asks about the CURRENT state, value, or mode (e.g., \"Is the lock"
    " locked?\",\n\"Is the fan on?\", \"What mode is the thermostat in?\", \"What is"
    " the temperature outside?\"):\n    1.  Recognize this requires live data.\n"
    "    2.  You MUST call `GetLiveContext`. This tool will provide the needed"
    " real-time information (like temperature from the local weather, lock status,"
    " etc.).\n    3.  Use the tool's response** to answer the user accurately"
    " (e.g., \"The temperature outside is [value from tool].\").\n"
    "For general knowledge questions not about the home: Answer truthfully from"
    " internal knowledge.\n"
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

# Round 1 winner (9/12). Names two tools, which is the suspected leak source.
CURRENT_PROMPT = """You are JARVIS, the assistant for this house. Always reply in English.

Reply in AT MOST 8 words. No preamble, no restating the request, no "I have" or "I've confirmed". Just the result.
Good: "Lights on." / "AC set to 22." / "Nothing here reports temperature."
Bad: "I have confirmed that I have turned on the lights for you."

Call the tool, then state the result in those few words. Temperatures are integers, never strings.
Never claim you changed something that does not exist -- say what is missing instead.
NEVER print raw JSON, braces, or tool names like HassTurnOn in your reply. The user hears your words aloud.

Scenes need MORE THAN ONE call. Make every call, then confirm once:
- movie mode -> lights dim low, AC 22
- sleep mode -> lights off, AC 24
- I am home -> lights on, AC 24
- good morning -> lights on, AC off"""

# Zero tool names anywhere. Hinglish carried as vocabulary, not as routing.
CLEAN_PROMPT = """You are JARVIS, Natik's home assistant. You control one bedroom in India.

STYLE: brief, competent, dry, never chirpy. Your words are SPOKEN aloud.
Reply in ONE short sentence, at most 8 words. Never use markdown, lists, emoji,
braces, JSON, or the internal name of any tool or entity.
Good: "Lights on." / "AC set to 22." / "Fan up."
Bad: "I have confirmed that I have turned on the lights for you."

LANGUAGE: Natik speaks English, Hindi and Hinglish. Understand all of them.
ALWAYS reply in English only. Never reply in Hindi.
garmi = hot. thand = cold. band karo = turn off. chalu karo / on karo = turn on.
tez = faster or higher. dheema = slower or lower. sona = sleep. roshni/light = light.

ACT, do not ask, whenever the intent is clear. Ask only when a genuine choice is
missing. If someone describes how they FEEL, change the room for them and never
ask which device to use.

Only a fan, an air conditioner, a TV and three lights exist here. Never claim to
control anything else -- say plainly that it is not connected."""


def target_schema():
    return {
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


def tool(name, desc, params=None):
    return {
        "type": "function",
        "function": {"name": name, "description": desc, "parameters": params or target_schema()},
    }


NONE_P = {"type": "object", "properties": {}, "required": []}

GET_LIVE_CONTEXT = tool(
    "GetLiveContext",
    "Provides real-time information about the CURRENT state, value, or mode of"
    " devices, sensors, entities, or areas. Use this tool for: 1. Answering questions"
    " about current conditions (e.g., 'Is the light on?'). 2. As the first step in"
    " conditional actions. You may filter for devices by name, domain, and area.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Filter entities by name or alias (case-insensitive)."},
            "domain": {"type": "string", "description": "Filter entities by domain (e.g. 'light', 'sensor')."},
            "area": {"type": "string", "description": "Filter entities by area name or alias (case-insensitive)."},
        },
        "required": [],
    },
)

BASE = [
    tool("HassTurnOn", "Turns on/opens/presses a device or entity. For locks, this performs a 'lock' action."),
    tool("HassTurnOff", "Turns off/closes a device or entity. For locks, this performs an 'unlock' action."),
    tool("HassCancelAllTimers", "Cancels all timers"),
    tool("HassLightSet", "Sets the brightness or color of a light"),
    tool("HassFanSetSpeed", "Sets a fan's speed by percentage"),
    GET_LIVE_CONTEXT,
]

AC_SET_T = tool(
    "ac_set_temperature",
    "Set the air conditioner temperature in Celsius, 16 to 30. Also turns the AC on.",
    {
        "type": "object",
        "properties": {"temperature": {"type": "number", "description": "Target temperature in Celsius, 16 to 30"}},
        "required": ["temperature"],
    },
)

# Plain English descriptions, as used in round 1.
SCRIPTS_PLAIN = [
    tool("ac_dry", "Switch the air conditioner to dry / dehumidify mode. Use when the room feels damp, humid, or muggy.", NONE_P),
    tool("ac_max_cool", "Cool the room as fast as possible -- AC at its lowest temperature with the fan high.", NONE_P),
    tool("ac_off", "Turn the air conditioner off.", NONE_P),
    tool("ac_on", "Turn the air conditioner on at a comfortable temperature.", NONE_P),
    AC_SET_T,
    tool("all_off", "Shut the whole room down -- every light, the fan and the AC.", NONE_P),
    tool("fan_boost", "Put the fan on boost / turbo -- the highest setting, above speed 5.", NONE_P),
    tool("good_night", "Bedtime. Lights off, comfortable sleeping temperature, quiet fan.", NONE_P),
    tool("movie_time", "Movie or gaming mode. TV on, most lights off, comfortable temperature.", NONE_P),
    tool("tv_toggle", "Turn the TV or monitor on or off. One button, so this always toggles.", NONE_P),
]

# Same tools, descriptions loaded with the Hindi/Hinglish a user would actually say.
SCRIPTS_HINGLISH = [
    tool("ac_dry", "Switch the air conditioner to dry or dehumidify mode. Use when the room feels damp, humid, muggy, sticky, 'humid lag raha hai', or during the monsoon.", NONE_P),
    tool("ac_max_cool", "Cool the room as fast as possible: AC at its lowest temperature with the fan high. Use for 'it's boiling', 'I'm burning up', 'bahut garmi hai', 'bohot garmi lag rahi hai', 'jaldi thanda karo', 'max cooling', 'cool it down fast'.", NONE_P),
    tool("ac_off", "Turn the air conditioner off. Use for 'turn off the AC', 'AC band karo', 'AC bandh kar do'.", NONE_P),
    tool("ac_on", "Turn the air conditioner on at a comfortable temperature. Use for 'turn on the AC', 'AC chalu karo', 'AC on kar do'.", NONE_P),
    tool(
        "ac_set_temperature",
        "Set the air conditioner temperature in Celsius, 16 to 30, and turn the AC on. Use whenever the user is warm or cold and names no device: 'I'm hot', 'I'm cold', 'garmi lag rahi hai', 'thand lag rahi hai', 'mujhe garmi lag rahi hai', 'mujhe thand lag rahi hai', 'thoda thanda karo', 'make it warmer', 'make it cooler', 'AC 22 kar do'.",
        {
            "type": "object",
            "properties": {"temperature": {"type": "number", "description": "Target temperature in Celsius, 16 to 30. Use 22 when the user is hot, 27 when the user is cold."}},
            "required": ["temperature"],
        },
    ),
    tool("all_off", "Shut the whole room down: every light, the fan and the AC. Use for 'turn everything off', 'shut it all down', \"I'm leaving\", 'sab band kar do', 'sab kuch band kar do'.", NONE_P),
    tool("fan_boost", "Put the fan on boost or turbo, the highest setting above speed 5. Use for 'boost the fan', 'fan turbo', 'fan full blast', 'fan tez karo', 'pankha tez karo'.", NONE_P),
    tool("good_night", "Bedtime: lights off, comfortable sleeping temperature, quiet fan. Use for 'good night', \"I'm going to sleep\", 'sone ja raha hoon', 'so raha hoon', 'bedtime', 'sona hai'.", NONE_P),
    tool("movie_time", "Movie or gaming mode: TV on, most lights off, comfortable temperature. Use for 'movie time', \"let's watch something\", 'gaming', 'film dekhni hai', 'movie lagao'.", NONE_P),
    tool("tv_toggle", "Turn the TV or monitor on or off. The remote has ONE button so this always toggles. Use for 'turn the TV on', 'turn the TV off', 'TV chalu karo', 'TV band karo'.", NONE_P),
]

TESTS = [
    ("turn on the lights", {"HassTurnOn"}),
    ("turn off the lights", {"HassTurnOff"}),
    ("set the AC to 22", {"ac_set_temperature"}),
    ("fan speed 3", {"HassFanSetSpeed"}),
    ("turn the TV on", {"tv_toggle"}),
    ("boost the fan", {"fan_boost"}),
    ("turn the AC off", {"ac_off"}),
    ("I'm feeling hot", {"ac_set_temperature", "ac_max_cool", "HassFanSetSpeed", "fan_boost", "ac_on"}),
    ("garmi lag rahi hai", {"ac_set_temperature", "ac_max_cool", "HassFanSetSpeed", "fan_boost", "ac_on"}),
    ("mujhe thand lag rahi hai", {"ac_set_temperature", "ac_off", "HassFanSetSpeed"}),
    ("good night", {"good_night", "HassTurnOff", "ac_set_temperature"}),
    ("light band kar do", {"HassTurnOff"}),
    ("it's so stuffy in here", {"ac_dry", "ac_set_temperature", "HassFanSetSpeed", "fan_boost"}),
    ("sab band kar do", {"all_off", "HassTurnOff"}),
    ("pankha tez karo", {"HassFanSetSpeed", "fan_boost"}),
    ("I'm going to sleep", {"good_night", "HassTurnOff"}),
]


def run(system, tools, user, num_ctx):
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "tools": tools,
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": 0},
    }
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        res = json.load(r)
    return res, (time.time() - t0) * 1000


def score(name, system, tools, num_ctx=4096):
    print(f"\n===== {name}  ({len(tools)} tools) =====")
    ok = leaked = 0
    lat = []
    ptoks = None
    for utt, expected in TESTS:
        res, ms = run(system, tools, utt, num_ctx)
        msg = res.get("message", {})
        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        ptoks = res.get("prompt_eval_count")
        lat.append(ms)
        got = {c["function"]["name"] for c in calls}
        hit = bool(got & expected)
        leak = ("{" in content) or any(t["function"]["name"] in content for t in tools)
        ok += hit
        leaked += leak
        flag = "PASS" if hit and not leak else ("LEAK" if leak else "FAIL")
        print(f"  [{flag}] {utt!r:32} -> {sorted(got) or '(none)'}  {round(ms)}ms")
        if leak:
            print(f"          leaked: {content[:100]!r}")
    print(f"  ---- {ok}/{len(TESTS)} correct, {leaked} leaked | {ptoks} prompt tokens"
          f" | median {round(sorted(lat)[len(lat)//2])} ms")
    return ok, leaked


if __name__ == "__main__":
    cur = CURRENT_PROMPT + "\n" + HA_BOILERPLATE
    clean = CLEAN_PROMPT + "\n" + HA_BOILERPLATE
    score("F. current prompt + PLAIN descriptions (round-1 baseline)", cur, BASE + SCRIPTS_PLAIN)
    score("G. current prompt + HINGLISH descriptions", cur, BASE + SCRIPTS_HINGLISH)
    score("H. CLEAN prompt (no tool names) + HINGLISH descriptions", clean, BASE + SCRIPTS_HINGLISH)
    score("I. CLEAN prompt + PLAIN descriptions", clean, BASE + SCRIPTS_PLAIN)
