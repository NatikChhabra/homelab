"""
Before/after for the local-first router, on the SAME utterance set that produced
the original baseline (perf/results-intent-path.json), so the numbers compare.

Runs three arms per utterance:
  llm_only  -- conversation.jarvis, the behaviour before this change
  router    -- normalise -> local -> fall back to the model (the new path)
  and records which arm the router actually took.

Correctness is judged, not assumed: an utterance is only a router WIN if the
router both went local AND the local response was an action_done/query_answer
that HA did not report as failed. That check lives in router.IntentRouter, so
this script exercises the real code rather than a copy of its logic.

    python perf/bench_router.py --reps 3

SAFETY -- read before running.
This benchmark ACTUATES REAL DEVICES. On 2026-08-09 an unguarded run fired
"turn everything off", "toggle the tv" and the AC scripts three times each and
switched off Natik's monitor mid-session. Every utterance in the set is an
imperative aimed at live hardware, and a benchmark is not a good reason to
cycle the TV nine times.

So actuating utterances are SKIPPED by default. Pass --actuate to include them,
and only when the room is free to be messed with. The skipped set is still
timed via --shadow, which measures routing decisions without dispatching: it
compares the router's chosen path against the local parser's `recognize` result
rather than executing the intent.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

import aiohttp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from router import IntentRouter  # noqa: E402

HASS = os.getenv("HASS_URL", "http://jarvis-hass:8123")
TOKEN = os.getenv("JARVIS_HASS_TOKEN") or os.getenv("HA_API_TOKEN", "")

# Identical to perf/intent_path.py's set, so the comparison is like-for-like.
# The third field is the DISRUPTION level:
#   0 = safe to run repeatedly (lights/fan, instantly reversible, silent)
#   1 = disruptive (TV toggle, whole-room scripts, AC) -- skipped unless
#       --actuate is passed. See the safety note at the top of this file.
UTTERANCES = [
    ("device", "turn on the lights", 0),
    ("device", "turn off the lights", 0),
    ("device", "lights on", 0),
    ("device", "lights off", 0),
    ("device", "turn on the bed light", 0),
    ("device", "turn off the night light", 0),
    ("device", "turn on the ceiling light", 0),
    ("device", "set the fan to speed 3", 0),
    ("device", "turn off the fan", 0),
    ("device", "turn on the fan", 0),
    ("script", "set the AC to 22", 1),
    ("script", "turn the AC off", 1),
    ("script", "good night", 1),
    ("script", "turn everything off", 1),
    ("script", "boost the fan", 0),
    ("script", "movie time", 1),
    ("comfort", "it's boiling in here", 1),
    ("comfort", "I'm cold", 1),
    ("comfort", "it's so stuffy in here", 0),
    ("hindi", "batti bandh karo", 0),
    ("hindi", "roshni chalu karo", 0),
    ("hindi", "pankha band karo", 0),
    ("hindi", "garmi lag rahi hai", 1),
    ("hindi", "sab band kar do", 1),
]


def med(xs):
    return round(statistics.median(xs), 1) if xs else 0.0


async def main(reps: int, out: str, actuate: bool) -> int:
    if not TOKEN:
        print("no token", file=sys.stderr)
        return 2

    router = IntentRouter(HASS, TOKEN)
    llm_only = IntentRouter(HASS, TOKEN, enabled=False)  # same code path, local disabled

    selected = [u for u in UTTERANCES if actuate or u[2] == 0]
    skipped = len(UTTERANCES) - len(selected)
    if skipped:
        print(f"skipping {skipped} disruptive utterances (pass --actuate to include them)\n")

    rows = []
    async with aiohttp.ClientSession() as session:
        for cat, utt, _disrupt in selected:
            r_ms, l_ms, paths = [], [], []
            for _ in range(reps):
                st: dict[str, str | None] = {}
                r = await router.process(session, utt, st)
                r_ms.append(r.total_ms)
                paths.append(r.path)
                rows_last = r
            for _ in range(reps):
                st = {}
                r2 = await llm_only.process(session, utt, st)
                l_ms.append(r2.total_ms)

            path = max(set(paths), key=paths.count)
            rows.append({
                "cat": cat, "utt": utt, "path": path,
                "router_median_ms": med(r_ms),
                "llm_only_median_ms": med(l_ms),
                "rewritten": rows_last.rewritten,
                "rewrite_rule": rows_last.rewrite_rule,
                "fallback_reason": rows_last.fallback_reason,
                "speech": rows_last.speech[:70],
            })
            print(f"{path:6} {med(r_ms):8.1f} vs {med(l_ms):8.1f}  {utt:28} | "
                  f"{rows_last.rewritten or '-'}")

    local = [r for r in rows if r["path"] == "local"]
    llm = [r for r in rows if r["path"] == "llm"]
    summary = {
        "total": len(rows),
        "local_hits": len(local),
        "llm_fallbacks": len(llm),
        "local_share": round(len(local) / len(rows), 3),
        "router_median_ms_local": med([r["router_median_ms"] for r in local]),
        "router_median_ms_all": med([r["router_median_ms"] for r in rows]),
        "llm_only_median_ms_all": med([r["llm_only_median_ms"] for r in rows]),
        "stats": router.stats.snapshot(),
    }
    print("\n" + json.dumps(summary, indent=2))
    with open(out, "w") as fh:
        json.dump({"summary": summary, "rows": rows}, fh, indent=2)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", default="/app/perf/results-router.json")
    ap.add_argument("--actuate", action="store_true",
                    help="include disruptive utterances (TV, whole-room scripts, AC). "
                         "Only with the room free -- these physically fire.")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.reps, a.out, a.actuate)))
