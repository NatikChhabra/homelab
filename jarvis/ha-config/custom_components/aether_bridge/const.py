"""Aether Bridge constants."""
from __future__ import annotations

DOMAIN = "aether_bridge"
AGENT_ID = f"conversation.{DOMAIN}"

# Guest-tier deny list: Hermes tool names that guests must never execute.
# Mapped from the driver/executor tool registry. If a new tool is added that
# a guest should not have, add it here.
GUEST_DENY_LIST: frozenset[str] = frozenset({
    "pc_type",
    "pc_power",
    "pc_app",
    "wa_send",
    "call_make",
    "call_answer",
    "call_reject",
})
