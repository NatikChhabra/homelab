#!/usr/bin/env python3
"""
Throwaway probe round 5: numbers for the README.

  (a) SearxNG latency over repeated queries -- one sample is not a measurement.
  (b) How many TOKENS a raw result set costs. num_ctx is 8192 and the system
      prompt plus tool schemas already live in it, so "search results are long"
      needs to be an actual number before anyone argues about truncation.
  (c) Extractive summarisation vs a second pass through qwen2.5:3b: latency and
      output quality, measured rather than assumed.

Token counts come from Ollama's own tokeniser via /api/embed's prompt eval --
no tiktoken here, and a GPT tokeniser would be the wrong one anyway.
"""
from __future__ import annotations

import asyncio
import json
import statistics
import sys
import time

import aiohttp

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SEARX = "http://127.0.0.1:8100"
OLLAMA = "http://127.0.0.1:11434"
MODEL = "qwen2.5:3b"

QUERIES = [
    "population of jaipur",
    "who won the 2024 cricket world cup",
    "what time does big bazaar close",
    "how tall is mount everest",
    "when is diwali 2026",
]


async def count_tokens(session, text: str) -> int:
    """Ollama reports prompt_eval_count; ask for 0 output tokens."""
    async with session.post(f"{OLLAMA}/api/generate", json={
        "model": MODEL, "prompt": text, "stream": False,
        "options": {"num_predict": 1},
    }, timeout=aiohttp.ClientTimeout(total=120)) as r:
        d = await r.json()
        return d.get("prompt_eval_count", -1)


async def searx(session, q: str):
    t0 = time.monotonic()
    async with session.get(f"{SEARX}/search", params={"q": q, "format": "json"},
                           timeout=aiohttp.ClientTimeout(total=20)) as r:
        d = await r.json()
    return int((time.monotonic() - t0) * 1000), d


async def main():
    async with aiohttp.ClientSession() as s:
        print("=== (a) SearxNG latency, 5 real queries ===")
        times, payloads = [], []
        for q in QUERIES:
            ms, d = await searx(s, q)
            res = d.get("results", [])
            times.append(ms)
            payloads.append((q, d))
            dead = [e[0] for e in d.get("unresponsive_engines", [])]
            print(f"  {q[:38]:<40} {ms:>6} ms  {len(res):>3} results  blocked={dead}")
        print(f"  median {statistics.median(times):.0f} ms   "
              f"min {min(times)}  max {max(times)}")

        print("\n=== (b) context cost of a result set ===")
        q, d = payloads[0]
        raw = json.dumps(d)
        res = d.get("results", [])

        # Three candidate shapes, smallest to largest.
        def shape(results, n, snippet_chars):
            return "\n".join(
                f"[{i+1}] {r.get('title','')[:90]}\n{(r.get('content') or '')[:snippet_chars]}"
                for i, r in enumerate(results[:n]))

        variants = [
            ("raw searxng json", raw),
            ("all 20 results, full snippets", shape(res, 20, 10_000)),
            ("top 5, 300-char snippets", shape(res, 5, 300)),
            ("top 3, 200-char snippets", shape(res, 3, 200)),
        ]
        for name, text in variants:
            n = await count_tokens(s, text)
            pct = 100.0 * n / 8192
            print(f"  {name:<32} {len(text):>7} chars  {n:>6} tokens  "
                  f"{pct:5.1f}% of num_ctx 8192")

        print("\n=== (c) summarise: extractive vs local 3B ===")
        # Extractive: no model at all, just the top snippets stitched.
        for q, d in payloads[:3]:
            res = d.get("results", [])
            t0 = time.monotonic()
            extractive = (res[0].get("content") or res[0].get("title") or "")[:220]
            ex_ms = int((time.monotonic() - t0) * 1000)

            ctx = shape(res, 5, 300)
            prompt = (
                "Answer the question in ONE short sentence of at most 12 words, "
                "using only the search results. If the results do not contain the "
                "answer, reply exactly: NO ANSWER.\n\n"
                f"Question: {q}\n\nSearch results:\n{ctx}\n\nAnswer:"
            )
            t1 = time.monotonic()
            async with s.post(f"{OLLAMA}/api/generate", json={
                "model": MODEL, "prompt": prompt, "stream": False,
                "options": {"num_predict": 40, "temperature": 0.1, "num_ctx": 8192},
            }, timeout=aiohttp.ClientTimeout(total=180)) as r:
                out = await r.json()
            llm_ms = int((time.monotonic() - t1) * 1000)
            print(f"\n  Q: {q}")
            print(f"    extractive ({ex_ms} ms): {extractive[:150]}")
            print(f"    3B pass   ({llm_ms} ms): {(out.get('response') or '').strip()[:180]}")
            print(f"      prompt_eval={out.get('prompt_eval_count')} "
                  f"eval={out.get('eval_count')}")


if __name__ == "__main__":
    asyncio.run(main())
