#!/usr/bin/env python3
"""
Unit tests for the parsing that a 3B model's arguments have to survive.

Run:  python test_parsing.py

These are the cases that actually reach the service, taken from how Whisper
transcribes speech ("ten minutes", not "10 minutes") and how a small model
forwards it (often the user's whole phrase, unnormalised).
"""
from __future__ import annotations

import sys
from datetime import datetime

import parsing

NOW = datetime(2026, 8, 8, 13, 30, 0)   # Saturday afternoon, fixed

DURATION_CASES = [
    ("10 minutes", 600),
    ("ten minutes", 600),
    ("in ten minutes", 600),
    ("for 20 minutes", 1200),
    ("5 min", 300),
    ("45m", 2700),
    ("90 seconds", 90),
    ("thirty seconds", 30),
    ("1 hour", 3600),
    ("an hour", 3600),
    ("2 hours", 7200),
    ("1 hour 30 minutes", 5400),
    ("half an hour", 1800),
    ("a quarter of an hour", 900),
    ("twenty five minutes", 1500),
    ("1.5 hours", 5400),
    ("3", 180),                 # bare number -> minutes, spoken back for confirmation
    ("two", 120),
]

CLOCK_CASES = [
    ("7 am", (2026, 8, 9, 7, 0)),        # already past 07:00 today -> tomorrow
    ("7:30", (2026, 8, 9, 7, 30)),
    ("19:45", (2026, 8, 8, 19, 45)),
    ("6 pm", (2026, 8, 8, 18, 0)),
    ("at 6:15 pm", (2026, 8, 8, 18, 15)),
    ("tomorrow at 7", (2026, 8, 9, 7, 0)),
    ("at seven", (2026, 8, 9, 7, 0)),
    ("9:00 pm", (2026, 8, 8, 21, 0)),
    ("12 am", (2026, 8, 9, 0, 0)),
]

REJECT_CASES = ["", "   ", "banana", "the pasta"]

failures = []


def check(name, got, want):
    if got != want:
        failures.append(f"{name}: got {got!r}, want {want!r}")
        print(f"  FAIL  {name}: got {got!r}, want {want!r}")
    else:
        print(f"  OK    {name} -> {got!r}")


print("== durations ==")
for text, want in DURATION_CASES:
    fire, _ = parsing.resolve(text, NOW)
    check(text, int(round((fire - NOW).total_seconds())), want)

print("\n== clock times ==")
for text, want in CLOCK_CASES:
    fire, _ = parsing.resolve(text, NOW)
    check(text, (fire.year, fire.month, fire.day, fire.hour, fire.minute), want)

print("\n== rejections ==")
for text in REJECT_CASES:
    try:
        parsing.resolve(text, NOW)
        failures.append(f"{text!r} should have been rejected")
        print(f"  FAIL  {text!r} was accepted")
    except parsing.ParseError:
        print(f"  OK    {text!r} rejected")

print("\n== spoken descriptions ==")
for seconds, want in [(30, "30 seconds"), (60, "1 minute"), (600, "10 minutes"),
                      (3600, "1 hour"), (5400, "1 hour 30 minutes"), (90, "1 minute 30 seconds")]:
    check(f"describe({seconds})", parsing.describe_duration(seconds), want)

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    sys.exit(1)
print("all parsing tests passed")
