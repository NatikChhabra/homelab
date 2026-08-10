#!/usr/bin/env python3
"""
The measurement that goes in the README. Runs the REAL service code path -- not
a reimplementation of it -- over a fixed question set, in both modes, and scores
whether the spoken answer actually contains the right answer.

Scoring is deliberately crude: does the 12-word spoken string contain a token
that only a correct answer would contain. A fluent sentence with no answer in it
scores MISS, which is the whole point -- that failure mode is what this exists
to catch, and it is invisible to any check that only asks "did it return text".

Usage:
  SEARXNG_URL=http://127.0.0.1:8100 OLLAMA_URL=http://127.0.0.1:11434 \
      python _bench_modes.py
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

import aiohttp

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import websearch_server as ws  # noqa: E402

CASES = [
    ("how tall is mount everest", ["8,848", "8848", "29,0", "29029", "29031"]),
    ("who won the 2024 t20 cricket world cup", ["india"]),
    ("population of jaipur", ["million", "lakh", "3,0", "30,"]),
    ("what time does big bazaar close", ["pm", "10:", "11:", "22:"]),
    ("capital of australia", ["canberra"]),
    ("what is the boiling point of water in fahrenheit", ["212"]),
    ("when did india gain independence", ["1947"]),
    ("who is the ceo of nvidia", ["huang", "jensen"]),
]


def hit(answer: str, needles: list[str]) -> bool:
    a = answer.lower()
    return any(n.lower() in a for n in needles)


async def run_mode(session, mode: str):
    rows, times = [], []
    for q, needles in CASES:
        t0 = time.monotonic()
        try:
            out = await ws.do_search(session, q, mode=mode)
            ms = int((time.monotonic() - t0) * 1000)
            ok = hit(out["answer"], needles)
            rows.append((q, ok, ms, out["answer"], out["mode"], len(out["detail"])))
        except ws.SearchError as exc:
            ms = int((time.monotonic() - t0) * 1000)
            rows.append((q, False, ms, f"[{exc.code}] {exc.spoken}", "error", 0))
        times.append(ms)
    return rows, times


async def main():
    async with aiohttp.ClientSession(headers={"User-Agent": ws.USER_AGENT}) as s:
        # Warm the model. The first inference after idle is not representative
        # and reporting it as the median would overstate every later number.
        try:
            await s.post(f"{ws.OLLAMA_URL}/api/generate",
                         json={"model": ws.SUMMARY_MODEL, "prompt": "hi",
                               "stream": False, "options": {"num_predict": 1}},
                         timeout=aiohttp.ClientTimeout(total=120))
        except Exception as exc:
            print(f"warm-up failed: {exc}")

        for mode in ("summarise", "extractive"):
            print(f"\n===== MODE: {mode} =====")
            rows, times = await run_mode(s, mode)
            ok = sum(1 for r in rows if r[1])
            for q, good, ms, ans, how, dlen in rows:
                words = len(ans.split())
                print(f"  [{'OK  ' if good else 'MISS'}] {ms:>5} ms {words:>3}w "
                      f"{how:<11} {q[:34]:<36} -> {ans[:78]}")
            print(f"  score {ok}/{len(rows)}   "
                  f"median {statistics.median(times):.0f} ms   "
                  f"min {min(times)}  max {max(times)}  "
                  f"p90 {sorted(times)[int(len(times)*0.9)-1]} ms")

        print("\n===== error paths (must be distinct, never empty) =====")
        for name, q in [
            ("device command", "turn on the fan"),
            ("too short", "hi"),
            ("nonsense, no results", "zqxwvutsrponmlk qwertyuiop asdfghjkl zxcvbnm 99417"),
        ]:
            try:
                out = await ws.do_search(s, q)
                print(f"  {name:<22} -> ok={out['ok']} answer={out['answer'][:70]!r}")
            except ws.SearchError as exc:
                print(f"  {name:<22} -> code={exc.code:<16} spoken={exc.spoken!r}")

        print("\n===== kill switch =====")
        ws.ENABLED = False
        try:
            await ws.do_search(s, "population of jaipur")
            print("  FAIL: search ran while disabled")
        except ws.SearchError as exc:
            print(f"  code={exc.code} spoken={exc.spoken!r} detail={exc.detail}")
        ws.ENABLED = True

        print(f"\n===== egress log ({len(ws.EGRESS)} entries) =====")
        for e in list(ws.EGRESS)[-4:]:
            print(f"  {e['at']}  {e['upstream']:<8} {e['outcome']:<16} "
                  f"{e['ms']:>5} ms  {e['query'][:40]}")


if __name__ == "__main__":
    asyncio.run(main())
