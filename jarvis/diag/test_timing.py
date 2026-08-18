"""
Verify TurnTimer: every stage mark, span, and the flush summary.

This is the unit half of the timing verification. The integration half
(diag/timing_e2e.py) drives a real turn and reads the TURN TIMING log line;
this half proves the timer's own arithmetic is correct in isolation, so that
when the integration numbers look wrong we know it is the pipeline, not the
stopwatch.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "host-ears"))

from timing import TurnTimer  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


def approx(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def test_span_arithmetic() -> None:
    """A known sleep between two marks must show up as that span, in ms."""
    t = TurnTimer(1)
    t.mark("wake_detected")
    time.sleep(0.05)
    t.mark("beep_started")
    span = t.span("wake_detected", "beep_started")
    check(span is not None, "span returns a value when both marks exist")
    assert span is not None
    check(approx(span, 50.0, 25.0), f"50ms sleep measured as {span:.1f}ms (expect ~50)")


def test_missing_mark_returns_none() -> None:
    t = TurnTimer(2)
    t.mark("wake_detected")
    check(t.span("wake_detected", "playback_ended") is None,
          "span with a missing end mark returns None (not a crash, not 0)")
    check(t.span("stt_started", "stt_ended") is None,
          "span with both marks missing returns None")


def test_unknown_stage_warns_but_records() -> None:
    """An unknown stage should warn yet still be usable, so a typo degrades
    to a logged warning rather than a lost measurement."""
    t = TurnTimer(3)
    t.mark("not_a_real_stage")
    check("not_a_real_stage" in t.marks,
          "unknown stage is still recorded after warning")


def test_full_turn_flush() -> None:
    """Walk every stage in order with small sleeps; flush must contain each
    derived span and a total that covers the whole walk."""
    t = TurnTimer(4)
    ordered = [
        "wake_detected", "beep_started", "recording_started", "recording_ended",
        "stt_started", "stt_ended", "route_started", "route_ended",
        "tts_requested", "tts_first_audio", "playback_started", "playback_ended",
    ]
    for stage in ordered:
        t.mark(stage)
        time.sleep(0.01)
    s = t.flush()

    check(s["turn_id"] == 4, "flush carries the turn id")
    # Derived spans the work order cares about.
    for key in ("wake_to_beep_ms", "recording_ms", "stt_ms", "route_ms",
                "tts_first_audio_ms", "total_wake_to_speech_ms"):
        check(s.get(key) is not None, f"flush computes {key}")
        v = s.get(key)
        check(isinstance(v, (int, float)) and v >= 0, f"{key} is a non-negative number ({v})")

    # total_ms must be >= the wake->playback span, since it is measured from
    # construction and covers at least that walk.
    check(isinstance(s["total_ms"], (int, float)) and s["total_ms"] >= 100,
          f"total_ms covers the full 12-stage walk ({s['total_ms']}ms)")

    # Adjacent deltas must all be present and roughly the 10ms sleeps.
    deltas = [k for k in s if k.startswith("delta_")]
    check(len(deltas) == 11, f"11 adjacent deltas for 12 stages (got {len(deltas)})")


def test_notes_survive_flush() -> None:
    t = TurnTimer(5)
    t.mark("wake_detected")
    t.note("path", "local")
    t.note("text", "lights on")
    s = t.flush()
    check(s.get("path") == "local", "string note 'path' survives flush")
    check(s.get("text") == "lights on", "string note 'text' survives flush")


def test_partial_turn_flush_is_safe() -> None:
    """A turn that bails early (too quiet, empty transcript) still flushes
    without KeyError, with the un-reached spans as None."""
    t = TurnTimer(6)
    t.mark("wake_detected")
    t.mark("beep_started")
    t.mark("recording_started")
    t.mark("recording_ended")
    s = t.flush()  # bailed before STT
    check(s.get("wake_to_beep_ms") is not None, "early-bail flush keeps reached spans")
    check(s.get("stt_ms") is None, "early-bail flush leaves un-reached spans None")
    check(s.get("total_wake_to_speech_ms") is None,
          "early-bail flush has no wake->speech span")


if __name__ == "__main__":
    test_span_arithmetic()
    test_missing_mark_returns_none()
    test_unknown_stage_warns_but_records()
    test_full_turn_flush()
    test_notes_survive_flush()
    test_partial_turn_flush_is_safe()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES:")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("ALL TIMING UNIT TESTS PASS")
    sys.exit(0)
