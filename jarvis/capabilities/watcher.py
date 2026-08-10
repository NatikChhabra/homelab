#!/usr/bin/env python3
"""
Proactive announcements. Zero tools -- this is push, not pull, so the model
never sees it and it costs nothing against the tool budget that a 3B model
falls apart above.

THE RESTRAINT IS THE DESIGN. An assistant that talks unprompted is one bad week
away from being muted, and a muted assistant protects nothing at all. So a
failure has to clear four independent gates before it is ever spoken:

  1. SEVERITY FLOOR -- FAIL only. WARN is never announced. The report currently
     carries three WARNs that are all long-standing known conditions (no
     offsite backup, the known-lost Immich baseline, the broken docker
     credential helper). Announcing those would train the user to ignore the
     voice, which is the failure mode that matters.
  2. NOVELTY -- a fingerprint of each failure is persisted. A failure is spoken
     once. It is only ever spoken again if it CLEARS first and then comes back,
     which is genuinely new information. The scheduled audit rewrites the same
     two failures every week; without this gate that is a weekly nag forever.
  3. QUIET HOURS -- held, not dropped. A failure detected at 03:00 is spoken at
     the start of the day. Enforced in announce.py so timers can bypass it and
     proactive speech cannot.
  4. RATE LIMIT -- at most one push every 30 minutes and four a day, also in
     announce.py. This is the backstop for a flapping check that would
     otherwise produce a monologue.

Two failures appearing at once are coalesced into one sentence. Two separate
announcements 400 ms apart sound broken even when both are correct.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime

import status
from announce import Announcement, Announcer

log = logging.getLogger("capabilities.watcher")

DATA_DIR = os.getenv("CAPABILITIES_DATA", "/data")
SEEN_PATH = os.path.join(DATA_DIR, "seen_alerts.json")

POLL_S = int(os.getenv("WATCH_POLL_S", "300"))          # audit is weekly; 5 min is plenty
HELD_RETRY_S = int(os.getenv("WATCH_HELD_RETRY_S", "600"))


def fingerprint(body: str) -> str:
    """
    Stable identity for a failure line.

    Numbers are stripped before hashing on purpose. "738 originals, previous
    held 739" becomes a different string every time the backup runs, and
    without normalisation the same lost photo would be announced as new
    forever. Identity is the KIND of failure, not its current counters.
    """
    normalised = re.sub(r"\d+", "#", body.lower())
    normalised = re.sub(r"\s+", " ", normalised).strip()
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()[:12]


class AuditWatcher:
    def __init__(self, announcer: Announcer) -> None:
        self.announcer = announcer
        self.seen: dict[str, dict] = {}       # fingerprint -> {body, announced_at}
        self.held: list[str] = []             # bodies waiting out quiet hours
        self._task: asyncio.Task | None = None
        self.last_check: str | None = None
        self.checks = 0

    # --- Persistence ------------------------------------------------------
    def load(self) -> None:
        try:
            with open(SEEN_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            self.seen = data.get("seen", {})
            self.held = data.get("held", [])
            log.info("loaded %d known failure(s), %d held", len(self.seen), len(self.held))
        except FileNotFoundError:
            pass
        except (OSError, json.JSONDecodeError) as exc:
            log.error("seen-alerts state unreadable (%s); starting empty", exc)

    def save(self) -> None:
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            tmp = SEEN_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"seen": self.seen, "held": self.held}, fh, indent=2)
            os.replace(tmp, SEEN_PATH)
        except OSError as exc:
            log.error("could not persist seen alerts: %s", exc)

    # --- Priming ----------------------------------------------------------
    def prime(self, fails: list[str]) -> int:
        """
        Mark everything currently failing as already-known WITHOUT speaking.

        Run once on first boot. Otherwise the very first thing the assistant
        ever says, unprompted, is a recital of every pre-existing failure --
        which is exactly the behaviour that gets it muted on day one.
        """
        count = 0
        for body in fails:
            fp = fingerprint(body)
            if fp not in self.seen:
                self.seen[fp] = {"body": body, "announced_at": None, "primed": True}
                count += 1
        self.save()
        return count

    # --- Core -------------------------------------------------------------
    async def check_once(self, now: datetime | None = None) -> dict:
        """
        One pass. Returns what it decided and why, so silence is auditable.
        """
        now = now or datetime.now()
        self.checks += 1
        self.last_check = now.isoformat(timespec="seconds")

        result = await status.read_audit()
        fails = result["report"]["fails"]
        current = {fingerprint(b): b for b in fails}

        # Gate 2a: a failure that has cleared is forgotten, so that if it comes
        # back it is legitimately new again.
        cleared = [fp for fp in self.seen if fp not in current]
        for fp in cleared:
            self.seen.pop(fp, None)

        new = [body for fp, body in current.items() if fp not in self.seen]
        for fp, body in current.items():
            self.seen.setdefault(fp, {"body": body, "announced_at": None, "primed": False})

        to_say = self.held + [b for b in new if b not in self.held]
        if not to_say:
            self.save()
            return {"checked": True, "new": 0, "cleared": len(cleared), "spoken": False,
                    "reason": "nothing-new", "held": len(self.held)}

        text = self.compose(to_say)
        outcome = await self.announcer.speak(
            Announcement(text=text, kind="proactive"), now=now
        )

        if outcome["outcome"] == "spoken":
            for body in to_say:
                fp = fingerprint(body)
                if fp in self.seen:
                    self.seen[fp]["announced_at"] = now.isoformat(timespec="seconds")
            self.held = []
        else:
            # Gate 3: held, not dropped. Retried on the next pass.
            self.held = to_say
            log.info("holding %d failure(s): %s", len(to_say), outcome["reason"])

        self.save()
        return {
            "checked": True, "new": len(new), "cleared": len(cleared),
            "spoken": outcome["outcome"] == "spoken", "reason": outcome["reason"],
            "text": text, "held": len(self.held),
        }

    @staticmethod
    def compose(bodies: list[str]) -> str:
        """
        One sentence, whatever the count. Coalescing is not cosmetic: two
        announcements fired back to back overlap in the speaker and neither is
        intelligible.
        """
        spoken = [status._speakable(b) for b in bodies]
        if len(spoken) == 1:
            return f"Heads up. {spoken[0].capitalize()}."
        if len(spoken) == 2:
            return f"Heads up. {spoken[0].capitalize()}, and {spoken[1]}."
        return f"Heads up. {len(spoken)} problems on the server, starting with {spoken[0]}."

    # --- Loop -------------------------------------------------------------
    async def start(self, prime_on_first_run: bool = True) -> None:
        self.load()
        if prime_on_first_run and not self.seen and not os.path.exists(SEEN_PATH):
            result = await status.read_audit()
            primed = self.prime(result["report"]["fails"])
            log.info("first run: primed %d existing failure(s) silently", primed)
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("audit watch iteration failed")
            await asyncio.sleep(HELD_RETRY_S if self.held else POLL_S)
