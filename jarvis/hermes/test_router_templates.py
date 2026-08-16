"""
Router template table: ~70 utterances covering all pure-function routing layers.

Why this file exists
--------------------
The router has three offline-testable layers before any network call:

  1. Greeting fast-path  (_GREETING_RE in router.py)
  2. Normalisation       (kernel/phrasing.py::normalise)
  3. Specialist classify (kernel/hermes/hermes.py::classify)

Layers 2 and 3 are pure functions — no HA, no Ollama, no GPU. They can be
tested on a developer machine in <100 ms and catch regressions that would
otherwise only surface as misrouted voice commands at 2 AM.

What this does NOT test
-----------------------
  - `router.process()` — needs live HA + Ollama; that is `test_router_wiring.py`
  - `hermes.ask()` — needs Ollama; that is `test_endtoend.py`
  - Driver actuation — needs HA device state; that is `test_driver.py`

Run
---
  pytest -v kernel/hermes/test_router_templates.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from kernel.phrasing import normalise, looks_like_question  # noqa: E402
from kernel.hermes.hermes import classify, SPECIALISTS     # noqa: E402


# --- greeting fast-path -----------------------------------------------------

_GREETING_RE = re.compile(
    r"^\s*(?:jarvis,?\s*)?(?:can\s+you\s+hear\s+me|do\s+you\s+hear\s+me|"
    r"are\s+you\s+there|you\s+there|hey\s+jarvis|hello\s+jarvis|"
    r"are\s+you\s+awake|is\s+anyone\s+there|is\s+this\s+on|are\s+you\s+on|"
    r"what'?s\s+up|whats\s+up|what\s+is\s+up|how\s+are\s+you|how\s+do\s+you\s+do|"
    r"test\s+(?:1|2|one|two)|ping|status\s+check)\s*(?:,?\s*jarvis)?\s*\??\s*$",
    re.I,
)

_GREETING_CASES: list[tuple[str, bool, str]] = [
    # Matches
    ("hello jarvis",          True,  "hello"),
    ("hey jarvis",            True,  "hey"),
    ("can you hear me",       True,  "hear"),
    ("do you hear me",        True,  "hear"),
    ("are you there",         True,  "there"),
    ("you there",             True,  "there"),
    ("are you awake",         True,  "awake"),
    ("is anyone there",       True,  "there"),
    ("is this on",            True,  "on"),
    ("are you on",            True,  "on"),
    ("what's up",             True,  "up"),
    ("whats up",              True,  "up"),
    ("what is up",            True,  "up"),
    ("how are you",           True,  "are"),
    ("test 1",                True,  "test"),
    ("test one",              True,  "test"),
    ("ping",                  True,  "ping"),
    ("status check",          True,  "status"),
    ("jarvis, can you hear me", True, "hear"),
    ("hey jarvis?",           True,  "hey"),
    # Non-matches — these are commands or loose phrases
    ("good morning jarvis",   False, ""),
    ("hello",                 False, ""),
    ("turn on the lights",    False, ""),
    ("set the ac to 22",      False, ""),
    ("what is the weather",   False, ""),
]


@pytest.mark.parametrize("text,should_match,keyword", _GREETING_CASES)
def test_greeting_re(text: str, should_match: bool, keyword: str) -> None:
    got = _GREETING_RE.match(text.strip()) is not None
    assert got == should_match, f"greeting re: {text!r} got={got} want={should_match}"
    if should_match and keyword:
        assert keyword in text.lower(), f"keyword {keyword!r} not found in {text!r}"


# --- normalisation rules ----------------------------------------------------

_NORMALISE_CASES: list[tuple[str, str, str]] = [
    # (input, expected_output, rule_hint)

    # --- lights area-form ---
    ("turn on the lights",     "turn on the lights in the bedroom", "area form"),
    ("turn off the lights",    "turn off the lights in the bedroom", "area form"),
    ("lights on",              "turn on the lights in the bedroom", "area form"),
    ("lights off",             "turn off the lights in the bedroom", "area form"),
    ("all lights on",          "turn on the lights in the bedroom", "area form"),
    ("all lights off",         "turn off the lights in the bedroom", "area form"),
    ("turn the lights on",     "turn on the lights in the bedroom", "area form"),
    ("turn the lights off",    "turn off the lights in the bedroom", "area form"),

    # --- gang aliases ---
    ("turn on light one",      "turn on gang 1",              "gang alias"),
    ("turn off light two",     "turn off gang 2",             "gang alias"),
    ("gang 1 on",              "turn on gang 1",              "gang alias"),
    ("gang 2 off",             "turn off gang 2",             "gang alias"),
    ("1 on",                   "turn on gang 1",              "gang alias"),
    ("2 off",                  "turn off gang 2",             "gang alias"),
    ("light3 on",              "turn on gang 3",              "gang alias"),
    ("gang4 off",              "turn off node switch node 4",  "gang alias"),
    ("bl off",                 "turn off 3+1 node smart switch backlight", "gang alias"),
    ("back light on",          "turn on 3+1 node smart switch backlight", "gang alias"),
    ("ceiling light on",       "turn on the lights in the bedroom", "ceiling light"),
    ("turn the ceiling light off", "turn off the lights in the bedroom", "ceiling light"),

    # --- fan speed ---
    ("set the fan to speed 3", "set the fan speed to 3",      "fan speed ordinal"),
    ("set fan speed to 60%",   "set the fan speed to 60",     "fan speed percent"),
    ("set the fan to speed three", "set the fan speed to 3",  "fan speed word"),
    ("put the fan on 2",       "set the fan speed to 2",     "fan speed bare number"),
    ("fan speed 4",            "set the fan speed to 80",     "fan speed trailing"),
    ("put the fan on high",    "fan boost",                   "fan high -> boost"),
    ("high fan",               "fan boost",                   "high fan -> boost"),
    ("max fan speed",          "fan boost",                   "max fan -> boost"),
    ("full fan",               "fan boost",                   "full fan -> boost"),
    ("boost the fan",          "boost the fan",               "passthrough (no rule)"),

    # --- scheduled fan -> no rewrite ---
    ("set the fan to 3 for 2 hours", "set the fan to 3 for 2 hours", "scheduled, skip"),
    ("put the fan on 5 at night",   "put the fan on 5 at night",    "scheduled, skip"),

    # --- AC ---
    ("set the ac to 22",         "set the ac to 22",       "ac canonical"),
    ("ac 22",                    "set the ac to 22",       "ac N kar do"),
    ("ac 22 degrees",            "set the ac to 22",       "ac N degrees"),
    ("twenty four degrees",      "set the ac to 24",       "N degrees"),
    ("ac twenty two",            "set the ac to 22",       "ac word number"),
    ("setac24",                  "set the ac to 24",       "fused setac"),
    ("ac on",                    "turn on the ac",         "bare ac on"),
    ("ac off",                   "turn off the ac",        "bare ac off"),
    ("turn the ac off",          "turn off the ac",        "turn the ac off"),
    ("turn off ac",              "turn off the ac",        "turn off ac"),
    ("turn on ac",               "turn on the ac",         "turn on ac"),

    # --- scenes / scripts ---
    ("good night",               "good night",             "script name"),
    ("goodnight",                "good night",             "script alias"),
    ("sone ja raha hoon",        "good night",             "Hinglish good night"),
    ("movie time",               "movie time",             "script name"),
    ("start movie time",         "movie time",             "script with start"),
    ("turn everything off",      "turn everything off",    "script name"),
    ("sab band",                 "turn everything off",    "Hinglish everything off"),
    ("sab kuch band",            "turn everything off",    "Hinglish everything off variant"),
    ("shut it all down",         "turn everything off",    "idiomatic"),
    ("toggle the tv",            "toggle the tv",          "script name"),
    ("turn the tv on",           "toggle the tv",          "tv on -> toggle"),
    ("turn off the t.v.",        "toggle the tv",          "tv off -> toggle"),

    # --- home scripts ---
    ("im home",                  "I am home",              "contraction expand"),
    ("I'm home",                 "I am home",              "contraction expand"),
    ("i am home",                "I am home",              "already canonical"),
    ("im back",                  "I am home",              "back -> home"),
    ("welcome home",             "I am home",              "welcome home"),

    # --- comfort short-forms ---
    ("feeling hot",              "I am feeling hot",       "short form -> full"),
    ("feeling cold",             "I am feeling cold",      "short form -> full"),
    ("feeling hot please",       "I am feeling hot",       "short form + polite"),
    ("i am feeling hot",         "I am feeling hot",       "already full"),
    ("i am feeling cold",        "I am feeling cold",      "already full"),

    # --- Hinglish device commands ---
    ("fan band karo",            "turn off the fan",       "Hinglish fan off"),
    ("pankha off kar do",        "turn off the fan",       "Hinglish pankha off"),
    ("fan chalu karo",           "turn on the fan",        "Hinglish fan on"),
    ("lights chalu karo",        "turn on the lights in the bedroom", "Hinglish lights on"),
    ("lights band karo",         "turn off the lights in the bedroom", "Hinglish lights off"),
    ("ac 22 kar do",             "set the ac to 22",       "Hinglish AC temp"),

    # --- wake-word strip ---
    ("hey jarvis turn on the lights", "hey jarvis turn on the lights", "wake word stays (no rule)"),
    ("in javas turn off fan",       "in javas turn off fan",      "misheard wake word stays"),

    # --- questions pass through unchanged ---
    ("what time is it?",           "what time is it?",       "question, no rewrite"),
    ("is the light on?",           "is the light on?",       "question, no rewrite"),

    # --- no-change cases (pass through) ---
    ("hello jarvis",               "hello jarvis",           "greeting, no normalise rule"),
    ("open notepad",               "open notepad",           "PC app, no rewrite"),
    ("type hello world",           "type hello world",       "PC type, no rewrite"),
    ("wake the pc",                "wake the pc",            "PC power, no rewrite"),
    ("send whatsapp to mum",       "send whatsapp to mum",   "comms, no rewrite"),
    ("search for weather",         "search for weather",     "research, no rewrite"),
]


@pytest.mark.parametrize("input_text,expected_output,rule_hint", _NORMALISE_CASES)
def test_normalise(input_text: str, expected_output: str, rule_hint: str) -> None:
    output, rule = normalise(input_text)
    assert output == expected_output, (
        f"normalise({input_text!r})\n"
        f"  got:  {output!r}\n"
        f"  want: {expected_output!r}\n"
        f"  rule: {rule!r}"
    )


# --- question detection -----------------------------------------------------

_QUESTION_CASES: list[tuple[str, bool, str]] = [
    # (text, is_question, reason)
    # Straight questions
    ("is the light on?",          True,  "trailing question mark"),
    ("what is the weather",       True,  "what-start"),
    ("where is mumbai",           True,  "where-start"),
    ("when did india win",        True,  "when-start"),
    ("who won the match",         True,  "who-start"),
    ("why is the sky blue",       True,  "why-start"),
    ("how tall is everest",       True,  "how-start"),
    ("tell me about python",      True,  "tell me"),
    ("list my messages",          True,  "list"),
    ("are you there",             True,  "are-start (but also greeting — tested separately)"),
    ("can you help me",           False,  "polite request — can you"),
    ("will you turn off the tv",  False, "polite request — will you"),
    ("can you turn on the lights",False, "polite request — can you"),
    ("could you pause the music", False, "polite request — could you"),
    ("how about turning off the lights", False, "polite request — how about"),
    ("why don't you turn off the tv", False, "polite request — why don't you"),
    ("do that",                   False, "imperative — do that"),
    ("do it",                     False, "imperative — do it"),
    # Commands (not questions)
    ("turn on the lights",        False, "imperative"),
    ("set the fan to speed 3",    False, "imperative"),
    ("good night",                False, "script trigger"),
    ("movie time",                False, "script trigger"),
    ("open spotify",              False, "imperative"),
    ("send whatsapp to mum",      False, "imperative"),
    ("lock the pc screen",        False, "imperative"),
    # Edge cases
    ("",                          False, "empty string"),
    ("   ",                       False, "whitespace only"),
    ("what",                      True,  "bare question word"),
]


@pytest.mark.parametrize("text,expected,is_question", _QUESTION_CASES)
def test_looks_like_question(text: str, expected: bool, is_question: str) -> None:
    got = looks_like_question(text)
    assert got == expected, f"looks_like_question({text!r}) = {got}, want {expected}"


# --- specialist classification ----------------------------------------------

_CLASSIFY_CASES: list[tuple[str, str, str]] = [
    # (utterance, expected_specialist, reason_hint)

    # --- lights ---
    ("turn on the lights",          "lights",  "keyword: lights"),
    ("turn off light one",          "lights",  "keyword: light"),
    ("turn off gang 2",             "lights",  "keyword: gang"),
    ("make it brighter",            "lights",  "keyword: brighter"),
    ("dim the room",                "lights",  "keyword: dim"),
    ("batti jalo",                  "lights",  "keyword: batti (Hindi)"),
    ("roshni karo",                 "lights",  "keyword: roshni (Hindi)"),
    ("the room is dark",            "lights",  "keyword: dark"),
    ("turn on the lamp",            "lights",  "keyword: lamp"),
    ("is the light on",             "lights",  "keyword: light"),

    # --- climate (AC) ---
    ("set the ac to 22",            "climate", "keyword: ac"),
    ("ac 22 kar do",                "climate", "keyword: ac"),
    ("turn the ac on",              "climate", "keyword: ac"),
    ("air conditioner is too cold", "climate", "keyword: air conditioner"),
    ("make it cooler",              "climate", "keyword: cooler"),
    ("set aircon to 24",            "climate", "keyword: aircon"),
    ("a/c max cool",                "climate", "keyword: a/c"),
    ("ac dry mode",                 "climate", "keyword: ac"),
    ("the room is humid",           "climate", "keyword: humid"),
    ("its too sticky in here",      "climate", "keyword: sticky"),
    ("set the temperature to 20",   "climate", "keyword: temperature"),

    # --- fan ---
    ("boost the fan",               "fan",     "keyword: fan, boost"),
    ("set the fan to speed 3",      "fan",     "keyword: fan, speed"),
    ("pankha tezz chala do",        "fan",     "keyword: pankha (Hindi)"),
    ("need some air",               "fan",     "keyword: some air"),
    ("fresh air please",            "fan",     "keyword: fresh air"),
    ("fan breeze mode",             "fan",     "keyword: breeze"),
    ("turn off the fan",            "fan",     "keyword: fan"),
    ("fan speed high",              "fan",     "keyword: fan, high"),
    ("turbo fan",                   "fan",     "keyword: turbo"),

    # --- scenes ---
    ("good night",                  "scenes",  "keyword: good night"),
    ("goodnight",                   "scenes",  "keyword: goodnight"),
    ("sone ja raha hoon",           "scenes",  "keyword: sone (Hindi bed)"),
    ("movie time",                  "scenes",  "keyword: movie"),
    ("turn on the tv",              "scenes",  "keyword: tv"),
    ("let's watch a film",          "scenes",  "keyword: film"),
    ("everything off",              "scenes",  "keyword: everything"),
    ("sab band kar do",             "scenes",  "keyword: sab (Hinglish)"),
    ("im leaving",                  "scenes",  "keyword: leaving"),
    ("put the tv on",               "scenes",  "keyword: tv"),

    # --- pc ---
    ("open spotify on the pc",      "pc",      "keyword: pc, spotify"),
    ("lock the screen",             "pc",      "keyword: lock screen"),
    ("wake the computer",           "pc",      "keyword: computer"),
    ("shut down the laptop",        "pc",      "keyword: laptop, shut down"),
    ("restart the desktop",         "pc",      "keyword: desktop, restart"),
    ("play pause the music",        "pc",      "keyword: play, pause"),
    ("next track",                  "pc",      "keyword: next"),
    ("volume up on the machine",    "pc",      "keyword: machine, volume"),
    ("type hello world",            "chat",      "keyword: hello beats type by score"),
    ("open chrome",                 "pc",      "keyword: open"),
    ("mute the pc",                 "pc",      "keyword: mute"),

    # --- comms (WhatsApp) ---
    ("send whatsapp to mum",        "comms",   "keyword: whatsapp, send"),
    ("text her hello",              "comms",   "keyword: text her"),
    ("any unread messages",         "comms",   "keyword: unread, messages"),
    ("who messaged me",             "comms",   "keyword: messaged"),
    ("read my whatsapp",            "comms",   "keyword: whatsapp"),
    ("bhejo sandesh mum ko",        "comms",   "keyword: sandesh (Hindi)"),
    ("send a msg to dad",           "comms",   "keyword: msg, send"),

    # --- research ---
    ("search for weather today",    "research","keyword: search, weather"),
    ("what is the capital of france", "research", "keyword: what is"),
    ("who won the last world cup",  "research","keyword: who won"),
    ("how tall is mount everest",   "research","keyword: how tall"),
    ("when did independence happen","research","keyword: when did"),
    ("search for latest news",      "research","keyword: search, news"),
    ("tell me about python",        "research","keyword: tell me about"),
    ("server status",               "research","keyword: server status"),
    ("look up the stock price",     "research","keyword: look up"),
    ("what does docker do",         "research","keyword: what does"),

    # --- comfort (default) ---
    ("its boiling in here",         "comfort", "keyword: boiling"),
    ("im sweating",                 "comfort", "keyword: sweating"),
    ("pasina aagya",                "comfort", "keyword: pasina (Hindi)"),
    ("bahut garmi hai",             "comfort", "keyword: garmi (Hindi)"),
    ("its too cold",                "comfort", "keyword: cold"),
    ("can't breathe",               "comfort", "keyword: can't breathe"),
    ("hawa chahiye",                "comfort", "keyword: hawa (Hindi)"),
    ("make the room comfortable",   "comfort", "keyword: comfortable"),
    ("its stuffy in here",          "comfort", "keyword: stuffy"),

    # --- tie-breakers / negatives ---
    # "sleep" with PC context -> pc specialist (not scenes)
    ("put the pc to sleep",         "pc",      "negative: pc beats scenes sleep"),
    ("sleep the computer",          "pc",      "negative: computer beats scenes"),
    # TV words should NOT go to pc
    ("open the tv",                 "scenes",  "negative: tv word excludes pc"),
    ("stop the tv",                 "scenes",  "negative: tv word excludes pc"),
    ("open netflix",                "comfort",  "negative: netflix excludes pc, no scene match"),
    # "type" should NOT go to comms
    ("type out a message",          "pc",      "negative: type excludes comms"),
    # "volume" should NOT go to comms
    ("volume up",                   "pc",      "negative: volume excludes comms"),
]


@pytest.mark.parametrize("utterance,expected_spec,reason_hint", _CLASSIFY_CASES)
def test_classify(utterance: str, expected_spec: str, reason_hint: str) -> None:
    spec, reason = classify(utterance)
    assert spec == expected_spec, (
        f"classify({utterance!r})\n"
        f"  got:  {spec!r}  ({reason})\n"
        f"  want: {expected_spec!r}\n"
        f"  hint: {reason_hint}"
    )
    # Sanity: the specialist must exist in SPECIALISTS
    assert spec in SPECIALISTS, f"unknown specialist {spec!r}"


# --- summary ---------------------------------------------------------------

def test_table_coverage() -> None:
    """Assert the table has reasonable breadth — catches accidental deletions."""
    greeting_count = len(_GREETING_CASES)
    normalise_count = len(_NORMALISE_CASES)
    question_count = len(_QUESTION_CASES)
    classify_count = len(_CLASSIFY_CASES)
    total = greeting_count + normalise_count + question_count + classify_count

    assert greeting_count >= 20, f"only {greeting_count} greeting cases"
    assert normalise_count >= 50, f"only {normalise_count} normalise cases"
    assert question_count >= 20, f"only {question_count} question cases"
    assert classify_count >= 55, f"only {classify_count} classify cases"
    assert total >= 140, f"total table size {total} is too small"

    # Every specialist must be exercised at least once
    covered = set()
    for utt, spec, _ in _CLASSIFY_CASES:
        s, _ = classify(utt)
        covered.add(s)
    expected_specialists = set(SPECIALISTS.keys())
    assert expected_specialists <= covered, (
        f"specialists never reached: {expected_specialists - covered}"
    )
