"""
enrol.py — Device keypair enrolment and verification.

Ed25519 keypair lifecycle for the Aether permission model:
  1. Generate a keypair per device (one-time).
  2. Extract the public-key fingerprint (first 8 hex chars of SHA-256 of raw pubkey bytes).
  3. Register the fingerprint + public key in config/aether-devices.json.
  4. On each request, verify the Ed25519 signature against the stored public key.
  5. Reject replayed signatures (timestamp older than 60 s).

Guest/satellite devices that cannot store a private key (Voice PE, HA bridge)
are NOT enrolled — they use the existing JARVIS_BRIDGE_TOKEN path and are
mapped to identity "guest" by the bridge layer. Enrolment is for owner and
family devices that need device-bound identity.

Key format: PEM-encoded Ed25519 (private + public).
Fingerprint: first 8 chars of lowercase hex SHA-256 digest of the raw
             public key bytes (32 bytes for Ed25519).
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

# Re-import only if cryptography is available; fall back gracefully.
try:
    from cryptography.hazmat.primitives.asymmetric import ed25519 as _ed25519_mod
    from cryptography.hazmat.primitives import serialization as _ser
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

Verdict = Literal["ok", "unknown_device", "bad_signature", "replayed", "revoked"]


@dataclass(frozen=True)
class EnrolResult:
    """Result of device enrolment."""
    fingerprint: str
    identity: str
    device_name: str
    enrolled_at: str
    public_key_pem: str


@dataclass(frozen=True)
class VerifyResult:
    """Result of signature verification."""
    ok: bool
    verdict: Verdict
    fingerprint: str | None
    identity: str | None = None
    detail: str = ""


def _available() -> bool:
    return _CRYPTO_AVAILABLE


def generate_keypair(device_name: str = "", identity: str = "owner") -> tuple[bytes, bytes]:
    """Generate an Ed25519 keypair. Returns (private_key_pem, public_key_pem).

    Raises RuntimeError if the cryptography library is not available.
    """
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("enrol requires the 'cryptography' package")
    private_key = _ed25519_mod.Ed25519PrivateKey.generate()
    priv_pem = private_key.private_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PrivateFormat.PKCS8,
        encryption_algorithm=_ser.NoEncryption(),
    )
    pub_pem = private_key.public_key().public_bytes(
        encoding=_ser.Encoding.PEM,
        format=_ser.PublicFormat.SubjectPublicKeyInfo,
    )
    return priv_pem, pub_pem


def _load_private_key(priv_pem: bytes) -> _ed25519_mod.Ed25519PrivateKey:
    """Deserialize PEM private key bytes to a cryptography key object."""
    return _ser.load_pem_private_key(priv_pem, password=None)  # type: ignore[return-value]


def fingerprint_from_pubkey(pub_pem: bytes) -> str:
    """Extract the 8-char hex fingerprint from a PEM-encoded public key."""
    if not _CRYPTO_AVAILABLE:
        raise RuntimeError("enrol requires the 'cryptography' package")
    pub_key = _ser.load_pem_public_key(pub_pem)
    raw = pub_key.public_bytes(
        encoding=_ser.Encoding.Raw,
        format=_ser.PublicFormat.Raw,
    )
    digest = hashlib.sha256(raw).hexdigest()
    return digest[:8]


def fingerprint_from_file(pub_key_path: Path) -> str:
    """Read a PEM public key file and return its fingerprint."""
    return fingerprint_from_pubkey(pub_key_path.read_bytes())


@dataclass
class DeviceRegistry:
    """In-memory registry of enrolled devices, loaded from config/aether-devices.json."""
    devices: dict[str, dict] = field(default_factory=dict)  # fingerprint -> device info
    path: Path | None = None

    def load(self, path: Path | str | None = None) -> None:
        """Load registry from JSON file."""
        target = Path(path) if path else Path("config/aether-devices.json")
        self.path = target
        if not target.exists():
            return
        data = json.loads(target.read_text(encoding="utf-8"))
        self.devices = {k: v for k, v in data.get("devices", {}).items() if not v.get("revoked", False)}

    def save(self) -> None:
        """Persist registry to disk."""
        if not self.path:
            return
        payload = {"devices": self.devices, "updated_at": datetime.now(timezone.utc).isoformat()}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def enrol(self, fingerprint: str, device_name: str, identity: str,
              public_key_pem: bytes, enrolled_at: str | None = None) -> EnrolResult:
        """Register a device by fingerprint."""
        ts = enrolled_at or datetime.now(timezone.utc).isoformat()
        self.devices[fingerprint] = {
            "name": device_name,
            "identity": identity,
            "public_key_pem": public_key_pem.decode("utf-8"),
            "enrolled_at": ts,
            "revoked": False,
        }
        self.save()
        return EnrolResult(
            fingerprint=fingerprint,
            identity=identity,
            device_name=device_name,
            enrolled_at=ts,
            public_key_pem=public_key_pem.decode("utf-8"),
        )

    def revoke(self, fingerprint: str) -> bool:
        """Revoke a device by fingerprint. Returns True if found and revoked."""
        if fingerprint not in self.devices:
            return False
        self.devices[fingerprint]["revoked"] = True
        self.save()
        return True

    def get_identity(self, fingerprint: str) -> str | None:
        """Return the identity for a known device, or None."""
        dev = self.devices.get(fingerprint)
        if not dev or dev.get("revoked"):
            return None
        return dev.get("identity")


class Signer:
    """Signs request payloads with a device private key."""

    def __init__(self, private_key_pem: bytes):
        if not _CRYPTO_AVAILABLE:
            raise RuntimeError("enrol requires the 'cryptography' package")
        self._private_key = _load_private_key(private_key_pem)

    def sign(self, payload: str) -> bytes:
        """Return Ed25519 signature of payload bytes."""
        return self._private_key.sign(payload.encode("utf-8"))

    @staticmethod
    def build_payload(timestamp: str, utterance: str, tool_name: str) -> str:
        """Build the canonical request payload string to be signed."""
        return f"{timestamp}|{utterance}|{tool_name}"


def verify_signature(
    registry: DeviceRegistry,
    fingerprint: str,
    signature: bytes,
    timestamp: str,
    utterance: str,
    tool_name: str,
    max_age_s: int = 60,
) -> VerifyResult:
    """Verify an Ed25519 signature. Returns VerifyResult."""
    if not _CRYPTO_AVAILABLE:
        return VerifyResult(ok=False, verdict="unknown_device", fingerprint=None,
                            detail="cryptography library not available")

    if not fingerprint or fingerprint == "none":
        return VerifyResult(ok=True, verdict="ok", fingerprint=None,
                            identity="guest", detail="no fingerprint (guest/satellite)")

    # Check raw registry (including revoked entries) for proper verdict
    raw_dev = registry.devices.get(fingerprint)
    if not raw_dev:
        return VerifyResult(ok=False, verdict="unknown_device", fingerprint=fingerprint,
                            detail="device not enrolled")
    if raw_dev.get("revoked"):
        return VerifyResult(ok=False, verdict="revoked", fingerprint=fingerprint,
                            detail="device has been revoked")
    dev = raw_dev

    # Check replay window
    try:
        req_ts = datetime.fromisoformat(timestamp)
        age_s = (datetime.now(timezone.utc) - req_ts).total_seconds()
        if age_s > max_age_s:
            return VerifyResult(ok=False, verdict="replayed", fingerprint=fingerprint,
                                detail=f"signature too old: {age_s:.0f}s > {max_age_s}s")
    except (ValueError, TypeError):
        return VerifyResult(ok=False, verdict="bad_signature", fingerprint=fingerprint,
                            detail="invalid timestamp format")

    # Verify signature
    try:
        pub_pem = dev["public_key_pem"].encode("utf-8")
        pub_key = _ser.load_pem_public_key(pub_pem)
        payload = Signer.build_payload(timestamp, utterance, tool_name)
        pub_key.verify(signature, payload.encode("utf-8"))  # type: ignore[call-arg,attribute-access]
        return VerifyResult(ok=True, verdict="ok", fingerprint=fingerprint,
                            identity=dev.get("identity", "guest"))
    except Exception as e:
        return VerifyResult(ok=False, verdict="bad_signature", fingerprint=fingerprint,
                            detail=str(e))
