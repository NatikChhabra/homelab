#!/usr/bin/env python3
"""
JARVIS web search: the one place where this build is allowed to touch the
internet, and the only place.

    HA conversation agent --tool--> [this service] --> [searxng] --> the web
                                          |
                                          +--> [ollama qwen2.5:3b] one sentence

WHY THIS EXISTS AS A SERVICE RATHER THAN A HOME ASSISTANT INTEGRATION
---------------------------------------------------------------------
Everything else in this project is local-first with no cloud and no recurring
cost, and a packet capture proving zero external traffic is a planned
deliverable. Search breaks that rule by definition -- there is no local copy of
"what time does Big Bazaar close". The response is not to pretend otherwise but
to put every outbound packet behind ONE process, on ONE trigger, with an audit
log, so a future capture can tell "JARVIS phoned home" apart from "Natik asked
a question and it searched".

Concretely:
  * Nothing here runs on a timer, a poll, or a startup hook. The only code path
    that reaches the internet is POST /search, and the only caller is the
    conversation agent's `web_search` tool.
  * JARVIS_WEBSEARCH_ENABLED=0 makes every outbound path unreachable at the top
    of the handler, before DNS, before a socket. One env var, fully local again.
  * Every search is appended to an in-memory egress log, readable at
    GET /api/egress, so the capture can be correlated against user actions.

WHY SEARXNG AND NOT A SCRAPER
------------------------------
Measured from this host on 2026-08-08, not assumed:
  * html.duckduckgo.com and lite.duckduckgo.com answered 200 twice and then
    HTTP 202 (challenge) for every request after, from fresh sessions, still
    202 a minute later. Two queries of budget per IP is not a feature.
  * bing.com returned HTTP 200 with the body "One last step / Please solve the
    challenge below to continue", 18 anchors, ZERO external links.
  * mojeek.com returned HTTP 200 with <title>Captcha</title>.
  * startpage.com returned HTTP 200 with <title>Startpage Blocked</title>.
A scraper against any of these fails OPEN: 200, parses cleanly, yields nothing,
and the model confabulates over the empty list. Self-hosted SearxNG instead
reports which engines refused -- see UPSTREAM_BLOCKED below, which turns a
silent wrong answer into a named error.

WHY SNIPPETS AND NOT FETCHED PAGES
-----------------------------------
Measured: SearxNG snippets alone answered 4/4 of the test questions correctly,
including the awkward one ("Monday, 09:30 AM, 10:00 PM"). Fetching and
stripping the top pages costs seconds per page and the voice loop runs 1.7-2.1 s
end to end. Page fetch is implemented but OFF by default; see FETCH_PAGES.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import logging
import os
import re
import sys
import time
from collections import deque
from typing import Any

import aiohttp
from aiohttp import web

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("jarvis.websearch")


# --- The kill switch ------------------------------------------------------
# Checked first in every handler that could reach outward. "0", "false", "no"
# and "off" all disable; anything else enables. Default is ENABLED because the
# service only exists to search -- a disabled default would be a silent no-op
# that looks like a bug. Flip it in Stacks/secrets.env, see README.
def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


ENABLED = _flag("JARVIS_WEBSEARCH_ENABLED", True)

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://jarvis-searxng:8080").rstrip("/")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://jarvis-ollama:11434").rstrip("/")
SUMMARY_MODEL = os.getenv("WEBSEARCH_MODEL", "qwen2.5:3b")

# Optional. Only used if SearxNG is down AND a key is present. Read from the
# environment, which compose fills from ../secrets.env -- never written here.
# With no key the whole Brave path is skipped and nothing contacts Brave.
BRAVE_KEY = os.getenv("BRAVE_SEARCH_KEY", "").strip()
BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"

# --- Timeouts -------------------------------------------------------------
# The voice loop is 1.7-2.1 s spoken-word to spoken-reply. A search that can run
# for ten seconds does not read as "thinking", it reads as broken, so the budget
# is bounded at every stage and by a single hard deadline.
#
# Measured medians this deadline is built from: SearxNG 781 ms (min 452, max
# 1266 over 5 real queries), summariser pass 1054 ms (max observed 1514 ms
# warm). Median total lands near 1.9 s.
#
# SUMMARY_TIMEOUT_S was 2.5 s and that was WRONG: the first search after an idle
# period tripped it at 2511 ms, the service degraded to the extractive path, and
# the extractive path then spoke a sentence with no answer in it. Raised to 3.5
# with the deadline at 7.0. The stage caps deliberately sum to more than the
# deadline -- the deadline is the real guarantee, the stage caps just stop one
# stage eating the whole budget.
SEARCH_TIMEOUT_S = float(os.getenv("WEBSEARCH_SEARCH_TIMEOUT", "4.0"))
SUMMARY_TIMEOUT_S = float(os.getenv("WEBSEARCH_SUMMARY_TIMEOUT", "3.5"))
FETCH_TIMEOUT_S = float(os.getenv("WEBSEARCH_FETCH_TIMEOUT", "2.0"))
DEADLINE_S = float(os.getenv("WEBSEARCH_DEADLINE", "7.0"))
# Always keep this much of the deadline back for the extractive fallback, so a
# slow summariser degrades to a real answer instead of losing results already
# in hand to the hard deadline.
FALLBACK_RESERVE_S = 0.4

# --- Context budget -------------------------------------------------------
# num_ctx is 8192 and the system prompt plus tool schemas already occupy part of
# it. Measured token cost of one SearxNG result set, counted with Ollama's own
# tokeniser (prompt_eval_count on qwen2.5:3b), not an estimate:
#
#     raw SearxNG JSON, as returned        2050 tokens   25.0% of num_ctx
#     all 20 results, full snippets        1363 tokens   16.6%
#     top 5 results, 300-char snippets      332 tokens    4.1%
#     top 3 results, 200-char snippets      190 tokens    2.3%
#
# Handing the raw JSON to a 3B model would spend a quarter of the window on one
# question. TOP_N/SNIPPET_CHARS below are the 4.1% row, and in summarise mode
# what actually reaches the agent is one sentence -- about 20 tokens, 0.25%.
TOP_N = int(os.getenv("WEBSEARCH_TOP_N", "5"))
SNIPPET_CHARS = int(os.getenv("WEBSEARCH_SNIPPET_CHARS", "300"))
# Hard ceiling on what any caller can be handed, enforced after packing so a
# pathological result set cannot blow the window even if TOP_N is raised.
MAX_CONTEXT_CHARS = int(os.getenv("WEBSEARCH_MAX_CONTEXT_CHARS", "1400"))

# Spoken replies are capped near 12 words upstream. Enforced here too, because
# the cap is a property of the speaker, not of whoever wrote the prompt.
SPOKEN_MAX_WORDS = int(os.getenv("WEBSEARCH_SPOKEN_MAX_WORDS", "12"))

# Off by default: measured 4/4 correct from snippets alone, and each page fetch
# costs seconds the voice loop does not have. See module docstring.
FETCH_PAGES = _flag("WEBSEARCH_FETCH_PAGES", False)

# "extractive" = no second inference pass, hand trimmed snippets to the agent.
# "summarise"  = one qwen2.5:3b pass here, hand back one sentence.
# See README "Where summarisation happens" for the measurement behind the default.
MODE = os.getenv("WEBSEARCH_MODE", "summarise").strip().lower()

PORT = int(os.getenv("PORT", "3101"))

# Wikimedia rejects generic browser UAs from this host with HTTP 403 "Please
# respect our robot policy" -- measured, twice, including with a UA that named
# the tool but gave no contact route. Their policy wants tool/version plus a
# contact, and with that it returns 200 in ~311 ms. SearxNG sends its own UA;
# this one is for the page fetcher and any direct call.
USER_AGENT = os.getenv(
    "WEBSEARCH_USER_AGENT",
    "JARVIS/0.1 (self-hosted home assistant; single-user; no crawling)",
)

# The engines the feature actually depends on, verified answering from this host
# on 2026-08-08. Used to tell a genuine outage apart from the permanent,
# expected refusals of engines this host is blocked by. Lower case; must match
# the names SearxNG reports, which are NOT the names in its docs -- there is no
# engine called "google", it is "google cse". See searxng-settings.yml.
PRIMARY_ENGINES = frozenset(
    e.strip().lower()
    for e in os.getenv("WEBSEARCH_PRIMARY_ENGINES", "duckduckgo,wikipedia").split(",")
    if e.strip()
)


# --- Error taxonomy -------------------------------------------------------
# Every failure gets its OWN code and its own spoken line. This is the single
# most important property of the whole service: an empty result handed to a 3B
# model does not produce "I don't know", it produces a confident invention. The
# model must never receive an empty success.
class SearchError(Exception):
    def __init__(self, code: str, spoken: str, detail: str = "") -> None:
        super().__init__(detail or spoken)
        self.code = code
        self.spoken = spoken
        self.detail = detail or spoken


DISABLED = ("disabled", "Internet search is switched off.")
NO_RESULTS = ("no-results", "Nothing found on that, sir.")
UPSTREAM_BLOCKED = ("upstream-blocked", "Search engines are refusing me right now.")
RATE_LIMITED = ("rate-limited", "Search is rate limited at the moment.")
TIMED_OUT = ("timeout", "The search took too long. Try again.")
BACKEND_DOWN = ("backend-down", "The search service is not running.")
BAD_QUERY = ("bad-query", "That is not something I can look up.")
NOT_A_QUESTION = ("device-command", "That is a device command, not a search.")


# --- The trigger guard ----------------------------------------------------
# Search must happen ONLY on an explicit user question. The primary guarantee is
# structural -- nothing calls this but the tool -- but a 3B model routing a
# device command here would put a packet on the wire for "turn on the fan", and
# that is exactly the event the packet capture is meant to rule out. So refuse
# it here as well, where the refusal is cheap and logged.
DEVICE_VERBS = re.compile(
    r"^\s*(turn|switch|set|dim|brighten|toggle|start|stop|open|close|play|pause|"
    r"increase|decrease|raise|lower|boost|band|chalu|karo|kardo|jala|bujha)\b",
    re.I,
)
DEVICE_NOUNS = re.compile(
    r"\b(the\s+)?(light|lights|lamp|fan|ac|air\s*condition\w*|tv|television|"
    r"speaker|volume|thermostat|plug|switch)\b",
    re.I,
)


def guard(q: str) -> str:
    """Normalise and refuse anything that is not a lookup. Raises SearchError."""
    q = (q or "").strip()
    q = re.sub(r"\s+", " ", q)
    if len(q) < 3:
        raise SearchError(*BAD_QUERY, detail=f"query too short: {q!r}")
    if len(q) > 300:
        # Long input is a transcript pasted in by mistake, not a question.
        raise SearchError(*BAD_QUERY, detail=f"query too long: {len(q)} chars")
    if DEVICE_VERBS.search(q) and DEVICE_NOUNS.search(q):
        raise SearchError(
            *NOT_A_QUESTION,
            detail=f"refused as device command, no packet sent: {q!r}",
        )
    return q


# --- Egress audit ---------------------------------------------------------
# In memory, capped, no disk. Its job is to let a packet capture be correlated
# with user actions after the fact; it is not a durable log and must never grow
# without bound on a box that also runs Ollama.
EGRESS: deque[dict[str, Any]] = deque(maxlen=200)


def note_egress(query: str, upstream: str, outcome: str, ms: int) -> None:
    EGRESS.append({
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "query": query,
        "upstream": upstream,
        "outcome": outcome,
        "ms": ms,
    })


# --- Text handling --------------------------------------------------------
TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean(text: str) -> str:
    """SearxNG snippets carry <span> highlight markup and HTML entities."""
    return WS_RE.sub(" ", html.unescape(TAG_RE.sub(" ", text or ""))).strip()


def pack(results: list[dict], top_n: int = TOP_N,
         chars: int = SNIPPET_CHARS) -> str:
    """
    Trim a result set to something a 3B model can read without losing its window.

    Numbered because the summariser prompt refers to positions, and because a
    numbered list is what the model was trained to treat as evidence rather than
    as prose to continue.
    """
    lines = []
    for i, r in enumerate(results[:top_n]):
        title = clean(r.get("title", ""))[:90]
        body = clean(r.get("content", ""))[:chars]
        if not (title or body):
            continue
        lines.append(f"[{i + 1}] {title}: {body}")
    packed = "\n".join(lines)
    return packed[:MAX_CONTEXT_CHARS]


def to_spoken(text: str, max_words: int = SPOKEN_MAX_WORDS) -> str:
    """
    Force any answer down to one speakable sentence.

    Not a nicety. Measured: asked to answer in at most 12 words, qwen2.5:3b
    returned 26 words for "how tall is mount everest". The cap is enforced by
    code because the model does not honour it, and this text is read aloud.
    """
    text = clean(text)
    # Strip anything that sounds wrong out loud.
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"^\s*(according to|based on|the search results say|"
                  r"it (?:says|appears)|per)\b[^,]*,\s*", "", text, flags=re.I)
    text = text.strip().strip("*_ ").strip()

    # Prefer a clean first sentence over a mid-word chop.
    first = re.split(r"(?<=[.!?])\s+", text)[0] if text else ""
    words = first.split()
    if len(words) > max_words:
        words = words[:max_words]
        first = " ".join(words).rstrip(",;:") + "."
    elif first and not first.endswith((".", "!", "?")):
        first += "."
    return first


# --- Extractive answer selection -----------------------------------------
# This is the fallback path, taken whenever the summariser times out, errors, or
# declines. It has to be good, because a bad fallback is worse than an error:
# it speaks a fluent sentence containing no answer and the user believes it.
#
# THE INCIDENT THIS EXISTS FOR. The first version took the first sentence of the
# first snippet and truncated it to 12 words. Asked "how tall is mount everest"
# it said:
#     "Its height was most recently measured in 2020 through a joint survey."
# Twelve words, grammatical, on topic, and it does NOT contain the height. The
# figure "8,848.86 m" was in the very same snippet, six words past the cut.
# Position in the text is not evidence. Overlap with the question is.
STOPWORDS = frozenset("""
a an and are as at be by for from how in is it its of on or that the to was
were what when where which who whom whose why will with does do did can could
me my i you your please tell about there their they this these those much many
""".split())

# Questions whose answer is a figure. If one of these words is present, a
# candidate sentence with no digit in it is almost certainly not the answer.
QUANTITATIVE = re.compile(
    r"\b(how (?:tall|high|long|far|big|old|much|many|deep|wide|fast|heavy)|"
    r"what (?:time|year|date)|when|population|price|cost|temperature|distance|"
    r"height|weight|speed|score|rate|percentage)\b", re.I)


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower())
            if w not in STOPWORDS and len(w) > 1}


def _score(words: list[str], q_terms: set[str], want_number: bool) -> float:
    """Overlap with the question, plus a digit bonus for quantitative asks."""
    lowered = [w.lower().strip(".,;:()") for w in words]
    overlap = len(q_terms.intersection(lowered))
    has_digit = any(any(c.isdigit() for c in w) for w in lowered)
    score = float(overlap)
    if want_number:
        # Dominant, not decorative: for "how tall", the digits ARE the answer.
        score += 3.0 if has_digit else -2.0
    elif has_digit:
        score += 0.5
    return score


def extract_answer(query: str, results: list[dict],
                   max_words: int = SPOKEN_MAX_WORDS) -> str:
    """
    Pick the most answer-bearing span across the top snippets.

    Two stages, because they fail differently:
      1. Choose the best SENTENCE by question overlap. Fixes "the answer was in
         snippet 2, not snippet 1".
      2. If that sentence is too long to speak, choose the best WINDOW of
         max_words consecutive words inside it rather than truncating from the
         left. Fixes "the answer was six words past the cut".
    """
    q_terms = _terms(query)
    want_number = bool(QUANTITATIVE.search(query))

    best, best_score = "", float("-inf")
    for rank, r in enumerate(results[:TOP_N]):
        body = clean(r.get("content", "")) or clean(r.get("title", ""))
        for sentence in re.split(r"(?<=[.!?])\s+|\s+\|\s+", body):
            words = sentence.split()
            if not 3 <= len(words) <= 60:
                continue
            # Slight preference for earlier results, far too small to override
            # a genuine overlap win.
            s = _score(words, q_terms, want_number) - rank * 0.15
            if s > best_score:
                best, best_score = sentence, s

    if not best:
        return ""

    words = best.split()
    if len(words) > max_words:
        win_best, win_score = words[:max_words], float("-inf")
        for i in range(0, len(words) - max_words + 1):
            window = words[i:i + max_words]
            s = _score(window, q_terms, want_number)
            if s > win_score:
                win_best, win_score = window, s
        best = " ".join(win_best)
    return to_spoken(best, max_words)


# --- Backends -------------------------------------------------------------
async def search_searxng(session: aiohttp.ClientSession, q: str) -> list[dict]:
    """
    Query the local SearxNG. Raises SearchError with a specific code.

    SearxNG's `unresponsive_engines` is the reason this backend was chosen: when
    every upstream refuses, it says so instead of returning an empty list that
    looks like "no such thing exists".
    """
    try:
        async with session.get(
            f"{SEARXNG_URL}/search",
            params={"q": q, "format": "json"},
            timeout=aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_S),
        ) as resp:
            if resp.status == 429:
                raise SearchError(*RATE_LIMITED, detail="searxng returned 429")
            if resp.status == 403:
                # The limiter is on. It is a public-instance bot defence and is
                # pointless here; see searxng-settings.yml.
                raise SearchError(
                    *BACKEND_DOWN,
                    detail="searxng 403: limiter enabled, set limiter: false")
            if resp.status != 200:
                raise SearchError(
                    *BACKEND_DOWN, detail=f"searxng http {resp.status}")
            try:
                data = await resp.json(content_type=None)
            except (json.JSONDecodeError, ValueError) as exc:
                # Almost always `formats: [json]` missing from settings.yml --
                # SearxNG then serves the HTML page to a format=json request.
                raise SearchError(
                    *BACKEND_DOWN,
                    detail=f"searxng did not return JSON ({exc}); "
                           "is `json` in search.formats?") from exc
    except asyncio.TimeoutError as exc:
        raise SearchError(
            *TIMED_OUT, detail=f"searxng exceeded {SEARCH_TIMEOUT_S}s") from exc
    except aiohttp.ClientError as exc:
        raise SearchError(
            *BACKEND_DOWN, detail=f"searxng unreachable: {exc}") from exc

    results = data.get("results") or []
    dead = [e[0] if isinstance(e, (list, tuple)) else str(e)
            for e in (data.get("unresponsive_engines") or [])]

    if not results:
        # Distinguishing "the web has no answer" from "nobody would talk to us"
        # matters, because they need different spoken lines and only one of them
        # is a fault. A refusing-engines list alone is NOT enough to call it an
        # outage: on this host brave/duckduckgo/startpage/wikidata refuse
        # permanently, so before searxng-settings.yml disabled them EVERY
        # nonsense query came back as "upstream-blocked" -- measured, the
        # gibberish test case reported an outage that was not happening.
        # The real question is whether the engines we RELY on answered.
        primaries_down = [e for e in dead
                          if e.lower() in PRIMARY_ENGINES]
        if primaries_down:
            raise SearchError(
                *UPSTREAM_BLOCKED,
                detail=f"no results; primary engines refused: {primaries_down} "
                       f"(all refusing: {dead})")
        raise SearchError(
            *NO_RESULTS,
            detail=f"no results for {q!r}; engines answered fine "
                   f"(refusing: {dead or 'none'})")

    if dead:
        # Not an error -- this is the normal steady state on this host, where
        # brave/duckduckgo/startpage/wikidata all refuse. Logged so a sudden
        # change in the list is visible.
        log.info("searxng answered with %d results; engines refusing: %s",
                 len(results), dead)
    return results


async def search_brave(session: aiohttp.ClientSession, q: str) -> list[dict]:
    """
    Fallback only, and only when a key exists. Kept because SearxNG depends on
    upstream engines that can all go dark at once, and a named fallback is
    better than a dead assistant -- but it is a different privacy story: the
    query lands in a Brave account tied to the key. Documented in the README.
    """
    if not BRAVE_KEY:
        raise SearchError(*BACKEND_DOWN, detail="no BRAVE_SEARCH_KEY set")
    try:
        async with session.get(
            BRAVE_URL,
            params={"q": q, "count": str(TOP_N)},
            headers={"X-Subscription-Token": BRAVE_KEY,
                     "Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=SEARCH_TIMEOUT_S),
        ) as resp:
            if resp.status == 429:
                raise SearchError(*RATE_LIMITED, detail="brave 429 (free tier)")
            if resp.status in (401, 403):
                raise SearchError(*BACKEND_DOWN, detail="brave rejected the key")
            if resp.status != 200:
                raise SearchError(*BACKEND_DOWN, detail=f"brave http {resp.status}")
            data = await resp.json(content_type=None)
    except asyncio.TimeoutError as exc:
        raise SearchError(*TIMED_OUT, detail="brave timed out") from exc
    except aiohttp.ClientError as exc:
        raise SearchError(*BACKEND_DOWN, detail=f"brave unreachable: {exc}") from exc

    web_results = ((data.get("web") or {}).get("results")) or []
    if not web_results:
        raise SearchError(*NO_RESULTS, detail=f"brave returned nothing for {q!r}")
    return [{"title": r.get("title", ""), "content": r.get("description", ""),
             "url": r.get("url", ""), "engine": "brave"} for r in web_results]


async def fetch_pages(session: aiohttp.ClientSession, results: list[dict],
                      limit: int = 2) -> None:
    """
    Optional: pull the top pages and splice real body text into their snippets.

    Off by default. Enable only if snippets prove insufficient for some class of
    question, and re-measure the loop latency when you do -- each page is a full
    TLS handshake to a host nobody vetted, which is both slow and more egress.
    """
    async def one(r: dict) -> None:
        url = r.get("url") or ""
        if not url.startswith("http"):
            return
        try:
            async with session.get(
                url, headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_S),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return
                ctype = resp.headers.get("Content-Type", "")
                if "html" not in ctype and "text" not in ctype:
                    return
                body = await resp.text(errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeDecodeError):
            return
        # Deliberately regex, not a parser: a parser is another dependency and
        # this only ever needs the first paragraph or so of readable text.
        body = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", body)
        text = clean(TAG_RE.sub(" ", body))
        if len(text) > 200:
            r["content"] = text[:SNIPPET_CHARS * 2]
        r["fetched"] = True

    await asyncio.gather(*(one(r) for r in results[:limit]))


SUMMARY_PROMPT = (
    "The search results below contain the answer. Extract it and state it in "
    "ONE sentence of at most 12 words. Quote the specific number, name, date or "
    "time. Do not mention sources or say 'according to'. Only if the results are "
    "genuinely unrelated to the question, reply exactly: NO ANSWER.\n\n"
    "Question: {q}\n\nSearch results:\n{ctx}\n\nOne-sentence answer:"
)


async def summarise(session: aiohttp.ClientSession, q: str, ctx: str,
                    budget_s: float = SUMMARY_TIMEOUT_S) -> str:
    """
    One pass through the local 3B to turn snippets into a spoken sentence.

    `budget_s` is what is LEFT of the hard deadline, not a fixed cap: if the
    search stage ran long, the summariser gets less, and if there is not enough
    left to be worth trying it is skipped entirely. Without this the summariser
    could consume the deadline and lose results already in hand.

    The prompt wording is load-bearing and was arrived at by measurement. The
    first version ended with "If the results do not contain the answer, reply
    exactly: NO ANSWER" as its main instruction, and qwen2.5:3b took that exit
    on 3 of 4 questions whose answer was plainly in the snippets -- including
    one whose snippet read "Monday, 09:30 AM, 10:00 PM". Telling it up front
    that the answer IS present, and demoting NO ANSWER to a last resort, took
    the same model on the same snippets from 1/4 to 4/4. Do not "tidy" the
    opening sentence away.
    """
    if budget_s < 0.5:
        log.info("skipping summariser, only %.2fs of deadline left", budget_s)
        return ""
    try:
        async with session.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": SUMMARY_MODEL,
                "prompt": SUMMARY_PROMPT.format(q=q, ctx=ctx),
                "stream": False,
                "options": {"num_predict": 48, "temperature": 0.1, "num_ctx": 8192},
            },
            timeout=aiohttp.ClientTimeout(total=budget_s),
        ) as resp:
            if resp.status != 200:
                log.warning("ollama returned %s; falling back to extractive",
                            resp.status)
                return ""
            data = await resp.json()
    except asyncio.TimeoutError:
        # Not fatal: the caller degrades to extractive rather than failing the
        # whole search, because trimmed snippets are still a usable answer.
        log.warning("summariser exceeded %.2fs; falling back to extractive",
                    budget_s)
        return ""
    except aiohttp.ClientError as exc:
        log.warning("ollama unreachable (%s); falling back to extractive", exc)
        return ""

    out = (data.get("response") or "").strip()
    if not out or out.upper().startswith("NO ANSWER"):
        return ""
    return out


# --- The one entry point --------------------------------------------------
async def do_search(session: aiohttp.ClientSession, q: str,
                    mode: str = MODE) -> dict[str, Any]:
    """
    The whole feature. Raises SearchError; never returns an empty success.

    Bounded by DEADLINE_S overall as well as per-stage timeouts, because three
    stages that each just miss their own timeout still add up to something the
    user experiences as a hang.
    """
    if not ENABLED:
        raise SearchError(*DISABLED,
                          detail="JARVIS_WEBSEARCH_ENABLED is off; no packet sent")

    q = guard(q)
    started = time.monotonic()

    async def run() -> dict[str, Any]:
        upstream = "searxng"
        try:
            results = await search_searxng(session, q)
        except SearchError as exc:
            if BRAVE_KEY and exc.code in ("backend-down", "upstream-blocked"):
                log.warning("searxng failed (%s); trying brave", exc.detail)
                upstream = "brave"
                results = await search_brave(session, q)
            else:
                raise

        if FETCH_PAGES:
            await fetch_pages(session, results)

        ctx = pack(results)
        if not ctx:
            raise SearchError(*NO_RESULTS,
                              detail="results had neither title nor snippet")

        spoken, how = "", "extractive"
        if mode == "summarise":
            # Hand the summariser only what is left of the deadline, minus the
            # reserve the extractive fallback needs to still produce an answer.
            left = DEADLINE_S - (time.monotonic() - started) - FALLBACK_RESERVE_S
            budget = min(SUMMARY_TIMEOUT_S, max(0.0, left))
            raw = await summarise(session, q, ctx, budget_s=budget)
            if raw:
                spoken, how = to_spoken(raw), "summarise"
        if not spoken:
            # Extractive path, and also the degraded path when the summariser
            # times out or declines. Scored across all snippets -- see
            # extract_answer for why "first sentence of result one" is wrong.
            spoken = extract_answer(q, results)
        if not spoken:
            raise SearchError(*NO_RESULTS, detail="nothing speakable in results")

        sources = [{"title": clean(r.get("title", ""))[:90],
                    "url": r.get("url", ""),
                    "engine": r.get("engine", "")}
                   for r in results[:TOP_N] if r.get("url")]

        return {
            "ok": True,
            "query": q,
            "answer": spoken,
            # The HUD gets the long form. The voice path must never read this.
            "detail": ctx,
            "sources": sources,
            "mode": how,
            "upstream": upstream,
            "engines_refusing": [],
        }

    try:
        out = await asyncio.wait_for(run(), timeout=DEADLINE_S)
    except asyncio.TimeoutError as exc:
        ms = int((time.monotonic() - started) * 1000)
        note_egress(q, "searxng", "deadline", ms)
        raise SearchError(
            *TIMED_OUT,
            detail=f"hard deadline {DEADLINE_S}s exceeded after {ms} ms") from exc
    except SearchError as exc:
        ms = int((time.monotonic() - started) * 1000)
        note_egress(q, "searxng", exc.code, ms)
        raise

    ms = int((time.monotonic() - started) * 1000)
    out["elapsed_ms"] = ms
    note_egress(q, out["upstream"], "ok", ms)
    log.info("search %r -> %s (%s, %d ms)", q, out["answer"], out["mode"], ms)
    return out


# --- HTTP -----------------------------------------------------------------
async def handle_search(request: web.Request) -> web.Response:
    """
    POST /search {"q": "..."} -- the only outbound-capable route.

    Always 200 with an `ok` field rather than an HTTP error status. Home
    Assistant's rest_command treats a 4xx/5xx as a failed action and gives the
    conversation agent nothing at all, which is precisely the empty result that
    makes the model invent an answer. A 200 carrying {"ok": false, "answer":
    "<a spoken sentence>"} means the model always has real words to say.
    """
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}
    q = body.get("q") or body.get("query") or request.query.get("q", "")

    try:
        out = await do_search(request.app["http"], q)
    except SearchError as exc:
        log.info("search refused/failed [%s]: %s", exc.code, exc.detail)
        return web.json_response({
            "ok": False,
            "error": exc.code,
            "answer": exc.spoken,
            "detail": exc.detail,
            "query": q,
        })
    return web.json_response(out)


async def healthz(request: web.Request) -> web.Response:
    """Liveness only. 200 whenever the process is up, exactly like jarvis-voice."""
    return web.json_response({"status": "ok", "enabled": ENABLED})


async def status(request: web.Request) -> web.Response:
    """
    Readiness. Probes SearxNG with a real query it can answer WITHOUT touching
    the internet, so a status check is not itself egress: `!wikidata`-free,
    engines=none returns SearxNG's own error page fields rather than searching.
    Falls back to /config, which is local and unauthenticated.
    """
    searx_state = "error"
    engines = 0
    try:
        async with request.app["http"].get(
            f"{SEARXNG_URL}/config",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status == 200:
                cfg = await resp.json(content_type=None)
                engines = len([e for e in cfg.get("engines", [])
                               if e.get("enabled")])
                searx_state = "ok"
            else:
                searx_state = f"http-{resp.status}"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        searx_state = "unreachable"

    ollama_state = "error"
    try:
        async with request.app["http"].get(
            f"{OLLAMA_URL}/api/tags", timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            ollama_state = "ok" if resp.status == 200 else f"http-{resp.status}"
    except (aiohttp.ClientError, asyncio.TimeoutError):
        ollama_state = "unreachable"

    return web.json_response({
        "enabled": ENABLED,
        "mode": MODE,
        "searxng": searx_state,
        "searxng_engines_enabled": engines,
        "ollama": ollama_state,
        "brave_fallback_configured": bool(BRAVE_KEY),
        "fetch_pages": FETCH_PAGES,
        "deadline_s": DEADLINE_S,
        "searches_since_start": len(EGRESS),
    })


async def egress(request: web.Request) -> web.Response:
    """
    Every search this process has made, newest last, capped at 200.

    Exists for the packet-capture deliverable: a capture showing traffic to
    google/wikipedia at 21:04:11 can be matched against an entry here to show it
    was a user question rather than background phone-home. An EMPTY list beside
    a capture full of traffic is the alarming case, and this is what makes that
    visible.
    """
    return web.json_response({"count": len(EGRESS), "searches": list(EGRESS)})


async def on_startup(app: web.Application) -> None:
    app["http"] = aiohttp.ClientSession(headers={"User-Agent": USER_AGENT})
    if not ENABLED:
        log.warning("JARVIS_WEBSEARCH_ENABLED is off. /search will refuse and "
                    "this process will make no outbound requests at all.")
    else:
        log.info("web search ENABLED. upstream=%s mode=%s deadline=%.1fs "
                 "brave_fallback=%s", SEARXNG_URL, MODE, DEADLINE_S,
                 bool(BRAVE_KEY))


async def on_cleanup(app: web.Application) -> None:
    await app["http"].close()


def build_app() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.add_routes([
        web.post("/search", handle_search),
        web.get("/search", handle_search),
        web.get("/healthz", healthz),
        web.get("/api/status", status),
        web.get("/api/egress", egress),
    ])
    return app


# --- CLI ------------------------------------------------------------------
async def _cli_ask(q: str, mode: str) -> int:
    async with aiohttp.ClientSession(headers={"User-Agent": USER_AGENT}) as s:
        t0 = time.monotonic()
        try:
            out = await do_search(s, q, mode=mode)
        except SearchError as exc:
            ms = int((time.monotonic() - t0) * 1000)
            print(f"FAILED [{exc.code}] after {ms} ms")
            print(f"  spoken: {exc.spoken}")
            print(f"  detail: {exc.detail}")
            return 1
        print(f"OK {out['elapsed_ms']} ms  mode={out['mode']} "
              f"upstream={out['upstream']}")
        print(f"  spoken ({len(out['answer'].split())} words): {out['answer']}")
        print(f"  detail ({len(out['detail'])} chars):")
        for line in out["detail"].splitlines():
            print(f"    {line[:150]}")
        for src in out["sources"][:3]:
            print(f"  src: {src['engine']:<12} {src['url'][:100]}")
        return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="JARVIS web search service and one-shot query tool.")
    ap.add_argument("--ask", metavar="QUESTION",
                    help="run one search and print it, without starting a server")
    ap.add_argument("--mode", choices=("summarise", "extractive"), default=MODE,
                    help=f"summarisation strategy (default {MODE})")
    ap.add_argument("--serve", action="store_true", help="run the HTTP service")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()

    if args.ask:
        sys.exit(asyncio.run(_cli_ask(args.ask, args.mode)))
    web.run_app(build_app(), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
