"""
Local-first intent routing.

Why this exists
---------------
Home Assistant ships two conversation agents and they have wildly different
costs. `conversation.home_assistant` is a pure local template matcher -- no
model, no GPU, measured here at 11-85 ms. `conversation.jarvis` is qwen2.5:3b
through Ollama, measured at 750-5600 ms for the same utterances. That is a
30-200x gap on the commands the local parser can actually handle.

So: try local first, fall back to the model only when local genuinely failed.

The measurement that motivated this, and the correction
-------------------------------------------------------
HANDOFF.md claimed "18 of 25 commands are handled locally". Re-measured on
2026-08-09 with perf/probe_local.py, the truth is more specific and the
distinction matters:

  - Commands matched by EXACT friendly name are handled: the scripts
    ("good night", "fan boost", "AC dry mode", "maximum cooling"), and the fan,
    whose friendly name is literally "Fan".
  - Commands using NATURAL phrasing for the lights all FAIL, because the lights
    are registered as "Node Switch Gang 1/2/3" and nobody says that out loud.
    "turn on the lights", "turn on the ceiling light", "turn on gang 1" -> error.
  - Argument extraction FAILS: "set the AC to 22", "set the fan to speed 3".

11 of 19 on that mixed set. The gap between 11 and 18 is entirely a NAMING
problem, not a parser problem -- see aliases.py, which closes it.

What must not happen
--------------------
A false local success is worse than a fallback. If the local parser matches the
wrong entity and returns action_done, JARVIS confirms an action that never
happened where the user wanted it. So `_handled()` below is deliberately strict:
an action_done carrying a non-empty `failed` list is treated as NOT handled and
falls through to the model.

Conversation state
------------------
The two agents keep SEPARATE conversation histories. Threading a conversation_id
minted by the Ollama agent into the built-in matcher (or the reverse) makes HA
resolve follow-ups against a history that does not exist. This class therefore
keeps one id per agent and never crosses them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from phrasing import looks_like_question, normalise

log = logging.getLogger("jarvis.router")

# Response types HA uses for a turn that actually did something.
_SUCCESS_TYPES = ("action_done", "query_answer")


@dataclass
class RouteResult:
    """One routed turn, with enough detail to measure and to display."""

    speech: str
    path: str                 # "local" | "hermes" | "llm" | "error"
    local_ms: float = 0.0     # always populated; local is always attempted
    llm_ms: float = 0.0       # 0.0 when the model was not needed
    total_ms: float = 0.0
    fallback_reason: str = ""  # why local was rejected, "" when local won
    rewritten: str = ""        # normalised form actually sent, "" if unchanged
    rewrite_rule: str = ""     # which phrasing rule fired
    # --- Hermes attempt, populated only when the specialist path ran.
    hermes_ms: float = 0.0
    specialist: str = ""       # which specialist classify() picked
    tool_calls: int = 0        # how many calls it dispatched
    hermes_reason: str = ""     # why Hermes did not win, "" when it did


@dataclass
class RouterStats:
    """Rolling counters, surfaced on /status so the win stays visible."""

    local_hits: int = 0
    llm_hits: int = 0
    hermes_hits: int = 0
    # Turns that ended in an explained refusal -- a WhatsApp contact not on the
    # allowlist, a service that was down. Counted separately from errors on
    # purpose: a refusal is the system working correctly and saying so, and
    # burying it in the error count would make a well-behaved safety gate look
    # like a fault.
    refusals: int = 0
    greetings: int = 0
    errors: int = 0
    local_ms: list[float] = field(default_factory=list)
    llm_ms: list[float] = field(default_factory=list)
    hermes_ms: list[float] = field(default_factory=list)

    def record(self, r: RouteResult) -> None:
        if r.path == "local":
            self.local_hits += 1
            self.local_ms.append(r.total_ms)
        elif r.path == "hermes":
            self.hermes_hits += 1
            self.hermes_ms.append(r.total_ms)
        elif r.path == "llm":
            self.llm_hits += 1
            self.llm_ms.append(r.total_ms)
        elif r.path == "refused":
            self.refusals += 1
        elif r.path == "greeting":
            self.greetings += 1
        else:
            # Anything unrecognised counts as an error DELIBERATELY. A new path
            # added without a branch here would otherwise inflate the success
            # numbers silently, and these counters are how the local-first win
            # stays visible.
            self.errors += 1
        # Bounded: this runs for weeks without a restart.
        del self.local_ms[:-200]
        del self.llm_ms[:-200]
        del self.hermes_ms[:-200]

    @staticmethod
    def _median(xs: list[float]) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    def snapshot(self) -> dict[str, Any]:
        total = self.local_hits + self.hermes_hits + self.llm_hits
        return {
            "local_hits": self.local_hits,
            "hermes_hits": self.hermes_hits,
            "llm_hits": self.llm_hits,
            "refusals": self.refusals,
            "greetings": self.greetings,
            "errors": self.errors,
            "local_share": round(self.local_hits / total, 3) if total else 0.0,
            # The number that matters for the tool-cliff problem: how often the
            # 17-tool agent was still needed. Every hermes hit is a command that
            # used to reach it and now sees 4-6 tools instead.
            "big_agent_share": round(self.llm_hits / total, 3) if total else 0.0,
            "local_median_ms": round(self._median(self.local_ms), 1),
            "hermes_median_ms": round(self._median(self.hermes_ms), 1),
            "llm_median_ms": round(self._median(self.llm_ms), 1),
        }


class IntentRouter:
    """Tries the built-in matcher, falls back to the LLM agent."""

    def __init__(
        self,
        hass_url: str,
        token: str,
        local_agent: str = "conversation.home_assistant",
        llm_agent: str = "conversation.jarvis",
        # 3s: local matcher usually answers in <100ms but HA can take 2-3s
        # after a cold restart or when intent scripts are heavy (movie_time).
        local_timeout_s: float = 3.0,
        # 10s for the LLM agent (conversation.jarvis via Ollama).
        llm_timeout_s: float = 10.0,
        # 15s aggregate: local(3s) + Hermes(5-8s when model loaded) + buffer.
        # When model is evicted, Hermes will timeout at 10s and fail fast
        # rather than making the user wait 25s. The proactive reload in the
        # Hermes block handles the cold-start case.
        turn_timeout_s: float = 15.0,
        enabled: bool = True,
        hermes_enabled: bool | None = None,
    ) -> None:
        self.hass_url = hass_url.rstrip("/")
        self.token = token
        self.local_agent = local_agent
        self.llm_agent = llm_agent
        self.local_timeout_s = local_timeout_s
        self.llm_timeout_s = llm_timeout_s
        self.turn_timeout_s = turn_timeout_s
        # Kill switch. If routing ever misbehaves, JARVIS must still work:
        # setting this False restores the previous LLM-only behaviour exactly.
        self.enabled = enabled
        self.stats = RouterStats()

        # --- Hermes: the specialist tier between local and the big agent.
        #
        # WHY IT SITS HERE. The measured defect that has cost this project the
        # most is the tool cliff: at 17 exposed tools qwen2.5:3b narrates
        # actions it never performs ("Movie time activated." with
        # last_triggered still None). At ~13 it scores 4/4. Hermes classifies
        # the utterance to ONE specialist carrying only 4-6 tools and a prompt
        # scoped to exactly those, which is the structural fix -- a specialist
        # inheriting the full prompt hallucinates actions for tools it does not
        # have.
        #
        # Built here but attempted only AFTER local misses, so the ~26 ms
        # no-model path is untouched. Falls through to the 17-tool agent when it
        # cannot produce a dispatchable call, so this can only add successes.
        if hermes_enabled is None:
            hermes_enabled = os.getenv("JARVIS_HERMES", "1") not in ("0", "false", "no")
        self.hermes_enabled = hermes_enabled
        self._hermes = None
        self._executor = None
        self._classify = None
        # Specialists that own a tool which READS real state and can therefore
        # answer a question truthfully. Derived from the tool tables rather than
        # hand-listed: a new read tool must not require someone to remember to
        # update a second list here, which is how a check goes stale.
        self._readers: set[str] = set()
        # The read-only tool NAMES, derived from the same source. Kept for
        # the partial-success guard in process(): when the only failures are
        # reads the actuation already happened and re-running is worse than
        # honest silence.
        self._read_tools: set[str] = set()
        # Specialists with NO tools ("chat"): they answer conversationally, so
        # a question is exactly their job and a speech-without-tool-call reply
        # is a win, not a fall-through. Same derived-from-source rule as
        # `_readers` -- any specialist marked talk_only joins automatically.
        self._talk_only: set[str] = set()
        if self.hermes_enabled:
            try:
                from hermes.hermes import Hermes, SPECIALISTS, classify
                from hermes.executor import Executor
                self._hermes = Hermes()
                self._executor = Executor(self.hass_url, self.token)
                self._classify = classify
                _read_tools = {"wa_read", "pc_status", "GetLiveContext", "server_status", "call_state"}
                self._read_tools = set(_read_tools)
                self._readers = {
                    n for n, s in SPECIALISTS.items()
                    if _read_tools.intersection(s.tools)
                }
                self._talk_only = {
                    n for n, s in SPECIALISTS.items() if s.talk_only
                }
            except Exception as exc:  # noqa: BLE001
                # A missing or broken Hermes must degrade to the previous
                # behaviour, never take the voice loop down. This is exactly the
                # failure that hid hud.py: import errors at startup are fatal.
                log.warning("Hermes unavailable, using LLM fallback only: %s", exc)
                self.hermes_enabled = False

    # --- internals ---------------------------------------------------------

    async def _ask(
        self,
        session: aiohttp.ClientSession,
        text: str,
        agent_id: str,
        conversation_id: str | None,
        timeout_s: float,
    ) -> tuple[dict[str, Any] | None, str]:
        """POST one turn. Returns (data, error_reason). data is None on failure."""
        payload: dict[str, Any] = {"text": text, "language": "en", "agent_id": agent_id}
        if conversation_id:
            payload["conversation_id"] = conversation_id
        try:
            async with session.post(
                f"{self.hass_url}/api/conversation/process",
                json=payload,
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as resp:
                if resp.status == 401:
                    return None, "unauthorized"
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    log.warning("%s returned %s: %s", agent_id, resp.status, body)
                    return None, f"http_{resp.status}"
                return await resp.json(), ""
        except asyncio.TimeoutError:
            return None, "timeout"
        except aiohttp.ClientError as exc:
            log.warning("%s unreachable: %s", agent_id, exc)
            return None, "unreachable"

    @staticmethod
    def _speech(data: dict[str, Any]) -> str:
        return (
            data.get("response", {})
            .get("speech", {})
            .get("plain", {})
            .get("speech")
            or ""
        )

    @staticmethod
    def _handled(data: dict[str, Any], utterance: str) -> tuple[bool, str]:
        """
        Did the local parser genuinely succeed?

        Strict on purpose. A spoken confirmation for an action that did not
        happen is silent and undetectable by the listener, which is the single
        most expensive failure mode in this project.
        """
        resp = data.get("response", {})
        rtype = resp.get("response_type")
        if rtype not in _SUCCESS_TYPES:
            # HA puts a machine-readable reason here: no_intent_match,
            # no_valid_targets, ...
            code = resp.get("data", {}).get("code") or rtype or "unknown"
            return False, str(code)
        # An imperative that matched a QUERY intent is a mis-route, not a win.
        # Observed: "all lights on" was ANSWERED -- "No, Gang 1, Gang 2 and
        # Gang 3 are off" -- instead of turning anything on. Trusting that would
        # make JARVIS reply to a command as though it were a question.
        if rtype == "query_answer" and not looks_like_question(utterance):
            return False, "query_for_imperative"
        # action_done can still carry failures per-target.
        failed = resp.get("data", {}).get("failed") or []
        if failed:
            return False, "targets_failed"
        # NOTE: do NOT reject on an empty `success` list. Measured 2026-08-09:
        # every intent_script handler returns {"success": [], "failed": []} even
        # on a completely successful run -- HA only populates those lists for
        # the built-in entity intents. An earlier version of this check treated
        # empty success as "nothing happened" and silently sent all 13 custom
        # intents to the model, dropping local hits from 13/24 to 9/24 while
        # every one of them worked when called directly. `failed` is the only
        # trustworthy signal here.
        return True, ""

    # --- public ------------------------------------------------------------

    async def process(
        self,
        session: aiohttp.ClientSession,
        text: str,
        state: dict[str, str | None],
    ) -> RouteResult:
        """Route one utterance under a hard aggregate timeout."""
        started = time.perf_counter()
        try:
            return await asyncio.wait_for(
                self._process_impl(session, text, state),
                timeout=self.turn_timeout_s,
            )
        except asyncio.TimeoutError:
            r = RouteResult(
                speech="I took too long to process that, so I stopped.",
                path="error",
                total_ms=round((time.perf_counter() - started) * 1000, 1),
                fallback_reason="turn_timeout",
            )
            self.stats.record(r)
            return r

    async def _process_impl(
        self,
        session: aiohttp.ClientSession,
        text: str,
        state: dict[str, str | None],
    ) -> RouteResult:
        """
        Route one utterance.

        `state` is a mutable per-connection dict holding the two conversation
        ids. Pass the same dict back on every turn of a session.
        """
        started = time.perf_counter()

        if not self.token:
            return RouteResult(
                speech="Home Assistant token is not configured, so I cannot act on that.",
                path="error",
            )

        # --- greeting fast-path: respond instantly, no model, no HA call -------
        # Common non-action phrases that users say to check if JARVIS is alive.
        # Sending these to the local HA agent costs ~2.5s and often falls through
        # to the LLM anyway (HA has no concept of "can you hear me?"). The model
        # path then takes 3-8s more. Total: 5-10s of silence for "hello".
        #
        # These are answered immediately with a short status reply. The user gets
        # an instant acknowledgment and the voice loop stays snappy.
        #
        # KEPT STRICT: only literal matches, no fuzzy matching. A wrong reply to
        # an action request is worse than a missed reply to a greeting.
        _GREETING_RE = re.compile(
            r"^\s*(?:jarvis,?\s*)?(?:can\s+you\s+hear\s+me|do\s+you\s+hear\s+me|"
            r"are\s+you\s+there|you\s+there|hey\s+jarvis|hello\s+jarvis|"
            r"are\s+you\s+awake|is\s+anyone\s+there|is\s+this\s+on|are\s+you\s+on|"
            r"what'?s\s+up|whats\s+up|what\s+is\s+up|how\s+are\s+you|how\s+do\s+you\s+do|"
            r"test\s+(?:1|2|one|two)|ping|status\s+check)\s*(?:,?\s*jarvis)?\s*\??\s*$",
            re.I,
        )
        _GREETING_REPLY = {
            "hear":      "Loud and clear, sir.",
            "there":     "Always, sir.",
            "awake":     "Wide awake, sir.",
            "on":        "Systems online, sir.",
            "hello":     "Good evening, sir.",
            "hey":       "Yes, sir?",
            "up":        "All systems nominal, sir.",
            "are":       "I am, sir.",
            "test":      "Test acknowledged, sir.",
            "ping":      "Pong, sir.",
            "status":    "Everything green, sir.",
        }

        gm = _GREETING_RE.match(text.strip())
        if gm:
            low = text.strip().lower()
            for key, reply in _GREETING_REPLY.items():
                if key in low:
                    r = RouteResult(
                        speech=reply,
                        path="greeting",
                        local_ms=0.0,
                        total_ms=0.0,
                    )
                    self.stats.record(r)
                    return r

        # Rewrite into a phrasing the local parser accepts. Free, and it is what
        # turns most of the near-misses into local hits. The ORIGINAL text is
        # what goes to the model on fallback -- the model reasons better about
        # what was actually said than about our rewrite of it.
        local_text, rule = normalise(text)

        # --- attempt 1: local, no model in the path
        local_ms = 0.0
        reason = "disabled"
        if self.enabled:
            t0 = time.perf_counter()
            data, err = await self._ask(
                session, local_text, self.local_agent,
                state.get("local_cid"), self.local_timeout_s,
            )
            local_ms = (time.perf_counter() - t0) * 1000
            if data is not None:
                state["local_cid"] = data.get("conversation_id") or state.get("local_cid")
                ok, why = self._handled(data, local_text)
                if ok:
                    r = RouteResult(
                        speech=self._speech(data) or "At your service, sir.",
                        path="local",
                        local_ms=round(local_ms, 1),
                        total_ms=round((time.perf_counter() - started) * 1000, 1),
                        rewritten=local_text if local_text != text else "",
                        rewrite_rule=rule,
                    )
                    self.stats.record(r)
                    return r
                reason = why
            else:
                reason = err
                # An unreachable HA will fail the LLM call too, but let it try:
                # only the local agent may be the thing that is down.

        # --- attempt 2: Hermes, one specialist carrying 4-6 tools
        #
        # Questions are handled by Hermes only when that specialist can either READ
        # real state or has no tools at all. Never fall through to the big HA agent
        # merely because a question mentioned an actuation-only thing: "is the TV
        # on" reached conversation.jarvis on 2026-08-09 and toggled the TV scene
        # while speaking as if it had answered a question. Unknown/unreadable
        # questions are forced to chat, which has zero tools and cannot mutate the
        # room.
        hermes_ms = 0.0
        specialist = ""
        n_calls = 0
        hermes_reason = "disabled" if not self.hermes_enabled else ""
        if self.hermes_enabled and self._hermes and self._executor:
            # Proactive Ollama model reload. On the 4GB RTX 3050 with WDDM,
            # the GPU driver evicts the model after ~2-3 min idle despite
            # OLLAMA_KEEP_ALIVE=-1 (display driver claims VRAM first).
            # Reload takes ~15s; without it, every Hermes command fails at
            # turn_timeout. The keepalive background task tries to keep it
            # loaded, but when it falls behind we reload on-demand here.
            # Cost when model IS loaded: ~8ms for the /api/ps check.
            try:
                async with session.get(
                    f"{self._hermes.ollama_url}/api/ps",
                    timeout=aiohttp.ClientTimeout(total=2),
                ) as resp:
                    ps = await resp.json()
                    if not ps.get("models"):
                        log.info("Ollama model evicted, reloading before Hermes...")
                        t_warm = time.perf_counter()
                        await self._hermes.ask(session, "warmup", specialist="chat")
                        log.info("Ollama reloaded in %.0f ms", (time.perf_counter() - t_warm) * 1000)
            except Exception as exc:  # noqa: BLE001 - never let a failed reload block the turn
                log.debug("Ollama check failed (%s); proceeding to Hermes anyway", exc)
            # Questions are normally the big agent's job, because a specialist
            # holds only actuation tools and would answer from its own head.
            #
            # The exception is a specialist that owns a genuine READ. "any
            # messages?" is a question, and sending it to the big agent -- which
            # has no WhatsApp tool at all -- is precisely how this project
            # produces a confident answer about something nothing ever looked
            # at. classify() is keyword scoring, ~0 ms and no model, so asking
            # it first costs nothing.
            #
            # A talk_only specialist ("chat", zero tools) is the other exception:
            # it exists to answer questions conversationally, and it has no tool
            # to phantom-fire, so a question is its whole job.
            spec_guess = ""
            if looks_like_question(text) and (self._readers or self._talk_only):
                try:
                    spec_guess, _ = self._classify(text)
                except Exception:  # noqa: BLE001 - never let routing kill the turn
                    spec_guess = ""
            forced_specialist = None
            if (looks_like_question(text)
                    and spec_guess not in self._readers
                    and spec_guess not in self._talk_only):
                # Safety valve for ungrounded questions. The old path fell through
                # to the full Home Assistant LLM agent, which still holds actuation
                # tools; "is the tv on" triggered scene.monitor_tv_on_and_off while
                # sounding like a read. Chat has no tools, so it can answer or admit
                # ignorance without changing the room.
                forced_specialist = "chat"
                hermes_reason = "question_to_chat"
            t_h = time.perf_counter()
            try:
                h = await self._hermes.ask(session, text, specialist=forced_specialist)
                specialist = h.specialist
                if h.error:
                    hermes_reason = f"error:{h.error[:60]}"
                elif not h.tool_calls:
                    # A talk_only specialist produces speech WITHOUT tool
                    # calls by design -- it has no tools, so this is its win,
                    # not a phantom. Speaking it is the whole point of the
                    # tier. Every other specialist still falls through:
                    # speaking speech there would confirm an action that
                    # nothing performed.
                    if h.speech and specialist in self._talk_only:
                        r = RouteResult(
                            speech=h.speech.strip(),
                            path="hermes",
                            local_ms=round(local_ms, 1),
                            hermes_ms=round((time.perf_counter() - t_h) * 1000, 1),
                            total_ms=round((time.perf_counter() - started) * 1000, 1),
                            fallback_reason=reason,
                            hermes_reason="talk_only",
                            specialist=specialist,
                        )
                        self.stats.record(r)
                        return r
                    if forced_specialist == "chat":
                        r = RouteResult(
                            speech="I cannot read that state yet, sir.",
                            path="hermes",
                            local_ms=round(local_ms, 1),
                            hermes_ms=round((time.perf_counter() - t_h) * 1000, 1),
                            total_ms=round((time.perf_counter() - started) * 1000, 1),
                            fallback_reason=reason,
                            hermes_reason="question_to_chat_no_speech",
                            specialist=specialist,
                        )
                        self.stats.record(r)
                        return r
                    # No dispatchable call. Falling through is the correct
                    # move: speaking h.speech here would confirm an action
                    # that nothing performed.
                    hermes_reason = "no_tool_call"
                else:
                    results = await self._executor.run(session, h.tool_calls)
                    n_calls = len(results)
                    failed = [r for r in results if not r.ok]
                    if failed:
                        hermes_reason = f"exec_failed:{failed[0].tool}:{failed[0].detail[:50]}"
                        # An EXPLAINED refusal ends the turn here rather than
                        # falling through. Falling through would hand "message
                        # my boss" -- refused because he is not in contacts --
                        # to an agent holding no messaging tool at all, whose
                        # most natural reply is to confirm a send that never
                        # happened. Only refusals the executor could actually
                        # name carry speech; anything unrecognised still falls
                        # through to the model as before.
                        explained = next(
                            (x.speech for x in failed if x.speech), "")
                        if explained:
                            r = RouteResult(
                                speech=explained,
                                path="refused",
                                local_ms=round(local_ms, 1),
                                hermes_ms=round((time.perf_counter() - t_h) * 1000, 1),
                                total_ms=round((time.perf_counter() - started) * 1000, 1),
                                fallback_reason=reason,
                                specialist=specialist,
                                tool_calls=n_calls,
                                hermes_reason=hermes_reason,
                            )
                            self.stats.record(r)
                            return r
                        # The common voice pattern is an actuation followed by
                        # a trailing read that was only meant to report the
                        # new state: "turn the fan off" -> [fan_set,
                        # GetLiveContext]. If the actuation succeeded but the
                        # non-essential read failed, the turn must not fall
                        # through and re-run the same actuation again.
                        failed_tools = {x.tool for x in failed}
                        non_read_failures = failed_tools - self._read_tools
                        if not non_read_failures and n_calls > len(failed):
                            grounded = (results[-1].speech
                                        if results and results[-1].speech else "")
                            r = RouteResult(
                                speech=(grounded or h.speech or "Done.").strip(),
                                path="hermes",
                                local_ms=round(local_ms, 1),
                                hermes_ms=round((time.perf_counter() - t_h) * 1000, 1),
                                total_ms=round((time.perf_counter() - started) * 1000, 1),
                                fallback_reason=reason,
                                rewritten=local_text if local_text != text else "",
                                rewrite_rule=rule,
                                specialist=specialist,
                                tool_calls=n_calls,
                                hermes_reason=f"{hermes_reason};read_failed",
                            )
                            self.stats.record(r)
                            return r
                    else:
                        hermes_ms = (time.perf_counter() - t_h) * 1000
                        # A read tool builds its sentence from the service's
                        # actual response and that ALWAYS wins over the
                        # model's. Without this, "who messaged me" is
                        # answered by a 3B model that has the message text
                        # nowhere in its context and will happily invent a
                        # sender. Actuation tools leave it empty, so the
                        # model's short confirmation is still what is heard.
                        #
                        # Only the LAST call's speech is honoured, and only
                        # when it is a read. Ordering carries meaning here:
                        # "turn the fan off" can emit [fan_set, GetLiveContext]
                        # to report the result, and the read is meant to be
                        # heard -- but a read BEFORE an actuation is the fan
                        # state before the change, and narrating it after the
                        # fact would contradict what just happened. A leading
                        # read's speech must not leak past its own moment.
                        grounded = (results[-1].speech
                                    if results and results[-1].speech else "")
                        r = RouteResult(
                            # The model often returns empty content when it
                            # emits tool calls, so a confirmation must not
                            # depend on it having said anything.
                            speech=(grounded or h.speech or "At your service, sir.").strip(),
                            path="hermes",
                            local_ms=round(local_ms, 1),
                            hermes_ms=round(hermes_ms, 1),
                            total_ms=round((time.perf_counter() - started) * 1000, 1),
                            fallback_reason=reason,
                            rewritten=local_text if local_text != text else "",
                            rewrite_rule=rule,
                            specialist=specialist,
                            tool_calls=n_calls,
                        )
                        self.stats.record(r)
                        return r
            except Exception as exc:  # noqa: BLE001
                hermes_reason = f"{type(exc).__name__}:{exc}"
            hermes_ms = (time.perf_counter() - t_h) * 1000

        # --- attempt 3: the model
        t1 = time.perf_counter()
        data, err = await self._ask(
            session, text, self.llm_agent,
            state.get("llm_cid"), self.llm_timeout_s,
        )
        llm_ms = (time.perf_counter() - t1) * 1000

        if data is None:
            msg = {
                "unauthorized": "Home Assistant rejected my token.",
                "timeout": "Home Assistant took too long to answer.",
                "unreachable": "I could not reach Home Assistant.",
            }.get(err, "Home Assistant returned an error.")
            r = RouteResult(
                speech=msg, path="error",
                local_ms=round(local_ms, 1), llm_ms=round(llm_ms, 1),
                total_ms=round((time.perf_counter() - started) * 1000, 1),
                fallback_reason=reason,
                hermes_ms=round(hermes_ms, 1), specialist=specialist,
                hermes_reason=hermes_reason,
            )
            self.stats.record(r)
            return r

        state["llm_cid"] = data.get("conversation_id") or state.get("llm_cid")
        r = RouteResult(
            speech=self._speech(data) or "At your service, sir.",
            path="llm",
            local_ms=round(local_ms, 1),
            llm_ms=round(llm_ms, 1),
            total_ms=round((time.perf_counter() - started) * 1000, 1),
            fallback_reason=reason,
            hermes_ms=round(hermes_ms, 1),
            specialist=specialist,
            hermes_reason=hermes_reason,
        )
        self.stats.record(r)
        return r
