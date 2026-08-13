"""Aether Bridge — no persistent state, agent is stateless."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from .const import DOMAIN

async def async_setup_entry(hass: HomeAssistant) -> bool:
    """Set up Aether Bridge — handled by conversation.py async_setup."""
    return True
