#!/usr/bin/env python3
"""
Throwaway probe round 4: round 3 parsed ZERO results from bing, mojeek and
startpage even though all three returned HTTP 200. Two possible causes and they
lead to opposite conclusions:

  (a) my CSS selectors are stale        -> scraping is workable, just brittle
  (b) the 200 body is a block/JS page   -> scraping is dead on this host

Dump what actually came back and decide by looking.
"""
from __future__ import annotations

import asyncio
import re
import sys

import aiohttp
from bs4 import BeautifulSoup

# Windows console is cp1252; engine SERPs contain Devanagari and IPA. Without
# this the probe dies on print(), which looks like a network failure and is not.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


async def main():
    to = aiohttp.ClientTimeout(total=15)
    q = "population+of+jaipur"
    targets = [
        ("bing", f"https://www.bing.com/search?q={q}"),
        ("mojeek", f"https://www.mojeek.com/search?q={q}"),
        ("startpage", f"https://www.startpage.com/sp/search?query={q}"),
    ]
    async with aiohttp.ClientSession(timeout=to, headers={
        "User-Agent": CHROME_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }) as s:
        for name, url in targets:
            async with s.get(url) as r:
                body = await r.text()
            soup = BeautifulSoup(body, "html.parser")
            title = soup.title.get_text(strip=True) if soup.title else "(no title)"
            text = soup.get_text(" ", strip=True)
            # How many outbound links to non-engine domains? A real SERP has many.
            hrefs = [a.get("href", "") for a in soup.find_all("a")]
            external = [h for h in hrefs if h.startswith("http")
                        and name not in h]
            print(f"\n===== {name}  status={r.status} bytes={len(body)}")
            print(f"  <title>: {title[:110]}")
            print(f"  anchors: {len(hrefs)} total, {len(external)} external")
            print(f"  text[:400]: {text[:400]}")
            # Show the class names that repeat -- that is where results live.
            classes = re.findall(r'class="([^"]{1,60})"', body)
            common = {}
            for c in classes:
                common[c] = common.get(c, 0) + 1
            top = sorted(common.items(), key=lambda kv: -kv[1])[:12]
            print(f"  repeated classes: {top}")


if __name__ == "__main__":
    asyncio.run(main())
