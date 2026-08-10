#!/usr/bin/env python3
"""Generate 4 JARVIS-style acknowledgment sounds.

Each sound is a short WAV file (~0.3s) that plays when the wake word is detected.
Use: python generate_beeps.py
"""
import os
import struct
import math

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "beeps")
os.makedirs(OUTPUT_DIR, exist_ok=True)

SAMPLE_RATE = 24000
DURATION = 0.3  # seconds
VOLUME = 0.5

def write_wav(filename: str, samples: list[int]) -> None:
    """Write a mono WAV file at SAMPLE_RATE."""
    filepath = os.path.join(OUTPUT_DIR, filename)
    num_samples = len(samples)
    with open(filepath, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + num_samples * 2))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))       # PCM
        f.write(struct.pack("<H", 1))       # mono
        f.write(struct.pack("<I", SAMPLE_RATE))
        f.write(struct.pack("<I", SAMPLE_RATE * 2))
        f.write(struct.pack("<H", 2))
        f.write(struct.pack("<H", 16))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", num_samples * 2))
        for s in samples:
            f.write(struct.pack("<h", max(-32767, min(32767, s))))
    print(f"  Created: {filepath} ({num_samples//SAMPLE_RATE:.2f}s)")


# 1. JARVIS ONLINE — rising sine sweep (Iron Man style)
def beep_online() -> list[int]:
    samples = []
    for i in range(int(SAMPLE_RATE * DURATION)):
        t = i / SAMPLE_RATE
        freq = 300 + 600 * (t / DURATION)  # 300Hz -> 900Hz rising
        val = math.sin(2 * math.pi * freq * t) * VOLUME
        samples.append(int(val * 32767))
    return samples

# 2. READY — double beep (Star Trek communicator style)
def beep_ready() -> list[int]:
    samples = []
    for beep in [0.0, 0.12]:  # two beeps, 120ms apart
        for i in range(int(SAMPLE_RATE * 0.1)):
            t = i / SAMPLE_RATE
            val = math.sin(2 * math.pi * 800 * t) * math.exp(-5 * t) * VOLUME
            samples.append(int(val * 32767))
    # pad to DURATION
    while len(samples) < int(SAMPLE_RATE * DURATION):
        samples.append(0)
    return samples

# 3. LISTENING — soft chime (subtle, minimal)
def beep_listening() -> list[int]:
    samples = []
    for i in range(int(SAMPLE_RATE * DURATION)):
        t = i / SAMPLE_RATE
        # soft triangle wave
        val = (2 * abs(2 * (t % 0.5) - 1) - 1) * math.exp(-3 * t) * VOLUME * 0.3
        samples.append(int(val * 32767))
    return samples

# 4. ACTIVATED — three-tone ascending (futuristic)
def beep_activated() -> list[int]:
    samples = []
    for beep in [0.0, 0.08, 0.16]:
        for i in range(int(SAMPLE_RATE * 0.08)):
            t = i / SAMPLE_RATE + beep
            freq = 500 + 400 * (beep / 0.16)  # ascending
            val = math.sin(2 * math.pi * freq * t) * math.exp(-8 * (t - beep)) * VOLUME
            samples.append(int(val * 32767))
    while len(samples) < int(SAMPLE_RATE * DURATION):
        samples.append(0)
    return samples


if __name__ == "__main__":
    print("Generating JARVIS acknowledgment sounds...")
    write_wav("jarvis_online.wav", beep_online())
    write_wav("ready.wav", beep_ready())
    write_wav("listening.wav", beep_listening())
    write_wav("activated.wav", beep_activated())
    print("Done. Place in host-ears/beeps/ and select with JARVIS_BEEP=jarvis_online")
