"""
test_enrol.py — tests for the Aether device enrolment module.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kernel.enrol import (
    DeviceRegistry,
    Signer,
    VerifyResult,
    fingerprint_from_pubkey,
    fingerprint_from_file,
    generate_keypair,
    verify_signature,
)


class TestKeypair:
    """Key generation and fingerprinting."""

    def test_generate_returns_pem(self):
        priv, pub = generate_keypair("test-device")
        assert priv.startswith(b"-----BEGIN")
        assert pub.startswith(b"-----BEGIN")
        assert b"PRIVATE" in priv
        assert b"PUBLIC" in pub

    def test_fingerprint_from_pubkey(self):
        _, pub = generate_keypair("test-device")
        fp = fingerprint_from_pubkey(pub)
        assert len(fp) == 8
        assert all(c in "0123456789abcdef" for c in fp)

    def test_fingerprint_deterministic(self):
        _, pub = generate_keypair("test-device")
        fp1 = fingerprint_from_pubkey(pub)
        fp2 = fingerprint_from_pubkey(pub)
        assert fp1 == fp2

    def test_different_keys_different_fingerprints(self):
        _, pub1 = generate_keypair("device-a")
        _, pub2 = generate_keypair("device-b")
        assert fingerprint_from_pubkey(pub1) != fingerprint_from_pubkey(pub2)


class TestRegistry:
    """Device registry CRUD."""

    def test_empty_registry(self):
        reg = DeviceRegistry()
        assert reg.devices == {}
        assert reg.get_identity("any-fp") is None

    def test_enrol_and_retrieve(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        priv, pub = generate_keypair("test-device")
        fp = fingerprint_from_pubkey(pub)
        result = reg.enrol(fp, "test-device", "owner", pub)
        assert result.fingerprint == fp
        assert result.identity == "owner"
        assert reg.get_identity(fp) == "owner"
        assert reg.get_identity("other-fp") is None

    def test_revoke(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        priv, pub = generate_keypair("test-device")
        fp = fingerprint_from_pubkey(pub)
        reg.enrol(fp, "test-device", "owner", pub)
        assert reg.get_identity(fp) == "owner"
        assert reg.revoke(fp) is True
        assert reg.get_identity(fp) is None

    def test_save_load_roundtrip(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        priv, pub = generate_keypair("device-1")
        fp = fingerprint_from_pubkey(pub)
        reg.enrol(fp, "device-1", "family", pub)
        reg.save()

        reg2 = DeviceRegistry(path=tmp_path / "devices.json")
        reg2.load()
        assert reg2.get_identity(fp) == "family"

    def test_revoke_survives_roundtrip(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        priv, pub = generate_keypair("device-1")
        fp = fingerprint_from_pubkey(pub)
        reg.enrol(fp, "device-1", "owner", pub)
        reg.revoke(fp)
        reg.save()

        reg2 = DeviceRegistry(path=tmp_path / "devices.json")
        reg2.load()
        assert reg2.get_identity(fp) is None


class TestSignature:
    """Sign and verify request payloads."""

    def test_sign_and_verify(self):
        priv, pub = generate_keypair("test-device")
        fp = fingerprint_from_pubkey(pub)
        signer = Signer(priv)
        payload = signer.build_payload("2026-08-16T10:00:00Z", "turn on lights", "light_on")
        sig = signer.sign(payload)
        assert isinstance(sig, bytes)
        assert len(sig) == 64  # Ed25519 signature is 64 bytes

    def test_verify_ok(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        priv, pub = generate_keypair("test-device")
        fp = fingerprint_from_pubkey(pub)
        reg.enrol(fp, "test-device", "owner", pub)

        signer = Signer(priv)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = signer.build_payload(ts, "turn on lights", "light_on")
        sig = signer.sign(payload)

        result = verify_signature(reg, fp, sig, ts, "turn on lights", "light_on")
        assert result.ok is True
        assert result.verdict == "ok"
        assert result.identity == "owner"

    def test_verify_unknown_device(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        result = verify_signature(reg, "deadbeef", b"\x00" * 64,
                                  "2026-08-16T10:00:00Z", "hi", "light_on")
        assert result.ok is False
        assert result.verdict == "unknown_device"

    def test_verify_bad_signature(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        priv, pub = generate_keypair("test-device")
        fp = fingerprint_from_pubkey(pub)
        reg.enrol(fp, "test-device", "owner", pub)

        # Sign with a different key
        priv2, _ = generate_keypair("other-device")
        signer2 = Signer(priv2)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sig = signer2.sign("tampered|payload")

        result = verify_signature(reg, fp, sig, ts, "turn on lights", "light_on")
        assert result.ok is False
        assert result.verdict == "bad_signature"

    def test_verify_replayed(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        priv, pub = generate_keypair("test-device")
        fp = fingerprint_from_pubkey(pub)
        reg.enrol(fp, "test-device", "owner", pub)

        signer = Signer(priv)
        # Old timestamp
        old_ts = "2020-01-01T00:00:00Z"
        payload = signer.build_payload(old_ts, "turn on lights", "light_on")
        sig = signer.sign(payload)

        result = verify_signature(reg, fp, sig, old_ts, "turn on lights", "light_on", max_age_s=60)
        assert result.ok is False
        assert result.verdict == "replayed"

    def test_verify_none_fingerprint(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        result = verify_signature(reg, "none", b"\x00" * 64,
                                  "2026-08-16T10:00:00Z", "hi", "light_on")
        assert result.ok is True
        assert result.verdict == "ok"
        assert result.fingerprint is None
        assert result.identity == "guest"

    def test_verify_empty_fingerprint(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        result = verify_signature(reg, "", b"\x00" * 64,
                                  "2026-08-16T10:00:00Z", "hi", "light_on")
        assert result.ok is True
        assert result.verdict == "ok"

    def test_verify_revoked(self, tmp_path: Path):
        reg = DeviceRegistry(path=tmp_path / "devices.json")
        reg.load()
        priv, pub = generate_keypair("test-device")
        fp = fingerprint_from_pubkey(pub)
        reg.enrol(fp, "test-device", "owner", pub)
        reg.revoke(fp)

        signer = Signer(priv)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = signer.build_payload(ts, "turn on lights", "light_on")
        sig = signer.sign(payload)

        result = verify_signature(reg, fp, sig, ts, "turn on lights", "light_on")
        assert result.ok is False
        assert result.verdict == "revoked"
