#!/usr/bin/env python3
"""
Throwaway probe: which search backends actually work from THIS host, and how
fast. Run once, read the numbers, keep the numbers, delete nothing until the
README quotes them.

Deliberately makes real external requests -- that is the thing being measured.
Every host it touches is listed in websearch/README.md under "What leaves the
network", because a probe that quietly contacts a host nobody documented is
exactly the failure mode this feature is supposed to avoid.
"""
from __future__ import annotations

import asyncio
import statistics
import time

import aiohttp

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

QUERIES = [
    "what time does big bazaar close",
    "population of jaipur",
    "who won the 2024 cricket world cup",
]


async def timed(session, name, method, url, **kw):
    t0 = time.monotonic()
    try:
        async with session.request(method, url, **kw) as r:
            body = await r.text()
            ms = int((time.monotonic() - t0) * 1000)
            return {"name": name, "status": r.status, "ms": ms, "bytes": len(body),
                    "sample": body[:160].replace("\n", " ")}
    except asyncio.TimeoutError:
        return {"name": name, "status": "TIMEOUT",
                "ms": int((time.monotonic() - t0) * 1000)}
    except aiohttp.ClientError as exc:
        return {"name": name, "status": f"ERR {type(exc).__name__}: {exc}",
                "ms": int((time.monotonic() - t0) * 1000)}


async def main():
    timeout = aiohttp.ClientTimeout(total=12)
    async with aiohttp.ClientSession(timeout=timeout,
                                     headers={"User-Agent": UA}) as s:
        q = QUERIES[0]
        checks = [
            # --- DuckDuckGo scraped endpoints ---
            ("ddg-html POST", "POST", "https://html.duckduckgo.com/html/",
             {"data": {"q": q}}),
            ("ddg-lite POST", "POST", "https://lite.duckduckgo.com/lite/",
             {"data": {"q": q}}),
            ("ddg-html GET", "GET", f"https://html.duckduckgo.com/html/?q={q.replace(' ', '+')}", {}),
            # --- DuckDuckGo Instant Answer API (official, keyless) ---
            ("ddg-ia api", "GET",
             "https://api.duckduckgo.com/?q=population+of+jaipur&format=json&no_html=1", {}),
            # --- public SearxNG instances, JSON format ---
            ("searx.be json", "GET",
             f"https://searx.be/search?q={q.replace(' ', '+')}&format=json", {}),
            ("priv.au json", "GET",
             f"https://priv.au/search?q={q.replace(' ', '+')}&format=json", {}),
            ("searxng.site json", "GET",
             f"https://searxng.site/search?q={q.replace(' ', '+')}&format=json", {}),
            # --- keyless structured sources (the cheap path) ---
            ("wikipedia summary", "GET",
             "https://en.wikipedia.org/api/rest_v1/page/summary/Jaipur", {}),
            ("wikipedia opensearch", "GET",
             "https://en.wikipedia.org/w/api.php?action=query&list=search"
             "&srsearch=population%20of%20jaipur&format=json&srlimit=3", {}),
            ("open-meteo geocode", "GET",
             "https://geocoding-api.open-meteo.com/v1/search?name=Jaipur&count=1", {}),
            ("open-meteo forecast", "GET",
             "https://api.open-meteo.com/v1/forecast?latitude=26.91&longitude=75.79"
             "&current=temperature_2m,relative_humidity_2m,weather_code"
             "&timezone=Asia%2FKolkata", {}),
            ("wttr.in", "GET", "https://wttr.in/Jaipur?format=j1", {}),
            # --- API-key services: expect an auth error, proves reachability only ---
            ("brave api (no key)", "GET",
             "https://api.search.brave.com/res/v1/web/search?q=test", {}),
        ]
        for name, method, url, kw in checks:
            r = await timed(s, name, method, url, **kw)
            print(f"{r['name']:<24} status={r['status']!s:<12} "
                  f"{r['ms']:>6} ms  bytes={r.get('bytes', 0)}")
            if r.get("sample"):
                print(f"{'':<24} {r['sample'][:120]}")

        # Repeat the winner three times to get a spread rather than one sample.
        print("\n--- repeat timing, ddg-html POST, 3 queries ---")
        times = []
        for q in QUERIES:
            r = await timed(s, "ddg-html", "POST",
                            "https://html.duckduckgo.com/html/", data={"q": q})
            times.append(r["ms"])
            print(f"  {q[:40]:<42} status={r['status']!s:<8} {r['ms']:>6} ms "
                  f"bytes={r.get('bytes', 0)}")
        if len(times) > 1:
            print(f"  median {statistics.median(times):.0f} ms  "
                  f"min {min(times)} max {max(times)}")


if __name__ == "__main__":
    asyncio.run(main())
