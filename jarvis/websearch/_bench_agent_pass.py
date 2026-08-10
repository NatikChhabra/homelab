#!/usr/bin/env python3
"""
The last number needed to choose where summarisation happens.

The service can hand the conversation agent either:
  A. ~330 tokens of trimmed snippets (extractive), and the agent writes the
     sentence. Zero extra inference passes.
  B. ~20 tokens, one finished sentence (summarise). One extra pass, measured at
     a median of 1187 ms end to end.

A looks free, but it is not: the agent still has to READ those 330 tokens, and
prompt evaluation is not free on a 3B on a 4 GB card. This measures the agent's
own pass in both cases so the comparison is like-for-like rather than
"B costs a pass and A costs nothing".

Simulates the agent turn with the real system prompt shape and a tool result.
"""
from __future__ import annotations

import asyncio
import re
import statistics
import sys
import time

import aiohttp

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen2.5:3b"

JARVIS_SYS = (
    "You are JARVIS, Natik's assistant for this bedroom. Dry, composed, "
    "competent. Never eager, never apologetic, never chatty.\n"
    "YOUR REPLY IS SPOKEN ALOUD. At most 12 words. No preamble, no restating "
    "the request, no markdown, no lists, no emoji."
)

# What the agent receives after the tool returns, in each design.
LONG_RESULT = """[1] Jaipur - Wikipedia: Jaipur is the capital and the largest city of the north-western Indian state of Rajasthan. As of 2011, the city had a population of 3.1 million, making it the tenth most populous city in the country.
[2] Jaipur City Population 2026 | Literacy and Hindu Muslim Population: Jaipur Population 2026. Jaipur city is governed by Municipal Corporation and is situated in Rajasthan State/UT. The current estimate population of Jaipur in 2026 is around 4.1 million.
[3] Jaipur, India Metro Area Population (1950-2026) - Macrotrends: Chart and table of population level and growth rate for the Jaipur, India metro area from 1950 to 2026. United Nations population projections are also included.
[4] Home | District Jaipur, Rajasthan Government: Census 2011 suggests the district's population as 66,26,178. The population density is calculated at 598 persons per square kilometre.
[5] Jaipur Population 2026 - World Population Review: Jaipur's 2026 population is now estimated at 4,238,000. In 1950, the population of Jaipur was 293,000."""

SHORT_RESULT = "As of 2011, Jaipur had a population of 3.1 million."

QUESTION = "what is the population of jaipur"


async def turn(session, tool_result: str, n: int = 5):
    prompt = (f"The user asked: {QUESTION}\n\n"
              f"web_search returned:\n{tool_result}\n\n"
              f"Answer the user out loud now.")
    times, evals, outs = [], [], []
    for _ in range(n):
        t0 = time.monotonic()
        async with session.post(f"{OLLAMA}/api/generate", json={
            "model": MODEL, "prompt": prompt, "system": JARVIS_SYS,
            "stream": False,
            "options": {"num_predict": 48, "temperature": 0.1, "num_ctx": 8192},
        }, timeout=aiohttp.ClientTimeout(total=120)) as r:
            d = await r.json()
        times.append(int((time.monotonic() - t0) * 1000))
        evals.append(d.get("prompt_eval_count", -1))
        outs.append((d.get("response") or "").strip())
    return times, evals, outs


async def main():
    async with aiohttp.ClientSession() as s:
        # Warm, so the first sample is not a cold load.
        async with s.post(f"{OLLAMA}/api/generate",
                          json={"model": MODEL, "prompt": "hi", "stream": False,
                                "options": {"num_predict": 1}},
                          timeout=aiohttp.ClientTimeout(total=180)) as r:
            await r.json()

        for name, payload in [("A extractive (long tool result)", LONG_RESULT),
                              ("B summarise (one sentence)", SHORT_RESULT)]:
            times, evals, outs = await turn(s, payload)
            words = [len(re.findall(r"\S+", o)) for o in outs]
            over = sum(1 for w in words if w > 12)
            print(f"\n=== {name} ===")
            print(f"  tool result: {len(payload)} chars")
            print(f"  agent pass:  median {statistics.median(times):.0f} ms  "
                  f"min {min(times)}  max {max(times)}")
            print(f"  prompt_eval: {statistics.median(evals):.0f} tokens")
            print(f"  reply words: {words}   over 12 words: {over}/{len(words)}")
            for o in outs[:3]:
                print(f"    -> {o[:100]}")


if __name__ == "__main__":
    asyncio.run(main())
