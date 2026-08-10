"""Draft validation harness for proposed phrasing.py rules. NOT part of the app."""
import re
import sys

sys.path.insert(0, r"C:\ServerData\Stacks\jarvis")
from phrasing import _SPEED_WORDS, _speed_to_pct

RULES = []


def add(pattern, repl):
    RULES.append((re.compile(pattern, re.I), repl))


def out(repl, m):
    return repl(m) if callable(repl) else repl


def n_off():
    return lambda m: "turn off the fan"


def n_on():
    return lambda m: "turn on the fan"


def onoff(fmt_on, fmt_off):
    return lambda m: (fmt_on if m.group(1).lower() in ("on", "chalu") else fmt_off)


# ---- FAN on/off -------------------------------------------------------------
add(r"^(?:turn|switch)\s+(on|off)\s+(?:the\s+)?(?:fan|pankha|ceiling\s+fan)$", onoff("turn on the fan", "turn off the fan"))
add(r"^(?:turn|switch)\s+(?:the\s+)?(?:fan|pankha|ceiling\s+fan)\s+(on|off)$", onoff("turn on the fan", "turn off the fan"))
add(r"^(?:fan|pankha|ceiling\s+fan)\s+(on|off|chal)$", onoff("turn on the fan", "turn off the fan"))
add(r"^(?:fan|pankha|ceiling\s+fan)\s+(?:off|band|bandh)(?:\s*kar(?:\s*(?:o|do|de))?)?$", "turn off the fan")
add(r"^(?:pankha|fan)\s+(on|off|chalu)\s*(?:kar(?:\s*(?:o|do|de))?)?$", onoff("turn on the fan", "turn off the fan"))

# ---- FAN zero -> off --------------------------------------------------------
add(r"^(?:set|change|put|make)?\s*(?:the\s+)?fan\s+(?:speed\s+)?(?:to\s+|at\s+|on\s+)?(?:speed\s+)?(?:zero|0)$", "turn off the fan")
add(r"^zero\s+(?:fan|speed)$", "turn off the fan")

# ---- FAN max/full -----------------------------------------------------------
add(r"^(?:set\s+)?(?:the\s+)?(?:fan|pankha)\s+(?:to\s+)?(?:full|max(?:imum)?)(?:\s+speed)?$", "set the fan speed to 100")
add(r"^(?:fan|pankha)\s+(?:full|max(?:imum)?)$", "set the fan speed to 100")
add(r"^(?:full|max(?:imum)?)\s+(?:fan|pankha)\s+speed$", "set the fan speed to 100")
add(r"^(?:full|max(?:imum)?)\s+(?:fan|pankha)$", "set the fan speed to 100")
add(r"^(?:full|max(?:imum)?)\s+speed$", "set the fan speed to 100")

# ---- FAN bare percentage / high numbers -------------------------------------
add(r"^(?:set|change|put)\s+(?:the\s+)?fan\s+(?:speed\s+)?(?:to|at)\s*(\d{1,3})\s*(?:%|percent)?$",
    lambda m: f"set the fan speed to {m.group(1)}")
add(r"^(?:fan|pankha)\s+(?:speed\s+)?(?:to|at)?\s*(\d{1,3})\s*(?:%|percent)$",
    lambda m: f"set the fan speed to {m.group(1)}")
add(r"^(?:fan|pankha)\s+(?:speed\s+)?(?:to|at)?\s*(\d{2,3})$",
    lambda m: f"set the fan speed to {m.group(1)}")
# 5-speed unit: any 6-9 means max. Clamps consistently with _speed_to_pct.
add(r"^(?:set|change|put|make)?\s*(?:the\s+)?fan\s+(?:speed\s+)?(?:to\s+|on\s+|at\s+)?(?:speed\s+)?(six|seven|eight|nine|[6-9])\s*(?:please|pls|yaar|bhai|bro|jaldi)?$",
    "set the fan speed to 100")

# ---- FAN slow/fast / Hinglish numbers / politeness ---------------------------
_HINDI_NUM = {"ek": 20, "do": 40, "teen": 60, "char": 80, "paanch": 100}
add(r"^(?:pankha|fan)\s+(?:tez|fast|full)\s+kar(?:o|do|de)?$", "set the fan speed to 100")
add(r"^(?:pankha|fan)\s+(?:dheere|dheema|slow|kam)\s*(?:kar(?:o|do|de)?)?$", "set the fan speed to 20")
add(r"^(?:slow\s+down\s+the\s+fan|slow\s+the\s+fan|make\s+the\s+fan\s+slow|fan\s+slow\s+down)$", "set the fan speed to 20")
add(r"^(?:pankha|fan)\s+(ek|do|teen|char|paanch)\s*(?:pe\s+)?(?:laga\s+do|kar(?:\s*(?:o|do|de))?)?$",
    lambda m: f"set the fan speed to {_HINDI_NUM[m.group(1)]}")
add(r"^(?:pankha|fan)\s+(6|7|8|9)\s*(?:pe\s+)?(?:laga\s+do|kar(?:\s*(?:o|do|de))?)?$",
    "set the fan speed to 100")
add(r"^(?:set|change|put|make)?\s*(?:the\s+)?fan\s+(?:speed\s+)?(?:to\s+|on\s+|at\s+)?(?:speed\s+)?(one|two|three|four|five|[1-5])\s+(?:please|pls|yaar|bhai|bro|jaldi)$",
    lambda m: f"set the fan speed to {_speed_to_pct(_SPEED_WORDS[m.group(1).lower()])}")

# ---- LIGHTS switch/put on-off ------------------------------------------------
add(r"^(?:switch|put)\s+(on|off)\s+(?:all\s+)?(?:the\s+)?lights?$", onoff("turn on the lights in the bedroom", "turn off the lights in the bedroom"))
add(r"^(?:switch|put)\s+(?:all\s+)?(?:the\s+)?lights?\s+(on|off)$", onoff("turn on the lights in the bedroom", "turn off the lights in the bedroom"))
add(r"^put\s+(?:the\s+)?lights?\s+on$", "turn on the lights in the bedroom")

# ---- LIGHTS toggle ------------------------------------------------------------
add(r"^(?:toggle|switch|flip|invert)\s+(?:the\s+)?lights?$", "toggle the lights in the bedroom")
add(r"^lights?\s+(?:toggle|flip)$", "toggle the lights in the bedroom")

# ---- LIGHTS dim/brighten ------------------------------------------------------
add(r"^(?:set|turn|put|dim|brighten)\s+(?:the\s+)?lights?\s+(?:to|at|down\s+to|up\s+to)?\s*(\d{1,3})\s*(?:%|percent)?$",
    lambda m: f"set the brightness of the lights in the bedroom to {m.group(1)}%")
add(r"^lights?\s+(?:at|to|down\s+to|up\s+to)\s*(\d{1,3})\s*(?:%|percent)?$",
    lambda m: f"set the brightness of the lights in the bedroom to {m.group(1)}%")
add(r"^(?:dim|darken)\s+(?:the\s+)?lights?$", "set the brightness of the lights in the bedroom to 50%")
add(r"^lights?\s+(?:dim|dimmer|dimmed)$", "set the brightness of the lights in the bedroom to 50%")
add(r"^(?:brighten|bright)\s+(?:the\s+)?lights?$", "set the brightness of the lights in the bedroom to 100%")
add(r"^lights?\s+(?:brighter|bright)$", "set the brightness of the lights in the bedroom to 100%")

# ---- LIGHTS Hinglish mixed -----------------------------------------------------
add(r"^(?:batti|roshni|light)\s+(on|off|chalu|band|bandh)\s*kar(?:\s*(?:o|do|de))?$", onoff("turn on the lights in the bedroom", "turn off the lights in the bedroom"))
add(r"^lights?\s+(on|off)\s+kar(?:\s*(?:o|do|de))?$", onoff("turn on the lights in the bedroom", "turn off the lights in the bedroom"))
add(r"^(?:batti|roshni|light)\s+(on|off|chalu|band|bandh)$", onoff("turn on the lights in the bedroom", "turn off the lights in the bedroom"))

# ---- LIGHTS andhera / ujala ----------------------------------------------------
add(r"^andhera(?:\s+kar(?:\s*(?:o|do|de))?)?$", "turn off the lights in the bedroom")
add(r"^(?:ujala|roshni)(?:\s+kar(?:\s*(?:o|do|de))?)?$", "turn on the lights in the bedroom")

# ---- AC on/off -----------------------------------------------------------------
add(r"^(?:turn|switch)\s+(on|off)\s+(?:the\s+)?(?:a[/.]?c\.?|ac|air\s+conditioner)$", onoff("turn on the ac", "turn off the ac"))
add(r"^(?:a[/.]?c\.?|ac|air\s+conditioner)\s+(on|off)$", onoff("turn on the ac", "turn off the ac"))
add(r"^(?:a[/.]?c\.?|ac)\s+(on|off|chalu|band|bandh)\s*kar(?:\s*(?:o|do|de))?$", onoff("turn on the ac", "turn off the ac"))

# ---- AC temperature -------------------------------------------------------------
add(r"^(?:set|change|put)?\s*(?:the\s+)?(?:a[/.]?c\.?|ac|air\s+conditioner)\s+(?:temperature|temp)\s+(?:to|at)\s+(1[6-9]|2[0-9]|30)\b.*$",
    lambda m: f"set the ac to {m.group(1)}")
add(r"^(?:set|change|put)?\s*(?:the\s+)?(?:temperature|temp)\s+(?:to\s+)?(1[6-9]|2[0-9]|30)\b.*$",
    lambda m: f"set the ac to {m.group(1)}")
add(r"^set\s+(?:the\s+)?(?:a[/.]?c\.?|ac)\s+to\s+(1[6-9]|2[0-9]|30)\b.*$",
    lambda m: f"set the ac to {m.group(1)}")
add(r"^(?:a[/.]?c\.?|ac)\s+(?:to\s+|temperature\s+)?(1[6-9]|2[0-9]|30)\b.*$",
    lambda m: f"set the ac to {m.group(1)}")

# ---- AC cool / warm ---------------------------------------------------------------
add(r"^(?:cool\s+it\s+down|cool\s+down|cool\s+the\s+room|make\s+it\s+cooler|thanda\s+kar(?:o|do|de)?|ac\s+tez\s+kar(?:o|do|de)?|ac\s+thanda\s+kar(?:o|do|de)?)$",
    "set the ac to 22")
add(r"^(?:heat\s+it\s+up|warm\s+it\s+up|make\s+it\s+warmer|garam\s+kar(?:o|do|de)?|ac\s+garam\s+kar(?:o|do|de)?)$",
    "set the ac to 28")

# ---- whole-room scripts ------------------------------------------------------------
add(r"^(?:time\s+to\s+sleep|time\s+for\s+bed|bed\s*time|going\s+to\s+bed|off\s+to\s+bed|heading\s+to\s+bed|night\s+night|gn|so\s+ja(?:o)?|neend\s+aa\s+rahi\s+hai|nind\s+aa\s+rahi\s+hai|nind\s+a\s+rahi\s+hai|jaldi\s+so\s+jao)$",
    "good night")
add(r"^lights\s+out$", "good night")
add(r"^(?:i(?:'m|\s+am)\s+)?(?:leaving|going\s+out|heading\s+out|stepping\s+out)$", "turn everything off")
add(r"^(?:sab|sab\s+kuch)\s+(?:band|bandh)(?:\s*kar(?:\s*(?:o|do|de))?)?$", "turn everything off")
add(r"^(?:all\s+on|sab\s+on\s+kar(?:o|do|de)?|sab\s+chalu)$", "turn everything on")

# ---- copy of normalise() over existing + proposed ----------------------------
def norm(text, rules):
    cleaned = re.sub(r"\s+", " ", text.strip().rstrip(".!?"))
    if not cleaned:
        return text
    for p, repl in rules:
        m = p.match(cleaned)
        if m:
            o = out(repl, m)
            if o.lower() == cleaned.lower():
                return text
            return o
    return text


import importlib
import phrasing as P
importlib.reload(P)
combined = [(p, r) for p, r, w in P._RULES] + [(p, r) for p, r in RULES]

cases = [
    ("fan on", "turn on the fan"), ("turn on the fan", "turn on the fan"),
    ("turn the fan on", "turn on the fan"), ("switch on the fan", "turn on the fan"),
    ("switch the fan off", "turn off the fan"), ("fan off", "turn off the fan"),
    ("pankha off karo", "turn off the fan"), ("pankha band karde", "turn off the fan"),
    ("pankha band kardo", "turn off the fan"), ("fan on karo", "turn on the fan"),
    ("ceiling fan on", "turn on the fan"),
    ("fan speed zero", "turn off the fan"), ("set the fan to zero", "turn off the fan"),
    ("fan speed 0", "turn off the fan"), ("zero fan", "turn off the fan"),
    ("max fan", "set the fan speed to 100"), ("fan max", "set the fan speed to 100"),
    ("maximum fan", "set the fan speed to 100"), ("full fan speed", "set the fan speed to 100"),
    ("fan full", "set the fan speed to 100"), ("set the fan to max", "set the fan speed to 100"),
    ("max speed", "set the fan speed to 100"), ("full speed", "set the fan speed to 100"),
    ("set the fan to 60", "set the fan speed to 60"), ("fan 60", "set the fan speed to 60"),
    ("set fan to 60 percent", "set the fan speed to 60"), ("fan 60 percent", "set the fan speed to 60"),
    ("fan speed 60", "set the fan speed to 60"), ("fan speed 6", "set the fan speed to 100"),
    ("set the fan to speed 6", "set the fan speed to 100"),
    ("tez karo", "tez karo"), ("dheere karo", "dheere karo"),
    ("pankha tez karo", "set the fan speed to 100"), ("fan tez karo", "set the fan speed to 100"),
    ("pankha dheere karo", "set the fan speed to 20"), ("slow the fan", "set the fan speed to 20"),
    ("fan slow", "set the fan speed to 20"), ("slow down the fan", "set the fan speed to 20"),
    ("pankha teen pe laga do", "set the fan speed to 60"), ("fan do karo", "set the fan speed to 40"),
    ("pankha ek karo", "set the fan speed to 20"), ("fan paanch laga do", "set the fan speed to 100"),
    ("fan speed 3 please", "set the fan speed to 60"), ("fan 2 please", "set the fan speed to 40"),
    ("set fan to 3 please", "set the fan speed to 60"),
    ("fan speed 3", "set the fan speed to 60"), ("set the fan to speed 3", "set the fan speed to 60"),
    ("set the fan speed to 60", "set the fan speed to 60"),
    ("fan on speed four", "set the fan speed to 80"),
    ("toggle the lights", "toggle the lights in the bedroom"),
    ("switch the lights", "toggle the lights in the bedroom"),
    ("switch on the lights", "turn on the lights in the bedroom"),
    ("switch off the lights", "turn off the lights in the bedroom"),
    ("put on the lights", "turn on the lights in the bedroom"),
    ("put the lights off", "turn off the lights in the bedroom"),
    ("dim the lights", "set the brightness of the lights in the bedroom to 50%"),
    ("lights dim", "set the brightness of the lights in the bedroom to 50%"),
    ("brighten the lights", "set the brightness of the lights in the bedroom to 100%"),
    ("lights brighter", "set the brightness of the lights in the bedroom to 100%"),
    ("set the lights to 50", "set the brightness of the lights in the bedroom to 50%"),
    ("set the lights to 50 percent", "set the brightness of the lights in the bedroom to 50%"),
    ("lights at 60 percent", "set the brightness of the lights in the bedroom to 60%"),
    ("dim the lights to 30", "set the brightness of the lights in the bedroom to 30%"),
    ("batti on karo", "turn on the lights in the bedroom"),
    ("batti off karo", "turn off the lights in the bedroom"),
    ("light off karo", "turn off the lights in the bedroom"),
    ("lights on karo", "turn on the lights in the bedroom"),
    ("batti band kar de", "turn off the lights in the bedroom"),
    ("batti bandh kardo", "turn off the lights in the bedroom"),
    ("batti band", "turn off the lights in the bedroom"),
    ("andhera karo", "turn off the lights in the bedroom"),
    ("andhera", "turn off the lights in the bedroom"),
    ("andhera kar de", "turn off the lights in the bedroom"),
    ("ujala karo", "turn on the lights in the bedroom"),
    ("roshni karo", "turn on the lights in the bedroom"),
    ("ac on", "turn on the ac"), ("turn on the ac", "turn on the ac"),
    ("ac off", "turn off the ac"), ("turn off the ac", "turn off the ac"),
    ("a/c on", "turn on the ac"), ("a.c on", "turn on the ac"),
    ("air conditioner on", "turn on the ac"),
    ("a/c 22", "set the ac to 22"),
    ("switch on the ac", "turn on the ac"), ("ac chalu karo", "turn on the ac"),
    ("ac band karo", "turn off the ac"), ("ac bandh kar de", "turn off the ac"),
    ("set the ac to 22", "set the ac to 22"), ("ac to 22", "set the ac to 22"),
    ("ac 22", "set the ac to 22"), ("temperature to 22", "set the ac to 22"),
    ("set temperature to 22", "set the ac to 22"), ("temp 22", "set the ac to 22"),
    ("set the temperature to 22", "set the ac to 22"),
    ("ac temperature to 24", "set the ac to 24"),
    ("set the ac to 22", "set the ac to 22"),
    ("cool it down", "set the ac to 22"), ("make it cooler", "set the ac to 22"),
    ("cool down", "set the ac to 22"), ("thanda karo", "set the ac to 22"),
    ("ac tez karo", "set the ac to 22"),
    ("heat it up", "set the ac to 28"), ("make it warmer", "set the ac to 28"),
    ("garam karo", "set the ac to 28"), ("ac garam karo", "set the ac to 28"),
    ("time to sleep", "good night"), ("going to bed", "good night"), ("night night", "good night"),
    ("so ja", "good night"), ("so jao", "good night"), ("neend aa rahi hai", "good night"),
    ("lights out", "good night"), ("i am leaving", "turn everything off"),
    ("i'm leaving", "turn everything off"), ("going out", "turn everything off"),
    ("leaving", "turn everything off"), ("sab band kardo", "turn everything off"),
    ("sab kuch band kar de", "turn everything off"),
    ("all on", "turn everything on"), ("sab on karo", "turn everything on"),
    # decoys / already-handled forms that must stay as-is (or use existing rule)
    ("good night", "good night"), ("sone ja raha hoon", "good night"),
    ("turn on the lights in the bedroom", "turn on the lights in the bedroom"),
    ("turn everything off", "turn everything off"), ("all off", "all off"),
    # 'all off' deliberately falls through now: it names "all", not "everything",
    # and must not fire the whole-room script from a lights request (see phrasing.py).
    ("shut it all down", "turn everything off"),
    ("pankha band", "turn off the fan"), ("pankha chalu", "turn on the fan"),
    ("batti chalu", "turn on the lights in the bedroom"), ("roshni chalu karo", "turn on the lights in the bedroom"),
    ("set the fan speed to 60", "set the fan speed to 60"),
    ("turn on the lights", "turn on the lights in the bedroom"),
    ("it is hot outside", "it is hot outside"), ("what is the temperature", "what is the temperature"),
    ("turn on the fan and the ac", "turn on the fan and the ac"),
    ("fan speed", "fan speed"), ("set the fan", "set the fan"),
    ("light one on", "light one on"), ("turn on light one", "turn on light one"),
    ("set the lights to 5", "set the brightness of the lights in the bedroom to 5%"),
]

fails = 0
for text, want in cases:
    got = norm(text, combined)
    if got != want:
        fails += 1
        print(f"MISMATCH {text!r}: want {want!r} got {got!r}")
print(f"done: {len(cases)} cases, {fails} mismatches")
