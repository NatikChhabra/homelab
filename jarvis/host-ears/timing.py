"""
Turn timing instrumentation for JARVIS.

Tracks per-stage latency across a single turn:
  wake_detected -> beep_started -> recording_ended -> stt_ended -> route_ended -> tts_first_audio -> playback_ended
"""

from __future__ import annotations

import logging
import time
from typing import ClassVar

log = logging.getLogger("jarvis.timing")


class TurnTimer:
    STAGES: ClassVar[tuple[str, ...]] = (
        "wake_detected",
        "beep_started",
        "recording_started",
        "recording_ended",
        "stt_started",
        "stt_ended",
        "route_started",
        "route_ended",
        "tool_started",
        "tool_ended",
        "tts_requested",
        "tts_first_audio",
        "playback_started",
        "playback_ended",
    )

    def __init__(self, turn_id: int) -> None:
        self.turn_id = turn_id
        self.marks: dict[str, float] = {}
        self.notes: dict[str, str] = {}
        self.start = time.perf_counter()

    def mark(self, stage: str) -> None:
        if stage not in self.STAGES:
            log.warning("unknown stage: %s", stage)
        self.marks[stage] = time.perf_counter()

    def note(self, key: str, val: str) -> None:
        self.notes[key] = val

    def span(self, start_stage: str, end_stage: str) -> float | None:
        if start_stage in self.marks and end_stage in self.marks:
            return (self.marks[end_stage] - self.marks[start_stage]) * 1000
        return None

    def flush(self) -> dict[str, float | str | None]:
        summary: dict[str, float | str | None] = {
            "turn_id": self.turn_id,
            "total_ms": round((time.perf_counter() - self.start) * 1000, 1),
        }
        for k, v in self.notes.items():
            summary[k] = v

        prev_stage: str | None = None
        for stage in self.STAGES:
            if stage in self.marks:
                if prev_stage:
                    span_ms = round(
                        (self.marks[stage] - self.marks[prev_stage]) * 1000, 1
                    )
                    summary[f"delta_{prev_stage}_to_{stage}"] = span_ms
                prev_stage = stage

        # Key milestone spans
        summary["wake_to_beep_ms"] = (
            round(v, 1)
            if (v := self.span("wake_detected", "beep_started")) is not None
            else None
        )
        summary["recording_ms"] = (
            round(v, 1)
            if (v := self.span("recording_started", "recording_ended"))
            is not None
            else None
        )
        summary["stt_ms"] = (
            round(v, 1)
            if (v := self.span("stt_started", "stt_ended")) is not None
            else None
        )
        summary["route_ms"] = (
            round(v, 1)
            if (v := self.span("route_started", "route_ended")) is not None
            else None
        )
        summary["tts_first_audio_ms"] = (
            round(v, 1)
            if (v := self.span("tts_requested", "tts_first_audio")) is not None
            else None
        )
        summary["total_wake_to_speech_ms"] = (
            round(v, 1)
            if (v := self.span("wake_detected", "playback_started")) is not None
            else None
        )

        log.info("TURN TIMING: %s", summary)
        return summary
