#!/usr/bin/env python3
"""
audio_probe.py -- proves whether the mic capture and 2ch/44.1k -> 1ch/16k conversion
is correct, independent of the rest of the JARVIS pipeline.

Usage:
    python audio_probe.py --list
    python audio_probe.py --device 1 --seconds 6 --outdir out/

Speak normally for the whole recording window when prompted.
"""
from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
from scipy.signal import resample_poly

TARGET_RATE = 16_000
FRAME_MS = 30  # webrtcvad accepts 10/20/30 ms frames


def list_devices() -> None:
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            print(
                f"{idx:3d}: {dev['name']!r} "
                f"ch={dev['max_input_channels']} "
                f"rate={dev['default_samplerate']:.0f} "
                f"api={sd.query_hostapis(dev['hostapi'])['name']}"
            )


def record(device: int, seconds: float) -> tuple[np.ndarray, int]:
    info = sd.query_devices(device)
    rate = int(info["default_samplerate"])
    channels = int(info["max_input_channels"])
    print(f"Recording {seconds}s from {info['name']!r} @ {rate} Hz, {channels} ch")
    print(">>> SPEAK NOW -- say 'hey jarvis, turn on the lights' a few times <<<")
    audio = sd.rec(
        int(seconds * rate), samplerate=rate, channels=channels,
        dtype="int16", device=device,
    )
    sd.wait()
    return audio, rate


def write_wav(path: Path, data: np.ndarray, rate: int) -> None:
    """data: int16, shape (n,) mono or (n, ch)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    ch = 1 if data.ndim == 1 else data.shape[1]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data.astype("<i2").tobytes())


def to_16k_mono(mono: np.ndarray, src_rate: int) -> np.ndarray:
    """Polyphase resample int16 mono -> int16 mono at TARGET_RATE."""
    if src_rate == TARGET_RATE:
        return mono.astype(np.int16)
    g = np.gcd(src_rate, TARGET_RATE)
    out = resample_poly(mono.astype(np.float64), TARGET_RATE // g, src_rate // g)
    return np.clip(np.round(out), -32768, 32767).astype(np.int16)


def stats(name: str, x: np.ndarray) -> dict:
    xf = x.astype(np.float64)
    rms = float(np.sqrt(np.mean(xf ** 2)))
    peak = int(np.max(np.abs(x)))
    clipped = int(np.sum(np.abs(x) >= 32700))
    return {
        "name": name, "rms": round(rms, 1), "peak": peak,
        "clipped_samples": clipped,
        "clipped_pct": round(100.0 * clipped / max(len(x), 1), 4),
    }


def voiced_stats(mono16k: np.ndarray) -> dict:
    """RMS over voiced frames only. Falls back to an energy gate if webrtcvad is absent."""
    n = int(TARGET_RATE * FRAME_MS / 1000)
    frames = [mono16k[i:i + n] for i in range(0, len(mono16k) - n, n)]
    if not frames:
        return {"error": "no frames"}

    try:
        import webrtcvad
        vad = webrtcvad.Vad(2)
        flags = [vad.is_speech(f.astype("<i2").tobytes(), TARGET_RATE) for f in frames]
        method = "webrtcvad(mode=2)"
    except Exception:
        energies = np.array([np.sqrt(np.mean(f.astype(np.float64) ** 2)) for f in frames])
        gate = max(np.percentile(energies, 30) * 3.0, 50.0)
        flags = list(energies > gate)
        method = "energy-gate(fallback)"

    def rms(fs):
        if not fs:
            return None
        cat = np.concatenate(fs).astype(np.float64)
        return float(np.sqrt(np.mean(cat ** 2)))

    voiced = [f for f, v in zip(frames, flags) if v]
    silent = [f for f, v in zip(frames, flags) if not v]
    v_rms = np.array([np.sqrt(np.mean(f.astype(np.float64) ** 2)) for f in voiced]) \
        if voiced else np.array([])

    return {
        "method": method,
        "frames_total": len(frames),
        "frames_voiced": len(voiced),
        "voiced_pct": round(100.0 * len(voiced) / len(frames), 1),
        "voiced_rms_overall": None if not voiced else round(rms(voiced), 1),
        "voiced_rms_p10": None if not voiced else round(float(np.percentile(v_rms, 10)), 1),
        "voiced_rms_p50": None if not voiced else round(float(np.percentile(v_rms, 50)), 1),
        "voiced_rms_p90": None if not voiced else round(float(np.percentile(v_rms, 90)), 1),
        "silence_noise_floor_rms": None if not silent else round(rms(silent), 1),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--device", type=int, default=None)
    ap.add_argument("--seconds", type=float, default=6.0)
    ap.add_argument("--outdir", default="out")
    args = ap.parse_args()

    if args.list or args.device is None:
        list_devices()
        return 0

    outdir = Path(args.outdir)
    audio, rate = record(args.device, args.seconds)
    report: dict = {"device": args.device, "src_rate": rate,
                    "shape": list(audio.shape), "channels": {}, "variants": {}}

    # --- per-channel analysis: this is where a bad downmix shows itself ---
    if audio.ndim == 2 and audio.shape[1] >= 2:
        ch0, ch1 = audio[:, 0], audio[:, 1]
        report["channels"]["ch0"] = stats("ch0", ch0)
        report["channels"]["ch1"] = stats("ch1", ch1)
        corr = float(np.corrcoef(ch0.astype(np.float64), ch1.astype(np.float64))[0, 1])
        report["channels"]["correlation"] = round(corr, 4)
        # Negative correlation => averaging partially CANCELS the signal.
        report["channels"]["phase_cancellation_risk"] = bool(corr < 0.0)
        variants = {
            "ch0_only": ch0,
            "ch1_only": ch1,
            "mean_both": np.round(audio.mean(axis=1)).astype(np.int16),
        }
    else:
        mono = audio.reshape(-1)
        report["channels"]["mono"] = stats("mono", mono)
        variants = {"mono": mono}

    write_wav(outdir / "raw_native.wav", audio, rate)

    for name, mono in variants.items():
        m16 = to_16k_mono(mono, rate)
        write_wav(outdir / f"{name}_16k.wav", m16, TARGET_RATE)
        report["variants"][name] = {
            "native": stats(name, mono),
            "resampled_16k": stats(f"{name}_16k", m16),
            "voiced": voiced_stats(m16),
        }

    (outdir / "audio_probe.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    print(f"\nWrote wavs + audio_probe.json to {outdir.resolve()}")
    print("LISTEN to each *_16k.wav. If speech is unintelligible in one but fine in "
          "another, the downmix is the bug.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
