# PROJECT.md — Aether Bridge, current state

**Last updated:** 2026-08-13 18:47 IST
**Rollback in one line:** `docker compose -f Stacks/jarvis/docker-compose.yml up -d` (drops the env_file change if needed; rebuild drops the bridge image)

---

## What works (live)

| Item | Evidence |
|------|----------|
| `jarvis-voice` container | Running healthy on port3100, built2026-08-13T15:04:10+0530 IST |
| `router.py` guest-tier enforcement | `GUEST_DENY_LIST` + tier param + enforcement block before executor (commit `29283d2`) |
| `voice_server.py` `/turn` endpoint | `async def turn()`, `BRIDGE_TOKEN` read from env, route registered (commit `29283d2`) |
| `ha-config/custom_components/aether_bridge/` | `__init__.py`, `const.py`, `manifest.json`, `conversation.py` (commit `29283d2`) |
| `.gitignore` exception for bridge manifest | `!ha-config/custom_components/aether_bridge/manifest.json` (commit `29283d2`) |
| `jarvis-hass` env_file for bridge token | Added commit `6a9e504` — HA now reads `JARVIS_BRIDGE_TOKEN` from `secrets.env` |
| Git history secret scan | CLEAN — 68 remote + 55 unpushed scanned; fixtures-only matches; `secrets.env` gitignored |
| Orphaned volume `jarvis_hass_config` | CLOSED — does not exist |

---

## What is NOT yet live

| Item | Blocker |
|------|---------|
| `JARVIS_BRIDGE_TOKEN` in `secrets.env` | **User action in progress** — HA user `aether_bridge` created, token minted, being typed into file; first "token is in" was FALSE FIRE (unsaved editor) — retrying with local presence check |
| `jarvis-voice` rebuilt with bridge code | Stale image (built09:34 UTC) predates commits `29283d2`; needs `up -d --build jarvis-voice` |
| `jarvis-hass` recreated with env_file + bind mount | Compose changes committed (`6a9e504`, `6a9d8de`) but container not yet restarted |
| Step 3 acceptance battery | Blocked on token + rebuild |
| Voice PE → `/turn` → kernel pipeline | Unverified end-to-end |

---

## Commits on this branch (unpushed)

| Commit | Subject |
|--------|---------|
| `6a9e504` | fix(compose): add env_file to home-assistant for JARVIS_BRIDGE_TOKEN |
| `6a9d8de` | fix(compose): bind-mount custom_components into home-assistant for aether_bridge |
| `99b49f2` | docs: add PROJECT.md with bridge state, debt ledger, deploy command, verification set |
| `29283d2` | feat(bridge): add aether_bridge HA integration and guest-tier enforcement |
| `2e1c435` | feat(ears): add virtual-device detection warning; refresh update-audit-staged.sh |
| `b75e202` | feat(a6): add per-actuation logging and fix audit detection |
| (older unpushed commits…) | — |

`aether` kernel repo: commit `cc632c7` (same bridge code, relative imports, no remote).

---

## Debt

- **env_file secret surface:** Both `jarvis-voice` and `jarvis-hass` load the full `../secrets.env` + `./secrets.env`. HA now also carries `JARVIS_WA_TOKEN`, `JARVIS_WOL_TOKEN`, `QB_PASS`, etc. — all readable from inside the container. **Cleanup path:** compose-level single-var passthrough (`environment: JARVIS_BRIDGE_TOKEN: ${JARVIS_BRIDGE_TOKEN}`) once the file is split into scoped secrets. Not a blocker today.
- **Token rotation:** Rotating `JARVIS_BRIDGE_TOKEN` requires `docker compose up -d jarvis-hass` to re-read env. No hot-reload path exists.
- **WA token parity:** Old WA token (`11ab9e92`) is still in the running `jarvis-voice` image. The rebuild picks up the new one (`884639b1`).

---

## Deploy command (locked, do not simplify)

```bash
docker compose -f Stacks/jarvis/docker-compose.yml up -d --build jarvis-voice
docker compose -f Stacks/jarvis/docker-compose.yml up -d jarvis-hass
```

Two explicit steps. The rebuild only touches `jarvis-voice`; the second line forces `jarvis-hass` to recreate so it picks up the new `env_file`.

---

## Verification set (status codes and function only — token never printed)

After deploy:

1. **Image provenance:** `docker image inspect jarvis-voice:latest --format '{{.Created}}'` → new timestamp; `docker inspect --format '{{index .Config.Env}}'` → contains bridge functions
2. **Unauthenticated /turn:** `curl -s -o /dev/null -w '%{http_code}' http://localhost:3100/turn -X POST -H 'Content-Type: application/json' -d '{"text":"hello","tier":"guest"}'` → expected `401` (flip from stale404 is itself evidence)
3. **Restricted token →200 on house service:** `curl -s -o /dev/null -w '%{http_code}' http://localhost:3100/turn -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{"text":"turn on the living room light","tier":"guest"}'` → expected `200`
4. **Restricted token →401 on ai_task.***: Same call with `{"text":"generate data","tier":"guest"}` → expected `401`
5. **WA token parity:** Functional call to WA status endpoint from inside the container returns `200`-class (prefix `884639b1` confirmed in container env, full value never printed)
6. **BRIDGE_TOKEN provisioned:** `voice_server.py` line67 reads `os.getenv("JARVIS_BRIDGE_TOKEN", "")` — confirmed populated in container env
7. **aether_bridge loaded in HA:** `docker exec jarvis-hass journalctl ...` or HA logs show `"Aether Bridge registered as conversation.aether_bridge"` with no `"JARVIS_BRIDGE_TOKEN not set"` warning

---

## Next action

Say **"token is in"** to trigger the deploy and verification set above, then the Step 3 acceptance battery.
