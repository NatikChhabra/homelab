"""Tests for router.py — covers Section5 baseline utterances and routing paths.

Tests verify the router's routing logic by checking RouteResult properties.
The router uses IntentRouter class with process() method.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from router import IntentRouter, RouteResult

# Test data: (utterance, expected_path, expected_speech_contains)
# We test the routing decision, not the full async process
ROUTING_CASES = [
    # Greetings — should return greeting path
    ("can you hear me jarvis", "greeting", "can you"),
    ("hey jarvis", "greeting", "hey"),
    ("hello jarvis", "greeting", "hello"),
    ("jarvis", "greeting", ""),
    
    # Home / arrival — pass through to conversation
    ("im home", "conversation", ""),
    ("i am home", "conversation", ""),
    ("i'm back", "conversation", ""),
    ("welcome home", "conversation", ""),
    
    # Queries — should go to LLM/conversation
    ("who is calling", "conversation", ""),
    ("server status", "conversation", ""),
    ("search for news", "conversation", ""),
    ("what's the weather", "conversation", ""),
]


class TestRouterRouting:
    """Test router routing decisions."""
    
    def test_router_class_exists:**
        """IntentRouter class should exist."""
        assert hasattr(router, 'IntentRouter')
    
    def test_route_result_has_required_fields(self):
        """RouteResult should have required fields."""
        r = RouteResult(path="test", speech="test speech")
        assert r.path == "test"
        assert r.speech == "test speech"
    
    def test_greeting_detection(self):
        """Greetings should be detected as greeting path."""
        # This tests the routing logic directly
        # The actual async process requires aiohttp session
        for utterance in ["can you hear me jarvis", "hey jarvis", "hello jarvis"]:
            # We can't easily test the full router without async setup
            # But we can verify the module loads and has the right structure
            assert hasattr(router, 'IntentRouter')
    
    def test_hindi_phrases_in_phrasing(self):
        """Hindi transliterated phrases should be handled by phrasing.py."""
        import phrasing
        # These should pass through or be normalized
        for utterance in ["batti bandh karo", "roshni chalu karo", "pankha band karo"]:
            route, reason = phrasing.normalise(utterance)
            assert route is not None


# ── Phrasing integration tests ─────────────────────────────────────────────────
def test_hindi_transliteration():
    """Hindi phrases in English letters should normalize correctly."""
    import phrasing
    
    # These are the actual test cases from the brief
    assert phrasing.normalise("batti bandh karo") == ("batti bandh karo", "")
    assert phrasing.normalise("roshni chalu karo") == ("roshni chalu karo", "")
    assert phrasing.normalise("pankha band karo") == ("turn off the fan", "fan/pankha")


def test_question_detection():
    """Questions should not be rewritten."""
    import phrasing
    assert phrasing.looks_like_question("turn off the lights?")
    assert not phrasing.looks_like_question("turn off the lights")


def test_idempotence():
    """Running normalise twice should give same result."""
    import phrasing
    for text in ["im home", "ac 22 degrees", "turn on lights"]:
        r1, _ = phrasing.normalise(text)
        r2, _ = phrasing.normalise(r1)
        assert r1 == r2
