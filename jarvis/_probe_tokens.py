"""Read-only probe: measure the real prompt token cost of the current JARVIS agent.

Reconstructs the system prompt Home Assistant builds (custom prompt + Assist API
static context + device-control prompt + datetime) and the tool list, then asks
Ollama for prompt_eval_count. Nothing is written and no device is touched.
"""

import json
import urllib.request

OLLAMA = "http://localhost:11434/api/chat"
MODEL = "qwen2.5:3b"

# --- The prompt currently stored in the Ollama conversation subentry ---------
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

# --- What HA appends: homeassistant/llm.py DYNAMIC_CONTEXT_PROMPT ------------
DYNAMIC_CONTEXT_PROMPT = (
    "You ARE equipped to answer questions about the current state of\n"
    "the home using the `GetLiveContext` tool. This is a primary function."
    " Do not state you lack the\nfunctionality if the question requires live data.\n"
    "If the user asks about device existence/type (e.g., \"Do I have lights in the bedroom?\"): Answer\n"
    "from the static context below.\n"
    "If the user asks about the CURRENT state, value, or mode (e.g., \"Is the lock locked?\",\n"
    "\"Is the fan on?\", \"What mode is the thermostat in?\", \"What is the temperature outside?\"):\n"
    "    1.  Recognize this requires live data.\n"
    "    2.  You MUST call `GetLiveContext`. This tool will provide the needed real-time"
    " information (like temperature from the local weather, lock status, etc.).\n"
    "    3.  Use the tool's response** to answer the user accurately"
    " (e.g., \"The temperature outside is [value from tool].\").\n"
    "For general knowledge questions not about the home: Answer truthfully from internal knowledge.\n"
)

STATIC_CONTEXT = """Static Context: An overview of the areas and the devices in this smart home:
- names: Fan
  domain: fan
  areas: Bedroom
- names: Gang 1
  domain: light
  areas: Bedroom
- names: Gang 2
  domain: light
  areas: Bedroom
- names: Gang 3
  domain: light
  areas: Bedroom
"""

DEVICE_CONTROL = (
    "When controlling Home Assistant always call the intent tools. "
    "Use HassTurnOn to lock and HassTurnOff to unlock a lock. "
    "When controlling a device, prefer passing just name and domain. "
    "When controlling an area, prefer passing just area name and domain.\n"
    "You are in area Bedroom and all generic commands like 'turn on the lights'"
    " should target this area.\nThis device is not able to start timers."
)

DATE_TIME = "Current time is 20:45:00. Today's date is 2026-08-08.\n"

SYSTEM = "\n".join(
    [CURRENT_PROMPT, DYNAMIC_CONTEXT_PROMPT, STATIC_CONTEXT, DEVICE_CONTROL, DATE_TIME]
)


def target_schema():
    """The name/area/floor/domain/device_class shape HA gives intent tools."""
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
        "function": {
            "name": name,
            "description": desc,
            "parameters": params or target_schema(),
        },
    }


TOOLS = [
    tool("HassTurnOn", "Turns on/opens/presses a device or entity. For locks, this performs a 'lock' action."),
    tool("HassTurnOff", "Turns off/closes a device or entity. For locks, this performs an 'unlock' action."),
    tool("HassCancelAllTimers", "Cancels all timers"),
    tool("HassLightSet", "Sets the brightness or color of a light"),
    tool("HassFanSetSpeed", "Sets a fan's speed by percentage"),
    tool(
        "GetLiveContext",
        "Provides real-time information about the CURRENT state, value, or mode of devices,"
        " sensors, entities, or areas. Use this tool for: 1. Answering questions about current"
        " conditions (e.g., 'Is the light on?'). 2. As the first step in conditional actions"
        " (e.g., 'If the weather is rainy, turn off sprinklers' requires checking the weather"
        " first). You may filter for devices by name, domain, and area, including combining"
        " those filters. Prefer filtering by domain when searching for multiple devices of the"
        " same type.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filter entities by name or alias (case-insensitive)."},
                "domain": {"type": "string", "description": "Filter entities by domain (e.g. 'light', 'sensor'). Accepts a single domain or a list."},
                "area": {"type": "string", "description": "Filter entities by area name or alias (case-insensitive)."},
            },
            "required": [],
        },
    ),
    # The nine exposed scripts, each a ScriptTool named after the object_id.
    tool("ac_dry", "Switch the air conditioner to dry / dehumidify mode. Use when the room feels damp, humid, or muggy.", {"type": "object", "properties": {}, "required": []}),
    tool("ac_max_cool", "Cool the room as fast as possible -- AC to its lowest temperature with the fan high. Use for 'it's boiling', 'max cooling', 'cool it down fast'.", {"type": "object", "properties": {}, "required": []}),
    tool("ac_off", "Turn the air conditioner off.", {"type": "object", "properties": {}, "required": []}),
    tool("ac_on", "Turn the air conditioner on at a comfortable temperature.", {"type": "object", "properties": {}, "required": []}),
    tool(
        "ac_set_temperature",
        "Set the air conditioner temperature in Celsius, 16 to 30. Also turns the AC on. Use for any request to make the room warmer or cooler.",
        {"type": "object", "properties": {"temperature": {"type": "number", "description": "Target temperature in Celsius, 16 to 30"}}, "required": []},
    ),
    tool("all_off", "Shut the whole room down -- every light, the fan and the AC. Use for 'turn everything off', 'shut it all down', \"I'm leaving\", 'sab band kar do'.", {"type": "object", "properties": {}, "required": []}),
    tool("fan_boost", "Put the fan on boost / turbo -- the highest setting, above speed 5. Use for 'boost the fan', 'fan turbo', 'fan full blast'.", {"type": "object", "properties": {}, "required": []}),
    tool("good_night", "Bedtime. Turns the lights off, sets a comfortable sleeping temperature and a quiet fan speed. Use for 'good night', \"I'm going to sleep\", 'sone ja raha hoon', 'bedtime'.", {"type": "object", "properties": {}, "required": []}),
    tool("movie_time", "Movie or gaming mode. Turns the TV on, dims the room by turning most lights off, and sets a comfortable temperature. Use for 'movie time', \"let's watch something\", 'gaming'.", {"type": "object", "properties": {}, "required": []}),
    tool("tv_toggle", "Turn the TV or monitor on or off. The remote uses ONE button for both, so this always toggles -- there is no separate on and off. Use it for any request to turn the TV on or off.", {"type": "object", "properties": {}, "required": []}),
]


def ask(system, tools, user, num_ctx):
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
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


if __name__ == "__main__":
    print("system prompt chars:", len(SYSTEM))
    print("tools:", len(TOOLS))
    print()
    for ctx in (2048, 4096):
        res = ask(SYSTEM, TOOLS, "I'm feeling hot", ctx)
        msg = res.get("message", {})
        print(f"--- num_ctx={ctx} ---")
        print("  prompt_eval_count :", res.get("prompt_eval_count"), "tokens")
        print("  eval_count        :", res.get("eval_count"))
        print("  total_duration_ms :", round(res.get("total_duration", 0) / 1e6))
        print("  tool_calls        :", json.dumps(msg.get("tool_calls")))
        print("  content           :", repr(msg.get("content"))[:200])
        print()
