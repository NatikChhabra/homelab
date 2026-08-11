"""Tests for phrasing.py normalise() — covers the Section5 baseline and edge cases."""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
import phrasing

# (input, expected_route, expected_reason_contains)
CASES = [
    # Greetings — pass through, handled by router fast-path
    ("can you hear me jarvis", "can you hear me jarvis", ""),
    ("hey jarvis", "hey jarvis", ""),
    ("hello jarvis", "hello jarvis", ""),
    # Home / arrival
    ("im home", "I am home", "im home"),
    ("i am home", "I am home", "im home"),
    ("i'm home", "I am home", "im home"),
    ("im back", "I am home", "im back"),
    ("i'm back", "I am home", "im back"),
    ("welcome home", "I am home", "welcome home"),
    # AC
    ("ac 22 degrees", "set the ac to 22", "ac N degrees"),
    ("ac22 degrees", "set the ac to 22", "ac N degrees"),
    ("set the ac to 22", "set the ac to 22", ""),
    # Fans
    ("fan speed 3", "set the fan speed to 60", "fan speed"),
    ("fan boost", "fan boost", ""),
    ("boost the fan", "boost the fan", ""),  # no rule — HA handles it
    # Scenes
    ("movie time", "movie time", ""),
    ("start movie time", "movie time", "movie time"),
    # Lights
    ("turn on lights", "turn on the lights in the bedroom", "turn on the lights"),
    ("lights on", "turn on the lights in the bedroom", "lights on"),
    ("turn off lights", "turn off the lights in the bedroom", "turn off the lights"),
    ("lights off", "turn off the lights in the bedroom", "lights off"),
    # Hindi — these pass through; HA handles them directly (verified in benchmark)
    ("batti bandh karo", "batti bandh karo", ""),
    ("roshni chalu karo", "roshni chalu karo", ""),
    ("pankha band karo", "turn off the fan", "fan/pankha"),
    # Good night
    ("good night", "good night", ""),
    ("goodnight", "good night", "Good night"),
    # Pass-through (no rule matches)
    ("who is calling", "who is calling", ""),
    ("server status", "server status", ""),
    ("search for news", "search for news", ""),
]


@pytest.mark.parametrize("inp,expected_route,reason_contains", CASES)
def test_normalise(inp, expected_route, reason_contains):
    route, reason = phrasing.normalise(inp)
    assert route == expected_route, f"{inp!r}: got route={route!r}, expected {expected_route!r}"
    if reason_contains:
        assert reason_contains in reason, f"{inp!r}: reason={reason!r} should contain {reason_contains!r}"


def test_idempotent():
    """Running normalise twice should give the same result."""
    for inp in ["im home", "ac 22 degrees", "turn on lights", "good night"]:
        r1, _ = phrasing.normalise(inp)
        r2, _ = phrasing.normalise(r1)
        assert r1 == r2, f"idempotent failed for {inp!r}: {r1!r} != {r2!r}"


def test_question_untouched():
    """Trailing ? should prevent any rewrite."""
    assert phrasing.normalise("tv off?") == ("tv off?", "")
    assert phrasing.normalise("is the light on?") == ("is the light on?", "")
