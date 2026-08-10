#!/usr/bin/env python3
"""
Throwaway probe round 3: what is actually parseable from the engines that still
answer this host, and does Wikimedia relent given a policy-compliant UA?

Round 2 established:
  * html.duckduckgo.com AND lite.duckduckgo.com return 202 (challenge) to this
    IP on every request after the first two. Not a cooldown -- still 202 a
    minute later from a fresh session.
  * Wikimedia returned 403 "respect our robot policy" to a UA that named the
    tool but gave no contact route. Their policy asks for a contact URL/email,
    so retry with the documented format before writing Wikipedia off.
"""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlparse, parse_qs, unquote

import aiohttp
from bs4 import BeautifulSoup

CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
# The format Wikimedia's User-Agent policy actually asks for: tool/version plus
# a contact route in parentheses.
WIKI_UA = ("JARVIS/0.1 (https://github.com/self-hosted/jarvis; "
           "local home assistant; contact via repo)")


async def fetch(session, url, **kw):
    t0 = time.monotonic()
    async with session.get(url, **kw) as r:
        body = await r.text()
        return r.status, int((time.monotonic() - t0) * 1000), body


def parse_bing(html: str):
    """Bing's organic results: li.b_algo > h2 > a, snippet in .b_caption p."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("li.b_algo")[:6]:
        a = li.select_one("h2 a")
        if not a:
            continue
        snip = li.select_one(".b_caption p") or li.select_one("p")
        out.append({
            "title": a.get_text(" ", strip=True),
            "url": a.get("href", ""),
            "snippet": snip.get_text(" ", strip=True) if snip else "",
        })
    return out


def parse_mojeek(html: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for li in soup.select("ul.results-standard li")[:6]:
        a = li.select_one("a.title") or li.select_one("h2 a")
        if not a:
            continue
        p = li.select_one("p.s")
        out.append({
            "title": a.get_text(" ", strip=True),
            "url": a.get("href", ""),
            "snippet": p.get_text(" ", strip=True) if p else "",
        })
    return out


def parse_startpage(html: str):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for div in soup.select("div.w-gl__result, div.result")[:6]:
        a = div.select_one("a.w-gl__result-title, a.result-link, h3 a, a")
        if not a:
            continue
        p = div.select_one("p.w-gl__description, p.description, p")
        out.append({
            "title": a.get_text(" ", strip=True),
            "url": a.get("href", ""),
            "snippet": p.get_text(" ", strip=True) if p else "",
        })
    return out


async def main():
    to = aiohttp.ClientTimeout(total=15)
    q = "what time does big bazaar close"
    q2 = "population of jaipur"

    print("=== Wikimedia with a policy-compliant UA ===")
    async with aiohttp.ClientSession(timeout=to, headers={"User-Agent": WIKI_UA}) as s:
        for name, url in [
            ("rest summary", "https://en.wikipedia.org/api/rest_v1/page/summary/Jaipur"),
            ("action query",
             "https://en.wikipedia.org/w/api.php?action=query&prop=extracts&exintro=1"
             "&explaintext=1&titles=Jaipur&format=json"),
        ]:
            try:
                st, ms, body = await fetch(s, url)
                print(f"  {name:<14} {st} {ms:>5} ms bytes={len(body)}")
                print(f"  {'':<14} {body[:180]}")
            except Exception as exc:
                print(f"  {name:<14} ERR {exc}")

    print("\n=== engine parse quality, real query: %r ===" % q)
    async with aiohttp.ClientSession(timeout=to, headers={"User-Agent": CHROME_UA}) as s:
        for name, url, parser in [
            ("bing", f"https://www.bing.com/search?q={q.replace(' ', '+')}", parse_bing),
            ("mojeek", f"https://www.mojeek.com/search?q={q.replace(' ', '+')}", parse_mojeek),
            ("startpage",
             f"https://www.startpage.com/sp/search?query={q.replace(' ', '+')}",
             parse_startpage),
        ]:
            try:
                st, ms, body = await fetch(s, url)
                res = parser(body)
                print(f"\n  --- {name}: status={st} {ms} ms, {len(body)} bytes, "
                      f"{len(res)} results parsed")
                for r in res[:4]:
                    print(f"      {r['title'][:70]}")
                    print(f"        {r['snippet'][:110]}")
            except Exception as exc:
                print(f"  {name}: ERR {type(exc).__name__}: {exc}")

        print("\n=== same engines, factual query: %r ===" % q2)
        for name, url, parser in [
            ("bing", f"https://www.bing.com/search?q={q2.replace(' ', '+')}", parse_bing),
            ("mojeek", f"https://www.mojeek.com/search?q={q2.replace(' ', '+')}", parse_mojeek),
        ]:
            try:
                st, ms, body = await fetch(s, url)
                res = parser(body)
                print(f"\n  --- {name}: status={st} {ms} ms, {len(res)} results")
                for r in res[:4]:
                    print(f"      {r['title'][:70]}")
                    print(f"        {r['snippet'][:110]}")
            except Exception as exc:
                print(f"  {name}: ERR {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
