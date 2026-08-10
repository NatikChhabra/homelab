#!/usr/bin/env python3
"""
Turn what a 3B model passes as `duration_or_time` into an absolute fire time.

WHY THIS IS ITS OWN MODULE AND IS AGGRESSIVE ABOUT WORD FORMS: the argument
reaching us has been through Whisper and then through a small LLM. Whisper
writes "ten minutes", not "10 minutes". The model frequently forwards the user's
words verbatim rather than normalising them, and sometimes forwards the whole
phrase ("in ten minutes", "for 20 minutes", "at 7 am"). Every one of those has
to land on the same answer or the feature is a coin flip.

The one genuine ambiguity is a bare number. "Seven" could be seven minutes or
seven o'clock. This module resolves bare numbers as MINUTES and the caller
always speaks its interpretation back ("Timer set for seven minutes"), so a
misread is corrected in one turn instead of silently sleeping through an alarm.
A clock time therefore needs a colon, an am/pm, or an explicit "at"/"o'clock".
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60,
    "ninety": 90,
    # Articles carry a real value in speech: "an hour" is one hour.
    "a": 1, "an": 1,
}

# Words that may follow a tens word to form a compound ("twenty five" -> 25).
# "a" and "an" are excluded even though they map to 1: without that exclusion
# "half an hour" becomes "30" + "an"=1 -> 31 hours, which is exactly the bug
# this set exists to prevent.
COMPOUND_UNITS = {
    w for w, v in WORD_NUMBERS.items() if 1 <= v <= 9 and w not in ("a", "an")
}

# Fixed idioms, substituted before any word-to-digit work. These do not
# decompose into "number + unit" and every attempt to make the general parser
# handle them produced a worse general parser.
IDIOMS: list[tuple[str, str]] = [
    (r"\ban?\s+hour\s+and\s+a\s+half\b", "90 minutes"),
    (r"\bone\s+and\s+a\s+half\s+hours?\b", "90 minutes"),
    (r"\bthree\s+quarters\s+of\s+an\s+hour\b", "45 minutes"),
    (r"\b(?:a\s+)?quarter\s+(?:of\s+)?(?:an\s+)?hour\b", "15 minutes"),
    (r"\b(?:a\s+)?half\s+(?:an\s+)?hour\b", "30 minutes"),
    (r"\bhalf\s+a\s+minute\b", "30 seconds"),
    (r"\ba\s+couple\s+of\b", "2"),
    (r"\ba\s+few\b", "3"),
    # Clock idioms, so "half past seven" stays a clock time instead of falling
    # through to the duration parser and being rejected.
    (r"\bhalf\s+past\s+(\w+)\b", r"\1:30"),
    (r"\b(?:a\s+)?quarter\s+past\s+(\w+)\b", r"\1:15"),
]

UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}

_FILLER = re.compile(
    r"\b(in|for|after|about|around|roughly|please|me|the|and|of|to|set|timer|alarm)\b"
)


class ParseError(ValueError):
    """Raised when the text carries no usable duration or time."""


def _apply_idioms(text: str) -> str:
    for pattern, replacement in IDIOMS:
        text = re.sub(pattern, replacement, text)
    return text


def _words_to_digits(text: str) -> str:
    """
    Replace spelled-out numbers with digits, handling "twenty five" as 25.

    Compound handling matters: Whisper transcribes "twenty five minutes" as
    three words, and treating them independently yields "20 5 minutes", which
    then parses as twenty seconds plus five minutes -- wrong in a way the user
    only discovers when the timer fires late.
    """
    tokens = text.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        raw = tokens[i]
        word = raw.strip(".,!?").lower()
        if word in WORD_NUMBERS:
            value = WORD_NUMBERS[word]
            # tens + units, e.g. "twenty five" -> 25
            if value in (20, 30, 40, 50, 60, 90) and i + 1 < len(tokens):
                nxt = tokens[i + 1].strip(".,!?").lower()
                if nxt in COMPOUND_UNITS:
                    out.append(str(value + WORD_NUMBERS[nxt]))
                    i += 2
                    continue
            out.append(str(value))
        else:
            out.append(raw)
        i += 1
    return " ".join(out)


def normalise(text: str) -> str:
    """Lowercase, expand idioms, then digitise words. One shared entry point so
    the clock parser and the duration parser can never disagree about the
    input they are looking at."""
    return _words_to_digits(_apply_idioms(text.lower().strip()))


def parse_duration(text: str) -> int | None:
    """Seconds, or None if the text does not look like a duration."""
    t = normalise(text)

    total = 0
    found = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", t):
        unit = unit.rstrip(".")
        if unit in UNIT_SECONDS:
            total += int(round(float(value) * UNIT_SECONDS[unit]))
            found = True
    if found:
        return total

    # Bare number, no unit: minutes. Spoken back to the user for confirmation.
    bare = re.fullmatch(r"\s*(\d+)\s*", _FILLER.sub(" ", t))
    if bare:
        return int(bare.group(1)) * 60
    return None


def parse_clock(text: str, now: datetime) -> datetime | None:
    """
    Next occurrence of a clock time, or None if the text is not a clock time.

    Always resolves forward: "wake me at seven" said at 08:00 means tomorrow
    07:00, never a time that has already passed.
    """
    t = normalise(text)
    tomorrow = "tomorrow" in t
    explicit = bool(re.search(r"\b(at|o'?clock)\b", t)) or tomorrow

    # A unit word anywhere means this is a duration, not a clock time. Without
    # this guard "30 minutes" matches the hour/minute regex below as 30:00 ->
    # rejected -> and the whole phrase is lost.
    if re.search(r"\d+\s*(" + "|".join(sorted(UNIT_SECONDS, key=len, reverse=True)) + r")\b", t):
        return None

    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", t)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").replace(".", "")

    has_colon = m.group(2) is not None
    if not meridiem and not has_colon and not explicit:
        return None  # bare number -> caller treats it as a duration
    if hour > 23 or minute > 59:
        return None

    if meridiem == "pm" and hour < 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    elif not meridiem and hour <= 7 and not has_colon:
        # "wake me at six" almost never means 06:00 the way "at 6" is spoken in
        # the evening... but it almost always does for an alarm. Leave morning
        # hours as morning; the next-occurrence rule below pushes them to
        # tomorrow if they have already passed today.
        pass

    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if tomorrow or target <= now:
        target += timedelta(days=1)
    return target


def resolve(text: str, now: datetime) -> tuple[datetime, str]:
    """
    (fire_time, human_description). Raises ParseError if nothing is usable.

    Clock is tried first because "at 7:30" also contains digits a duration
    parser would happily eat as "7 seconds and 30 of something".
    """
    text = (text or "").strip()
    if not text:
        raise ParseError("no duration or time given")

    clock = parse_clock(text, now)
    if clock is not None:
        return clock, describe_clock(clock, now)

    seconds = parse_duration(text)
    if seconds is None or seconds <= 0:
        raise ParseError(f"could not read a duration or time from {text!r}")
    if seconds > 7 * 86400:
        raise ParseError("that is longer than a week")
    return now + timedelta(seconds=seconds), describe_duration(seconds)


def describe_duration(seconds: int) -> str:
    """Spoken form. Speech caps near 12 words, so never more than two units."""
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, rem_s = divmod(seconds, 60)
    hours, rem_m = divmod(minutes, 60)
    if hours:
        if rem_m:
            return f"{hours} hour{'s' if hours != 1 else ''} {rem_m} minute{'s' if rem_m != 1 else ''}"
        return f"{hours} hour{'s' if hours != 1 else ''}"
    if rem_s and minutes < 5:
        return f"{minutes} minute{'s' if minutes != 1 else ''} {rem_s} second{'s' if rem_s != 1 else ''}"
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def describe_clock(target: datetime, now: datetime) -> str:
    # %-I is a glibc extension and this module is unit-tested on the Windows
    # host as well as in the Linux container, so strip the pad zero by hand.
    when = target.strftime("%I:%M %p").lstrip("0").lower()
    day = "tomorrow" if target.date() != now.date() else ""
    return (when + (" " + day if day else "")).strip()


def remaining_phrase(seconds: float) -> str:
    """Short 'time left' phrase for the list reply."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = (seconds + 30) // 60
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes / 60
    return f"{hours:.1f} hours".replace(".0 ", " ")
