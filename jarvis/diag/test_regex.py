"""
Pin the light-command regex contract in the PRODUCTION router.

Why this imports instead of copying
-----------------------------------
The first version of this file pasted its own copy of _ALL_LIGHTS_RE. That is
the CLAUDE.md rule-4 trap: it passed while jarvis/router.py line 671 crashed,
because the copy used `(g1 or g2)` and production used `g1` alone. A test that
holds its own copy of the thing under test proves nothing about production.

It also imports from jarvis/router.py specifically, NOT aether/kernel/router.py.
Those are two different modules with different group numbering, and the
actuation battery was pointed at the aether one while the container ran this
one. Only this module is baked into jarvis-voice as /app/router.py.

What it pins
------------
Line 671 does `(m.group(1) or m.group(2)).lower()`. That is only correct while
the regex has exactly two action groups and exactly one of them is set per
match. This asserts that invariant for every supported phrasing, so widening
the regex again without revisiting line 671 fails here instead of at runtime.

Run: python jarvis/diag/test_regex.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from router import _ALL_LIGHTS_RE, _INDIVIDUAL_LIGHT_RE, _LIGHT_GANG  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("PASS  " if cond else "FAIL  ") + msg)
    if not cond:
        FAILS.append(msg)


# (utterance, expected action) — both word orders the regex must accept.
ALL_LIGHTS_CASES = [
    ("turn on the lights", "on"),
    ("turn off the lights", "off"),
    ("turn on lights", "on"),
    ("turn off lights", "off"),
    ("on the lights", "on"),
    ("off the lights", "off"),
    ("lights on", "on"),
    ("lights off", "off"),
    ("light on", "on"),
    ("light off", "off"),
]


def test_all_lights_action_extraction() -> None:
    """Every phrasing must match and yield an action via the line-671 idiom."""
    for utt, want in ALL_LIGHTS_CASES:
        m = _ALL_LIGHTS_RE.match(utt)
        check(m is not None, f"_ALL_LIGHTS_RE matches {utt!r}")
        if m is None:
            continue

        # This is verbatim the expression on router.py line 671.
        got = (m.group(1) or m.group(2))
        check(got is not None,
              f"{utt!r} yields a non-None action group (line-671 idiom)")
        if got is not None:
            check(got.lower() == want,
                  f"{utt!r} -> action {got.lower()!r} (expect {want!r})")


def test_exactly_one_action_group_is_set() -> None:
    """The `or` idiom is only sound if the alternations are mutually exclusive.

    If a future edit adds a third action group or lets both fire, `or` would
    silently pick the wrong one. Pin it now.
    """
    check(_ALL_LIGHTS_RE.groups == 2,
          f"_ALL_LIGHTS_RE has exactly 2 groups (got {_ALL_LIGHTS_RE.groups})")
    for utt, _ in ALL_LIGHTS_CASES:
        m = _ALL_LIGHTS_RE.match(utt)
        if m is None:
            continue
        set_groups = [g for g in (m.group(1), m.group(2)) if g is not None]
        check(len(set_groups) == 1,
              f"{utt!r} sets exactly one action group (got {len(set_groups)})")


def test_bare_group1_would_crash_on_reversed_order() -> None:
    """Regression pin for the actual bug.

    'lights on' matches the second alternation, so group(1) is None and the old
    `m.group(1).lower()` raised AttributeError. Assert group(1) really is None
    there, so this file documents *why* line 671 needs the `or`.
    """
    m = _ALL_LIGHTS_RE.match("lights on")
    check(m is not None, "'lights on' matches at all")
    if m is not None:
        check(m.group(1) is None,
              "'lights on' leaves group(1) None — bare .group(1).lower() would crash")
        check(m.group(2) == "on", "'lights on' puts the action in group(2)")


def test_individual_light_groups_are_action_then_gang() -> None:
    """Line 638 uses group(1)=action, group(2)=gang. Pin that ordering.

    The alternation fix was once misapplied here, where group(2) is a gang
    name, not an action. If the groups ever swap, this catches it.
    """
    for utt, want_action, want_gang in [
        ("turn on light one", "on", "one"),
        ("turn off light two", "off", "two"),
        ("on gang 3", "on", "3"),  # 'gang ' is a non-capturing prefix; group2='3'
    ]:
        m = _INDIVIDUAL_LIGHT_RE.match(utt)
        check(m is not None, f"_INDIVIDUAL_LIGHT_RE matches {utt!r}")
        if m is None:
            continue
        check(m.group(1) is not None and m.group(1).lower() == want_action,
              f"{utt!r} action group(1)={m.group(1)!r} (expect {want_action!r})")
        gang = m.group(2).lower().replace(" ", "")
        check(gang == want_gang, f"{utt!r} gang group(2)={gang!r} (expect {want_gang!r})")
        check(gang in _LIGHT_GANG, f"{utt!r} gang {gang!r} resolves to an entity id")


def test_all_lights_does_not_swallow_individual() -> None:
    """'turn on light one' must NOT be caught by the all-lights regex.

    The individual fast-path runs first, but if _ALL_LIGHTS_RE also matched,
    reordering the two blocks would silently turn on all three switches.
    """
    for utt in ("turn on light one", "turn off light two", "on gang 3"):
        check(_ALL_LIGHTS_RE.match(utt) is None,
              f"_ALL_LIGHTS_RE does NOT match individual command {utt!r}")


if __name__ == "__main__":
    test_all_lights_action_extraction()
    test_exactly_one_action_group_is_set()
    test_bare_group1_would_crash_on_reversed_order()
    test_individual_light_groups_are_action_then_gang()
    test_all_lights_does_not_swallow_individual()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES:")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("ALL LIGHT-REGEX CONTRACT TESTS PASS")
    sys.exit(0)
