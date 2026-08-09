#!/usr/bin/env python3
"""
Offline tests for the pure logic in closed_loop.py -- claim extraction, tool
attribution, and the last-turn scoping rule. No Home Assistant, no hardware.

These exist because the regex layer is the part most likely to fail SILENTLY.
A missed claim means a hallucination sails through; an over-eager match means a
correct reply gets contradicted, which trains the user to ignore corrections.
Both are trust bugs, so both are pinned here.

Run:  python3 verify/test_logic.py
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from closed_loop import (  # noqa: E402
    ToolCall,
    _calls_from_last_turn,
    claims_action,
    extract_claims,
)

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}\n         got  {got!r}\n         want {want!r}")


print("\n--- claim extraction -------------------------------------------------")

# The exact hallucination measured on this build, 2026-08-08.
c = extract_claims("AC set to 22. Fan speed is now 3.")
check("measured defect: both claims found",
      sorted((x.device, x.kind, x.value) for x in c),
      [("ac", "value", 22), ("fan", "value", 3)])

c = extract_claims("Fan on four.")
check("spelled-out fan speed", [(x.device, x.value) for x in c], [("fan", 4)])

c = extract_claims("AC at 22, sir.")
check("persona phrasing", [(x.device, x.value) for x in c], [("ac", 22)])

c = extract_claims("Lights on.")
check("light power on", [(x.device, x.kind, x.value) for x in c],
      [("light", "power", "on")])

c = extract_claims("Lights off.")
check("light power off", [(x.device, x.kind, x.value) for x in c],
      [("light", "power", "off")])

c = extract_claims("Air conditioner is now 24 degrees.")
check("long form AC", [(x.device, x.value) for x in c], [("ac", 24)])

# Out-of-range numbers must not be read as settings.
c = extract_claims("It is 45 degrees outside.")
check("out-of-range number ignored", c, [])

c = extract_claims("Fan speed is now 3.")
check("fan only", [(x.device, x.value) for x in c], [("fan", 3)])

print("\n--- action assertion detection ---------------------------------------")

check("plain confirmation", claims_action("AC set to 22."), True)
check("lights on is a claim", claims_action("Lights on."), True)
check("done is a claim", claims_action("Done."), True)
check("honest refusal is not", claims_action("There is no kettle here."), False)
check("cannot is not a claim", claims_action("I cannot do that."), False)
check("could not find is not", claims_action("I could not find the fan."), False)
check("question answer is not",
      claims_action("The current time is 7:48 PM."), False)
check("empty is not", claims_action(""), False)
check("sorry is not", claims_action("Sorry, that failed."), False)

print("\n--- tool call -> device attribution ----------------------------------")

check("script ac_set_temperature",
      ToolCall("ac_set_temperature", {"temperature": 22}).devices(), {"ac"})
check("script fan_set", ToolCall("fan_set", {"level": 3}).devices(), {"fan"})
check("script all_off", ToolCall("all_off", {}).devices(), {"light", "fan", "ac"})
check("script good_night",
      ToolCall("good_night", {}).devices(), {"light", "ac", "fan"})
check("builtin HassTurnOn light domain",
      ToolCall("HassTurnOn", {"area": "Bedroom", "domain": ["light"]}).devices(),
      {"light"})
check("builtin HassFanSetSpeed",
      ToolCall("HassFanSetSpeed", {"percentage": 20, "domain": ["fan"]}).devices(),
      {"fan"})

print("\n--- failure classification -------------------------------------------")

ok = ToolCall("ac_set_temperature", {}, {"success": True, "result": {}}, True)
check("success not failed", ok.failed, False)

err = ToolCall("HassFanSetSpeed", {}, {"error": "MatchFailedError"}, True)
check("MatchFailedError is failed", err.failed, True)

no_resp = ToolCall("fan_set", {"level": 3}, None, False)
check("called but no result is failed", no_resp.failed, True)

partial = ToolCall(
    "HassTurnOn", {},
    {"data": {"success": [], "failed": [{"id": "light.x"}]}}, True,
)
check("intent with failed targets", partial.failed, True)

print("\n--- last-turn scoping (history must not be credited) -----------------")

# max_history=2, so a previous turn's successful call sits in the same log. If
# it were counted, a hallucinated second turn would verify as genuine.
content = [
    {"role": "system", "content": "prompt"},
    {"role": "user", "content": "turn on the lights"},
    {"role": "assistant", "tool_calls": [
        {"tool_name": "HassTurnOn", "tool_args": {"domain": ["light"]}, "id": "a"}]},
    {"role": "tool_result", "tool_call_id": "a", "tool_name": "HassTurnOn",
     "tool_result": {"data": {"success": [{"id": "light.x"}], "failed": []}}},
    {"role": "assistant", "content": "Lights on."},
    {"role": "user", "content": "set the ac to 22"},
    {"role": "assistant", "content": "AC set to 22."},
]
calls = _calls_from_last_turn(content)
check("previous turn's calls excluded", [c.name for c in calls], [])

content_ok = content[:-1] + [
    {"role": "assistant", "tool_calls": [
        {"tool_name": "ac_set_temperature", "tool_args": {"temperature": 22},
         "id": "b"}]},
    {"role": "tool_result", "tool_call_id": "b", "tool_name": "ac_set_temperature",
     "tool_result": {"success": True, "result": {}}},
    {"role": "assistant", "content": "AC set to 22."},
]
calls = _calls_from_last_turn(content_ok)
check("current turn's call found", [c.name for c in calls], ["ac_set_temperature"])
check("result paired by id", calls[0].responded and not calls[0].failed, True)

print(f"\n{'=' * 70}\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
