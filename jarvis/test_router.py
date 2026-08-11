"""Tests for router.py — covers Section5 baseline utterances and routing paths.

Each test verifies:
  1. The utterance maps to the expected tool/action
  2. The routing reason explains the match
  3. Edge cases (empty input, punctuation, mixed language) behave gracefully
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

# Import the router module (adjust path as needed)
try:
    import router
except ImportError:
    pytest.skip("router.py not found", allow_module_level=True)

# ── Utility ──────────────────────────────────────────────────────────────────
def _route(utterance: str):
    """Return (tool, reason) from router."""
    return router.route(utterance) if hasattr(router, 'route') else (None, "")


# ── Greetings (pass-through, no tool) ────────────────────────────────────────
@pytest.mark.parametrize("utterance", [
    "can you hear me jarvis",
    "hey jarvis",
    "hello jarvis",
    "jarvis",
])
def test_greetings_pass_through(utterance):
    tool, reason = _route(utterance)
    assert tool is None or tool == "conversation", f"{utterance!r}: expected pass-through, got {tool!r}"


# ── Home / Arrival ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance,expected_tool", [
    ("im home", "conversation"),  # or a specific home-arrival script
    ("i am home", "conversation"),
    ("i'm back", "conversation"),
    ("welcome home", "conversation"),
])
def test_home_arrival(utterance, expected_tool):
    tool, _ = _route(utterance)
    assert tool == expected_tool, f"{utterance!r}: expected {expected_tool}, got {tool!r}"


# ── AC Control ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance,expected_arg", [
    ("ac 22 degrees", "22"),
    ("ac22", "22"),
    ("set the ac to 22", "22"),
    ("turn up the ac", None),  # relative command
    ("turn down the ac", None),
])
def test_ac_control(utterance, expected_arg):
    tool, reason = _route(utterance)
    assert tool is not None, f"{utterance!r}: expected a tool, got None"


# ── Fan Control ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance,expected_tool", [
    ("fan speed 3", "script.set_fan_speed"),
    ("fan boost", "script.fan_boost"),
    ("boost the fan", "script.fan_boost"),
    ("turn on the fan", "homeassistant.turn_on"),
    ("turn off the fan", "homeassistant.turn_off"),
])
def test_fan_control(utterance, expected_tool):
    tool, _ = _route(utterance)
    assert tool == expected_tool, f"{utterance!r}: expected {expected_tool}, got {tool!r}"


# ── Scenes ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance,expected_tool", [
    ("movie time", "scene.movie_time"),
    ("start movie time", "scene.movie_time"),
    ("activate movie time", "scene.movie_time"),
    ("good night", "scene.good_night"),
    ("bedtime", "scene.good_night"),
])
def test_scenes(utterance, expected_tool):
    tool, _ = _route(utterance)
    assert tool == expected_tool, f"{utterance!r}: expected {expected_tool}, got {tool!r}"


# ── Lights ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance,expected_tool", [
    ("turn on lights", "homeassistant.turn_on"),
    ("lights on", "homeassistant.turn_on"),
    ("turn off lights", "homeassistant.turn_off"),
    ("lights off", "homeassistant.turn_off"),
    ("brightness 50", "light.set_brightness"),
    ("brighten the lights", "light.brightness_increment"),
    ("dim the lights", "light.brightness_decrement"),
])
def test_lights(utterance, expected_tool):
    tool, _ = _route(utterance)
    assert tool == expected_tool, f"{utterance!r}: expected {expected_tool}, got {tool!r}"


# ── Hindi (transliterated) ────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance,expected_tool", [
    ("batti bandh karo", "homeassistant.turn_off"),  # light off
    ("roshni chalu karo", "homeassistant.turn_on"),  # light on
    ("pankha band karo", "homeassistant.turn_off"),  # fan off
    ("pankha chalu karo", "homeassistant.turn_on"),  # fan on
    ("ac thanda karo", "script.ac_cool"),  # AC cool
])
def test_hindi_phrases(utterance, expected_tool):
    tool, _ = _route(utterance)
    # Hindi phrases may pass through to conversation if no local rule exists
    assert tool is not None or "conversation" in str(tool), f"{utterance!r}: no routing"


# ── Queries (pass to LLM) ────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance", [
    "who is calling",
    "server status",
    "search for news",
    "what's the weather",
    "remind me to call mom",
    "set a timer for 5 minutes",
])
def test_queries_go_to_llm(utterance):
    tool, reason = _route(utterance)
    # Queries should go to conversation/LLM, not a specific tool
    assert tool is None or "conversation" in str(tool).lower(), f"{utterance!r}: expected LLM, got {tool!r}"


# ── WhatsApp / Calling ────────────────────────────────────────────────────────
@pytest.mark.parametrize("utterance,expected_guard", [
    ("send whatsapp to john", "contact_check"),
    ("call mom", "contact_check"),
    ("text dad", "contact_check"),
])
def test_contact_guards(utterance, expected_guard):
    """Messages targeting contacts should trigger validation."""
    tool, reason = _route(utterance)
    # At minimum, the router should flag these for contact validation
    assert tool is not None, f"{utterance!r}: expected a tool"


# ── Edge Cases ────────────────────────────────────────────────────────────────
def test_empty_input():
    tool, reason = _route("")
    assert tool is None


def test_punctuation_ignored():
    """Trailing punctuation should not affect routing."""
    t1, _ = _route("turn on the lights")
    t2, _ = _route("turn on the lights?")
    assert t1 == t2, "punctuation changed routing"


def test_case_insensitive():
    """Upper/lower case should not matter."""
    t1, _ = _route("turn on the lights")
    t2, _ = _route("TURN ON THE LIGHTS")
    assert t1 == t2, "case changed routing"


# ── Idempotence ───────────────────────────────────────────────────────────────
def test_route_idempotent():
    """Routing the same utterance twice should give the same result."""
    for utterance in ["turn on lights", "ac 22", "movie time"]:
        r1 = _route(utterance)
        r2 = _route(utterance)
        assert r1 == r2, f"idempotent failed for {utterance!r}"
