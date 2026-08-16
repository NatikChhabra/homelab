"""
Unit tests for hermes.executor — pure logic, no live HA required.

Why this file exists
--------------------
`test_endtoend.py` proves actuation against real devices.
`test_router_templates.py` proves the routing table.
Neither covers the A6 verification edge case: an entity returning
`unavailable` / `unknown` / `none` must be reported as MISMATCH, not
silently accepted. That bug caused JARVIS to confirm actions that never
reached the device.

Run
---
  python -m pytest hermes/test_executor.py -v
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hermes.executor import NON_ACTIONABLE_STATES, Executor  # noqa: E402

# good_night tracks these 5 entities; all must be present in the mock session
# or the before-snapshot try/except swallows the error and returns None.
GOOD_NIGHT_ENTITIES = [
    "switch.3_1_node_smart_switch_switch_1",
    "switch.3_1_node_smart_switch_switch_2",
    "switch.3_1_node_smart_switch_switch_3",
    "fan.fan",
    "input_boolean.ac_power",
]


# --- helpers ----------------------------------------------------------------

class _FakeResponse:
    """Minimal aiohttp ClientResponse stand-in that supports async with."""

    def __init__(self, status: int, state: str) -> None:
        self.status = status
        self._state = state

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def json(self) -> dict[str, str]:
        return {"state": self._state}


class _FakeSession:
    """
    Fake aiohttp.ClientSession that returns different states on successive
    calls per entity — simulating before/after snapshots across a tool call.

    Call 1 (before snapshot)  → initial state
    Call 2+ (after snapshot)  → after state if set, else initial state
    """

    def __init__(self, initial: dict[str, str]) -> None:
        self._initial = dict(initial)
        self._after: dict[str, str] = {}
        self._calls: dict[str, int] = {}

    def get(self, url: str, **_kwargs: object) -> _FakeResponse:
        if "/api/states/" in url:
            eid = url.split("/api/states/")[-1]
            self._calls[eid] = self._calls.get(eid, 0) + 1
            # Second call for this entity = after snapshot
            if self._calls[eid] > 1 and eid in self._after:
                return _FakeResponse(200, self._after[eid])
            state = self._initial.get(eid)
            if state is not None:
                return _FakeResponse(200, state)
        return _FakeResponse(404, "?")

    def set_after(self, eid: str, state: str) -> None:
        """Override the after-snapshot state for one entity."""
        self._after[eid] = state

    def set_all_after(self, state: str) -> None:
        """Set the after-snapshot state for every tracked entity."""
        for eid in self._initial:
            self._after[eid] = state


# --- fixtures ---------------------------------------------------------------

@pytest.fixture
def executor() -> Executor:
    return Executor("http://fake-hass", "fake-token")


# --- tests ------------------------------------------------------------------

class TestNonActionableStates:
    """Direct assertion on the frozenset — catches accidental mutations."""

    def test_contains_unavailable(self) -> None:
        assert "unavailable" in NON_ACTIONABLE_STATES

    def test_contains_unknown(self) -> None:
        assert "unknown" in NON_ACTIONABLE_STATES

    def test_contains_none(self) -> None:
        assert "none" in NON_ACTIONABLE_STATES

    def test_does_not_contain_off(self) -> None:
        """off is a valid operational state; only absent states are non-actionable."""
        assert "off" not in NON_ACTIONABLE_STATES

    def test_does_not_contain_on(self) -> None:
        assert "on" not in NON_ACTIONABLE_STATES

    def test_is_frozenset_not_list(self) -> None:
        assert isinstance(NON_ACTIONABLE_STATES, frozenset)


class TestVerifyActuationUnavailable:
    """
    A6 must return a MISMATCH complaint when any tracked entity reads
    unavailable / unknown / none after actuation, even if no EXPECTED state
    was set for that tool.
    """

    def _make_session(
        self, initial: dict[str, str] | None = None
    ) -> _FakeSession:
        """Build a session covering all good_night entities with defaults."""
        states = dict(initial or {})
        for eid in GOOD_NIGHT_ENTITIES:
            states.setdefault(eid, "off")
        return _FakeSession(states)

    @pytest.mark.asyncio
    async def test_unavailable_entity_returns_mismatch(
        self, executor: Executor
    ) -> None:
        """Entity becomes unavailable after a call → complaint returned."""
        session = self._make_session({e: "off" for e in GOOD_NIGHT_ENTITIES})
        session.set_all_after("unavailable")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "good_night", {}
            )
        assert result is not None
        assert "switch_3_1_node_smart_switch_switch_1" in result

    @pytest.mark.asyncio
    async def test_unknown_entity_returns_mismatch(
        self, executor: Executor
    ) -> None:
        session = self._make_session({e: "off" for e in GOOD_NIGHT_ENTITIES})
        session.set_all_after("unknown")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "good_night", {}
            )
        assert result is not None
        assert "switch_3_1_node_smart_switch_switch_1" in result

    @pytest.mark.asyncio
    async def test_none_entity_returns_mismatch(
        self, executor: Executor
    ) -> None:
        session = self._make_session({e: "off" for e in GOOD_NIGHT_ENTITIES})
        session.set_all_after("none")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "good_night", {}
            )
        assert result is not None
        assert "switch_3_1_node_smart_switch_switch_1" in result

    @pytest.mark.asyncio
    async def test_on_state_passes(self, executor: Executor) -> None:
        """
        All entities already on; tool turns them off. After snapshot reads off.
        At least one entity changed → no complaint.
        """
        session = self._make_session({e: "on" for e in GOOD_NIGHT_ENTITIES})
        session.set_all_after("off")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "good_night", {}
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_expected_state_mismatch_still_works(
        self, executor: Executor
    ) -> None:
        """
        When ACTUATION_EXPECTED sets a target (e.g. ac_on → ac_power=on),
        a wrong state still returns MISMATCH before the unavailable check.
        """
        session = _FakeSession({"input_boolean.ac_power": "off"})
        session.set_after("input_boolean.ac_power", "off")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "ac_on", {}
            )
        assert result is not None
        assert "ac_power" in result

    @pytest.mark.asyncio
    async def test_any_changed_passes(self, executor: Executor) -> None:
        """
        good_night has no EXPECTED dict — it falls through to the
        'any_changed' check. switch_1 flips on→off, others stay off.
        At least one changed → no complaint.
        """
        session = self._make_session({
            "switch.3_1_node_smart_switch_switch_1": "on",
            "switch.3_1_node_smart_switch_switch_2": "off",
            "switch.3_1_node_smart_switch_switch_3": "off",
            "fan.fan": "off",
            "input_boolean.ac_power": "off",
        })
        # After: switch_1 changed on→off, rest unchanged
        session.set_after("switch.3_1_node_smart_switch_switch_1", "off")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "good_night", {}
            )
        assert result is None

    @pytest.mark.asyncio
    async def test_no_change_returns_mismatch(self, executor: Executor) -> None:
        """
        good_night with no entity changes → complaint about no change.
        """
        session = self._make_session({e: "off" for e in GOOD_NIGHT_ENTITIES})
        # After stays the same as before — nothing changed
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "good_night", {}
            )
        assert result is not None
        assert "did not change" in result.lower()

    @pytest.mark.asyncio
    async def test_partial_success_names_only_failed_entities(
        self, executor: Executor
    ) -> None:
        """
        good_night with 5 entities: switch_1 unavailable, rest off.
        Speech must name only switch_1, not the 4 successful entities.
        """
        session = self._make_session({e: "off" for e in GOOD_NIGHT_ENTITIES})
        session.set_after("switch.3_1_node_smart_switch_switch_1", "unavailable")
        # Others stay off (successful)
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "good_night", {}
            )
        assert result is not None
        assert "switch_3_1_node_smart_switch_switch_1" in result
        assert "switch_3_1_node_smart_switch_switch_2" not in result
        assert "switch_3_1_node_smart_switch_switch_3" not in result
        assert "fan_fan" not in result
        assert "ac_power" not in result
        assert "unavailable" in result

    @pytest.mark.asyncio
    async def test_multiple_mismatches_in_single_speech(
        self, executor: Executor
    ) -> None:
        """
        Two entities unavailable → speech names both, joined with 'and'.
        """
        session = self._make_session({e: "off" for e in GOOD_NIGHT_ENTITIES})
        session.set_after("switch.3_1_node_smart_switch_switch_1", "unavailable")
        session.set_after("switch.3_1_node_smart_switch_switch_2", "unknown")
        with patch.object(asyncio, "sleep", new_callable=AsyncMock):
            result = await executor._verify_actuation(
                session, "good_night", {}
            )
        assert result is not None
        assert "switch_3_1_node_smart_switch_switch_1" in result
        assert "switch_3_1_node_smart_switch_switch_2" in result
        assert "and" in result
