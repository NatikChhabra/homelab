# AETHER OPERATING RULES (auto-loaded. Do not bypass. Full context: ask user for latest master prompt.)
1. Numbers first. Claims need command evidence; chat signals are not evidence.
2. Secrets: name-only checks. findstr /c:"NAME=" | find /c /v "" must equal 1. Never print values.
3. Deploys ONLY: docker compose up -d --build jarvis-voice THEN docker compose up -d jarvis-hass. Never bare restart. Locked.
4. One commit per item. git push is HELD until the step3 battery passes.
5. Dangerous verbs keep spoken confirm. Thresholds/enforce flags live in config, never code.
6. Local-only: new code declares egress:none and passes a sockets-blocked contract test.
7. Close every report: LIVE / STAGED / BLOCKED. Unchecked = "UNKNOWN — not checked".
STATE: bridge GATE = JARVIS_BRIDGE_TOKEN in both secrets.env (PROJECT.md). Speaker lane shadow-only.
Battery: step3-battery.md. Verify: bridge-verify.ps1. Wiring: pipeline-wiring.md.

## SESSION START PROTOCOL (before any task, every session, no exceptions)
1. If the SESSION PROBE hook printed a block: read it. If BRIDGE_TOKEN counts
   flipped 0 -> 1 in both files, announce "GATE G0 OPENED" immediately.
2. Read PROJECT.md (and RECOVERY.md in aether). Skim; do not paste contents.
3. Run git status --short. Unexpected modifications vs the task at hand = say so
   BEFORE touching anything.
4. State the lowest open gate — order: G0 token -> G1 two-step deploy + bridge
   battery -> L0 hygiene -> L1 speaker -> L2 clicky-parity -> L3 ui -> L4 brain ->
   L5 packs — then work on it unless the user directed a different task.
5. Startup work is read-only and <30s total. No deploys, restarts, installs, or
   writes during startup. Never.
