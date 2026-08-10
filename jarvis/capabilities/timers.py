#!/usr/bin/env python3
"""
Persistent timers, reminders and alarms.

WHY THIS EXISTS RATHER THAN USING HOME ASSISTANT'S OWN TIMER INTENTS -- two
independent blockers, either one fatal:

  1. `homeassistant/components/intent/llm.py` gates `supports_timers` on a
     `device_id` being present in the LLM context. voice_server.py posts to
     /api/conversation/process with only text/language/conversation_id/agent_id,
     so there is no device and HA never even offers the seven timer intents to
     the model. The model cannot call a tool it was never shown.
  2. `homeassistant/components/intent/timers.py` keeps timers in memory and is
     not a RestoreEntity. Even with blocker 1 solved, every timer would die on
     the next container restart, and this stack restarts for unrelated reasons.

So: absolute epoch fire times on disk, reloaded on boot. Storing a remaining
duration instead would be the same bug as blocker 2 wearing a different hat --
a 20 minute timer written at 10:00 and reloaded at 10:15 must have 5 minutes
left, not 20.

Multiple concurrent timers are held in one file and one scheduler task. The
scheduler sleeps until the NEXT fire time rather than polling every second, and
is re-woken whenever the set changes, so an idle house costs nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime

import parsing
from announce import Announcement, Announcer

log = logging.getLogger("capabilities.timers")

DATA_DIR = os.getenv("CAPABILITIES_DATA", "/data")
STATE_PATH = os.path.join(DATA_DIR, "timers.json")

# A timer that should have fired while the service was down still fires, but
# only if it is recent. Waking someone at 09:00 for a 07:00 alarm is worse than
# staying quiet; a pasta timer 30 seconds late is still useful.
LATE_GRACE_S = int(os.getenv("TIMER_LATE_GRACE_S", "900"))


@dataclass
class Timer:
    id: str
    label: str
    fire_at: float                  # absolute epoch seconds -- survives restart
    created_at: float
    kind: str = "timer"             # timer | reminder | alarm
    spoken_for: str = ""            # how it was described when set
    clock: bool = False             # set from a clock time rather than a duration
    fired: bool = False

    @property
    def remaining(self) -> float:
        return self.fire_at - time.time()


# The model routinely forwards the user's whole phrase as the label, so the raw
# value is "remind me to call mum" rather than "call mum". Left alone that
# produces "I'll remind you for the remind me to call mum", which is the kind of
# sentence that makes the whole assistant sound broken.
_LABEL_PREFIXES = (
    "remind me to ", "remind me ", "reminder to ", "reminder ",
    "set a timer for ", "set a timer ", "timer for ", "timer ",
    "wake me up ", "wake me ", "alarm for ", "alarm ",
    "to ", "the ", "my ", "a ", "an ", "for ",
)


def clean_label(label: str) -> str:
    """Strip the command scaffolding a small model leaves on the label."""
    text = (label or "").strip().strip(".!?,").lower()
    changed = True
    while changed:
        changed = False
        for prefix in _LABEL_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                changed = True
                break
    # A label that was only scaffolding ("set a timer") leaves nothing useful.
    if text in ("", "timer", "alarm", "reminder", "up"):
        return ""
    return text


def classify(label: str, text: str, is_clock: bool) -> str:
    """
    Word choice when it fires. "Your alarm" and "your pasta timer" are both
    better than a generic beep, and the distinction is free at this point.
    """
    blob = f"{label} {text}".lower()
    if is_clock or any(w in blob for w in ("wake", "alarm", "get up")):
        return "alarm"
    if any(w in blob for w in ("remind", "reminder", "remember", "don't forget")):
        return "reminder"
    return "timer"


class TimerStore:
    def __init__(self, announcer: Announcer) -> None:
        self.announcer = announcer
        self.timers: dict[str, Timer] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    # --- Persistence ------------------------------------------------------
    def load(self) -> tuple[int, int]:
        """
        Restore from disk. Returns (restored, dropped).

        Called before the scheduler starts so a restart is transparent.
        """
        restored = dropped = 0
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return (0, 0)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("timer state unreadable (%s); starting empty", exc)
            return (0, 0)

        now = time.time()
        for item in raw.get("timers", []):
            try:
                timer = Timer(**item)
            except TypeError:
                dropped += 1
                continue
            if timer.fired:
                continue
            # Long overdue: it fired into a void while we were down. Drop it
            # rather than shouting about a timer from three hours ago.
            if timer.fire_at < now - LATE_GRACE_S:
                log.info("dropping stale timer %r (%.0fs overdue)", timer.label, now - timer.fire_at)
                dropped += 1
                continue
            self.timers[timer.id] = timer
            restored += 1

        log.info("restored %d timer(s), dropped %d", restored, dropped)
        return (restored, dropped)

    def save(self) -> None:
        """Atomic write -- a torn state file would lose every pending timer."""
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            payload = {
                "version": 1,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "timers": [asdict(t) for t in self.timers.values() if not t.fired],
            }
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, STATE_PATH)
        except OSError as exc:
            log.error("could not persist timers: %s", exc)

    # --- Scheduler --------------------------------------------------------
    async def start(self) -> None:
        self.load()
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
                pending = [t for t in self.timers.values() if not t.fired]
                if not pending:
                    self._wake.clear()
                    await self._wake.wait()
                    continue

                soonest = min(t.fire_at for t in pending)
                delay = max(0.0, soonest - time.time())
                if delay > 0:
                    self._wake.clear()
                    try:
                        # Wakes early if a timer is added or cancelled, so a new
                        # 10 second timer is not stuck behind an existing 1 hour
                        # one.
                        await asyncio.wait_for(self._wake.wait(), timeout=delay)
                        continue
                    except asyncio.TimeoutError:
                        pass

                now = time.time()
                due = [t for t in self.timers.values() if not t.fired and t.fire_at <= now + 0.25]
                for timer in due:
                    await self._fire(timer)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("timer scheduler iteration failed")
                await asyncio.sleep(1)

    async def _fire(self, timer: Timer) -> None:
        timer.fired = True
        self.timers.pop(timer.id, None)
        self.save()

        text = self.fire_sentence(timer)
        # urgent=True: the user personally asked for this at this time, so it
        # overrides quiet hours. An alarm that respects quiet hours is broken.
        await self.announcer.speak(
            Announcement(text=text, kind="timer", urgent=True)
        )

    @staticmethod
    def fire_sentence(timer: Timer) -> str:
        label = timer.label.strip()
        if timer.kind == "alarm":
            return f"Good morning. {label}." if label else "Good morning. Your alarm."
        if timer.kind == "reminder":
            return f"Reminder: {label}." if label else "This is your reminder."
        return f"Your {label} timer is done." if label else "Your timer is done."

    # --- Actions ----------------------------------------------------------
    def add(self, text: str, label: str) -> tuple[Timer, str]:
        now_dt = datetime.now()
        fire_dt, described = parsing.resolve(text, now_dt)
        is_clock = ":" in described

        timer = Timer(
            id=uuid.uuid4().hex[:8],
            label=clean_label(label),
            fire_at=fire_dt.timestamp(),
            created_at=time.time(),
            # classify() sees the RAW label, not the cleaned one: "remind me to"
            # is the very evidence that says this is a reminder, and cleaning
            # removes it.
            kind=classify(label, text, is_clock),
            spoken_for=described,
            clock=is_clock,
        )
        self.timers[timer.id] = timer
        self.save()
        self._wake.set()
        return timer, described

    def cancel(self, label: str) -> list[Timer]:
        """
        Cancel by label, or everything if no label was given.

        Matching is substring and case-insensitive on purpose: the user says
        "cancel the pasta timer" and the label stored was "pasta".
        """
        needle = clean_label(label)
        if not needle or needle in ("all", "everything", "every timer"):
            killed = list(self.timers.values())
            self.timers.clear()
        else:
            killed = [
                t for t in self.timers.values()
                # `t.label and` is load-bearing: an unlabelled timer has label
                # "", and "" is a substring of every string, so without it
                # "cancel the pasta timer" silently killed every anonymous
                # timer in the house as well.
                if needle in t.label.lower() or (t.label and t.label.lower() in needle)
            ]
            for t in killed:
                self.timers.pop(t.id, None)
        if killed:
            self.save()
            self._wake.set()
        return killed

    def active(self) -> list[Timer]:
        return sorted(
            (t for t in self.timers.values() if not t.fired),
            key=lambda t: t.fire_at,
        )

    # --- Spoken replies ---------------------------------------------------
    # Composed here, in Python, and handed to the model to read out verbatim.
    # A 3B model asked to summarise a list of timers produces a paragraph; a
    # 12-word sentence is the whole requirement.
    def reply_set(self, timer: Timer, described: str) -> str:
        preposition = "at" if timer.clock else "in"
        if timer.kind == "alarm":
            return f"Alarm set for {described}."
        if timer.kind == "reminder":
            if timer.label:
                return f"I'll remind you to {timer.label} {preposition} {described}."
            return f"Reminder set for {described}."
        if timer.label:
            return f"{timer.label.capitalize()} timer set for {described}."
        return f"Timer set for {described}."

    def reply_cancel(self, killed: list[Timer], label: str) -> str:
        if not killed:
            if self.active():
                return "I couldn't find that one. Ask me to list your timers."
            return "You have no timers running."
        if len(killed) == 1:
            name = killed[0].label
            return f"Cancelled your {name} timer." if name else "Timer cancelled."
        return f"Cancelled {len(killed)} timers."

    def reply_list(self) -> str:
        active = self.active()
        if not active:
            return "Nothing running."
        first = active[0]
        left = parsing.remaining_phrase(first.remaining)
        name = f"{first.label} " if first.label else ""
        if len(active) == 1:
            return f"One timer: {name}{left} left."
        return f"{len(active)} timers. Next is {name}in {left}."
