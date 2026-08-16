"""
test_policy.py — tests for the Aether policy engine.
"""
from __future__ import annotations

import pytest
import tempfile
from pathlib import Path

from kernel.policy import PolicyEngine, Rule


class TestRuleMatches:
    """Rule matching logic."""

    def test_exact_capability_match(self):
        r = Rule(capability="wa_send", target_device="any", source_device=None, verdict="deny")
        assert r.matches("wa_send", "any", None) is True
        assert r.matches("light_on", "any", None) is False

    def test_glob_target_device(self):
        r = Rule(capability="ha_turn_on", target_device="light.*", source_device=None, verdict="allow")
        assert r.matches("ha_turn_on", "light.bedroom", None) is True
        assert r.matches("ha_turn_on", "switch.living", None) is False

    def test_source_device_filter(self):
        r = Rule(capability="wa_send", target_device="any", source_device="a3f1c902", verdict="allow")
        assert r.matches("wa_send", "any", "a3f1c902") is True
        assert r.matches("wa_send", "any", "deadbeef") is False
        assert r.matches("wa_send", "any", None) is False

    def test_any_target_device(self):
        r = Rule(capability="pc_power", target_device="any", source_device=None, verdict="deny")
        assert r.matches("pc_power", "pc.desktop", None) is True
        assert r.matches("pc_power", "any", None) is True


class TestPolicyEngine:
    """End-to-end policy evaluation."""

    def test_load_and_evaluate(self):
        yaml = """
identities:
  owner:
    default_verdict: allow
  guest:
    default_verdict: deny
rules:
  - when:
      capability: "wa_send"
      target_device: "any"
    then: deny
  - when:
      capability: "ha_turn_on"
      target_device: "light.*"
    then: allow
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            f.flush()
            engine = PolicyEngine(f.name)
            assert engine.policy is not None
            # Guest cannot send WhatsApp
            assert engine.evaluate("guest", "wa_send", "any") == "deny"
            # Owner can turn on lights
            assert engine.evaluate("owner", "ha_turn_on", "light.bedroom") == "allow"
            # Fail-closed: unknown capability denies
            assert engine.evaluate("owner", "unknown_tool", "any") == "allow"  # owner default

    def test_fail_closed(self):
        yaml = """
identities:
  guest:
    default_verdict: deny
rules: []
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            f.flush()
            engine = PolicyEngine(f.name)
            assert engine.evaluate("guest", "anything", "any") == "deny"

    def test_get_deny_list(self):
        yaml = """
identities: {}
rules:
  - when:
      capability: "wa_send"
      target_device: "any"
    then: deny
  - when:
      capability: "pc_power"
      target_device: "any"
    then: deny
  - when:
      capability: "pc_type"
      target_device: "any"
    then: deny
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            f.flush()
            engine = PolicyEngine(f.name)
            deny_list = engine.get_deny_list()
            assert "wa_send" in deny_list
            assert "pc_power" in deny_list
            assert "pc_type" in deny_list
            assert "ha_turn_on" not in deny_list

    def test_missing_file_returns_deny(self):
        engine = PolicyEngine("/nonexistent/policy.yaml")
        assert engine.policy is None
        assert engine.evaluate("guest", "wa_send", "any") == "deny"

    def test_rule_ordering_specific_first(self):
        """More specific rules (longer target patterns) should match first."""
        yaml = """
identities: {}
rules:
  - when:
      capability: "ha_call_service"
      target_device: "script.good_night"
    then: allow
  - when:
      capability: "ha_call_service"
      target_device: "script.*"
    then: deny
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml)
            f.flush()
            engine = PolicyEngine(f.name)
            # good_night should match the specific rule first
            assert engine.evaluate("guest", "ha_call_service", "script.good_night") == "allow"
            # Other scripts should hit the deny rule
            assert engine.evaluate("guest", "ha_call_service", "script.tv_toggle") == "deny"
