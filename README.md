# Homelab

Docker Compose stacks and automation for a Windows-hosted home server: media
library, photos, passwords, tasks, dashboards, and the scheduled jobs that keep
them honest.

This repository is the *configuration* for that server. It deliberately contains
no application data, no databases, and no credentials.

Published as a reference / portfolio piece, not a turnkey installer. It's my
real setup, so paths, IPs and ports below are mine — see **Getting started**
for what to change before any of this runs on your own hardware. Licensed MIT
(see `LICENSE`): copy whatever is useful.

**Why look at this repo:**
- A worked example of a multi-service Docker Compose homelab: the *arr stack,
  Jellyfin, Immich, Vikunja, Vaultwarden, Uptime Kuma, Homarr, wired together
  with per-stack networks and cross-stack status checks.
- An allowlist `.gitignore` + pre-commit secret scanner (`.githooks/pre-commit`)
  for a repo that mixes real infrastructure config with public visibility.
- A read-only health-audit script (`server-audit.sh`) that encodes real past
  incidents as checks, each with a comment naming what broke.
- Documented Docker-Desktop-on-Windows/WSL2 quirks (port publishing, DNS
  binding) that cost real debugging time — see **Networking** below.

---

## What runs here

| Stack | Services | Purpose |
|---|---|---|
| `streaming` | qbittorrent, prowlarr, flaresolverr, sonarr, radarr, bazarr, jellyfin, jellyseerr, maintainerr, cleanuparr | Media acquisition, organisation and playback. The largest and most frequently edited stack. |
| `adguard` | adguardhome | Network-wide DNS filtering. |
| `homarr` | homarr | Dashboard and single entry point to everything else. |
| `uptime-kuma` | uptime-kuma | Uptime monitoring, including push monitors for the scheduled jobs. |
| `vaultwarden` | vaultwarden | Password manager. |
| `vikunja` | vikunja | Task manager. |
| `filebrowser` | filebrowser | Web access to a personal file-sync directory. |
| `server-room` | glances | Host metrics. |
| `immich` | immich-server, machine-learning, postgres, valkey | Photo library. |
| `portainer` | portainer | Container management UI. |
| `navidrome` | navidrome | Self-hosted music streaming (Subsonic API). |
| `jarvis` | home-assistant, ollama, whisper, piper, openwakeword, voice bridge | Local voice assistant. Speech recognition, language model and speech synthesis all run on this box; no cloud speech API is involved. |

Immich and Portainer were brought into this tree on 2026-07-28. Immich's compose
file had been living in a Docker Desktop AI-agent scratch directory that Docker
is free to delete, and Portainer had no compose file at all — it existed only as
a running container created by hand. Every running container is now defined by a
file here, and the audit fails if one is not.

Traccar still runs outside this tree.

## Ports

Kept in one place because the cost of a collision is a service that silently
fails to bind.

```
   53  adguard (DNS)      6881  qbittorrent (peer)   9443  portainer
 2283  immich             7575  homarr               9696  prowlarr
 3001  uptime-kuma        7878  radarr              10200  jarvis (piper TTS)
 3080  adguard (web UI)   8080  qbittorrent (web)   10300  jarvis (whisper STT)
 3100  jarvis (voice UI)  8095  filebrowser         10400  jarvis (wake word)
 3456  vikunja            8096  jellyfin            11011  cleanuparr
 4533  navidrome          8123  jarvis (home asst)  11434  jarvis (ollama)
 5055  jellyseerr         8191  flaresolverr        61208  glances
 6246  maintainerr        8222  vaultwarden
 6767  bazarr             8989  sonarr
```

## Storage layout

Two physical disks, on purpose.

- **C:** (NVMe SSD) — application databases and config only. Fast, small.
- **D:** (450 GB HDD) — the media library, the torrent download and staging
  area, and the recycle bin. Also the nightly backup archives.

The library and the torrent staging directory **must** stay on the same
filesystem. If they are split, hardlinking breaks and every import silently
doubles disk usage instead of costing nothing.

Backups live on a different physical disk from the live data, which is the only
reason they are worth anything.

## Conventions

- Containers run as `PUID=1000` / `PGID=1000` with a fixed timezone.
- Application config lives in **external named volumes**, not bind mounts. Only
  the media tree is a bind mount. This keeps app state off the media disk and
  makes the volumes independently backup-able.
- `restart: always` on every service.
- Compose creates a per-stack default network and services resolve each other by
  container name. There are no hand-written networks.
- The media services pin their own DNS resolvers and disable IPv6. This is
  required, not decorative: the ISP resolver hijacks the metadata provider's
  hostname, and a container without the override silently loses all metadata
  while appearing perfectly healthy.
- Dashboard and monitoring containers join *every* stack network. On Docker
  Desktop for Windows a container cannot reach the host's published ports, so
  status checks must target container names rather than the host address.

## Secrets

Credentials live in `Stacks/secrets.env`, which is ACL-restricted and excluded
from version control. `homarr/.env` holds the key that decrypts Homarr's stored
integration credentials and is likewise excluded. Templates ending in
`.example` show the required keys.

Nothing else in this repository should ever contain a key, token or password.
`.gitignore` is written as an **allowlist** — everything is ignored and specific
files are added back — because a denylist fails open, and the first file type
nobody thought to exclude is the one that leaks.

To rotate a credential, change it in `secrets.env` only. Nothing else needs
editing.

## Getting started

This is a real, running server's config, adapted for public reading rather than
built as a generic installer. Expect to read and edit before running.

**Prerequisites:** Docker Desktop with WSL2 backend on Windows. Most of this
adapts to plain Docker on Linux — drop the Windows-specific pieces
(`PUID`/`PGID`, `C:` drive-letter bind mounts, `.wslconfig`, the Task Scheduler
jobs, `docker-pull.sh`) and it's a standard Compose tree.

1. Clone the repo.
2. `git config core.hooksPath .githooks` — enables the pre-commit scan that
   blocks committing a credential. See **Secrets** above for why this exists.
3. For each stack that ships a `.env.example` (currently `homarr/`, `immich/`,
   `vikunja/`), copy it to `.env` in that directory and fill in real values.
4. Create `secrets.env` in the repo root for the automation scripts (API keys,
   Uptime Kuma push tokens, qBittorrent credentials — grep the scripts for
   `SECRETS_FILE` / `$Secrets[` to see every key they expect). Nothing in this
   repo ships with real credentials in it.
5. Replace the paths (`C:/ServerData/...`, `D:/Media/...`) and any Tailscale/LAN
   IPs in the compose files and scripts with your own. `grep -rn` for
   `C:/ServerData` and `100\.` across the tree will find what's left.
6. From inside a stack directory: `docker compose up -d`.
7. `bash server-audit.sh` to sanity-check what came up. Several checks assume
   pieces of my setup (the *arr apps, a specific media layout) and will WARN
   or FAIL harmlessly if you haven't brought those stacks up — read the check
   name before treating it as a real problem.

## Automation

Scheduled through Windows Task Scheduler, each wrapped by `run-job.ps1`, which
captures a per-run transcript, writes a status file, pings an Uptime Kuma push
monitor, logs failures to the Windows event log, and refuses to start if the
previous run is still going.

| Job | Cadence | What it does |
|---|---|---|
| Rolling Window | every 20 min | Keeps the watched/unwatched library window in shape. |
| Mount Watchdog | frequent | Confirms the media disk is really mounted. |
| Orphan Cleanup | daily | Removes leftovers from removed downloads. |
| Update Audit | daily | Compares local image digests against the registry. |
| Daily Backup | nightly | Archives volumes and bind mounts, 14-day retention. |
| Weekly Audit | Sundays 05:00 | Full read-only health audit. |

Updates are **never** applied automatically. The audit reports what changed and
classifies it; anything beyond a patch bump waits for a human.

## The audit

`server-audit.sh` is a strictly read-only health check of the entire server. It
changes nothing and is safe to run at any time, including mid-download.

Run it with `bash server-audit.sh`. Exit code is 0 for all clear, 1 for
warnings, 2 for failures.

**Every check in it exists because the thing it looks for actually broke once
and went unnoticed.** Each carries a comment naming the incident. Do not delete
checks to make the output shorter — a shorter report is not the goal, a true one
is.

It covers containers and HTTP endpoints for every stack, that the media disk is
genuinely mounted rather than an empty stub, disk headroom, hardlink efficiency,
BitTorrent transport, outbound DNS to the metadata providers, app health, library
consistency, that DNS filtering actually resolves a name rather than merely
running, that the voice services answer a real Wyoming handshake with their
models installed, that every photo in the database still has its original file on
disk, that backups are current and actually extract, that the photo backup is not
silently shrinking, and that no credential is hardcoded or tracked by git.

A few of those deserve their own note, because they are the ones that taught the
most:

- **"The container is running" is not the question.** A DNS resolver that is up
  but not answering breaks every device pointed at it exactly as hard as one
  that is missing. The check resolves a real name.
- **A service absent from the check list fails invisibly.** The audit once
  reported a clean 67/67 while a container had been removed from the host
  entirely, simply because nothing had ever asked about it.
- **Thumbnails outliving originals looks like a healthy library.** Photos whose
  original file has vanished still render in the UI. Only a download reveals the
  loss, which is why the count is asserted directly against the database.
- **A backup that runs perfectly can still protect nothing.** If the source is
  quietly emptying out, every individual backup "succeeds" while faithfully
  archiving less and less. Consecutive archives are compared for shrinkage.
- **Probing the wrong endpoint fails forever and looks like an outage.** A
  healthcheck aimed at an authenticated Home Assistant endpoint returned 401 on
  every attempt, so the container reported `unhealthy` across 56 consecutive
  probes while serving perfectly — and each probe was logged as a failed login,
  walking it towards banning its own healthcheck. The audit probes an
  unauthenticated endpoint for liveness, and separately checks that the stored
  token still authenticates, because a revoked token has no other symptom.

## The voice assistant

`jarvis/` is a fully local voice assistant: six containers, no cloud speech API,
no vendor account. Audio is captured in the browser, transcribed on this box,
answered by a local language model, and spoken back by a local synthesiser.

| Container | Port | Role |
|---|---|---|
| `jarvis-hass` | 8123 | Home Assistant — owns the devices and runs the conversation agent. |
| `jarvis-ollama` | 11434 | Local language model runtime. |
| `jarvis-whisper` | 10300 | Speech to text (Wyoming). |
| `jarvis-piper` | 10200 | Text to speech (Wyoming). |
| `jarvis-wake-word` | 10400 | Wake word detection, `hey_jarvis` (Wyoming). |
| `jarvis-voice` | 3100 | Browser client, and the bridge between it and everything above. |

Three things about this stack are non-obvious enough to be worth stating
outright, because each one cost real time to work out:

- **The three voice services do not speak HTTP.** Wyoming is a raw TCP JSONL
  protocol. `curl http://localhost:10300/` fails against a perfectly healthy
  Whisper, and that failure is not evidence of anything. An earlier health
  endpoint did exactly that and reported all three services down while all three
  were fine. The audit opens a socket and sends a real `describe` handshake
  instead, using the client in `jarvis/wyoming_client.py`.
- **A Wyoming container answers its port before it can do its job.** These
  images fetch their models on first run, so the container is up and accepting
  connections for minutes before it can transcribe or speak — and forever, if
  the download failed. The audit counts *installed models*, not connections.
- **The browser client has to be served over HTTPS.** `getUserMedia` requires a
  secure context. Over plain HTTP on a tailnet address `navigator.mediaDevices`
  is `undefined`, so the microphone fails with a bare `TypeError` and the page
  looks broken with nothing anywhere naming the cause. `tailscale serve` supplies
  the TLS on port **8443**, deliberately not 443 — 443 already serves Vaultwarden,
  and `tailscale serve` without an explicit port replaces that handler rather
  than adding to it. The audit asserts both mappings for that reason.

The bridge exists so the Home Assistant token stays server-side: the browser
never receives a credential. `JARVIS_HASS_TOKEN` lives in `secrets.env` like
every other credential here.

**This stack is not finished.** The language model currently runs on CPU, which
puts a warm round trip around 8 seconds against a 3 second target, and model
selection is still open. `jarvis/STATUS.md` records what has actually been
measured and what has not; it is the honest account, and it is deliberately kept
free of aspirational claims.

### Adding the JARVIS tile to Homarr

Homarr stores its boards in **SQLite** (`/appdata/db/db.sqlite`, a Drizzle-managed
schema spread across `app`, `item`, `item_layout`, `layout` and `section`), not in
an editable JSON file. Do not hand-edit that database — a tile is several rows
that have to stay mutually consistent, and Homarr caches board state. Add it
through the UI:

1. Open Homarr and go to **`/manage/apps/new`**.
2. Fill in:
   - **Name** — `JARVIS`
   - **URL** — `https://<your-tailnet-host>:8443/`
     **This must be the HTTPS address, not `http://<host>:3100/`.** The tile is
     how the assistant actually gets opened, and over plain HTTP the microphone
     silently will not start. This is the same reason Vaultwarden's tile uses an
     HTTPS URL while every other tile uses a plain LAN one.
   - **Ping URL** — `http://jarvis-voice:3100/healthz`
     A container name, not the host address: Homarr runs its status checks
     server-side from inside its own container, and on Docker Desktop for
     Windows a container cannot reach the host's published ports. `/healthz`
     returns `{"status": "ok"}` with a 200.
   - **Icon** — search `home-assistant`, or any icon you prefer.
3. Save, then open the board at **`/boards/dashboard`**, turn on edit mode, add
   an **App** item to the **Infrastructure** section, and select `JARVIS`.

The status dot only works because `homarr-homarr-1` is attached to the
`jarvis_jarvis` network — that is what the entry in `homarr/docker-compose.yml`
is for. Without it the ping resolves nothing and the tile sits permanently red
while the service is perfectly healthy.

## Networking, and one trap worth knowing

`.wslconfig` must keep `networkingMode=nat` and `dnsTunneling=false`.

Under `networkingMode=mirrored`, Docker Desktop's port relay listens *inside*
the WSL VM and those listeners never appear on Windows. The symptoms are
confusing and look like several unrelated faults: published services answer on
`127.0.0.1` but not on the LAN or the Tailscale address, and port 53 cannot be
published at all because WSL's DNS tunnelling already holds it — Docker logs
`bind: address already in use`, then starts the container anyway with no host
mapping.

The lesson generalises: **`docker port` reports what was requested, not what
was achieved.** It will happily print `53/udp -> 0.0.0.0:53` when nothing is
listening. Verify with `Get-NetUDPEndpoint -LocalPort 53`, or by actually
resolving a name.

DNS filtering is reachable only from the tailnet: the AdGuard firewall rules
are scoped to `100.64.0.0/10`. No Tailscale DNS override is used, deliberately
— a tailnet-wide override means this machine being off takes DNS down with it.

## Pulling images

`docker pull` does not work on this host. Every pull fails with

```
error getting credentials - err: exit status 1, out: `A specified logon
session does not exist. It may already have been terminated.`
```

including anonymous pulls of public images. Restarting Docker Desktop does not
fix it, removing `credsStore` does not fix it, and Windows Credential Manager
holds no Docker entry at all. Use `bash docker-pull.sh <image>`, which asks the
daemon to pull over its own API. The daemon is fine; only the CLI's credential
path is broken.

## Working on this

Compose files are the source of truth for a service's ports, volumes and
environment. Never infer configuration from the contents of a data directory.

From inside a stack directory:

```
docker compose up -d          # start or apply changes
docker compose pull           # fetch newer images
docker compose logs -f <svc>  # tail one service
docker compose restart <svc>  # restart after a config change
```

After editing a compose file, apply it with `docker compose up -d` in that
stack's directory. Compose only recreates services whose definitions actually
changed.

Do not delete or rewrite files under application data directories. They are live
state, not disposable build artifacts.
