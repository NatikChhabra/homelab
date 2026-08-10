"""Read-only benchmark round 3.

Rounds 1-2 pinned the failure down: nearly every leak of tool JSON into speech
involves HassFanSetSpeed, the only tool carrying both the five-field target
schema AND a required `percentage`. Round 3 tests whether removing it -- by not
exposing fan.fan and driving the fan through a one-parameter script instead --
stops the leaking.

Also tests marking the ac_set_temperature `temperature` field required, since
today it is optional and the model may omit it.

Nothing is written; Home Assistant is not reconfigured.
"""

import json
import time
import urllib.request

OLLAMA = "http://jarvis-ollama:11434/api/chat"
MODEL = "qwen2.5:3b"


def boilerplate(include_fan: bool) -> str:
    """HA's own prompt. The Fan line and HassFanSetSpeed appear only if fan.fan is exposed."""
    ctx = (
        "You ARE equipped to answer questions about the current state of\n"
        "the home using the `GetLiveContext` tool. This is a primary function."
        " Do not state you lack the\nfunctionality if the question requires live data.\n"
        "If the user asks about device existence/type (e.g., \"Do I have lights in the"
        " bedroom?\"): Answer\nfrom the static context below.\n"
        "If the user asks about the CURRENT state, value, or mode (e.g., \"Is the lock"
        " locked?\",\n\"Is the fan on?\", \"What mode is the thermostat in?\"):\n"
        "    1.  Recognize this requires live data.\n"
        "    2.  You MUST call `GetLiveContext`.\n"
        "    3.  Use the tool's response** to answer the user accurately.\n"
        "For general knowledge questions not about the home: Answer truthfully from"
        " internal knowledge.\n"
        "Static Context: An overview of the areas and the devices in this smart home:\n"
    )
    if include_fan:
        ctx += "- names: Fan\n  domain: fan\n  areas: Bedroom\n"
    ctx += (
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
    return ctx


PROMPT = """You are JARVIS, Natik's assistant. You control one bedroom, in India.

Your reply is SPOKEN aloud. Answer in ONE short sentence, at most 8 words.
Never use markdown, lists, emoji, braces, JSON, or any internal tool or entity name.
Good: "Lights on." / "AC set to 22." / "Fan up." / "The TV is not connected."
Bad: "I have confirmed that I have turned on the lights for you."

Be brief, competent and dry. Never chirpy, never apologetic, never enthusiastic.

Natik speaks English, Hindi and Hinglish. Understand all three.
ALWAYS reply in English. Never reply in Hindi.

Act, do not ask, whenever the intent is clear. Ask only when a real choice is
missing. When Natik says how he FEELS, change the room yourself and never ask
which device to use.

Only a fan, an air conditioner, a TV and three lights exist here. If asked for
anything else, say plainly that it is not connected. Never invent a device."""


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

HASS_FAN_SET_SPEED = tool(
    "HassFanSetSpeed",
    "Sets a fan's speed by percentage",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "area": {"type": "string"},
            "floor": {"type": "string"},
            "domain": {"type": "array", "items": {"type": "string"}},
            "device_class": {"type": "array", "items": {"type": "string"}},
            "percentage": {"type": "integer", "description": "The speed percentage of the fan"},
        },
        "required": ["percentage"],
    },
)

CORE = [
    tool("HassTurnOn", "Turns on/opens/presses a device or entity. For locks, this performs a 'lock' action."),
    tool("HassTurnOff", "Turns off/closes a device or entity. For locks, this performs an 'unlock' action."),
    tool("HassLightSet", "Sets the brightness or color of a light"),
    GET_LIVE_CONTEXT,
]

FAN_SET = tool(
    "fan_set",
    "Set the ceiling fan speed, or turn the fan on or off. Use for 'fan on', 'fan off',"
    " 'fan speed 3', 'slow the fan down', 'speed up the fan', 'pankha chalu karo',"
    " 'pankha band karo', 'pankha tez karo', 'pankha dheema karo', 'fan tez karo'.",
    {
        "type": "object",
        "properties": {
            "level": {
                "type": "integer",
                "description": "0 turns the fan off, 1 is slowest, 5 is fastest, 6 is boost.",
            }
        },
        "required": ["level"],
    },
)


def ac_set_t(required: bool):
    return tool(
        "ac_set_temperature",
        "Set the air conditioner temperature in Celsius, 16 to 30, and turn the AC on."
        " Use whenever Natik is warm or cold and names no device: 'I'm hot', 'I'm cold',"
        " 'garmi lag rahi hai', 'thand lag rahi hai', 'mujhe garmi lag rahi hai',"
        " 'mujhe thand lag rahi hai', 'thoda thanda karo', 'make it warmer',"
        " 'make it cooler', 'AC 22 kar do'.",
        {
            "type": "object",
            "properties": {
                "temperature": {
                    "type": "integer",
                    "description": "Target temperature in Celsius, 16 to 30. Use 22 when he is hot, 27 when he is cold.",
                }
            },
            "required": ["temperature"] if required else [],
        },
    )


def scripts(required_temp=True):
    return [
        tool("ac_dry", "Switch the air conditioner to dry or dehumidify mode. Use when the room feels stuffy, damp, humid, muggy, sticky, close, airless, 'humid lag raha hai', or during the monsoon.", NONE_P),
        tool("ac_max_cool", "Cool the room as fast as possible: AC at its lowest temperature with the fan high. Use for 'it's boiling', 'I'm burning up', 'bahut garmi hai', 'bohot garmi lag rahi hai', 'jaldi thanda karo', 'max cooling'.", NONE_P),
        tool("ac_off", "Turn the air conditioner off. Use for 'turn off the AC', 'AC band karo', 'AC bandh kar do'.", NONE_P),
        tool("ac_on", "Turn the air conditioner on at a comfortable temperature. Use for 'turn on the AC', 'AC chalu karo', 'AC on kar do'.", NONE_P),
        ac_set_t(required_temp),
        tool("all_off", "Shut the whole room down: every light, the fan and the AC. Use for 'turn everything off', 'shut it all down', \"I'm leaving\", 'sab band kar do', 'sab kuch band kar do'.", NONE_P),
        tool("fan_boost", "Put the fan on boost or turbo, the highest setting above speed 5. Use for 'boost the fan', 'fan turbo', 'fan full blast', 'fan full speed'.", NONE_P),
        tool("good_night", "Bedtime: lights off, comfortable sleeping temperature, quiet fan. Use for 'good night', \"I'm going to sleep\", 'sone ja raha hoon', 'so raha hoon', 'bedtime', 'sona hai'.", NONE_P),
        tool("movie_time", "Movie or gaming mode: TV on, most lights off, comfortable temperature. Use for 'movie time', \"let's watch something\", 'gaming', 'film dekhni hai', 'movie lagao'.", NONE_P),
        tool("tv_toggle", "Turn the TV or monitor on or off. The remote has ONE button so this always toggles. Use for 'turn the TV on', 'turn the TV off', 'TV chalu karo', 'TV band karo'.", NONE_P),
    ]


FAN_OK = {"HassFanSetSpeed", "fan_set", "fan_boost"}
TESTS = [
    ("turn on the lights", {"HassTurnOn"}),
    ("turn off the lights", {"HassTurnOff"}),
    ("set the AC to 22", {"ac_set_temperature"}),
    ("fan speed 3", FAN_OK),
    ("turn the TV on", {"tv_toggle"}),
    ("boost the fan", {"fan_boost", "fan_set", "HassFanSetSpeed"}),
    ("turn the AC off", {"ac_off"}),
    ("turn the fan off", FAN_OK | {"HassTurnOff"}),
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
    ("open the curtains", set()),  # no such device: must NOT call a tool
]


def run(system, tools, user):
    body = {
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "tools": tools,
        "stream": False,
        "options": {"num_ctx": 4096, "temperature": 0},
    }
    req = urllib.request.Request(
        OLLAMA, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r), (time.time() - t0) * 1000


def score(name, system, tools):
    print(f"\n===== {name}  ({len(tools)} tools) =====")
    ok = leaked = 0
    lat = []
    ptoks = None
    for utt, expected in TESTS:
        res, ms = run(system, tools, utt)
        msg = res.get("message", {})
        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        ptoks = res.get("prompt_eval_count")
        lat.append(ms)
        got = {c["function"]["name"] for c in calls}
        hit = (not expected and not got) or bool(got & expected)
        leak = ("{" in content) or any(t["function"]["name"] in content for t in tools)
        ok += hit
        leaked += leak
        flag = "PASS" if hit and not leak else ("LEAK" if leak else "FAIL")
        print(f"  [{flag}] {utt!r:30} -> {sorted(got) or '(none)'}  {round(ms)}ms")
        if leak:
            print(f"          leaked: {content[:95]!r}")
        elif not hit:
            print(f"          said:   {content[:95]!r}")
    print(f"  ---- {ok}/{len(TESTS)} correct, {leaked} leaked | {ptoks} prompt tokens"
          f" | median {round(sorted(lat)[len(lat)//2])} ms")


if __name__ == "__main__":
    with_fan = PROMPT + "\n" + boilerplate(True)
    no_fan = PROMPT + "\n" + boilerplate(False)

    score("J. fan.fan EXPOSED (HassFanSetSpeed present) + fan_boost",
          with_fan, CORE + [HASS_FAN_SET_SPEED] + scripts())
    score("K. fan.fan NOT exposed -> fan_set script instead",
          no_fan, CORE + [FAN_SET] + scripts())
    score("L. BOTH HassFanSetSpeed and fan_set (worst case duplication)",
          with_fan, CORE + [HASS_FAN_SET_SPEED, FAN_SET] + scripts())
    score("M. K config but temperature OPTIONAL (as jarvis.yaml has it today)",
          no_fan, CORE + [FAN_SET] + scripts(required_temp=False))
