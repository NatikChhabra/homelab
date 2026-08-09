"""
Does specialist routing actually beat the 17-tool agent?

Two things are measured, because they fail independently:

  1. CLASSIFICATION -- does `classify()` send each utterance to a specialist
     that owns the right tool? A perfect specialist reached by a wrong router is
     worthless, and this half costs ~0 ms so it should be near-perfect.

  2. TOOL-CALL ACCURACY -- given the right specialist, does qwen2.5:3b emit the
     right tool AND the right arguments? Argument correctness is graded, not
     just the tool name. "set the AC to 22" scoring a hit while the model passed
     temperature="22" as a string, or 24 as the value, is precisely the failure
     that made the single-agent build feel broken.

Read-only: talks to Ollama directly and never contacts Home Assistant, so it
cannot actuate a device. That matters -- an earlier benchmark tonight fired
"turn everything off" and the TV toggle three times each and switched off
Natik's monitor mid-session.

    python hermes/bench_hermes.py [reps]
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hermes import Hermes, SPECIALISTS, classify  # noqa: E402

OLLAMA = os.getenv("OLLAMA_URL", "http://jarvis-ollama:11434")

# (utterance, expected specialist, acceptable tools, required arguments or None)
CASES: list[tuple[str, str, set[str], dict | None]] = [
    # --- argument extraction: the thing the single agent measurably got wrong
    ("set the AC to 22", "climate", {"ac_set_temperature"}, {"temperature": 22}),
    ("make it 26 degrees", "climate", {"ac_set_temperature"}, {"temperature": 26}),
    ("AC ko 20 par kar do", "climate", {"ac_set_temperature"}, {"temperature": 20}),
    ("set the fan to 5", "fan", {"fan_set"}, {"level": 5}),
    ("fan speed 2", "fan", {"fan_set"}, {"level": 2}),
    ("turn the fan off", "fan", {"fan_set"}, {"level": 0}),

    # --- ambiguous / natural targets
    ("turn on the lights", "lights", {"HassTurnOn"}, None),
    ("turn off the bed light", "lights", {"HassTurnOff"}, None),
    ("dim the lights", "lights", {"HassLightSet"}, None),
    ("batti bandh karo", "lights", {"HassTurnOff"}, None),

    # --- comfort reasoning
    ("it's boiling in here", "comfort", {"ac_set_temperature", "ac_max_cool", "fan_set"}, None),
    ("garmi lag rahi hai", "comfort", {"ac_set_temperature", "ac_max_cool", "fan_set"}, None),
    # The 8-out-of-8 failure of the single agent: humidity paired with a fan
    # action in the prompt while no fan tool existed.
    ("it's humid in here", "climate", {"ac_dry"}, None),
    ("the air feels sticky", "climate", {"ac_dry"}, None),
    ("I'm freezing", "comfort", {"ac_set_temperature", "fan_set"}, None),

    # --- scenes
    ("good night", "scenes", {"good_night"}, None),
    ("turn everything off", "scenes", {"all_off"}, None),
    ("I'm going to watch a movie", "scenes", {"movie_time", "tv_toggle"}, None),
    ("turn the tv on", "scenes", {"tv_toggle"}, None),

    # --- boost
    ("boost the fan", "fan", {"fan_boost", "fan_set"}, None),
    ("cool it down fast", "climate", {"ac_max_cool", "ac_set_temperature"}, None),
]


def args_ok(got: dict, want: dict | None) -> bool:
    if want is None:
        return True
    for k, v in want.items():
        g = got.get(k)
        # Accept "22" as well as 22: HA coerces it, but record it -- the model
        # emitting a string is a real signal about prompt quality.
        try:
            if int(g) != int(v):
                return False
        except (TypeError, ValueError):
            return False
    return True


async def main(reps: int) -> int:
    h = Hermes(ollama_url=OLLAMA)

    # --- half 1: classification, no model involved
    cls_ok = 0
    cls_bad = []
    for utt, want_spec, _, _ in CASES:
        got, why = classify(utt)
        if got == want_spec:
            cls_ok += 1
        else:
            cls_bad.append((utt, want_spec, got, why))
    print(f"CLASSIFICATION: {cls_ok}/{len(CASES)} correct")
    for utt, want, got, why in cls_bad:
        print(f"  MISROUTED {utt!r}: wanted {want}, got {got} ({why})")

    # --- half 2: tool-call accuracy through the real model
    print(f"\nTOOL CALLS (reps={reps}, temp=0, specialist forced to the correct one)")
    tool_hits = 0
    arg_hits = 0
    arg_cases = 0
    string_args = 0
    latencies = []
    rows = []
    async with aiohttp.ClientSession() as session:
        for utt, want_spec, ok_tools, want_args in CASES:
            names_seen, args_seen, hit, ahit = [], [], 0, 0
            for _ in range(reps):
                r = await h.ask(session, utt, specialist=want_spec)
                latencies.append(r.total_ms)
                got_names = {c["name"] for c in r.tool_calls}
                names_seen.append(sorted(got_names))
                if got_names & ok_tools:
                    hit += 1
                    if want_args is not None:
                        call = next(c for c in r.tool_calls if c["name"] in ok_tools)
                        raw = call["arguments"]
                        if any(isinstance(v, str) for v in raw.values()):
                            string_args += 1
                        args_seen.append(raw)
                        if args_ok(raw, want_args):
                            ahit += 1
            tool_hit = hit == reps
            tool_hits += tool_hit
            if want_args is not None:
                arg_cases += 1
                arg_hits += (ahit == reps)
            mark = "OK " if tool_hit and (want_args is None or ahit == reps) else "NO "
            extra = f" args={args_seen[0]}" if args_seen else ""
            print(f"  {mark}{utt:32} -> {names_seen[0]}{extra}")
            rows.append({"utt": utt, "specialist": want_spec, "tool_hit": tool_hit,
                         "arg_hit": None if want_args is None else (ahit == reps),
                         "seen": names_seen[0]})

    print(f"\ntool accuracy : {tool_hits}/{len(CASES)} ({100*tool_hits//len(CASES)}%)")
    if arg_cases:
        print(f"arg  accuracy : {arg_hits}/{arg_cases} ({100*arg_hits//arg_cases}%) "
              f"across the cases that carry an argument")
    print(f"string-typed args emitted: {string_args}")
    print(f"median latency: {statistics.median(latencies):.0f} ms")
    print(f"tools per specialist: {json.dumps({n: len(s.tools) for n, s in SPECIALISTS.items()})}")

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results-hermes.json")
    with open(out, "w") as fh:
        json.dump({"classification": {"ok": cls_ok, "total": len(CASES)},
                   "tool_accuracy": {"ok": tool_hits, "total": len(CASES)},
                   "arg_accuracy": {"ok": arg_hits, "total": arg_cases},
                   "median_ms": statistics.median(latencies),
                   "rows": rows}, fh, indent=2)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 2)))
