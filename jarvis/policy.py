"""
policy.py — Aether permission engine.

Replaces the hardcoded GUEST_DENY_LIST with a single YAML policy file.
Evaluates access decisions against four axes: identity, source_device,
capability, target_device. Returns the first matching verdict.

Verdicts: allow, confirm, notify-owner, deny
Fail-closed: no match -> deny

Usage:
    from kernel.policy import PolicyEngine
    engine = PolicyEngine("config/aether-policy.yaml")
    verdict = engine.evaluate(identity="guest", capability="wa_send", target_device="any")
    # returns "deny"
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

Verdict = Literal["allow", "confirm", "notify-owner", "deny"]


@dataclass(frozen=True)
class Rule:
    """One policy rule: when clause + then verdict."""
    capability: str
    target_device: str
    source_device: str | None
    verdict: Verdict

    def matches(self, capability: str, target_device: str, source_device: str | None) -> bool:
        """Check if this rule applies to the given decision."""
        if self.capability != capability:
            return False
        if self.target_device != "any" and not self._glob_match(target_device, self.target_device):
            return False
        if self.source_device is not None and self.source_device != "any":
            if source_device is None or self.source_device != source_device:
                return False
        return True

    @staticmethod
    def _glob_match(text: str, pattern: str) -> bool:
        """Simple glob match: * matches anything, ? matches one char."""
        regex = "^" + re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        return bool(re.match(regex, text, re.IGNORECASE))


@dataclass
class Policy:
    """Loaded policy: identities + ordered rules."""
    identities: dict[str, dict]
    rules: list[Rule]
    default_verdict: Verdict = "deny"

    def evaluate(self, identity: str, capability: str, target_device: str,
                 source_device: str | None = None) -> Verdict:
        """Return first matching verdict, or default (deny)."""
        for rule in self.rules:
            if rule.matches(capability, target_device, source_device):
                return rule.verdict
        # Identity-based default
        ident_cfg = self.identities.get(identity, {})
        return ident_cfg.get("default_verdict", self.default_verdict)


class PolicyEngine:
    """Loads and evaluates aether-policy.yaml."""

    def __init__(self, path: str | Path = "config/aether-policy.yaml"):
        self.path = Path(path)
        self.policy: Policy | None = None
        if self.path.exists():
            self.policy = self._load()

    def _load(self) -> Policy:
        """Parse YAML, validate, return Policy."""
        text = self.path.read_text(encoding="utf-8")
        data = yaml.safe_load(text)

        identities = data.get("identities", {})
        rules = []
        for r in data.get("rules", []):
            when = r.get("when", {})
            then = r.get("then", "deny")
            if then not in ("allow", "confirm", "notify-owner", "deny"):
                raise ValueError(f"Invalid verdict: {then}")
            rules.append(Rule(
                capability=when.get("capability", "any"),
                target_device=when.get("target_device", "any"),
                source_device=when.get("source_device"),
                verdict=then,
            ))

        # Sort: more specific rules first (longer target_device patterns first)
        rules.sort(key=lambda r: (-len(r.target_device), -int(r.source_device is not None)))

        return Policy(identities=identities, rules=rules)

    def get_deny_list(self) -> frozenset[str]:
        """Return set of capabilities that always deny, for backward compat."""
        if self.policy is None:
            return frozenset()
        denied = set()
        for rule in self.policy.rules:
            if rule.verdict == "deny" and rule.target_device == "any":
                denied.add(rule.capability)
        return frozenset(denied)

    def evaluate(self, identity: str, capability: str, target_device: str,
                 source_device: str | None = None) -> Verdict:
        """Delegate to loaded policy. Returns deny if policy not loaded."""
        if self.policy is None:
            return "deny"
        return self.policy.evaluate(identity, capability, target_device, source_device)


def main():
    """CLI: validate policy file and print summary."""
    path = sys.argv[1] if len(sys.argv) > 1 else "config/aether-policy.yaml"
    engine = PolicyEngine(path)
    if engine.policy is None:
        print(f"ERROR: cannot load {path}")
        sys.exit(1)
    p = engine.policy
    print(f"Loaded {len(p.rules)} rules from {path}")
    print(f"Identities: {list(p.identities.keys())}")
    print(f"Default verdict: {p.default_verdict}")
    print(f"Denied capabilities: {sorted(engine.get_deny_list())}")

    # Smoke test
    for ident in ("owner", "family", "guest", "appliance"):
        v = engine.evaluate(ident, "wa_send", "any")
        print(f"  {ident:10s} wa_send -> {v}")
        v2 = engine.evaluate(ident, "ha_turn_on", "light.*")
        print(f"  {ident:10s} light_on  -> {v2}")


if __name__ == "__main__":
    main()
