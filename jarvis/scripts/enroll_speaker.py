#!/usr/bin/env python3
"""
Enroll a speaker into speakers/profiles.yaml.

Usage:
    python scripts/enroll_speaker.py <name>

Prompts the user to speak 3 phrases (one at a time), records each to a
temporary buffer, extracts a mean embedding, and writes the profile.

Requirements (host-side, Python 3.11+):
    sounddevice  -- mic capture
    numpy        -- PCM conversion
    librosa      -- mel spectrogram
    onnxruntime  -- embedding extraction
    pyyaml       -- profiles.yaml I/O

The ONNX model is resolved the same way as speaker_model.py:
    1. SPEAKER_MODEL_DIR env var
    2. jarvis/models/ (bind-mounted from host)
    3. HuggingFace cache (first-run download only)

Embeddings are stored; raw audio is discarded immediately after extraction.
profiles.yaml is created if absent. Existing profiles are preserved; only
the named speaker is added or overwritten.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# Resolve the project root so speaker_model is importable regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

import sounddevice as sd  # type: ignore
import yaml

from speaker_model import SpeakerModel, _PROFILES_PATH  # noqa: E402

log = logging.getLogger("enroll")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# Phrases the user should read aloud. Each should be ~3-5 s of speech.
_DEFAULT_PHRASES = [
    "The quick brown fox jumps over the lazy dog.",
    "A journey of a thousand miles begins with a single step.",
    "To be or not to be, that is the question.",
]

# Capture params: 16 kHz mono int16, same as the voice pipeline.
_SR = 16000
_DTYPE = np.int16
_TIMEOUT_S = 30  # max seconds per phrase capture


def _record_phrase(phrase: str, duration_s: float = 4.0) -> bytes:
    """Record one phrase from the mic. Blocks until capture completes."""
    print(f"\nPhrase {phrase}")
    print(f"Speak now ({duration_s:.0f}s)...\n")
    # Allocate buffer. duration_s of 16 kHz mono int16.
    num_samples = int(_SR * duration_s)
    recordings: list[np.ndarray] = []
    start = time.monotonic()

    def _callback(indata, frames, time_info, status):
        if status:
            log.warning("mic status: %s", status)
        recordings.append(indata.copy())

    with sd.InputStream(
        channels=1,
        samplerate=_SR,
        dtype="int16",
        callback=_callback,
    ):
        elapsed = 0.0
        while elapsed < duration_s:
            time.sleep(0.1)
            elapsed = time.monotonic() - start
            remaining = max(0, duration_s - elapsed)
            print(f"\r  {elapsed:.1f}s / {duration_s:.0f}s  ", end="", flush=True)
    print()  # newline after progress

    if not recordings:
        raise RuntimeError("No audio captured — check mic permissions.")

    pcm = np.concatenate(recordings).astype(_DTYPE)
    log.info("captured %d samples (%.2f s)", len(pcm), len(pcm) / _SR)
    return pcm.tobytes()


def _extract_mean_embedding(model: SpeakerModel, pcms: list[bytes]) -> list[float]:
    """Extract one embedding per clip, return mean as float list."""
    embeddings: list[np.ndarray] = []
    for i, pcm in enumerate(pcms):
        emb, ms = model.extract(pcm)
        log.info("  phrase %d: embedding extracted in %.1f ms, norm=%.4f", i + 1, ms, np.linalg.norm(emb))
        embeddings.append(emb)

    mean_emb = np.mean(embeddings, axis=0)
    norm = np.linalg.norm(mean_emb)
    if norm > 0:
        mean_emb = mean_emb / norm
    return mean_emb.tolist()


def _load_existing() -> dict:
    """Load existing profiles.yaml, return mutable dict. File absent → empty."""
    if _PROFILES_PATH.exists():
        try:
            with open(_PROFILES_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                data = {}
            return data
        except Exception as exc:
            log.warning("could not read profiles.yaml (%s); starting fresh", exc)
    return {}


def _save(data: dict) -> None:
    """Write profiles.yaml atomically (write tmp + rename)."""
    _PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PROFILES_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    tmp.rename(_PROFILES_PATH)
    log.info("wrote %s", _PROFILES_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description="Enroll a speaker into profiles.yaml")
    parser.add_argument("name", help="Speaker identifier (lowercase, no spaces)")
    parser.add_argument(
        "--phrases",
        nargs="*",
        default=[],
        help="Phrases to read (default: 3 stock phrases)",
    )
    parser.add_argument("--n-embeddings", type=int, default=3, help="Number of phrases to record")
    parser.add_argument("--threshold", type=float, default=None, help="Override cosine threshold in meta")
    parser.add_argument("--margin", type=float, default=None, help="Override margin floor in meta")
    parser.add_argument("--enforce", choices=["true", "false"], default=None, help="Override enforce flag")
    args = parser.parse_args()

    # Validate name.
    name = args.name.strip().lower()
    if not name or " " in name:
        print("Error: name must be a single lowercase word (e.g. natik, demo-user).", file=sys.stderr)
        return 1

    model = SpeakerModel()
    model.load()
    if not model.is_available():
        print(f"Error: speaker model not available ({model.load_error})", file=sys.stderr)
        return 1

    phrases = args.phrases[: args.n_embeddings] or _DEFAULT_PHRASES[: args.n_embeddings]
    if len(phrases) < 1:
        print("Error: need at least 1 phrase", file=sys.stderr)
        return 1

    print(f"\nEnrolling speaker '{name}'")
    print(f"Model: {model.load_error or 'loaded OK'}")
    print(f"Phrases: {len(phrases)}\n")

    # Record and extract.
    pcms: list[bytes] = []
    for i, phrase in enumerate(phrases):
        print(f"\n--- Phrase {i+1}/{len(phrases)} ---")
        try:
            pcm = _record_phrase(phrase)
        except KeyboardInterrupt:
            print("\nAborted.")
            return 1
        pcms.append(pcm)

    print("\nExtracting embeddings...")
    try:
        embedding = _extract_mean_embedding(model, pcms)
    except Exception as exc:
        print(f"Error extracting embedding: {exc}", file=sys.stderr)
        return 1

    # Build or update profiles.
    data = _load_existing()
    speakers = data.setdefault("speakers", {})
    now = datetime.now(timezone.utc).isoformat()

    speakers[name] = {
        "embedding": embedding,
        "sample_count": len(pcms),
        "enrolled_at": now,
    }

    # Meta block: preserve existing, override only if flags passed.
    if "meta" not in data:
        data["meta"] = {}
    meta = data["meta"]
    if args.threshold is not None:
        meta["threshold"] = args.threshold
    if args.margin is not None:
        meta["margin"] = args.margin
    if args.enforce is not None:
        meta["enforce"] = args.enforce.lower() == "true"

    _save(data)

    # Summary.
    print(f"\nEnrolled '{name}': {len(embedding)}-dim embedding, {len(pcms)} samples.")
    print(f"Profiles now have {len(speakers)} speakers: {', '.join(sorted(speakers))}")
    print(f"Threshold={meta.get('threshold', 0.35)}, margin={meta.get('margin', 0.05)}, enforce={meta.get('enforce', False)}")
    print(f"\nTo activate in the running server, run:")
    print(f"  docker exec jarvis-voice python3 -c "
          f"'from speaker_model import reload_speaker_profiles; reload_speaker_profiles()'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
