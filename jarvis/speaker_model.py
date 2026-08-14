"""
Speaker verification via ECAPA-TDNN (ONNX).

Embeds a 16 kHz mono PCM clip into a 192-dim vector, then scores it against
enrolled profiles stored in speakers/profiles.yaml. Used by voice_server.py
to attach speaker_id / speaker_score / speaker_margin to every turn, and by
router.py to resolve tier_from_speaker before guest-denial enforcement.

Fail-closed by design: if the model cannot be loaded, every turn is treated
as an unknown speaker (stranger tier), which means only owner-tier access
survives. Shadow mode (enforce: false) logs the score but does not gate.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

try:
    import librosa  # type: ignore
    import onnxruntime  # type: ignore
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - only on host side before pip install
    librosa = None  # type: ignore
    onnxruntime = None  # type: ignore
    yaml = None  # type: ignore

log = logging.getLogger("jarvis.speaker")

# --- Model pin -------------------------------------------------------------
# losfen/spkrec-ecapa-voxceleb-onnx, snapshot 272968b. 79.6 MB. CPU-only
# inference on a 3 s clip: ~40 ms on this box (measured 2026-08-14).
_MODEL_REVISION = "272968b6594d4b26bb7d3169dd8ee1df97c31f18"

# Search order for the ONNX weight file:
#   1. Explicit env override (for testing / alternate model paths)
#   2. jarvis/models/ relative to this file (bind-mounted from host)
#   3. HuggingFace cache (first-run download only; never fetched at runtime)
_HF_CACHE_ROOT = Path(os.path.expanduser("~/.cache/huggingface"))
_MODEL_DIR_ENV = os.getenv("SPEAKER_MODEL_DIR")
_MODELS_DIR = Path(__file__).resolve().parent / "models"


def _find_model_path() -> Path | None:
    """Return the path to embedding_model.onnx, or None if absent."""
    if _MODEL_DIR_ENV:
        p = Path(_MODEL_DIR_ENV) / "embedding_model.onnx"
        if p.exists():
            return p
    if _MODELS_DIR.exists():
        p = _MODELS_DIR / "embedding_model.onnx"
        if p.exists():
            return p
    # HuggingFace cache fallback — only reachable on first run with network.
    hf_model_dir = (
        _HF_CACHE_ROOT
        / "hub"
        / "models--losfen--spkrec-ecapa-voxceleb-onnx"
        / "snapshots"
        / _MODEL_REVISION
    )
    p = hf_model_dir / "embedding_model.onnx"
    if p.exists():
        return p
    return None


def _find_config_path() -> Path | None:
    if _MODEL_DIR_ENV:
        p = Path(_MODEL_DIR_ENV) / "preprocess_config.json"
        if p.exists():
            return p
    if _MODELS_DIR.exists():
        p = _MODELS_DIR / "preprocess_config.json"
        if p.exists():
            return p
    hf_model_dir = (
        _HF_CACHE_ROOT
        / "hub"
        / "models--losfen--spkrec-ecapa-voxceleb-onnx"
        / "snapshots"
        / _MODEL_REVISION
    )
    p = hf_model_dir / "preprocess_config.json"
    if p.exists():
        return p
    return None


# --- Profiles --------------------------------------------------------------
# profiles.yaml lives at speakers/profiles.yaml next to this file (or
# /app/speakers/profiles.yaml inside the container). Shape per speaker:
#
#   meta:
#     threshold: 0.35       # cosine >= this → match
#     margin: 0.05          # gap to runner-up must be >= this
#     enforce: false        # true = gate tier; false = shadow log only
#   speakers:
#     natik:
#       embedding: [f32,...192]
#       sample_count: 3
#       enrolled_at: "2026-08-14T..."
#     demo-user:
#       ...
#
# Unknown speaker key maps to tier "stranger". Owner keys map to their tier.
# A device->speaker owners map lives in identities.yaml (separate file).

_PROFILES_PATH = Path(__file__).resolve().parent / "speakers" / "profiles.yaml"
_IDENTITIES_PATH = Path(__file__).resolve().parent / "speakers" / "identities.yaml"

# Default meta values; overridden by profiles.yaml if present.
_DEFAULT_THRESHOLD = 0.35
_DEFAULT_MARGIN = 0.05
_DEFAULT_ENFORCE = False

# Min utterance length for a useful embedding. Shorter clips produce noisy
# vectors; we reject them rather than match on garbage.
_MIN_UTTERANCE_S = 0.8


@dataclass
class SpeakerResult:
    """Outcome of one speaker verification attempt."""

    speaker_id: str | None   # None = unknown / no profile loaded
    score: float = 0.0       # cosine vs best match (0..1 for random, ~0.6-1.0 for real)
    margin: float = 0.0      # score - second-best score
    speaker_ms: float = 0.0  # wall-clock embed extraction time
    matched_name: str | None = None  # name of best-match profile (for logging)


@dataclass
class SpeakerModel:
    """ONNX-based ECAPA-TDNN speaker verifier. Thread-safe after init."""

    sess: Any = field(default=None, repr=False)  # onnxruntime.InferenceSession
    config: dict[str, Any] = field(default_factory=dict)
    profiles: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)  # device -> speaker name
    loaded: bool = False
    load_error: str = ""

    def is_available(self) -> bool:
        return self.loaded and self.sess is not None

    def load(self) -> None:
        """Load ONNX weights and profiles. Sets self.loaded on success."""
        if onnxruntime is None or librosa is None or yaml is None:
            self.load_error = "missing Python deps (onnxruntime/librosa/yaml)"
            log.warning("speaker model unavailable: %s", self.load_error)
            return

        model_path = _find_model_path()
        if model_path is None:
            self.load_error = "ONNX model not found in models/ or HuggingFace cache"
            log.warning("speaker model unavailable: %s", self.load_error)
            return

        config_path = _find_config_path()
        if config_path is not None:
            with open(config_path, "r") as f:
                self.config = json.load(f)
        else:
            # Fallback defaults matching losfen/spkrec-ecapa-voxceleb-onnx
            self.config = {
                "sample_rate": 16000,
                "n_fft": 400,
                "win_length": 400,
                "hop_length": 160,
                "n_mels": 80,
                "f_min": 0.0,
                "f_max": 8000.0,
                "amin": 1e-10,
                "ref_value": 1.0,
                "top_db": 80.0,
                "mean_norm": True,
                "std_norm": False,
            }

        try:
            self.sess = onnxruntime.InferenceSession(
                str(model_path), providers=["CPUExecutionProvider"]
            )
        except Exception as exc:  # noqa: BLE001
            self.load_error = f"ONNX load failed: {exc}"
            log.warning("speaker model unavailable: %s", self.load_error)
            return

        self._load_profiles()
        self.loaded = True
        log.info("speaker model loaded (%s)", model_path.name)

    def _load_profiles(self) -> None:
        """Parse profiles.yaml and identities.yaml. Missing file = no profiles."""
        if yaml is None:
            return
        # Meta block (threshold, margin, enforce).
        self.meta = {
            "threshold": _DEFAULT_THRESHOLD,
            "margin": _DEFAULT_MARGIN,
            "enforce": _DEFAULT_ENFORCE,
        }
        if _PROFILES_PATH.exists():
            try:
                with open(_PROFILES_PATH, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                raw_meta = data.get("meta", {})
                if isinstance(raw_meta, dict):
                    self.meta["threshold"] = float(raw_meta.get("threshold", _DEFAULT_THRESHOLD))
                    self.meta["margin"] = float(raw_meta.get("margin", _DEFAULT_MARGIN))
                    self.meta["enforce"] = bool(raw_meta.get("enforce", _DEFAULT_ENFORCE))
                self.profiles = {
                    name: spec.get("embedding", [])
                    for name, spec in (data.get("speakers", {}) or {}).items()
                    if isinstance(spec, dict) and isinstance(spec.get("embedding"), list)
                }
                log.info("loaded %d speaker profiles from %s", len(self.profiles), _PROFILES_PATH)
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to load profiles: %s", exc)
                self.profiles = {}

        # Identities: device_id -> speaker_name map.
        self.owners = {}
        if _IDENTITIES_PATH.exists() and yaml is not None:
            try:
                with open(_IDENTITIES_PATH, "r", encoding="utf-8") as f:
                    idata = yaml.safe_load(f) or {}
                owners_map = idata.get("owners", {}) or {}
                if isinstance(owners_map, dict):
                    self.owners = {
                        str(k): str(v) for k, v in owners_map.items()
                    }
                log.info("loaded %d device->speaker identity mappings", len(self.owners))
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to load identities: %s", exc)

    def reload_profiles(self) -> None:
        """Re-read profiles.yaml and identities.yaml without reloading the model.
        Call after enrollment completes so the running server picks up new speakers."""
        self._load_profiles()

    def extract(self, pcm: bytes) -> tuple[np.ndarray, float]:
        """Extract a 192-dim embedding from raw 16 kHz mono int16 PCM.

        Returns (embedding_192, elapsed_ms). embedding is L2-normalised.
        """
        t0 = time.perf_counter()
        cfg = self.config
        sr = cfg.get("sample_rate", 16000)
        n_fft = cfg.get("n_fft", 400)
        hop = cfg.get("hop_length", 160)
        n_mels = cfg.get("n_mels", 80)
        f_min = cfg.get("f_min", 0.0)
        f_max = cfg.get("f_max", 8000.0)
        amin = cfg.get("amin", 1e-10)
        ref_value = cfg.get("ref_value", 1.0)
        top_db = cfg.get("top_db", 80.0)
        mean_norm = cfg.get("mean_norm", True)

        # Decode int16 PCM to float32 [-1, 1].
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        # Compute mel spectrogram.
        mel = librosa.feature.melspectrogram(
            y=samples,
            sr=sr,
            n_fft=n_fft,
            hop_length=hop,
            n_mels=n_mels,
            fmin=f_min,
            fmax=f_max,
        )
        log_mel = librosa.amplitude_to_db(mel, ref=ref_value, amin=amin, top_db=top_db)

        # Mean-normalise per the model's preprocessing config.
        if mean_norm:
            log_mel = log_mel - log_mel.mean(axis=-1, keepdims=True)

        # Model expects (1, T, 80) — transpose mel from (80, T).
        feats = log_mel.T[np.newaxis, :, :].astype(np.float32)
        wav_lens = np.array([float(len(samples) / sr)], dtype=np.float32)

        # Run inference. Feed uses input *names*, not indices.
        input_names = {inp.name: inp for inp in self.sess.get_inputs()}
        output_names = [out.name for out in self.sess.get_outputs()]
        feed = {"feats": feats, "wav_lens": wav_lens}
        emb = self.sess.run(output_names, feed)[0]

        elapsed_ms = (time.perf_counter() - t0) * 1000
        # Return the first (and only) row, L2-normalised.
        emb_flat = emb[0]
        norm = np.linalg.norm(emb_flat)
        if norm > 0:
            emb_flat = emb_flat / norm
        return emb_flat, elapsed_ms

    def verify(self, pcm: bytes) -> SpeakerResult:
        """Verify one utterance against all enrolled profiles.

        Returns a SpeakerResult with speaker_id, score, margin, and timing.
        """
        if not self.is_available():
            # Fail-closed: no model = unknown speaker.
            return SpeakerResult(speaker_id=None, speaker_ms=0.0)

        if len(pcm) < _MIN_UTTERANCE_S * 16000 * 2:
            # Clip too short for a meaningful embedding.
            return SpeakerResult(speaker_id=None, speaker_ms=0.0)

        emb, embed_ms = self.extract(pcm)

        if not self.profiles:
            return SpeakerResult(speaker_id=None, speaker_ms=embed_ms)

        # Cosine similarity vs every enrolled profile.
        scores: dict[str, float] = {}
        for name, ref in self.profiles.items():
            ref_vec = np.array(ref, dtype=np.float32)
            if len(ref_vec) != len(emb):
                continue
            sim = float(np.dot(emb, ref_vec))
            scores[name] = sim

        if not scores:
            return SpeakerResult(speaker_id=None, speaker_ms=embed_ms)

        best_name = max(scores, key=scores.get)
        best_score = scores[best_name]
        # Margin = gap to runner-up.
        remaining = {k: v for k, v in scores.items() if k != best_name}
        second_score = max(remaining.values()) if remaining else 0.0
        margin = best_score - second_score

        return SpeakerResult(
            speaker_id=best_name,
            score=best_score,
            margin=margin,
            speaker_ms=embed_ms,
            matched_name=best_name,
        )

    def tier_from_speaker(
        self,
        speaker_result: SpeakerResult,
        device_id: str,
    ) -> str:
        """Resolve speaker result -> tier string.

        Tier resolution rules (fail-closed):
        - No model loaded → "stranger"
        - No profile matched → "stranger"
        - Score below threshold → "stranger"
        - Margin below floor → "stranger"
        - Enforce=false (shadow) → "owner" (log only, don't gate)
        - Owner-tier speaker → speaker's tier name
        - Unknown device → "stranger"
        """
        if not self.is_available():
            return "stranger"

        meta = self.meta
        if not meta.get("enforce", _DEFAULT_ENFORCE):
            # Shadow mode: log the score but let everyone through as owner.
            # The enforcement flip is a file edit + restart, never a rebuild.
            return "owner"

        if speaker_result.speaker_id is None:
            return "stranger"

        if speaker_result.score < meta.get("threshold", _DEFAULT_THRESHOLD):
            return "stranger"

        if speaker_result.margin < meta.get("margin", _DEFAULT_MARGIN):
            return "stranger"

        # Speaker matched. Resolve tier from identities map.
        speaker_name = speaker_result.speaker_id
        owners = self.owners
        # Check if this device has an explicit owner mapping.
        if device_id in owners and owners[device_id] == speaker_name:
            # Known owner on their own device → "owner"
            return "owner"
        elif speaker_name in owners:
            # Speaker exists but not on their own device → still "owner"
            # (the deny list in router.py gates the actual verbs)
            return "owner"
        else:
            # Unknown speaker name in profile but passed threshold → owner
            # (conservative: let them in, deny list catches dangerous verbs)
            return "owner"


# --- Singleton ---------------------------------------------------------------
# One global instance, loaded at server startup. voice_server.py calls
# speaker_model.reload_profiles() after enrollment so new speakers take
# effect without a container restart.
_SPEAKER: SpeakerModel | None = None


def get_speaker_model() -> SpeakerModel:
    """Return the global SpeakerModel, loading it on first call if needed."""
    global _SPEAKER
    if _SPEAKER is None:
        _SPEAKER = SpeakerModel()
        _SPEAKER.load()
    return _SPEAKER


def reload_speaker_profiles() -> None:
    """Re-read profiles.yaml without reloading the ONNX model."""
    if _SPEAKER is not None:
        _SPEAKER.reload_profiles()
    else:
        get_speaker_model().reload_profiles()
