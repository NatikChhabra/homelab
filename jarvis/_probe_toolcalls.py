"""Read-only benchmark: does trimming the tool set restore tool calling on qwen2.5:3b?

Sends requests straight to Ollama, exactly as Home Assistant would, and scores
whether a real `tool_calls` array comes back rather than tool JSON leaked into
speech. Home Assistant is never reconfigured; nothing here writes anything.
"""

import json
import time
import urllib.request

OLLAMA = "http://jarvis-ollama:11434/api/chat"
MODEL = "qwen2.5:3b"

# Fixed overhead Home Assistant always adds when llm_hass_api: assist is set.
# Verbatim from homeassistant/components/homeassistant/llm.py and intent/llm.py.
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

NEW_PROMPT = """You are JARVIS, Natik's home assistant. Bedroom, India.

Speak like JARVIS: brief, competent, dry. Never chirpy.
Your reply is SPOKEN. One short sentence, under 10 words. No markdown, no lists,
no emoji, no JSON, no tool or entity names.
Understand Hindi and Hinglish. ALWAYS reply in English only.

Act, do not ask, when the intent is clear. Ask only if a real choice is missing.

Comfort requests: pick the devices yourself, never ask which one.
- hot / garmi / sweating      -> ac_set_temperature 22, fan speed 4
- very hot / boiling / bahut garmi -> ac_max_cool
- cold / thand / shivering    -> ac_set_temperature 27, fan speed 2
- humid / muggy / stuffy      -> ac_dry
- sleeping / good night / sona -> good_night
- movie / gaming / film       -> movie_time
- leaving / everything off    -> all_off

Only these devices exist: fan, AC, TV, three lights. Never invent others."""


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
    " conditional actions (e.g., 'If the weather is rainy, turn off sprinklers'"
    " requires checking the weather first). You may filter for devices by name,"
    " domain, and area, including combining those filters. Prefer filtering by domain"
    " when searching for multiple devices of the same type.",
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

# Tools Home Assistant always contributes for these exposed domains.
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

SCRIPTS_FULL = [
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

# The brief's intended 9 entities: 5 scripts + fan + 3 lights.
SCRIPTS_BRIEF = [
    tool("ac_off", "Turn the air conditioner off.", NONE_P),
    tool("ac_on", "Turn the air conditioner on at a comfortable temperature.", NONE_P),
    AC_SET_T,
    tool("fan_boost", "Put the fan on boost / turbo -- the highest setting, above speed 5.", NONE_P),
    tool("tv_toggle", "Turn the TV or monitor on or off. One button, so this always toggles.", NONE_P),
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


def score(name, system, tools, num_ctx):
    print(f"\n===== {name}  ({len(tools)} tools, num_ctx={num_ctx}) =====")
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
        # A reply that contains braces or a tool name is JSON leaking into speech.
        leak = ("{" in content) or any(t["function"]["name"] in content for t in tools)
        ok += hit
        leaked += leak
        flag = "PASS" if hit and not leak else ("LEAK" if leak else "FAIL")
        print(f"  [{flag}] {utt!r:36} -> calls={sorted(got) or '(none)'}  {round(ms)}ms")
        if leak:
            print(f"          leaked: {content[:110]!r}")
    print(f"  ---- {ok}/{len(TESTS)} correct tool call, {leaked} leaked JSON into speech")
    print(f"  ---- prompt tokens {ptoks}, median latency {round(sorted(lat)[len(lat)//2])} ms")
    return ok, leaked


if __name__ == "__main__":
    cur = CURRENT_PROMPT + "\n" + HA_BOILERPLATE
    new = NEW_PROMPT + "\n" + HA_BOILERPLATE
    score("A. AS DEPLOYED TODAY (10 scripts, current prompt)", cur, BASE + SCRIPTS_FULL, 2048)
    score("B. AS DEPLOYED, num_ctx raised to 4096", cur, BASE + SCRIPTS_FULL, 4096)
    score("C. BRIEF'S 9 ENTITIES (5 scripts), current prompt", cur, BASE + SCRIPTS_BRIEF, 4096)
    score("D. BRIEF'S 9 ENTITIES, NEW PROMPT", new, BASE + SCRIPTS_BRIEF, 4096)
    score("E. FULL SCRIPTS + NEW PROMPT", new, BASE + SCRIPTS_FULL, 4096)
