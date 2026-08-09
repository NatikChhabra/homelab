"""
Phrase normalisation: rewrite what people say into what HA's parser accepts.

Measured 2026-08-09 against the live built-in parser. The parser is rigid about
PHRASING, not about capability -- the same action succeeds or fails purely on
wording, at 10-60 ms either way:

    "set the fan speed to 60"   -> OK   "Speed set"
    "set the fan to speed 3"    -> FAIL "not aware of any device called fan to"
    "fan speed 3"               -> FAIL "couldn't understand that"

    "turn on the lights in the bedroom" -> OK   "Turned on the lights"
    "turn on the lights"                -> FAIL "couldn't understand that"
    "lights on"                         -> FAIL "couldn't understand that"

So a rewrite table costs nothing and converts a fallback (~2500 ms through
qwen2.5:3b) into a local hit (~30 ms). Every rule below was derived from an
observed failure, not guessed, and each carries the phrasing it fixes.

Deliberately conservative. A rewrite that changes MEANING is far worse than a
missed rewrite: a missed rewrite costs 2.5 s, a wrong one performs the wrong
action and reports success. When in doubt the text is passed through untouched
and the model decides.

THE ONE-WAY DOOR: rules must never invent a target that was not named. Nothing
here maps a bare "turn it on" onto a device.
"""

from __future__ import annotations

import re

# The single area in this install. Plural/bare light commands are unresolvable
# without an area, and HA will not infer one from a REST conversation call --
# there is no device_id carrying an area, which is why "turn on the lights"
# fails while "turn on the lights in the bedroom" succeeds.
AREA = "bedroom"

# The fan is a 5-speed unit exposed as a percentage. speed N -> N*20%.
# Note boost and speed 5 both read 100% (see the fan-entity notes in HANDOFF),
# so "boost" is left alone here and handled by its own script.
#
# "off"/"zero"/"band(h)" are deliberately NOT in this table. No rule's fan regex
# can capture them, and if one ever did, _speed_to_pct(0) clamps to 20% -- the
# fan would turn ON at speed 1 instead of turning off. They fall to the model.
_SPEED_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5,
}


def _speed_to_pct(n: int) -> int:
    return max(1, min(5, n)) * 20


# --- rules -----------------------------------------------------------------
# Each entry: (compiled pattern, replacement callable or string, why).
# Applied in order, first match wins.

_RULES: list[tuple[re.Pattern[str], object, str]] = []


def _rule(pattern: str, repl, why: str) -> None:
    _RULES.append((re.compile(pattern, re.I), repl, why))


# Percentage phrasings the parser rejects only because of word order. Ordered
# BEFORE the trailing-words rule: a single-digit percent ("5%") must stay a
# percentage, not be read as the speed ordinal 5 (= 100%). "make" is included
# so "make the fan at 5%" cannot fall through to the trailing rule either.
_rule(
    r"^(?:set|change|put|make)\s+(?:the\s+)?fan\s+(?:speed\s+)?(?:to|on|at)\s+"
    r"(?:speed\s+)?(\d{1,3})(?:\s*(?:%|percent))?$",
    lambda m: f"set the fan speed to {m.group(1)}",
    "'set fan speed to 60%' failed on the percent sign; the bare number works",
)
# Fan speed as an ordinal/number -> the percentage form the parser accepts.
#   "set the fan to speed 3", "fan speed 3", "change fan speed to 4",
#   "put the fan on 2", "fan on speed four"
_rule(
    r"^(?:set|change|put|make)?\s*(?:the\s+)?fan\s+(?:speed\s+)?(?:to\s+|on\s+|at\s+)?"
    r"(?:speed\s+)?(one|two|three|four|five|[1-5])$",
    lambda m: f"set the fan speed to {_speed_to_pct(_SPEED_WORDS[m.group(1).lower()])}",
    "fan speed N -> percentage; 'set the fan to speed 3' failed, 'set the fan speed to 60' works",
)

# Trailing words that rule 2 is NOT allowed to swallow. "put the fan on 3 at
# night" is a schedule, not an instruction to hit 60% right now; "set the fan
# to speed 3 for 2 hours" is a timed request. Both must fall through to the
# model, which can time the action, rather than firing immediately.
_SCHEDULE_TAIL = re.compile(
    r"\b(?:for|until|till|after|before|at|in|when|while)\b"
    r"|\b(?:hour|hours|minute|minutes|min|mins|second|seconds|night|morning|"
    r"afternoon|evening|tonight|tomorrow|daily|every)\b",
    re.I,
)


def _fan_trailing_repl(m: re.Match[str]) -> str:
    if _SCHEDULE_TAIL.search(m.group(2)):
        return m.string  # scheduled/timed phrasing -> leave for the model
    return f"set the fan speed to {_speed_to_pct(_SPEED_WORDS[m.group(1).lower()])}"


# Same as the ordinal rule, with trailing words ("set the fan to 3 please").
# The lookahead keeps a trailing '%'/'percent' out of this rule even if the
# percent rule above missed it.
_rule(
    r"^(?:set|change|put|make)\s+(?:the\s+)?fan\s+(?:speed\s+)?(?:to|on|at)\s+"
    r"(?:speed\s+)?(one|two|three|four|five|[1-5])\b(?!\s*(?:%|percent)\b)(.*)$",
    _fan_trailing_repl,
    "same, with trailing words (politeness); '%' and schedule phrases excluded",
)

# Bare/plural light commands -> the area form.
#   "turn on the lights", "lights on", "turn the lights on", "all lights on"
# 'all lights on' is especially important: unrewritten it matches a QUERY intent
# and HA ANSWERS it ("No, Gang 1, Gang 2 and Gang 3 are off") instead of acting.
_rule(
    r"^(?:turn\s+)?(?:on\s+)?(?:the\s+)?(?:all\s+)?lights?\s+on$",
    f"turn on the lights in the {AREA}",
    "bare 'lights on' -> area form; also stops 'all lights on' matching a query intent",
)
_rule(
    r"^(?:turn\s+)?(?:off\s+)?(?:the\s+)?(?:all\s+)?lights?\s+off$",
    f"turn off the lights in the {AREA}",
    "bare 'lights off' -> area form",
)
_rule(
    r"^turn\s+on\s+(?:all\s+)?(?:the\s+)?lights?$",
    f"turn on the lights in the {AREA}",
    "'turn on the lights' -> area form",
)
_rule(
    r"^turn\s+off\s+(?:all\s+)?(?:the\s+)?lights?$",
    f"turn off the lights in the {AREA}",
    "'turn off the lights' -> area form",
)
_rule(
    r"^turn\s+(?:all\s+)?(?:the\s+)?lights?\s+on$",
    f"turn on the lights in the {AREA}",
    "'turn the lights on' -> area form",
)
_rule(
    r"^turn\s+(?:all\s+)?(?:the\s+)?lights?\s+off$",
    f"turn off the lights in the {AREA}",
    "'turn the lights off' -> area form",
)

# Whole-room scripts are matched by EXACT friendly name, so normalise onto it.
# The 'off' is required: a bare "everything" is not a command and must not fire
# a script that shuts down the whole room. "all off" is likewise NOT mapped
# here -- it names "all", not "everything", and more naturally means the lights;
# rewriting it would turn off the fan and TV (and possibly the AC) from a lights
# request. Both fall to the model.
_rule(r"^(?:turn\s+)?everything\s+off$|^turn\s+off\s+everything$", "turn everything off",
      "'turn off everything' failed: 'everything' is not a device. Script name is 'Turn everything off'")
_rule(r"^(?:sab|sab\s+kuch)\s+band(?:\s+kar(?:\s+do)?)?$", "turn everything off",
      "Hinglish for the same script")
_rule(r"^shut\s+it\s+all\s+down$", "turn everything off", "same script, other phrasings")
_rule(r"^(?:good\s*night|goodnight|sone\s+ja\s+raha\s+hoon)$", "good night",
      "script friendly name is 'Good night'")

# TV is a single toggle; any on/off phrasing means the same button.
_rule(r"^(?:turn\s+)?(?:the\s+)?t\.?v\.?\s*(?:on|off)$", "toggle the tv",
      "'turn the tv on' failed: no device called tv. The script is 'Toggle the TV'")
_rule(r"^turn\s+(?:on|off)\s+(?:the\s+)?t\.?v\.?$", "toggle the tv", "same")

# Hinglish that maps 1:1 onto an existing local target. Only unambiguous ones.
#
# "fan" + Hindi verb is a very common mixing pattern in this house; the
# existing pankha rules handled the fully-Hindi version but missed the code-switch.
_rule(r"^(?:fan|pankha)\s+(?:off|band|bandh)(?:\s+kar(?:o|\s+do)?)?$", "turn off the fan",
      "fan/pankha off/band karo")
_rule(r"^(?:fan|pankha)\s+(?:chalu|on)(?:\s+kar(?:o|\s+do)?)?$", "turn on the fan",
      "fan/pankha chalu karo")
_rule(r"^turn\s+(?:the\s+)?fan\s+off$", "turn off the fan",
      "'turn the fan off' -> canonical form")
# "lights chalu/band" covers the same code-switch for the lights, including the
# "kar karo/kar do" verb suffix the fan rules accept.
_rule(r"^(?:lights?)\s+(?:chalu|on)(?:\s+kar(?:o|\s+do)?)?$",
      f"turn on the lights in the {AREA}", "lights chalu/on karo")
_rule(r"^(?:lights?)\s+(?:band|bandh|off)(?:\s+kar(?:o|\s+do)?)?$",
      f"turn off the lights in the {AREA}", "lights band/off karo")
# Hinglish AC temperature: "ac 22 kar do" → local JarvisSetAcTemperature handles
# the rewritten form; bare "ac 22" also routes through this path.
_rule(r"^ac\s+(\d{1,2})(?:\s*(?:kar\s+do|karo|rakh\s+do|rakh\s+de))?\s*$",
      lambda m: f"set the ac to {m.group(1)}", "ac N kar do")
# "set ac N" -> canonical form the parser accepts
_rule(r"^set\s+ac\s+(\d{1,2})\s*$",
      lambda m: f"set the ac to {m.group(1)}", "set ac N -> set the ac to N")

# AC word-order fix: "turn the AC on/off" -> "turn on/off the AC"
# Bare "ac on/off" and Hinglish "ac chalu/band" already work locally.
_rule(r"^turn\s+the\s+ac\s+(on|off)$",
      lambda m: f"turn {m.group(1)} the ac", "turn the AC on/off -> turn on/off the AC")
_rule(r"^ac\s+(on|off)$",
      lambda m: f"turn {m.group(1)} the ac", "bare ac on/off -> turn on/off the AC")
# "turn off ac" / "turn on ac" (no "the") -> canonical form
_rule(r"^turn\s+(on|off)\s+ac$",
      lambda m: f"turn {m.group(1)} the ac", "'turn off ac' -> 'turn off the ac'")

# Scene scripts: "I am feeling hot/cold" -> exact script names.
# These are NOT light/fan/device actions; they trigger multi-device scenes.
# Without this rule the model handles them at ~2.5 s instead of the local ~40 ms.
#
# NOTE: the script entities have no friendly_name and no aliases in the HA
# registry, so the conversation agent only recognises the FULL phrase "I am
# feeling hot" / "I am feeling cold". Short forms ("feeling hot", "feeling hot
# please") are NOT rewritten because HA rejects them; they fall through to the
# model, which still succeeds at ~2.5 s.
_rule(r"^i\s+am\s+feeling\s+hot$", "I am feeling hot",
      "script entity has no alias; full phrase is the only recognised form")
_rule(r"^i\s+am\s+feeling\s+cold$", "I am feeling cold",
      "same for cold")

# Light gang names: "turn on light one" / "light one on" / "gang 1" etc.
# Node switch has 4 gangs (1-4) plus a backlight. Map ordinal/gang phrasings
# onto the exact entity names the HA parser accepts.
#
# Coverage needed (all observed from real use):
#   bare number then direction:  "1 on", "1 off"
#   bare number with direction first: "turn on 1", "turn off 1"
#   light prefix then number:    "light 1 on", "light1 on", "1 light on"
#   gang prefix then number:     "gang 1 on", "gang1 on", "1 gang on"
#   direction first with gang:   "turn on gang 1", "turn on gang1"
#   direction first with light:  "turn on light 1", "turn on light1"
#   word numerals everywhere:    "one on", "turn on one", "light two off"
#   backlight variants:          "bl on", "back light off", "turn on backlight"
_GANG_ALIASES = {
    "one": "gang 1", "1": "gang 1", "gang1": "gang 1", "light1": "gang 1",
    "two": "gang 2", "2": "gang 2", "gang2": "gang 2", "light2": "gang 2",
    "three": "gang 3", "3": "gang 3", "gang3": "gang 3", "light3": "gang 3",
    # gang 4 is a switch, not a light - map to node switch node 4
    "four": "node switch node 4", "4": "node switch node 4",
    # backlight entity is not exposed to conversation agent;
    # map to the 3+1 node smart switch backlight instead
    "backlight": "3+1 node smart switch backlight", "back light": "3+1 node smart switch backlight",
    "bl": "3+1 node smart switch backlight",
}

def _gang_rule(pat: str, repl: str, reason: str) -> None:
    """Helper to add a gang rule."""
    _RULES.append((re.compile(pat, re.I), repl, reason))

# Generate rules for each gang member.  We iterate once and emit all matching
# patterns; this is faster to read than a wall of hand-written _rule() calls.
for num, alias in _GANG_ALIASES.items():
    esc = re.escape(num)
    # --- direction first, then optional light/gang prefix, then number --------
    # "turn on gang 1", "turn on gang1", "turn on light 1", "turn on backlight"
    _gang_rule(
        rf"^(?:turn\s+)?on\s+(?:the\s+)?(?:(?:light|gang)\s*)?{esc}\s*$",
        f"turn on {alias}",
        f"'turn on {num}' / 'turn on gang {num}' / 'turn on gang{num}'",
    )
    _gang_rule(
        rf"^(?:turn\s+)?off\s+(?:the\s+)?(?:(?:light|gang)\s*)?{esc}\s*$",
        f"turn off {alias}",
        f"'turn off {num}' / 'turn off gang {num}' / 'turn off gang{num}'",
    )
    # --- number first, then optional light/gang, then direction ----------------
    # "1 on", "light 1 on", "light1 on", "gang 1 on", "gang1 on"
    _gang_rule(
        rf"^(?:turn\s+)?(?:the\s+)?(?:(?:light|gang)\s*)?{esc}\s*(?:light|gang)?\s+on$",
        f"turn on {alias}",
        f"'{num} on' / 'light {num} on' / 'light{num} on' / 'gang {num} on' / 'gang{num} on'",
    )
    _gang_rule(
        rf"^(?:turn\s+)?(?:the\s+)?(?:(?:light|gang)\s*)?{esc}\s*(?:light|gang)?\s+off$",
        f"turn off {alias}",
        f"'{num} off' / 'light {num} off' / 'light{num} off' / 'gang {num} off' / 'gang{num} off'",
    )
    # --- direction at end, number before it, no light/gang prefix ---------------
    # "one on", "two off", "backlight on"
    _gang_rule(
        rf"^(?:turn\s+)?(?:the\s+)?{esc}\s+on$",
        f"turn on {alias}",
        f"'{num} on' (bare)",
    )
    _gang_rule(
        rf"^(?:turn\s+)?(?:the\s+)?{esc}\s+off$",
        f"turn off {alias}",
        f"'{num} off' (bare)",
    )

# "ceiling light" is a natural name for the main gang (gang 1 = ceiling).
# Only rewrite when the user says "ceiling" explicitly; bare "the light" is
# already handled by the area-form rule above.
_rule(r"^turn\s+(?:on\s+)?(?:the\s+)?ceiling\s+light\s+(on|off)$",
      lambda m: f"turn {m.group(1)} the lights in the bedroom",
      "'ceiling light on/off' -> area lights")
_rule(r"^turn\s+on\s+(?:the\s+)?ceiling\s+light$",
      "turn on the lights in the bedroom",
      "'turn on the ceiling light' -> area lights")
_rule(r"^ceiling\s+light\s+on$",
      "turn on the lights in the bedroom",
      "'ceiling light on' -> area lights")


def _pre_normalize(text: str) -> str:
    """Insert spaces around numbers and common fused word boundaries.

    Handles typos like "to60", "speed3", "ac22", "turnon", "onthe", "setac24".
    Safe because it only touches obvious fusion points, never invents words.
    """
    # Fully fused commands FIRST (before generic number-splitting)
    # \b after digits: digit is \w, so \b matches at end-of-string or before \W.
    # Prevents "setac24please" from being rewritten (trailing text = model decides).
    text = re.sub(r"\bsetac(\d+)\b", r"set ac \1", text)
    text = re.sub(r"\bturnoffac\b", r"turn off ac", text)
    text = re.sub(r"\bturnonlight\b", r"turn on light", text)
    text = re.sub(r"\bturnonac\b", r"turn on ac", text)
    text = re.sub(r"fanspeed", r"fan speed", text)
    text = re.sub(r"\blightson\b", r"light on", text)
    text = re.sub(r"\blightoff\b", r"light off", text)
    text = re.sub(r"\bacmode\b", r"ac mode", text)
    # Number fused to preceding word: "to60" -> "to 60", "speed3" -> "speed 3"
    text = re.sub(r"([a-zA-Z])(\d+)", r"\1 \2", text)
    # Number fused to following word: "60percent" -> "60 percent"
    text = re.sub(r"(\d+)([a-zA-Z])", r"\1 \2", text)
    # Common fused pairs with word boundaries
    text = re.sub(r"\b(turn)(on)\b", r"\1 \2", text)
    text = re.sub(r"\b(on)(the)\b", r"\1 \2", text)
    text = re.sub(r"\b(turn)(off)\b", r"\1 \2", text)
    return text


def normalise(text: str) -> tuple[str, str]:
    """
    Return (rewritten_text, rule_reason). reason is "" when nothing matched.

    Idempotent and side-effect free, so it is safe to call on every utterance
    before the local parser.
    """
    # A trailing '?' marks a question, not a command. Rewriting destroys that
    # marker: "tv off?" is the user CHECKING the TV is off, but "tv off" matches
    # the toggle rule and would flip it ON. The router's query guard cannot catch
    # this -- it sees the rewritten text, which never looks like a question.
    # Refuse to rewrite: the model gets the original and answers the question.
    if text.strip().endswith("?"):
        return text, ""
    cleaned = _pre_normalize(text.strip().rstrip(".!?"))
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return text, ""
    for pattern, repl, why in _RULES:
        m = pattern.match(cleaned)
        if m:
            out = repl(m) if callable(repl) else repl
            if out.lower() == text.lower():
                return text, ""
            return out, why
    return text, ""


# --- query guard -----------------------------------------------------------
# HA returns response_type "query_answer" for questions. The hazard: an
# IMPERATIVE can also match a query intent -- "all lights on" was answered with
# "No, Gang 1, Gang 2 and Gang 3 are off" rather than acted on. Accepting that
# as a local success means JARVIS answers a command as though it were a
# question and never touches the lights. So a query_answer is only trusted when
# the utterance actually looks like a question.

_QUESTION_START = re.compile(
    r"^\s*(what|what's|whats|when|where|which|who|why|how|is|are|was|were|do|does|did|"
    r"can|could|will|would|should|has|have|had|am|tell\s+me|list)\b",
    re.I,
)

# Polite imperatives and requests that start with a question word but are NOT
# questions: "can you turn on the lights", "will you turn off the tv",
# "how about turning off the lights", "why don't you turn off the tv", "do that".
# Classifying them as questions would let the router trust a query_answer for an
# action request and would steer an action away from the actuation specialist.
_POLITE_REQUEST = re.compile(
    r"^\s*(?:can|could|will|would)\s+you\b"
    r"|^\s*(?:how\s+about|why\s+don'?t\s+you)\b"
    r"|^\s*do\s+(?:it|that)\b",
    re.I,
)


def looks_like_question(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if t.endswith("?"):
        return True
    return bool(_QUESTION_START.match(t) and not _POLITE_REQUEST.match(t))
