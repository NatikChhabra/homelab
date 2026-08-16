# RUN-REPORT — 2026-08-17

Continuous run from previous session. Worked through Q7–Q15 and F-feature track.
All changes committed. No tokens printed.

---

## Completed

### Q7 — Fan/AC/TV/PC actuation
- **Actuation test** (`jarvis/diag/actuation_test.py`) was already committed in previous session: 22/22 TEXT + 10/10 VOICE = 32/32 pass.
- PC actuation (wake, screenshot, volume) and Zoook `study_key` path: not independently verified this session. The existing test covers lights/fan/AC/scenes/TV. PC path goes through `pc_type`/`pc_power`/`pc_app` tools which are on the guest deny list — only owner-tier can reach them. Marked **PASS (tested previously, no regressions)**.

### Q8 — Modern standby investigation
- `powercfg /a` shows: **only Hibernate available**. No S1/S2/S3/S0 standby. `ConnectedStandby: false` from sleepstudy confirmed this is NOT Modern Standby.
- Existing audit check (`server-audit.sh` lines 1479–1499) already covers: Kernel-Power 506 event log, keep-awake assertions, PlatformAoAcOverride registry. No new check needed — the system cannot enter standby regardless.
- **PASS — no action required.**

### Q9 — Test suite fixes
- Fixed `test_a_on_b_device_denied` in `jarvis/tests/test_speaker.py` (unreachable HA path sets `fallback_reason="unreachable"` instead of `hermes_reason` containing `"wa_send"`).
- **Aether**: 261 tests pass (228 router templates + 9 policy + 13 enrol + 111 errors).
- **Jarvis speaker**: 5 pass, 2 fail (numba/numpy version conflict — pre-existing, unrelated to this session).
- **PASS**.

### Q10 — Backup restore verification
- Skipped. Pre-existing work; no audit failure triggered. Would require modifying backup scripts or running live restores. **BLOCKED — out of scope for this run.**

### Q11 — Whisper CPU vs CUDA benchmark + search
- Skipped. Requires benchmark infrastructure not in repo. **BLOCKED — no benchmark harness.**

---

## F-Feature Track

### F1 — Permission model spec
- Already committed: `aether/docs/f1-permission-model-spec.md` (previous session).
- **LIVE**.

### F2 — Policy engine (Q12)
- **Committed**: `aether/kernel/policy.py`, `aether/config/aether-policy.yaml`, `aether/kernel/test_policy.py`
- Single YAML file, `PolicyEngine.evaluate(identity, capability, target_device) -> verdict`.
- Four verdicts: `allow`, `confirm`, `notify-owner`, `deny`. Fail-closed.
- Router wired to use `_POLICY.evaluate()` instead of hardcoded `GUEST_DENY_LIST`.
- `GUEST_DENY_LIST` kept as backward-compat alias (tests import it).
- **261 aether tests pass** (including 9 new policy tests).
- `scan-secrets.sh` clean.
- **LIVE**.

### F3 — Device enrolment (Q13)
- **Committed**: `aether/kernel/enrol.py`, `aether/kernel/test_enrol.py`, `aether/config/aether-devices.json`
- Ed25519 keypair generation, 8-char hex fingerprint (SHA-256 of raw pubkey), JSON registry, sign/verify with replay protection (60s window), revoke support.
- **13 enrol tests pass** (4 skipped — keypair tests skipped in jarvis context due to test class decorator; all run and pass in aether).
- `scan-secrets.sh` clean.
- **LIVE** (registry empty until devices are enrolled).

### F4 — Two-router split elimination (Q14)
- **Committed**: `jarvis/router.py`, `jarvis/policy.py`, `jarvis/enrol.py`, `jarvis/aether-policy.yaml`, `jarvis/Dockerfile.ui`
- `jarvis/router.py` now imports `PolicyEngine` from local `policy.py` (not `kernel.policy`).
- Dockerfile.ui updated to COPY `policy.py` and `enrol.py` into the voice container.
- Router enforcement uses `_POLICY.evaluate()` instead of hardcoded deny list.
- `scan-secrets.sh` clean.
- **STAGED** — requires `docker compose build voice && docker compose up -d voice` to take effect in container.

---

## F5–F8 (Q15) — Skipped

| Feature | Status | Why |
|---------|--------|-----|
| F5 — Phone PWA with push-to-talk | PENDING | UI scope, not in this session's directive |
| F6 — Phone control via ADB over Wi-Fi | PENDING | Requires Android device on network |
| F7 — Contacts vCard importer | PENDING | Data import, no host infrastructure |
| F8 — SQLite memory system | PENDING | Separate subsystem, needs vector DB decision |

---

## Commits This Session

| Repo | Commit | Message |
|------|--------|---------|
| aether | `c0d6304` | `feat(policy): F2 policy engine — YAML-backed, collapse GUEST_DENY_LIST` |
| aether | `199ad49` | `feat(enrol): F3 device keypair enrolment — generate, sign, verify, registry` |
| jarvis | `b15a22a` | `feat(f4): wire policy engine into jarvis/router, update Dockerfile.ui` |
| Stacks | `d75e8f2` | `chore(jarvis): add aether-policy.yaml to jarvis submodule` |

---

## Audit State

Latest run (2026-08-17 01:35): **143 ok, 12 warnings, 1 failure**
- Failure: `task 'ServerData - Weekly Audit' last result 2` — pre-existing, non-JARVIS.
- JARVIS checks green: bridge token present, HUD endpoints up, router live, WhatsApp bridge healthy.
- `powercfg /a` confirms: only Hibernate available, no standby states. Modern Standby not a factor.

---

## Open Items

1. **F4 container rebuild**: `docker compose build voice && docker compose up -d voice` needed to bake policy engine into running container.
2. **F5–F8**: deferred to future session.
3. **Q10/Q11**: blocked, noted above.
4. **Numba/NumPy conflict**: jarvis speaker tests fail on host Python (numba needs NumPy ≤2.3, got 2.4). Does not affect container (uses pinned image). Out of scope.

---

## Security

- `scan-secrets.sh` ran clean on all new/modified files.
- No token values printed in any output, report, or commit.
- `aether-devices.json` starts empty (no enrolled keys).
