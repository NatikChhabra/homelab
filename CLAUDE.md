# CLAUDE.md — ServerData Stacks

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
`Jellyseerr/`, `qBittorrent/`, `Vaultwarden/`, `Vikunja/`,
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
  bazarr, jellyfin, jellyseerr, warp. Largest, most frequently edited stack. (maintainerr and
  cleanuparr were REMOVED 2026-11-01: unconfigured and unauthenticated; do not re-add without auth.)
- `Stacks/homarr/` — dashboard (homarr).
- `Stacks/filebrowser/` — exposes `Sync/` over web UI.
- `Stacks/uptime-kuma/` — uptime monitoring.
- `Stacks/vaultwarden/` — password manager.
- `Stacks/vikunja/` — task manager.
- `Stacks/server-room/` — glances host-metrics container.
- `Stacks/adguard/` — AdGuard Home (DNS for every device; 53 tcp/udp, UI on 3080).
- `Stacks/immich/` — photos (owner's rule: do not connect to or poke Immich during audits).
- `Stacks/navidrome/` — music server (4533); `Stacks/octo/` — Octo proxy, slskd, yt-dlp-shim (see Octo below).
- `Stacks/actual/` — Actual budget; `Stacks/portainer/` — Portainer (9443).

Note (2026-11-03): AdGuard and Immich DO have compose files under `Stacks/` now. Traccar has a
data dir only and no stack.

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
bash ../docker-pull.sh <image> # pull a newer image (plain `docker compose pull` fails: section 6)
docker compose logs -f <svc>  # tail logs for one service
docker compose restart <svc>  # restart a single service after a config change
```

After editing `docker-compose.yml`, apply with `docker compose up -d` in that stack's dir.

**Working on this repo:**
- Each `docker-compose.yml` is source of truth for a service's ports, volumes, env vars.
- Ports in use (avoid collisions): 8095 (filebrowser), 7575 (homarr), 61208 (glances),
  8080/6881 (qbittorrent), 9696 (prowlarr), 8989 (sonarr), 7878 (radarr), 6767 (bazarr,
  login required), 8096 (jellyfin), 5055 (jellyseerr), 3001 (uptime-kuma), 3456 (vikunja),
  2283 (immich), 9443 (portainer), 4533 (navidrome), 53 tcp/udp + 3080 (adguard DNS + UI),
  22 (Windows OpenSSH server). Host-only since 2026-11-01:
  8222 (vaultwarden, reached via Tailscale serve HTTPS); 8191 flaresolverr has no host port.
  Host-only since 2026-11-02: 5006 (actual budget, Tailscale serve HTTPS on :5006; data
  C:\ServerData\Actual, in backup BIND_DIRS, Kuma monitor + Homarr tile).
  Octo (2026-11-02): 5274 Subsonic proxy for Feishin, 50300 slskd peer port, 5030 slskd UI
  host-only. octo/docker-compose.yml is UPSTREAM VERBATIM; local changes only in
  docker-compose.override.yml, settings in octo/.env (upstream names). Never trim upstream's
  env block (its defaults differ from Octo's built-ins). Data C:\ServerData\Octo, music
  D:\Media\music (whole library since 2026-11-02). Search needs songCount>=~10 (small counts = type-ahead, no outside).
  Discovery (2026-11-02): Explo RETIRED (archive/explo-retired-20261102). scripts/lb-discovery.py (task
  "ServerData - LB Discovery" 06:30 via run-job.ps1, Kuma push KUMA_PUSH_LB_DISCOVERY) uploads ListenBrainz
  Weekly Exploration to Octo Imports as "Discover Weekly" (keep playlist + fetch FLAC-first). Daily list =
  Octo "Your Radio". Octo holds the LB token. LibraryActions UpgradePerWeek is 0 (PAUSED 2026-11-03: 13 'upgrades'
  were different songs and were reverted from .octo-trash; Octo's DB still lists them as upgraded).
  Do not raise it again without a DryRun pass and an exclusion for those 13.
  Phone (2026-11-03): the owner uses the Octo Android app only (Symfonium uninstalled). It skips
  read-only smart playlists, so scripts/offline-playlists.py (task "ServerData - Offline Playlists",
  every 2 min, Kuma push KUMA_PUSH_OFFLINE_PLAYLISTS) keeps a plain "Everything" playlist equal to
  the library; the phone keeps it downloaded. Favourites download via Octo's Keep Liked setting.
- `homarr` SECRET_ENCRYPTION_KEY and `vikunja` VIKUNJA_SERVICE_PUBLICURL come from each stack's
  gitignored `.env` since 2026-11-01 (they used to be hardcoded in the compose files).
- Don't delete/rewrite files under app data directories — live application state.

**Session hygiene:**
- Use `/clear` for unrelated tasks; use `/compact` at natural breakpoints and steer it.
- Prefer targeted commands (`docker logs --tail 50 <svc>`, `docker inspect --format`, grep)
  over dumping full files or full output.
- Don't re-read files already read earlier in the same session unless they've changed on disk.

---

## 1. EVIDENCE — THE RULES THAT MATTER MOST

1. **"Done" requires pasted command output.** Never mark something done that you did not run
   and verify.
2. **Never close a gate on verbal confirmation.** Gates close on evidence only.
3. **Never mark an item done that you skipped.** Say you skipped it, and say why.
4. **A test that doesn't test the thing is worse than no test.** Always verify the *outcome*,
   not the dispatch.
5. **Contradictions are P0.** If two of your own numbers disagree, stop and reconcile before
   any further work. Do not average them. Do not pick the nicer one.
6. **One report per phase.** If you revise a finding, *edit* the report. Never append a second
   contradictory version. This has happened four times and each time cost a session working
   out which version was true.
7. **Say "I don't know."** An honest gap costs an hour. A confident wrong answer costs days.
8. **Report the bad news first**, in the opening paragraph, not buried in section nine.

---

## 2. SECRETS — BROKEN FOUR TIMES, NEVER AGAIN

- **Never print a secret, in whole or in part, for any reason** — not to confirm it, not to
  say it's redacted, not the first and last eight characters, not hashed "for convenience".
- Report a leak as a **location plus a short hash** (`sha256:a3f1...`), never a value.
- Verify a secret exists **by count**: `grep -c "^KEY=" file` must return `1`.
- Run `scan-secrets.sh` before writing **any** report.
- Reports: the OWNER'S standing rule wins (2026-10-25, restated 2026-11-03): publish the report as a
  claude.ai Artifact AND put a copy in `C:/ServerData/Sync/` (Filebrowser :8095, login required,
  LAN/Tailscale only). Because that folder is HTTP-served, run `scripts/scan-secrets.sh` on the
  report first and never put a secret, key, token or session value in it.
- Secrets live only in `secrets.env`: gitignored, ACL-restricted to owner/SYSTEM/Administrators.
- Never commit a `.env`. Verify with
  `git ls-files | grep -iE 'secret|\.env' | grep -vE '\.env\.example$|scan-secrets\.sh$'` → empty.
  (The old form without -E treated `|` literally and could never fail.)

---

## 3. GIT — FORBIDDEN, NO EXCEPTIONS

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

## 4. PERSISTENCE

**No `docker cp` for anything that must survive a rebuild.** Bind-mount it, or bake it into
the image. A `docker cp`'d file vanishes on the next rebuild, silently.

Anything gitignored is one bad command from gone. If you create or restore something in a
gitignored path, force-add it in the same breath.

---

## 5. BEHAVIOUR RULES

- **Optimistic behaviour is banned.** Nothing reports success — logged or on screen — before
  the system confirms it.
- **Never invent** an entity ID, script name, port, path, or token. Ask.
- **Never imply certainty you don't have.**
- **Tailscale only.** Never expose a port to the public internet.

---

## 6. HARDWARE FACTS — DO NOT REDISCOVER THESE

- Docker Desktop requires an **interactive logon** before it starts.
- **Session 0 cannot see the desktop.** Anything that needs the desktop must run in the
  interactive session.
- The **docker credential helper is broken** — use `docker-pull.sh`, never a plain
  `docker pull`.
- **Modern Standby** has caused ~4h43m outages. It is the largest single source of downtime
  (see `keep-awake.ps1` and audit section 20).

---

## 7. WORKING STYLE

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
- **Read once, cache what you learned.** Don't re-read a file to "check". Batch related shell
  commands into one call. Don't re-run a passing suite to confirm it still passes. Pipe long
  output to a file and grep it rather than dumping it into context.

### Autonomy boundary

**GREEN — do without asking:** read · measure · benchmark · write tests · write scripts · add
audit checks · add logging · fix your own code · commit your own work · update docs · restart
containers · rebuild images.

**RED — never do; queue as a decision in the report:** delete or rename files · rotate
credentials · remove a test to make a suite green · reboot the machine.

**If unsure which colour it is, it is RED — and you still skip and continue.**

---

## 8. ORCHESTRATION

**Parallelise** — spawn subagents for work that touches nothing shared: benchmarks · log
analysis · documentation research · test investigation (one subagent per failing file) · audit
re-runs · backup restore into a temp dir · read-only reports.

Each subagent gets exactly four things: **one objective · a time-box · the precise acceptance
criterion · "return findings only."** No subagent commits, restarts a container, or edits
shared config. Ever.

**Never parallelise:**
- **git operations of any kind** — two agents committing is how history gets mangled
- **`docker compose` up/down/build** — concurrent restarts corrupt state
- any edit to `docker-compose.yml` or `secrets.env`
- anything writing to the same file

**You own every decision and every commit.** A subagent that says "done" without pasted output
is exactly as untrustworthy as you are.

**Don't delegate** work under five minutes, work needing context only you have, or anything
that mutates what another agent reads.

---

## 9. TOOLING

Plugins are for capability; **this file is for conventions**. Never expect a plugin to know
our rules.

Installed and expected:
- `security-guidance` — flags leaked secrets and unsafe shell calls at edit time. Given our
  history, treat its warnings as blocking.
- `code-review` — run against every non-trivial diff before committing.
- `commit-commands` — one task per commit, enforced.
- `ponytail` — YAGNI enforcement. When it and a work order disagree, **the work order wins** —
  its constraints are load-bearing.

Any plugin with hooks runs with full user privileges. A community memory plugin once blocked
533 consecutive prompts by failing closed. If a hook misbehaves, disable it and report; never
work around it silently.

---

## 10. THE QUALITY BAR FOR EVERY REPORT

1. **Bad news first**, in the opening paragraph.
2. A table: item | status | evidence | commit.
3. **Every BLOCKED item with a one-line reason**, and whether you retried.
4. **Every RED item as a specific question**, with your recommendation and the trade-off —
   answerable over coffee, not requiring investigation.
5. Current numbers against the last known ones (audit failures/warnings).
6. Anything that surprised you or contradicts this file.
7. What you would do next, and why.

Delivered as section 2 says (Artifact + scanned copy in Sync). Scanned for secrets. One report, edited, never appended.

---

## 11. WHEN TO STOP AND ASK

Stop for: a RED action · a security finding · a
contradiction you cannot resolve · a decision with no reversible option.

Do **not** stop for: a blocked item (skip it) · a question you can answer by reading the code ·
permission to do something GREEN · confirmation that you understood.
