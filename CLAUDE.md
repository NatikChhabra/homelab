# CLAUDE.md — Aether / JARVIS

Read this fully before any work. Every rule here exists because it was broken once and cost
real time. This file is not advice; it is the operating contract.

---

## 0. SERVER STACK OPERATIONAL NOTES

**Run the server audit at the start of every session:**

```bash
bash /c/ServerData/Stacks/server-audit.sh
```

It takes about a minute, is strictly read-only, and reports on containers, ports, the media mount,
disk, hardlinks, BitTorrent transport, DNS/TMDb, app health, library consistency, the scheduled
jobs, the dub quality profile, and indexers. Report anything that comes back WARN or FAIL to the
user, then fix it. If it is all clear, say so in one line and move on.

The same script runs unattended every Sunday at 05:00 as the "ServerData - Weekly Audit" scheduled
task, writing `Stacks/server-audit-report.txt`. If a session starts and that report is recent, read
it rather than re-running from scratch.

Every check in that script exists because the thing it looks for actually broke once and went
unnoticed. When adding a check, add a comment naming the incident. Do not delete checks to make the
output shorter.

**What this directory is:** `C:\ServerData` is a persistent data/config root for a Windows 11
home server running self-hosted apps via Docker Desktop. No app code to build/lint/test here.
Work = editing Docker Compose stacks, app config files, managing container/volume state.

Each top-level folder (`AdGuard/`, `Jellyfin/`, `Radarr/`, `Sonarr/`, `Bazarr/`, `Prowlarr/`,
`Jellyseerr/`, `Maintainerr/`, `Cleanuparr/`, `qBittorrent/`, `Vaultwarden/`, `Vikunja/`,
`Immich/`, `Traccar/`): bind-mounted config/data dir for the matching container — runtime state
(SQLite/Postgres DBs, YAML configs, logs, media libraries), not code. Leave alone unless debugging
that service.

**`D:\Media`** (NOT under `C:\ServerData` — moved 2026-07-28): media library
(`Media/library/{movies,tv,anime}`), torrent download/staging area
(`Media/torrents/{complete,incomplete}`) and `Media/recyclebin`, shared across *arr apps +
qBittorrent, all mounted as `/data`. D: is a 450GB HDD; C: is the NVMe SSD and keeps only app
databases. `library/` and `torrents/` MUST stay on the same filesystem or Sonarr's hardlinking
breaks and every import silently doubles disk use.

`D:\Backups`: nightly volume/config archives, 14-day retention. Deliberately on a different
physical disk from the live data.

`Sync/`: unrelated personal file sync storage (exposed via Filebrowser), not part of media stack.

**Stack definitions** — all Docker Compose files under `Stacks/<stack-name>/docker-compose.yml`:

- `Stacks/streaming/` — main media stack: qbittorrent, prowlarr, flaresolverr, sonarr, radarr,
  bazarr, jellyfin, jellyseerr, maintainerr, cleanuparr. Largest, most frequently edited stack.
- `Stacks/homarr/` — dashboard (homarr).
- `Stacks/filebrowser/` — exposes `Sync/` over web UI.
- `Stacks/uptime-kuma/` — uptime monitoring.
- `Stacks/vaultwarden/` — password manager.
- `Stacks/vikunja/` — task manager.
- `Stacks/server-room/` — glances host-metrics container.

Note: AdGuard, Immich, Traccar have data dirs at top level but **no** `docker-compose.yml` under
`Stacks/` — provisioned/run elsewhere. Don't assume a compose file exists for them without checking.

Config conventions:
- Containers use `PUID=1000`, `PGID=1000`, `TZ=Asia/Kolkata` where applicable (linuxserver.io images).
- The streaming stack keeps app config in **external named volumes** (`sonarr_config`,
  `jellyfin_config`, `qbittorrent_config`, ...), declared `external: true`. Only the media tree is a
  bind mount, and it now comes from `D:/Media`.
- `restart: always` standard on every service.
- The *arr services and Jellyfin all set `dns: 1.1.1.1 / 8.8.8.8` plus
  `net.ipv6.conf.all.disable_ipv6=1`. This is required: the ISP's resolver hijacks
  `api.themoviedb.org`, so a container without it silently loses all metadata.
- Credentials for the automation scripts live in `Stacks/secrets.env` (ACL-restricted). Never
  hardcode an API key or password in a script — they get swept into the nightly backup.

**Common commands** (run from inside the relevant `Stacks/<stack-name>/` directory in PowerShell):

```powershell
docker compose up -d          # start/update the stack
docker compose down           # stop and remove containers (volumes persist)
docker compose pull           # pull latest images
docker compose logs -f <svc>  # tail logs for one service
docker compose restart <svc>  # restart a single service after a config change
```

After editing `docker-compose.yml`, apply with `docker compose up -d` in that stack's dir.

**Working on this repo:**
- Each `docker-compose.yml` is source of truth for a service's ports, volumes, env vars.
- Ports in use (avoid collisions): 8095 (filebrowser), 7575 (homarr), 61208 (glances),
  8080/6881 (qbittorrent), 9696 (prowlarr), 8191 (flaresolverr), 8989 (sonarr),
  7878 (radarr), 6767 (bazarr), 8096 (jellyfin), 5055 (jellyseerr), 6246 (maintainerr),
  11011 (cleanuparr), 3001 (uptime-kuma), 8222 (vaultwarden), 3456 (vikunja),
  2283 (immich), 9443 (portainer).
- `homarr` compose has hardcoded `SECRET_ENCRYPTION_KEY`; `vikunja` has placeholder
  `YOUR-LAPTOP-IP` in `VIKUNJA_SERVICE_PUBLICURL` — environment-specific, not templates.
- Don't delete/rewrite files under app data directories — live application state.

**Session hygiene:**
- Use `/clear` for unrelated tasks; use `/compact` at natural breakpoints and steer it.
- Prefer targeted commands (`docker logs --tail 50 <svc>`, `docker inspect --format`, grep)
  over dumping full files or full output.
- Don't re-read files already read earlier in the same session unless they've changed on disk.

---

## 1. WHAT THIS IS

**JARVIS** — a local-first voice assistant on a Windows 11 home server. Always listening for
"hey jarvis". Controls lights, fan, AC and TV via Home Assistant. Drives the PC. Answers
questions with a local LLM.

**Aether** — the permission layer being built around it. The differentiator: every action is
bound to *who* asked, *which device* they asked from, and *what that device is allowed to do*.

**Owner: Natik.** "Sir" is how JARVIS addresses him in spoken replies — not how you address
him in chat. Persona: British butler, replies of twelve words or fewer.

---

## 2. THE PRIME DIRECTIVE

**Never break the voice loop.** mic → wake → STT → router → TTS → speaker is sacred.

UI, HUD, docs, packs, tests and the permission layer are all additive and isolated **on
purpose**. If a change *could* touch the loop, stop and ask before making it.

---

## 3. EVIDENCE — THE RULES THAT MATTER MOST

1. **"Done" requires pasted command output.** Never mark something done that you did not run
   and verify.
2. **Never close a gate on Natik's verbal confirmation.** Gates close on evidence only. This
   was violated on the most important claim in the project — that lights actuate.
3. **Never mark an item done that you skipped.** Say you skipped it, and say why.
4. **A test that doesn't test the thing is worse than no test.** A "20/20 actuation test" once
   checked `res.path in PASS_PATHS` — no service call, no state read-back. It proved routing
   and nothing else. Always verify the *outcome*, not the dispatch.
5. **Contradictions are P0.** If two of your own numbers disagree, stop and reconcile before
   any further work. Do not average them. Do not pick the nicer one.
6. **One report per phase.** If you revise a finding, *edit* the report. Never append a second
   contradictory version. This has happened four times and each time cost a session working
   out which version was true.
7. **Say "I don't know."** An honest gap costs an hour. A confident wrong answer costs days.
8. **Report the bad news first**, in the opening paragraph, not buried in section nine.

---

## 4. SECRETS — BROKEN FOUR TIMES, NEVER AGAIN

- **Never print a secret, in whole or in part, for any reason** — not to confirm it, not to
  say it's redacted, not the first and last eight characters, not hashed "for convenience".
- Report a leak as a **location plus a short hash** (`sha256:a3f1...`), never a value.
- Verify a secret exists **by count**: `grep -c "^KEY=" file` must return `1`.
- Run `scan-secrets.sh` before writing **any** report.
- Reports go to **repo root**. Never `C:/ServerData/Sync/` — it is HTTP-served on port 8095.
- Secrets live only in `secrets.env`: gitignored, ACL-restricted to owner/SYSTEM/Administrators.
- Never commit a `.env`. Verify with `git ls-files | grep -i "secret|\.env"` → empty.

---

## 5. GIT — FORBIDDEN, NO EXCEPTIONS

`git checkout <path>` · `git restore` · `git reset --hard` · `git clean` · `git stash drop` ·
**any `>` or `>>` redirect into a source file.**

A stray redirect once destroyed `ui/index.html`. It was gitignored, so there was no history
and nothing to recover. That single incident is why half these rules exist.

- Back up to `<file>.bak.<UTC-timestamp>` before any overwrite.
- Ask before deleting, renaming or moving anything.
- **One task per commit**, conventional message (`feat:`/`fix:`/`chore:`/`docs:`/`test:`).
- `.gitignore` includes `*.json`, `*.yaml`, `__pycache__/`, `*.pyc` — so `registry.json`,
  every `manifest.yaml`, and all config are **untracked by default**. Anything that matters
  needs `git add -f`.

---

## 6. PERSISTENCE

**No `docker cp` for anything that must survive a rebuild.** Bind-mount it, or bake it into
the image. A `docker cp` of the HA custom sentences vanished on the next rebuild and caused a
60% routing collapse that nobody noticed for days.

Anything gitignored is one bad command from gone. If you create or restore something in a
gitignored path, force-add it in the same breath.

---

## 7. BEHAVIOUR RULES

- **Optimistic behaviour is banned.** Nothing reports success — spoken, logged, or on screen —
  before the system confirms it. Every actuation passes through a pending state.
- **Latency is the product.** Declare the path for every change: local (<200 ms, no LLM) or
  Hermes (~1.7 s warm). Adding an LLM call where a regex works is a regression, not a feature.
- **Never invent** an entity ID, script name, port, path, or token. Ask.
- **Never imply certainty you don't have.** If a device gives no feedback, say so.
- **Don't "fix" `/api/vitals` disk** — always `available:false` because Docker Desktop exposes
  drives as 9p mounts. Documented platform limitation.
- **Tailscale only.** Never expose a port to the public internet.
- Never change the TTS voice blend, speed 0.92, the beep set, or the ≤12-word persona without
  asking.

---

## 8. HARDWARE FACTS — DO NOT REDISCOVER THESE

- **RTX 3050 Laptop, 4096 MiB VRAM, ~1500 MiB free.** `qwen2.5:3b` needs ~2770 MiB. Whisper
  CUDA competes for the same card.
- **WDDM reclaims VRAM regardless of `OLLAMA_KEEP_ALIVE=-1`.** This causes 8–16 s cold starts
  and 6.4 s STT outliers. It is a Windows platform limitation, not a config error, and it
  cannot be fully solved on this hardware.
- Docker Desktop requires an **interactive logon** before it starts.
- **Session 0 cannot see the desktop.** `pc-agent.ps1` must run in the interactive session.
- The **docker credential helper is broken** — use `docker-pull.sh`, never a plain
  `docker pull`.
- **Modern Standby** has caused ~4h43m outages. It is the largest single source of downtime
  and the real cause of the old "ears.py dies after 40 s" mystery.

---

## 9. DEVICE FACTS — DO NOT REDISCOVER THESE EITHER

- **Live light entities are `switch.3_1_node_smart_switch_switch_1/2/3`.** They are
  **`switch.` domain, not `light.`** — `light.turn_on` silently does nothing to them. Use
  `homeassistant.turn_on` / `homeassistant.turn_off`, which work across domains.
- `light.node_switch_gang_*`, `switch.node_switch_node_4`, `switch.node_switch_main_switch`
  and `light.node_switch_backlight` are **stale `unavailable` ghosts** from a profile that was
  never loaded. Never target them.
- **Fan, AC and TV are fire-and-forget IR with no state feedback.** "Verification" there means
  the command dispatched and the HA helper values updated — **not** that the physical device
  changed. Say this plainly every time; never imply otherwise.
- **The Zoook IR blaster needs DP1 = `study_key` with a RAW base64 code.** tuya-local's
  `remote.py` writes `send_ir`, which the hardware **accepts and silently discards**. IR can
  look like it works and do absolutely nothing.
- AZIOT switch: `192.168.29.12`, protocol 3.5. Zoook blaster: `192.168.29.236`, protocol 3.3.
  Both held by DHCP reservation.

---

## 10. ARCHITECTURE RULE FOR ANYTHING NEW

**Aether never talks to a device brand, protocol, or IP again.** It talks to **Home
Assistant**, and HA handles device diversity — that is precisely what it exists for, across
thousands of integrations.

Same principle twice more: **Ollama** absorbs model diversity, **Wyoming** absorbs speech
engine diversity. Our job is to stop hardcoding and let the existing ecosystems carry the
variety. Discovery over declaration, always.

---

## 11. WORKING STYLE

- **SKIP, NEVER STOP.** Blocked → write one line saying why, mark it BLOCKED, move to the next
  item **immediately**. Do not wait. Do not ask. Do not end your turn. Retry blocked items once
  at the end.
- Only three things end a turn: the queue is exhausted · a RED action you genuinely cannot
  proceed without · something is actively destroying data.
- **Time-box everything.** Over the box → record where you got to, move on. Do not grind. Four
  hours of silence is worse than an honest "stalled here".
- **Show the diff** before applying anything over ~50 lines.
- **State a rollback in one line** for every change.
- **No scope drift.** Never move to more interesting adjacent work while the core is broken.
  An investor demo once got built while voice didn't work at all.
- **Read once, cache what you learned.** Don't re-read a file to "check". Batch related shell
  commands into one call. Don't re-run a passing suite to confirm it still passes. Pipe long
  output to a file and grep it rather than dumping it into context.

### Autonomy boundary

**GREEN — do without asking:** read · measure · benchmark · write tests · write scripts · add
audit checks · add logging · fix your own code · commit your own work · update docs · restart
containers · rebuild images · edit HA config · create HA areas · call HA services.

**RED — never do; queue as a decision in the report:** delete or rename files · rotate
credentials · change TTS voice/speed/beeps/persona · change the wake word or drop its
threshold below 0.25 · modify the `ears.py`/`speaker.py` core loop beyond instrumentation ·
send a message or place a call outside the house · remove a test to make a suite green ·
reboot the machine.

**If unsure which colour it is, it is RED — and you still skip and continue.**

---

## 12. ORCHESTRATION

**Parallelise** — spawn subagents for work that touches nothing shared: benchmarks · log
analysis · documentation research · test investigation (one subagent per failing file) · audit
re-runs · backup restore into a temp dir · read-only reports.

Each subagent gets exactly four things: **one objective · a time-box · the precise acceptance
criterion · "return findings only."** No subagent commits, restarts a container, or edits
shared config. Ever.

**Never parallelise:**
- **git operations of any kind** — two agents committing is how history gets mangled
- **`docker compose` up/down/build** — concurrent restarts corrupt state
- any edit to `docker-compose.yml`, `secrets.env`, or HA config
- **physical device tests — there is ONE light.** Two agents toggling it produces results
  nobody can interpret
- **voice pipeline tests — one microphone, one speaker**
- anything writing to the same file

**You own every decision and every commit.** A subagent that says "done" without pasted output
is exactly as untrustworthy as you are.

**Don't delegate** work under five minutes, work needing context only you have, or anything
that mutates what another agent reads.

---

## 13. TOOLING

Plugins are for capability; **this file is for conventions**. Never expect a plugin to know
our rules.

Installed and expected:
- `security-guidance` — flags leaked secrets and unsafe shell calls at edit time. Given our
  history, treat its warnings as blocking.
- `python-lsp` — type awareness across `router.py`, `ears.py`, `executor.py`, `phrasing.py`.
- `code-review` — run against every diff touching the voice loop before committing.
- `commit-commands` — one task per commit, enforced.
- `ponytail` — YAGNI enforcement. **Highly relevant here:** this codebase has real
  over-engineering debt (dead code in `executor.py`, an inert `_verify_all()` closure, a
  duplicated deny-list, seven unusable UI packages). Run `/ponytail-audit` on both repos
  periodically. When it and a work order disagree, **the work order wins** — its constraints
  are load-bearing.

Any plugin with hooks runs with full user privileges. A community memory plugin once blocked
533 consecutive prompts by failing closed. If a hook misbehaves, disable it and report; never
work around it silently.

---

## 14. THE QUALITY BAR FOR EVERY REPORT

1. **Bad news first**, in the opening paragraph.
2. A table: item | status | evidence | commit.
3. **Every BLOCKED item with a one-line reason**, and whether you retried.
4. **Every RED item as a specific question**, with your recommendation and the trade-off —
   answerable over coffee, not requiring investigation.
5. Current numbers against the last known ones: wake detection rate, false wakes/hour, local
   p50, Hermes p50, actuation success rate, audit failures.
6. Anything that surprised you or contradicts this file.
7. What you would do next, and why.

Written to repo root. Scanned for secrets. One report, edited — never appended.

---

## 15. WHEN TO STOP AND ASK

Stop for: a RED action · a security finding · anything that would touch the voice loop · a
contradiction you cannot resolve · a decision with no reversible option.

Do **not** stop for: a blocked item (skip it) · a question you can answer by reading the code ·
permission to do something GREEN · confirmation that you understood.
