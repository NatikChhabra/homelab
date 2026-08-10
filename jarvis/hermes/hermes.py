"""
Hermes: specialist routing for the LLM leg of JARVIS.

Where this sits
---------------
    utterance
      -> phrasing.normalise            (free, string rewrite)
      -> conversation.home_assistant   (~18 ms, no model)     <- 80% of commands
      -> HERMES                        (this file)            <- the rest
           -> classify to ONE specialist   (keyword, ~0 ms; model only if unsure)
           -> hermes3:3b with THAT specialist's 3-6 tools and its own prompt
           -> execute the tool call against Home Assistant

The local-first router already removes most traffic. What reaches the model is
the hard remainder: ambiguous targets, argument extraction, and novel phrasings.
Hermes exists to make the model good at exactly that.

Why specialists rather than one agent
-------------------------------------
Two measurements from this build drive the whole design.

1. THE TOOL CLIFF. Accuracy collapses as the tool count grows. 30+ exposed tools
   broke the 3B model outright; at ~13 it scored 4/4 and reasoned unprompted.
   The live agent currently offers 17. A specialist offers 3-6.

2. PHANTOM ACTIONS ARE A PROMPT/TOOL MISMATCH. `dry` ("it's humid in here")
   failed 8 out of 8 times because the system prompt's comfort table paired
   humidity with a fan action while the tool set had no fan tool. The model
   composed the plan the prompt taught it, could not execute half of it, and
   NARRATED instead of acting.

   The consequence is a hard rule, and it is the single most expensive lesson in
   this project: EVERY SPECIALIST MUST SHIP A SYSTEM PROMPT SCOPED TO EXACTLY ITS
   OWN TOOLS. A specialist inheriting the full JARVIS prompt will hallucinate
   actions for tools it does not have. `PROMPTS` below is built from `TOOLS`
   programmatically so the two cannot drift apart.

Why one model and not several
-----------------------------
4 GB of VRAM, already shared by hermes3:3b (~2.2 GB) and CUDA Whisper. There is
no room for a second set of weights. "Specialists" here means one model reached
through different prompts and different tool subsets -- shared weights, swapped
context. That is the only shape this hardware allows.

Classification is keyword-first for the same reason routing is: a keyword match
costs nothing, and asking a 3B model which specialist to use would pay the full
model latency twice for one command.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

log = logging.getLogger("jarvis.hermes")

OLLAMA_URL = "http://jarvis-ollama:11434"
MODEL = "qwen2.5:3b"
# 2048, quartered from 8192. Hermes sends only system (~700 tokens) + user, no
# history, so the happy-path cost is unchanged while ~600MB of VRAM is freed
# on the 4GB RTX 3050 where Ollama, Whisper CUDA, and Kokoro CUDA all share
# the same memory pool. Without this, Ollama's model gets evicted every few
# minutes and the first user command pays a 5-8s cold-start penalty.
NUM_CTX = 2048

# num_predict is a HARD CAP on ALL generated tokens, the tool-call JSON
# included. Actuation commands measure a max of 48 out-tokens, so 150 is ~3x
# headroom and caps the phantom-narration failure mode at ~2s instead of the
# full context (60s+). But two tools carry UNBOUNDED free text -- wa_send's
# message and pc_type's text -- and that text rides inside the tool-call JSON.
# Capping those at 150 truncates the JSON mid-string: Ollama returns no tool
# call or broken arguments, router.py falls through to the 17-tool big agent,
# and an agent holding no messaging tool narrates "I've sent that, sir" for a
# message that never dispatched -- the phantom-action lie. A specialist that
# exposes a free-text tool gets 512 instead: enough for a message at the
# bridge's 1000-char refusal limit plus JSON framing and the spoken reply,
# while still capping a runaway narration rather than burning the whole 4096
# window.
_NUM_PREDICT_ACTUATION = 150
_NUM_PREDICT_FREE_TEXT = 512
_FREE_TEXT_TOOLS = {"wa_send", "pc_type"}


# --- tool schemas ----------------------------------------------------------
# Shapes copied from what HA actually puts on the wire (captured by the logging
# proxy at jarvis-voice:11435), not from the docs. Tool schemas cost ~89 tokens
# each; a 6-tool specialist spends ~530 of the 4096 window instead of the 1513
# the 17-tool agent spends.

def _tool(name: str, desc: str, params: dict) -> dict:
    return {"type": "function",
            "function": {"name": name, "description": desc, "parameters": params}}


_NONE: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
_TARGET = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "entity name, e.g. 'light one'"},
        "area": {"type": "string"},
        "domain": {"type": "array", "items": {"type": "string"}},
    },
    "required": [],
}

TOOLS: dict[str, dict] = {
    "HassTurnOn": _tool("HassTurnOn", "Turn on / open a device.", _TARGET),
    "HassTurnOff": _tool("HassTurnOff", "Turn off / close a device.", _TARGET),
    "HassLightSet": _tool("HassLightSet", "Set a light's brightness.", {
        "type": "object",
        "properties": {
            "name": {"type": "string"}, "area": {"type": "string"},
            "brightness": {"type": "integer", "description": "0-100 percent"},
        },
        "required": [],
    }),
    "ac_set_temperature": _tool(
        "ac_set_temperature",
        "Set the air conditioner temperature in Celsius. Also turns the AC on.", {
            "type": "object",
            "properties": {"temperature": {
                "type": "integer",
                "description": "Target temperature, an INTEGER between 16 and 30. Never a string.",
            }},
            "required": ["temperature"],
        }),
    "ac_on": _tool("ac_on", "Turn the air conditioner on.", _NONE),
    "ac_off": _tool("ac_off", "Turn the air conditioner off.", _NONE),
    "ac_max_cool": _tool("ac_max_cool", "Maximum cooling. Use for extreme heat.", _NONE),
    "ac_dry": _tool("ac_dry", "Dry / dehumidify mode. Use when the air is humid, sticky or muggy.", _NONE),
    "fan_set": _tool(
        "fan_set",
        "Set the ceiling fan speed. level is REQUIRED; never omit it.", {
            "type": "object",
            "properties": {"level": {
                "type": "integer",
                "description": (
                    "REQUIRED. 0 turns the fan off, 1-5 are the speeds, "
                    "6 is boost. An INTEGER. Never omit this argument."
                ),
            }},
            "required": ["level"],
        }),
    "fan_boost": _tool("fan_boost", "Ceiling fan boost mode, faster than speed 5.", _NONE),
    "tv_toggle": _tool("tv_toggle", "Toggle the TV. It has one button, so this both turns it on and off.", _NONE),
    "good_night": _tool("good_night", "Whole-room night routine. Use for 'good night' or going to bed.", _NONE),
    "all_off": _tool("all_off", "Turn everything in the room off. Use for leaving.", _NONE),
    "movie_time": _tool("movie_time", "Movie-watching scene.", _NONE),
    "GetLiveContext": _tool(
        "GetLiveContext",
        "Read the CURRENT state of a device. Use before answering a question about what is on.",
        {
            "type": "object",
            "properties": {
                "device": {
                    "type": "string",
                    "description": (
                        "Which device to read, e.g. 'fan', 'ac', 'light one', "
                        "'light two', 'light three', or 'lights'. Omit to read the whole room."
                    ),
                },
            },
            "required": [],
        }),

    # --- PC control -------------------------------------------------------
    # These reach the host-side pc-agent (host-helper/pc-agent.ps1), NOT Home
    # Assistant. They are grouped deliberately, following the measured rule from
    # this project: GROUP argument-free, lexically-distinct actions into one
    # action-enum tool; keep a SEPARATE tool wherever an argument rides along
    # with the choice. `set the AC to 22` proved the second half -- a grouped
    # action tool emitted action=on and dropped the temperature.
    #
    # So: pc_power and pc_media are enum-only (no argument rides along). Volume
    # LEVEL, app NAME, and typed TEXT each get their own tool because the
    # argument is the whole point of the command.
    "pc_power": _tool("pc_power", "Control PC power: sleep, lock the screen, shut down, restart, or wake it.", {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["sleep", "lock", "shutdown", "restart", "wake"],
                       "description": "What to do to the PC."},
            "confirm": {"type": "boolean",
                        "description": "Set true ONLY for shutdown or restart, and only when the user clearly asked to power the machine off or reboot it."},
        },
        "required": ["action"],
    }),
    "pc_media": _tool(
        "pc_media",
        "Media keys on the PC. play_pause toggles playback; it cannot force play or pause.", {
            "type": "object",
            "properties": {"action": {
                "type": "string",
                "enum": ["play_pause", "next", "previous", "stop", "mute",
                         "volume_up", "volume_down"],
                "description": (
                    "play_pause toggles playback (cannot force play or pause); "
                    "volume_up/volume_down nudge the level a little."
                ),
            }},
            "required": ["action"],
        }),
    "pc_set_volume": _tool("pc_set_volume", "Set the PC volume to an exact level.", {
        "type": "object",
        "properties": {"level": {
            "type": "integer",
            "description": "Target volume, an INTEGER between 0 and 100. Never a string.",
        }},
        "required": ["level"],
    }),
    "pc_app": _tool("pc_app", "Open or close a PC application by name.", {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["open", "close"]},
            "name": {"type": "string",
                     "description": "Friendly app name, e.g. 'chrome', 'spotify', 'notepad'. Only allow-listed apps work."},
        },
        "required": ["action", "name"],
    }),
    "pc_type": _tool("pc_type", "Type text on the PC into whatever window has focus.", {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The literal text to type."}},
        "required": ["text"],
    }),
    "pc_status": _tool(
        "pc_status",
        "Read the PC's state: whether it is awake, uptime, battery, logged-in user, active window, and which allow-listed apps are running. Use before answering a question about the PC.",
        _NONE),

    # --- WhatsApp ---------------------------------------------------------
    # These reach the Baileys bridge (wa/server.js) on 8097, not Home Assistant.
    #
    # `contact` is described as a NAME on purpose and the schema offers no field
    # for a number. The bridge refuses anything that is not on its allowlist, so
    # this is belt and braces -- but the description is what stops the model
    # trying in the first place, and a model that never attempts a phone number
    # never produces a confusing refusal to explain out loud.
    "wa_send": _tool("wa_send", "Send a WhatsApp message to one of Natik's saved contacts.", {
        "type": "object",
        "properties": {
            "contact": {"type": "string",
                        "description": "Who to message, by NAME only, e.g. 'mum'. Never a phone number."},
            "message": {"type": "string",
                        "description": "The exact words to send. Write it as Natik would say it, not as a description of it."},
        },
        "required": ["contact", "message"],
    }),
    "wa_read": _tool(
        "wa_read",
        "Read Natik's most recent WhatsApp messages. Use whenever he asks what messages he has or who messaged him.",
        _NONE),
}


# --- specialists -----------------------------------------------------------
# Each is 3-6 tools. `role` is one line; the rest of the prompt is generated
# from the tool list so a prompt can never mention a tool the specialist lacks.

@dataclass
class Specialist:
    name: str
    tools: list[str]
    role: str
    keywords: list[str]
    # Words that make this specialist WRONG even if a keyword matched. "fan" in
    # "fan of this song" is not a fan command; more usefully, "AC" appears in
    # both climate and comfort requests and the tie is broken by intent words.
    negative: list[str] = field(default_factory=list)
    # True for a specialist with NO tools ("chat"): it exists to answer social
    # and info utterances conversationally, so a no-tool-call reply is its win,
    # not a fall-through. The router reads this to let questions through and to
    # accept speech without a tool call.
    talk_only: bool = False


SPECIALISTS: dict[str, Specialist] = {
    "lights": Specialist(
        name="lights",
        tools=["HassTurnOn", "HassTurnOff", "HassLightSet", "GetLiveContext"],
        role="You control ONLY the three bedroom lights.",
        keywords=["light", "lights", "lamp", "batti", "roshni", "bulb",
                  "brighter", "dimmer", "dim", "bright", "gang",
                  "dark", "darken"],
    ),
    "climate": Specialist(
        name="climate",
        # GetLiveContext added 2026-08-09: without it the router classified an
        # AC question to climate, saw no read tool, and refused Hermes with
        # reason="question" -- sending "what is the ac doing" to the 15s LLM leg,
        # which timed out. A read tool here lets the executor answer from real
        # HA state instead ("The AC is on at 25 degrees"). 6 tools, same as
        # comfort, still inside the measured-safe band.
        tools=["ac_set_temperature", "ac_on", "ac_off", "ac_max_cool", "ac_dry",
               "GetLiveContext"],
        role="You control ONLY the air conditioner.",
        keywords=["ac", "a/c", "air con", "aircon", "air conditioner", "cooling",
                  "degrees", "temperature", "temp", "humid", "sticky", "muggy", "dry",
                  "cooler", "warmer", "cool down", "warm up"],
    ),
    "fan": Specialist(
        name="fan",
        tools=["fan_set", "fan_boost", "GetLiveContext"],
        role="You control ONLY the ceiling fan.",
        # "some air"/"fresh air" etc. as phrases, NOT a bare "air": a bare
        # "air" ties with comfort on "the air is hot" (both score 14) and wins
        # by iteration order, routing a cooling complaint to the fan. The phrase
        # forms score 28+, comfortably ahead, and never collide with "air
        # conditioner" (climate scores 36).
        keywords=["fan", "pankha", "breeze", "airflow", "speed", "boost", "turbo",
                  "some air", "fresh air", "need air", "get air"],
        negative=["fan of"],
    ),
    "scenes": Specialist(
        name="scenes",
        tools=["good_night", "all_off", "movie_time", "tv_toggle"],
        role="You run ONLY whole-room routines and the TV.",
        keywords=["good night", "goodnight", "sleep", "bed", "sona", "sone",
                  "everything", "all off", "sab band", "leaving", "movie", "film",
                  "tv", "television", "watch"],
        # Verified 2026-08-09: "put the pc to sleep" fired good_night. "sleep"
        # scored 16 against scenes while "pc" scored 13 against pc, and scenes
        # had no negative list to reject it. The PC owns the sleep verb when a
        # machine word is present.
        negative=["pc", "computer", "laptop", "desktop"],
    ),
    # The PC specialist. Its six tools reach the host-side pc-agent over HTTP,
    # NOT Home Assistant, so it never shares a tool with any other specialist and
    # a PC utterance can never misfire a light or the AC. Six tools is at the top
    # of the measured-safe band and every one is load-bearing; do not add a
    # seventh without dropping one, or this slides toward the tool cliff.
    #
    # `negative` guards the two words that collide with the room. "sleep" is a
    # scene here ("good night"), not PC sleep, unless "pc"/"computer"/"laptop" is
    # present -- the keyword list requires that context. "lock" never means a
    # device in this house, so it is safe as a bare keyword.
    "pc": Specialist(
        name="pc",
        tools=["pc_power", "pc_media", "pc_set_volume", "pc_app", "pc_type", "pc_status"],
        role="You control ONLY Natik's Windows PC: its power, media, volume, apps, and typing.",
        keywords=["pc", "computer", "laptop", "desktop", "machine", "screen lock",
                  "lock the screen", "lock screen", "shut down", "shutdown", "reboot",
                  "restart", "wake", "spotify", "chrome", "browser", "notepad",
                  "volume", "louder", "quieter", "play", "pause", "next track",
                  "previous track", "skip", "mute", "unmute", "type", "launch", "open app",
                  # Verified 2026-08-09: "next song", "open youtube", "stop the
                  # music" all fell to the comfort default with no tool. Bare
                  # "next"/"previous"/"open"/"stop" are safe here: pc.negative
                  # already rejects fan/light/ac/good night plus the TV words
                  # (tv/netflix/movie), so "stop the fan" still reaches the fan
                  # specialist and "open the tv" still reaches scenes.
                  "music", "song", "next", "previous", "open", "stop",
                  # "put the pc to sleep" must beat scenes' bare "sleep" (which
                  # owns the bedtime routine): the multi-word forms score 41 to
                  # scenes' 16, and scenes.negative already rejects machine
                  # words. Bare "sleep" still routes to scenes -- it ties at 16
                  # and loses on dict order, which is the intended bedtime.
                  "sleep", "put to sleep", "sleep the pc",
                  # "open whatsapp" is an app launch (pc owns pc_app, and
                  # wa_send/wa_read live in comms, not here); the multi-word
                  # form scores 34 against comms' bare "whatsapp" at 20, so the
                  # PC wins the open intent while "send whatsapp" stays comms.
                  "open whatsapp", "launch whatsapp"],
        # "whatsapp" was REMOVED from negative 2026-08-09. It used to block the
        # PC because pc_type and wa_send are one mishearing apart -- "type a
        # message" is a keyboard command, "send Mum a message" is not. The
        # multi-word keywords now separate them: only explicit "open whatsapp" /
        # "launch whatsapp" route to the PC, and every send/read phrasing still
        # lands in comms, whose own negative rejects "type"/"typing".
        negative=["fan", "light", "lights", "ac", "air conditioner", "good night",
                  "goodnight",
                  # TV words are the TV's, not the PC's. Without them the bare
                  # keywords above misroute: 'open the tv' -> pc_app open 'tv'
                  # (a refusal), 'stop the tv' -> pc_media stop while the TV
                  # stays on, 'open netflix' -> pc_app. All three must reach
                  # scenes (tv_toggle / movie_time). Verified live 2026-08-09.
                  "tv", "netflix", "movie"],
    ),
    # Messaging. Two tools, the smallest specialist here, and that is right:
    # sending a WhatsApp message is the only irreversible action JARVIS can
    # take. Everything else in this house can be undone by saying the opposite.
    #
    # The recipient is NOT resolved here. The bridge holds contacts.json and
    # refuses any name not on it, so a mishearing produces a refusal rather than
    # a message to a stranger. Deliberately no fuzzy matching anywhere in that
    # path -- unlike Resolve-App on the PC agent, where guessing wrong merely
    # opens the wrong allowed program.
    #
    # `negative` carries "type" because the PC specialist owns pc_type and
    # "type out a message" is a keyboard command, not a WhatsApp one.
    "comms": Specialist(
        name="comms",
        tools=["wa_send", "wa_read"],
        role="You send and read Natik's WhatsApp messages.",
        keywords=["whatsapp", "message", "messages", "msg", "text her", "text him",
                  "text mum", "text dad", "send", "reply", "unread", "bhej",
                  "bhejo", "sandesh", "who messaged", "any messages",
                  "text me", "texted", "texts", "anyone text", "notify",
                  "send whatsapp", "whatsapp message"],
        negative=["type", "typing", "light", "fan", "ac", "volume"],
    ),
    # The generalist. Reached when nothing matched, and for comfort requests
    # that legitimately span the AC and the fan together. It is the ONLY
    # specialist allowed more than one device, and it is deliberately the
    # largest at 6 tools -- still a third of the 17 the single agent carries.
    "comfort": Specialist(
        name="comfort",
        tools=["ac_set_temperature", "ac_max_cool", "ac_dry", "fan_set",
               "fan_boost", "GetLiveContext"],
        role="You make the room comfortable, using the air conditioner and the ceiling fan together.",
        # sweat/pasina/sweaty were missing -- "pasina aagya" fell to the comfort
        # default by accident of matching "default", and "i'm sweating" only
        # matched "sweating". humid/sticky/muggy mirror climate on purpose:
        # climate wins the tie (earlier in the list, owns ac_dry), comfort still
        # catches the combined AC+fan phrasing.
        keywords=["hot", "cold", "warm", "freezing", "boiling", "sweating",
                  "sweat", "sweaty", "pasina", "garmi", "thand", "stuffy",
                  "airless", "humid", "sticky", "clammy", "muggy",
                  "comfortable", "uncomfortable",
                  # comfortOpts additions: breathing/hawa and gentle warming.
                  # "can't breathe" matches because classify() keeps the
                  # apostrophe (see classify); "warmer" ties with climate and
                  # loses on dict order, which is correct -- climate owns the
                  # AC and the raise-it request.
                  "can't breathe", "cannot breathe", "hawa", "hawa nahi",
                  "no air", "less cold", "thanda kam"],
    ),
    # The talker. Zero tools by design. "what's the weather", "tell me a joke"
    # and "who are you" used to fall through to comfort's 6 tools, where the
    # model -- handed an AC and a fan -- answered conversationally at best and
    # phantom-fired ac_set_temperature at worst. A specialist with NO tools
    # cannot phantom-fire. Placed LAST so every device specialist wins any
    # keyword tie on dict order; only utterances nothing else claims land here.
    # talk_only lets the router accept its no-tool-call speech as a win.
    "chat": Specialist(
        name="chat",
        tools=[],
        role="You are JARVIS, Natik's butler. You control no devices: answer, chat, "
             "keep it short and dry. If asked something factual you do not know, say so.",
        keywords=["weather", "joke", "jokes", "time", "date", "news", "hello",
                  "hi", "hey", "thanks", "thank you", "who are you",
                  "how are you", "what can you do", "good morning",
                  "good afternoon", "good evening", "how's it going",
                  "how are things"],
        talk_only=True,
    ),
}

DEFAULT_SPECIALIST = "comfort"


# --- prompt construction ---------------------------------------------------

_BASE_VOICE = (
    "You are JARVIS, Natik's assistant for this bedroom. Dry, composed, competent.\n"
    "YOUR REPLY IS SPOKEN ALOUD. At most 12 words. No preamble, no markdown, no\n"
    "lists, no emoji, never an entity_id or tool name.\n"
    'Good: "Lights on." / "AC at 22, sir." / "Fan on four."\n'
    'Say "sir" occasionally, not in every reply.\n'
    "The user speaks English, Hindi, or a mix. ALWAYS REPLY IN ENGLISH.\n"
    "garmi = heat, thand = cold, band karo = turn off, chalu karo = turn on,\n"
    "tez = faster, dheere = slower.\n"
)

# The comfort table, but ONLY handed to a specialist that owns the tools it
# names. This is the fix for the phantom-action defect: the humidity row is
# given to a specialist with ac_dry, never to one without it.
_COMFORT_RULES = (
    "REASON ABOUT COMFORT. The user describes a feeling, not a device. Decide and\n"
    "act immediately; never ask which device.\n"
    "- hot, boiling, sweat, sweaty, sweating, pasina, garmi -> AC 22 and fan 4\n"
    "- very hot, unbearable -> maximum cooling and fan boost\n"
    "- warm, slightly hot -> AC 24 and fan 3\n"
    "- cold, freezing, shivering, thand -> AC 27 and fan 1\n"
    "- humid, sticky, muggy, clammy -> AC dry mode\n"
    "- stuffy, no air, can't breathe, hawa nahi -> fan 4\n"
    "- less cold, thanda kam, just a bit warmer -> raise the AC, 25-27, or fan 2\n"
)


def build_prompt(spec: Specialist) -> str:
    """
    Generate the specialist's system prompt FROM its tool list.

    Nothing here is hand-written per specialist beyond `role`, because a
    hand-written prompt is exactly how a specialist ends up describing an action
    it cannot perform.
    """
    lines = [_BASE_VOICE, "", spec.role, ""]
    if spec.tools:
        lines.append("You can do ONLY these things, and nothing else exists for you:")
        for t in spec.tools:
            desc = TOOLS[t]["function"]["description"]
            lines.append(f"- {desc}")
        lines.append("")
        lines.append(
            "If the request is not one of the things above, say so in a few words and\n"
            "call no tool. NEVER describe an action you cannot perform."
        )
    else:
        lines.append(
            "You have NO tools and NO devices. You cannot turn anything on or off,\n"
            "you cannot send messages, you cannot touch the PC. Answer questions,\n"
            "greet, joke, chat. NEVER claim to have done anything."
        )
    # Only give the comfort table to a specialist that actually owns those tools.
    # (comfort itself owns both ac_dry and fan_set, so no name special-case is
    # needed -- `or spec.name == "comfort"` would be dead code.)
    if {"ac_dry", "fan_set"} <= set(spec.tools):
        lines.append("")
        lines.append(_COMFORT_RULES)
    # Argument bounds, restated in English next to the tools they govern. A 3B
    # model that has only ever seen "between 16 and 30" inside a JSON schema
    # still proposes 14 when the user says "make it freezing"; putting the
    # bound in the prompt text is what actually sticks. Same for the fan's
    # 0-6 scale with 6 = boost -- the schema says it, the prompt must too.
    if "ac_set_temperature" in spec.tools:
        lines.append(
            "Target temperature must be an INTEGER between 16 and 30 inclusive.\n"
            "Never go below 16 or above 30, no matter how the user phrases it."
        )
    if "fan_set" in spec.tools:
        lines.append(
            "Fan speeds are integers: 0 is off, 1-5 are the standard speeds,\n"
            "and 6 is boost/maximum. Pass the level directly, never a percentage."
        )
    # Per-specialist mappings for feelings the specialist CAN act on alone.
    # Measured 2026-08-09: without these three lines the model returned NO tool
    # call at all for "it's humid in here" (climate), "good night" (scenes), and
    # picked max-cool for "I'm freezing" (comfort). It is not a capability
    # problem -- the tool was present each time. The model simply was not told
    # that a feeling maps to that tool, so it answered conversationally.
    if "ac_dry" in spec.tools:
        lines.append(
            "Humid, sticky, muggy, clammy, damp or heavy air ALWAYS means dry mode.\n"
            "Call it immediately; do not discuss it."
        )
    if "ac_set_temperature" in spec.tools:
        lines.append(
            "Cold, freezing, shivering or thand means RAISE the temperature: set 27.\n"
            "Never use maximum cooling when the user is cold."
        )
    if "ac_max_cool" in spec.tools:
        # Without this, climate -- which owns ac_max_cool but NOT fan_set, so it
        # never sees the _COMFORT_RULES table -- inferred max-cool for "very hot"
        # from nothing. Same measured pattern as ac_dry: a feeling is present,
        # the tool is present, but the model answers conversationally because
        # nothing told it the two connect.
        lines.append(
            "Very hot, boiling or unbearable heat ALWAYS means maximum cooling.\n"
            "Call it immediately; never settle for a lower temperature."
        )
    if "good_night" in spec.tools:
        lines.append(
            "Good night, goodnight, going to bed, sleeping or sona ALWAYS means the\n"
            "night routine. Call it immediately."
        )
    if spec.tools:
        lines.append("")
        lines.append("Act first, speak after. Temperatures and speeds are integers, never strings.")
    return "\n".join(lines)


PROMPTS: dict[str, str] = {n: build_prompt(s) for n, s in SPECIALISTS.items()}


# --- classification --------------------------------------------------------

def classify(text: str) -> tuple[str, str]:
    """
    Pick a specialist. Returns (name, reason).

    Keyword scoring, deliberately not a model call: asking qwen which specialist
    to use would pay full model latency twice for a single command, which would
    eat the entire benefit of routing.
    """
    # Word-boundary matching. An earlier version tested `f" {k} " in t`, which
    # silently failed whenever the keyword was followed by anything -- "turn the
    # fan off" did not match "fan" because the text held " fan off ", so it fell
    # through to the comfort default and lost the fan specialist entirely. Two
    # of 21 cases misrouted purely on that.
    # Apostrophe kept as a word character so "can't breathe" stays one token and
    # matches the comfort keyword. Replaced by a space it became "can t
    # breathe", which matches nothing -- and "tv's on" still matches the scenes
    # keyword "tv" because the apostrophe still forms a word boundary.
    t = re.sub(r"[^\w\s/']", " ", text.lower())
    t = re.sub(r"\s+", " ", t).strip()

    best, best_score, why = DEFAULT_SPECIALIST, 0, "default"
    for name, spec in SPECIALISTS.items():
        if any(re.search(rf"\b{re.escape(neg)}\b", t) for neg in spec.negative):
            continue
        hits = [k for k in spec.keywords if re.search(rf"\b{re.escape(k)}\b", t)]
        if not hits:
            continue
        # Longer keyword = more specific = stronger signal ("air conditioner"
        # should beat a bare "air"). Word count matters more than characters:
        # a two-word keyword is a much stronger signal than a long single word.
        score = max(len(k.split()) * 10 + len(k) for k in hits) + len(hits)
        if score > best_score:
            best, best_score, why = name, score, f"matched {hits[:3]}"
    return best, why


# --- execution -------------------------------------------------------------

@dataclass
class HermesResult:
    speech: str
    specialist: str
    tool_calls: list[dict]
    total_ms: float
    reason: str = ""
    error: str = ""


class Hermes:
    """Routes an utterance to one specialist and runs it against Ollama."""

    def __init__(self, ollama_url: str = OLLAMA_URL, model: str = MODEL,
                 num_ctx: int = NUM_CTX) -> None:
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.counts: dict[str, int] = {}

    async def ask(self, session: aiohttp.ClientSession, text: str,
                  specialist: str | None = None) -> HermesResult:
        started = time.perf_counter()
        name, why = (specialist, "forced") if specialist else classify(text)
        spec = SPECIALISTS[name]
        self.counts[name] = self.counts.get(name, 0) + 1

        num_predict = (
            _NUM_PREDICT_FREE_TEXT
            if _FREE_TEXT_TOOLS.intersection(spec.tools)
            else _NUM_PREDICT_ACTUATION
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": PROMPTS[name]},
                {"role": "user", "content": text},
            ],
            "tools": [TOOLS[t] for t in spec.tools],
            "stream": False,
            # temperature 0: this is command dispatch, not writing. Sampling here
            # buys nothing and costs reproducibility, which makes every
            # measurement in this project noisier.
            # num_predict: specialist-aware, see _NUM_PREDICT_ACTUATION /
            # _NUM_PREDICT_FREE_TEXT. Actuation specialists keep the 150 cap
            # (~3x the measured 48-token max, ~2s worst case); specialists
            # exposing a free-text tool (wa_send, pc_type) get 512 so a dictated
            # WhatsApp message or typed paragraph fits inside the tool-call JSON
            # instead of being truncated into no tool call and a phantom
            # "I've sent that, sir".
            "options": {
                "temperature": 0,
                "num_ctx": self.num_ctx,
                "num_predict": num_predict,
            },
        }
        try:
            async with session.post(
                f"{self.ollama_url}/api/chat", json=payload,
                # 15s, matching the executor and the router's big-agent timeout.
                # A healthy turn finishes in ~6s; 60s only delayed honest failure
                # (a hung Ollama) by a full minute. See IntentRouter.__init__.
                timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    return HermesResult("", name, [], (time.perf_counter() - started) * 1000,
                                        why, f"http_{resp.status}: {body}")
                data = await resp.json()
        except Exception as exc:  # noqa: BLE001 - any failure here must not kill speech
            return HermesResult("", name, [], (time.perf_counter() - started) * 1000,
                                why, f"{type(exc).__name__}: {exc}")

        msg = data.get("message", {})
        content = (msg.get("content") or "").strip()
        calls = []
        for c in msg.get("tool_calls") or []:
            fn = c.get("function", {})
            calls.append({"name": fn.get("name"), "arguments": fn.get("arguments") or {}})
        # Fallback: some Ollama models output tool calls as JSON in content
        # instead of the structured tool_calls array. Parse them here.
        if not calls and content:
            try:
                # Try to extract JSON object that looks like a tool call
                m = re.search(r'\{"function"\s*:', content)
                if m:
                    tc = json.loads(m.group())
                    fn = tc.get("function", tc)
                    calls.append({"name": fn.get("name"), "arguments": fn.get("arguments") or {}})
                    content = ""  # Clear content since we extracted the tool call
            except (json.JSONDecodeError, AttributeError):
                pass

        return HermesResult(
            speech=content,
            specialist=name,
            tool_calls=calls,
            total_ms=round((time.perf_counter() - started) * 1000, 1),
            reason=why,
        )

    def stats(self) -> dict[str, Any]:
        total = sum(self.counts.values())
        return {
            "total": total,
            "by_specialist": dict(sorted(self.counts.items(), key=lambda kv: -kv[1])),
            "tool_counts": {n: len(s.tools) for n, s in SPECIALISTS.items()},
        }
