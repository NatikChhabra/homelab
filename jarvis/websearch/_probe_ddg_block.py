#!/usr/bin/env python3
"""
Throwaway probe round 2. Round 1 turned up two things that decide the design,
and both need pinning down before anything is written:

  1. html.duckduckgo.com answered 200 twice and then returned 202 for every
     request after. 202 is DDG's anomaly/challenge response, not an answer.
     Question: is it a short cooldown, a per-session thing, or a hard block?

  2. Wikimedia's REST API returned 403 "Please respect our robot policy" for a
     generic Chrome UA. Question: does a descriptive UA fix it?
"""
from __future__ import annotations

import asyncio
import time

import aiohttp

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# Wikimedia's policy asks for a UA that identifies the tool and a contact route.
POLITE_UA = "JARVIS-homeassistant/0.1 (self-hosted home assistant; local use only)"


async def one(session, name, method, url, **kw):
    t0 = time.monotonic()
    try:
        async with session.request(method, url, **kw) as r:
            body = await r.text()
            return (name, r.status, int((time.monotonic() - t0) * 1000),
                    len(body), body[:200].replace("\n", " "))
    except Exception as exc:  # probe: any failure is data
        return (name, f"ERR {type(exc).__name__}", int((time.monotonic() - t0) * 1000), 0, str(exc)[:120])


async def main():
    to = aiohttp.ClientTimeout(total=15)

    print("=== 1. DDG: is 202 a cooldown or a block? fresh session, 5 spaced POSTs ===")
    async with aiohttp.ClientSession(timeout=to, headers={"User-Agent": CHROME_UA}) as s:
        for i in range(5):
            n, st, ms, b, samp = await one(
                s, f"ddg#{i}", "POST", "https://html.duckduckgo.com/html/",
                data={"q": f"population of jaipur {i}"})
            print(f"  {n:<8} status={st!s:<6} {ms:>5} ms bytes={b}")
            if st == 202:
                print(f"           body: {samp[:150]}")
            await asyncio.sleep(3)

    print("\n=== 2. DDG lite, separate host, fresh session ===")
    async with aiohttp.ClientSession(timeout=to, headers={"User-Agent": CHROME_UA}) as s:
        for i in range(3):
            n, st, ms, b, samp = await one(
                s, f"lite#{i}", "POST", "https://lite.duckduckgo.com/lite/",
                data={"q": f"who won the 2024 cricket world cup {i}"})
            print(f"  {n:<8} status={st!s:<6} {ms:>5} ms bytes={b}")
            await asyncio.sleep(3)

    print("\n=== 3. Wikimedia with a descriptive User-Agent ===")
    async with aiohttp.ClientSession(timeout=to, headers={"User-Agent": POLITE_UA}) as s:
        for name, url in [
            ("rest summary", "https://en.wikipedia.org/api/rest_v1/page/summary/Jaipur"),
            ("action search",
             "https://en.wikipedia.org/w/api.php?action=query&list=search"
             "&srsearch=population+of+jaipur&format=json&srlimit=3"),
            ("action extract",
             "https://en.wikipedia.org/w/api.php?action=query&prop=extracts"
             "&exintro=1&explaintext=1&titles=Jaipur&format=json"),
        ]:
            n, st, ms, b, samp = await one(s, name, "GET", url)
            print(f"  {n:<15} status={st!s:<6} {ms:>5} ms bytes={b}")
            print(f"  {'':<15} {samp[:170]}")

    print("\n=== 4. other keyless general-web options ===")
    async with aiohttp.ClientSession(timeout=to, headers={"User-Agent": CHROME_UA}) as s:
        for name, url in [
            ("mojeek html", "https://www.mojeek.com/search?q=population+of+jaipur"),
            ("marginalia", "https://search.marginalia.nu/search?query=jaipur"),
            ("startpage", "https://www.startpage.com/sp/search?query=jaipur"),
            ("brave html", "https://search.brave.com/search?q=jaipur"),
            ("bing html", "https://www.bing.com/search?q=population+of+jaipur"),
            ("ecosia html", "https://www.ecosia.org/search?q=jaipur"),
        ]:
            n, st, ms, b, samp = await one(s, name, "GET", url)
            print(f"  {n:<14} status={st!s:<6} {ms:>5} ms bytes={b}")

    print("\n=== 5. DDG again after ~1 min of the above ===")
    async with aiohttp.ClientSession(timeout=to, headers={"User-Agent": CHROME_UA}) as s:
        n, st, ms, b, samp = await one(
            s, "ddg-late", "POST", "https://html.duckduckgo.com/html/",
            data={"q": "current time in india"})
        print(f"  {n:<10} status={st!s:<6} {ms:>5} ms bytes={b}")


if __name__ == "__main__":
    asyncio.run(main())
