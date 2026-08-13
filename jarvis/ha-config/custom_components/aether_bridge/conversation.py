"""Aether Bridge — custom Home Assistant conversation agent.

Forwards utterances from satellite devices (Voice PE, etc.) to the JARVIS
kernel at POST /turn, which enforces tier-based permissions before routing
through the normal local→Hermes→LLM pipeline.

The agent ID is conversation.aether_bridge. Configure the pipeline to use it:
  conversation:
    conversation_agents:
      - conversation.aether_bridge

SECURITY: The bridge token (JARVIS_BRIDGE_TOKEN) is a restricted HA long-lived
access token that only grants access to the kernel's /turn endpoint. It must
NOT be the full-permission JARVIS_HASS_TOKEN. Generate a new one via
Settings > People > Long-Lived Access Tokens with only the system-users role.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Literal, override

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    AbstractConversationAgent,
    ConversationInput,
    ConversationResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import intent

_LOGGER = logging.getLogger(__name__)

DOMAIN = "aether_bridge"
AGENT_ID = f"conversation.{DOMAIN}"

# Guest-tier deny list: verbs a satellite mic must never be allowed to execute.
# These map directly to Hermes tool names / driver methods. If a new tool is
# added that a guest should not have, add it here.
GUEST_DENY_LIST: frozenset[str] = frozenset({
    "pc_type",       # type text into the PC
    "pc_power",      # power state changes on the PC
    "pc_app",        # launch apps on the PC
    "wa_send",       # send WhatsApp messages
    "call_make",     # place outgoing calls
    "call_answer",   # answer incoming calls
    "call_reject",   # reject incoming calls
})


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up the Aether Bridge conversation agent."""
    token = os.getenv("JARVIS_BRIDGE_TOKEN", "")
    kernel_url = os.getenv("JARVIS_KERNEL_URL", "http://jarvis-voice:3100")

    if not token:
        _LOGGER.warning(
            "Aether Bridge: JARVIS_BRIDGE_TOKEN not set — agent will NACK every request"
        )

    agent = AetherBridgeAgent(token=token, kernel_url=kernel_url)
    # Register directly with the agent manager (no ConfigEntry needed).
    conversation.get_agent_manager(hass).async_set_agent(AGENT_ID, agent)
    _LOGGER.info("Aether Bridge registered as %s", AGENT_ID)
    return True


class AetherBridgeAgent(AbstractConversationAgent):
    """Forwards utterances to the JARVIS kernel /turn endpoint."""

    def __init__(self, token: str, kernel_url: str) -> None:
        self._token = token
        self._kernel_url = kernel_url.rstrip("/")
        self._http: asyncio.Queue[object] | None = None  # set by async_added_to_hass

    @override
    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        return "*"

    @override
    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        text = user_input.text.strip()
        if not text:
            resp = intent.IntentResponse(language=user_input.language)
            resp.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "I did not hear anything.",
            )
            return ConversationResult(response=resp)

        # Determine tier from the device_id if available.
        # Voice PE satellites are expected to be guest-tier by default.
        tier = self._detect_tier(user_input)

        resp = intent.IntentResponse(language=user_input.language)

        try:
            import aiohttp  # lazy import — HA already ships it
        except ImportError:
            resp.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "I could not reach my brain. Try again.",
            )
            return ConversationResult(response=resp)

        url = f"{self._kernel_url}/turn"
        payload: dict[str, Any] = {"text": text}
        if tier != "owner":
            payload["tier"] = tier

        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        timeout = aiohttp.ClientTimeout(total=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as r:
                    if r.status == 401:
                        resp.async_set_error(
                            intent.IntentResponseErrorCode.UNKNOWN,
                            "I do not have permission to speak right now.",
                        )
                        return ConversationResult(response=resp)
                    if r.status != 200:
                        body = await r.text()
                        _LOGGER.warning("Aether Bridge /turn returned %d: %s", r.status, body[:200])
                        resp.async_set_error(
                            intent.IntentResponseErrorCode.UNKNOWN,
                            "My brain is busy. Try again in a moment.",
                        )
                        return ConversationResult(response=resp)

                    data = await r.json()
                    speech = data.get("speech", "").strip()
                    if not speech:
                        resp.async_set_error(
                            intent.IntentResponseErrorCode.UNKNOWN,
                            "I heard you but had nothing to say.",
                        )
                    else:
                        resp.async_set_speech(speech)
                        _LOGGER.debug(
                            "Aether Bridge routed %s tier=%s path=%s ms=%.0f",
                            text[:40], tier, data.get("path", "?"),
                            data.get("total_ms", 0),
                        )
        except asyncio.TimeoutError:
            resp.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "My brain took too long. Try again.",
            )
        except OSError as exc:
            _LOGGER.warning("Aether Bridge cannot reach kernel: %s", exc)
            resp.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                "I am disconnected from my brain. Try again.",
            )

        return ConversationResult(response=resp, conversation_id=user_input.conversation_id)

    def _detect_tier(self, user_input: ConversationInput) -> str:
        """Infer tier from the requesting device.

        Voice PE devices are guest-tier by default — they sit in the living room
        and should control lights/AC/fan but never touch the PC or messages.
        We detect them by satellite_id (set by HA when the pipeline routes
        through a satellite). If no satellite_id is present, we default to
        owner (the browser mic is always owner-tier).
        """
        if user_input.satellite_id:
            return "guest"
        return "owner"

    @callback
    @override
    async def async_reload(self, language: str | None = None) -> None:
        pass

    @callback
    @override
    async def async_prepare(self, language: str | None = None) -> None:
        pass
