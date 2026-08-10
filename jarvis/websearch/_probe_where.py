#!/usr/bin/env python3
"""
Throwaway probe round 6: WHERE should summarisation happen?

Round 5 measured a real defect, not a latency problem: qwen2.5:3b answered
"NO ANSWER" to all three questions even where the snippet plainly contained the
answer ("Monday, 09:30 AM, 10:00 PM"). The escape hatch was too easy to take.

Three architectures are on the table and only one of them is free:

  A. EXTRACTIVE ONLY. The service ranks and trims snippets and returns them.
     The Home Assistant conversation agent then does the pass it was ALWAYS
     going to do after a tool returns, and that pass is the summariser.
     Extra inference passes: ZERO.
  B. SECOND PASS IN THE SERVICE. The service calls qwen2.5:3b itself to write
     one sentence, then hands a finished string to the agent.
     Extra inference passes: ONE.
  C. Extractive with a tuned prompt on the agent side.

The thing being tested is not "does the model produce prose" -- it is whether
the model produces a CORRECT 12-word spoken answer from trimmed snippets, and
what each option costs in wall-clock.
"""
from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
import time

import aiohttp

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SEARX = "http://127.0.0.1:8100"
OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen2.5:3b"

# Questions with a knowable right answer, so "did it work" is checkable.
CASES = [
    ("what time does big bazaar close", ["10", "pm", "22"]),
    ("how tall is mount everest", ["8,849", "8849", "29,03"]),
    ("who won the 2024 t20 cricket world cup", ["india"]),
    ("population of jaipur", ["million", "30", "3,0", "lakh"]),
]

# The real system prompt's shape, trimmed to what matters for this test.
JARVIS_SYS = (
    "You are JARVIS, Natik's assistant. Dry, composed, competent.\n"
    "YOUR REPLY IS SPOKEN ALOUD. At most 12 words. No preamble, no markdown, "
    "no lists, no URLs, no citations."
)


async def searx(session, q):
    t0 = time.monotonic()
    async with session.get(f"{SEARX}/search", params={"q": q, "format": "json"},
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        d = await r.json()
    return int((time.monotonic() - t0) * 1000), d


def pack(results, n=5, chars=300):
    return "\n".join(
        f"[{i+1}] {r.get('title','')[:90]}: {(r.get('content') or '')[:chars]}"
        for i, r in enumerate(results[:n]))


async def gen(session, prompt, system=None, npred=48):
    body = {"model": MODEL, "prompt": prompt, "stream": False,
            "options": {"num_predict": npred, "temperature": 0.1, "num_ctx": 8192}}
    if system:
        body["system"] = system
    t0 = time.monotonic()
    async with session.post(f"{OLLAMA}/api/generate", json=body,
                            timeout=aiohttp.ClientTimeout(total=180)) as r:
        d = await r.json()
    return int((time.monotonic() - t0) * 1000), (d.get("response") or "").strip(), d


def hit(text, needles):
    t = text.lower()
    return any(n.lower() in t for n in needles)


async def main():
    async with aiohttp.ClientSession() as s:
        # Warm the model so the first case is not charged for a cold cache.
        await gen(s, "hi", npred=1)

        print("=== fetch all result sets once, reuse for every variant ===")
        sets = {}
        for q, _ in CASES:
            ms, d = await searx(s, q)
            sets[q] = d.get("results", [])
            print(f"  {q[:40]:<42} {ms:>5} ms  {len(sets[q])} results")

        variants = {
            # The round-5 prompt, kept so the fix is visible as a diff.
            "v1 escape-hatch": (
                "Answer the question in ONE short sentence of at most 12 words, "
                "using only the search results. If the results do not contain the "
                "answer, reply exactly: NO ANSWER.\n\n"
                "Question: {q}\n\nSearch results:\n{ctx}\n\nAnswer:"),
            # Same job, but the model is told the answer IS present and that
            # partial information still counts. NO ANSWER stays available or the
            # service loses its no-results signal, but it is no longer the easy path.
            "v2 assume-present": (
                "The search results below contain the answer. Extract it and state "
                "it in ONE sentence of at most 12 words. Quote the specific number, "
                "name, date or time. Do not mention sources or say 'according to'. "
                "Only if the results are genuinely unrelated to the question, reply "
                "exactly: NO ANSWER.\n\n"
                "Question: {q}\n\nSearch results:\n{ctx}\n\nOne-sentence answer:"),
            # Fewer, longer snippets: is the failure recall or dilution?
            "v3 top3 assume": (
                "The search results below contain the answer. Extract it and state "
                "it in ONE sentence of at most 12 words. Quote the specific number, "
                "name, date or time.\n\n"
                "Question: {q}\n\nSearch results:\n{ctx}\n\nOne-sentence answer:"),
        }

        print("\n=== B: second pass inside the service ===")
        for vname, tmpl in variants.items():
            n = 3 if "top3" in vname else 5
            oks, mss = 0, []
            print(f"\n  --- {vname} (top {n}) ---")
            for q, needles in CASES:
                ctx = pack(sets[q], n=n)
                ms, out, d = await gen(s, tmpl.format(q=q, ctx=ctx))
                ok = hit(out, needles)
                oks += ok
                mss.append(ms)
                print(f"    [{'OK ' if ok else 'MISS'}] {ms:>5} ms  "
                      f"peval={d.get('prompt_eval_count'):<5} {out[:100]}")
            print(f"    score {oks}/{len(CASES)}  median {statistics.median(mss):.0f} ms")

        print("\n=== A: extractive only, agent does its normal pass ===")
        # What the agent sees is a tool RESULT, so frame it the way HA does.
        oks, mss = 0, []
        for q, needles in CASES:
            ctx = pack(sets[q], n=5, chars=300)
            prompt = (f"The user asked: {q}\n\n"
                      f"web_search returned:\n{ctx}\n\n"
                      f"Answer the user out loud now.")
            ms, out, d = await gen(s, prompt, system=JARVIS_SYS)
            ok = hit(out, needles)
            oks += ok
            mss.append(ms)
            words = len(re.findall(r"\S+", out))
            print(f"    [{'OK ' if ok else 'MISS'}] {ms:>5} ms  {words:>2}w  "
                  f"peval={d.get('prompt_eval_count'):<5} {out[:110]}")
        print(f"    score {oks}/{len(CASES)}  median {statistics.median(mss):.0f} ms")


if __name__ == "__main__":
    asyncio.run(main())
