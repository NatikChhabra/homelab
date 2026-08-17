#!/usr/bin/env python3
"""
wake_probe.py -- score wav files against the hey_jarvis openWakeWord model offline.
Proves whether audio CAN trigger detection, independent of ears.py and the container.

    pip install openwakeword
    python wake_probe.py out/ch0_only_16k.wav out/mean_both_16k.wav
"""
from __future__ import annotations

import sys
import wave

import numpy as np

CHUNK = 1280  # 80 ms @ 16 kHz -- openWakeWord's expected frame size


def load_16k_mono(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == 16000, f"{path}: expected 16 kHz, got {w.getframerate()}"
        assert w.getnchannels() == 1, f"{path}: expected mono, got {w.getnchannels()}"
        assert w.getsampwidth() == 2, f"{path}: expected int16"
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def main(paths: list[str]) -> int:
    from openwakeword.model import Model
    import openwakeword

    try:
        openwakeword.utils.download_models()
    except Exception as exc:  # already present / offline
        print(f"(model download skipped: {exc})")

    model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")

    for path in paths:
        audio = load_16k_mono(path)
        model.reset()
        peak, peak_at, scores = 0.0, 0.0, []
        for i in range(0, len(audio) - CHUNK, CHUNK):
            out = model.predict(audio[i:i + CHUNK])
            score = float(out.get("hey_jarvis", 0.0))
            scores.append(score)
            if score > peak:
                peak, peak_at = score, i / 16000.0
        print(f"\n{path}")
        print(f"  frames={len(scores)}  PEAK={peak:.4f} at t={peak_at:.2f}s")
        print(f"  fires @0.3={'YES' if peak >= 0.3 else 'NO'}   "
              f"@0.5={'YES' if peak >= 0.5 else 'NO'}")
        top = sorted(scores, reverse=True)[:10]
        print(f"  top10={[round(s, 4) for s in top]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
