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
| `streaming` | qbittorrent, prowlarr, flaresolverr, sonarr, radarr, bazarr, jellyfin, jellyseerr, warp | Media acquisition, organisation and playback. The largest and most frequently edited stack. |
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
| *(retired)* explo | — | Replaced 2026-11-02 by `scripts/lb-discovery.py`: ListenBrainz Weekly Exploration is handed to Octo, which downloads it FLAC-first. |
| `actual` | actual | Actual Budget, local-first personal budgeting (added 2026-11-02). |
| `octo` | octo, yt-dlp-shim, slskd | Spotify-style search and download in front of Navidrome: outside songs in Feishin search, play streams from YouTube, a heart downloads (Soulseek FLAC, YouTube fallback) with tags, cover and synced lyrics (added 2026-11-02). Upstream compose verbatim + local `docker-compose.override.yml`. |

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
   53  adguard (DNS)      5055  jellyseerr          8080  qbittorrent (web)
 2283  immich             6767  bazarr (login)      8095  filebrowser
 3001  uptime-kuma        6881  qbittorrent (p2p)   8096  jellyfin
 3080  adguard (web UI)                       8989  sonarr
 3456  vikunja            7575  homarr              9443  portainer
 4533  navidrome          7878  radarr              9696  prowlarr
 5274  octo (Subsonic)   50300  slskd (Soulseek peer)
                                                   61208  glances
 host-only: 8222 vaultwarden (Tailscale serve HTTPS :443),
            5006 actual budget (Tailscale serve HTTPS :5006), 5030 slskd web UI
 no host port: flaresolverr (Prowlarr uses http://flaresolverr:8191)
 removed: JARVIS stack (2026-10-25); maintainerr, cleanuparr (2026-11-01)
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

Scheduled through Windows Task Scheduler (tasks named `ServerData - <Job>`). The jobs marked
"run-job" go through `run-job.ps1`, which captures a per-run transcript, writes
`logs/<job>-status.json`, pings the job's Uptime Kuma push monitor (the SUMMARY line becomes the
heartbeat message), logs failures to the Windows event log, refuses to start while the previous
run is still going, and (since 2026-11-03) kills a job that runs past its Timeout and records it
as failed. Mount Watchdog also flags any run-job job that has gone stale.

| Job | Cadence | What it does |
|---|---|---|
| Offline Playlists (run-job) | every 2 min | Keeps the Navidrome playlist "Everything" equal to the library; the phone's Octo app keeps it downloaded. |
| Mount Watchdog | every 3 min + at boot | Confirms the media disk is really mounted, repairs it, and flags stale jobs. |
| Rolling Window (run-job) | every 20 min + at boot | Keeps the watched/unwatched library window in shape. |
| Music Dupe Guard (run-job) | hourly | Moves Octo downloads that duplicate songs already owned to .octo-trash. |
| Shrink Library (run-job) | daily 01:30 | GPU re-encodes large H.264 files to HEVC (12 a night, 30 min limit per file, skip list for hangs). |
| Immich Auto Albums | daily 03:00 | Builds Immich albums. |
| Daily Backup (run-job) | daily 04:00 | Archives volumes and bind mounts to D:\Backups, 14-day retention. |
| Update Audit (run-job) | daily 04:30 | Compares image digests with the registries and stages updates. |
| LB Discovery (run-job) | daily 06:30 | Sends ListenBrainz Weekly Exploration to Octo once LB has generated one. |
| Weekly Audit (run-job) | Sun 05:00 | Full read-only health audit (`server-audit-report.txt`). |
| Orphan Cleanup (run-job) | Sun 05:30 | Removes leftovers from removed downloads. |
| Boot Check | at logon (+10 min) | Runs the full audit after a reboot and copies the result to Sync (viewable at :8095). |
| Keep Awake | at logon (runs continuously) | Stops Modern Standby from sleeping the server. |
| Power Shed | at logon + on power events | Sheds load (stops encodes) on battery. |
| UDP Exhaustion Snapshot | on Tcpip event 4266 | Records which process ran the host out of UDP ports. |

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

## The voice assistant (removed)

The `jarvis/` stack (Home Assistant, Ollama, Whisper, Piper, wake word, voice
bridge) was removed on 2026-10-25. Its notes live in git history.

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

## Restoring from a backup

Nightly archives live in `D:\Backups` for 14 days (`backup-volumes.sh`, 04:00):

| Archive | What it holds |
|---|---|
| `<volume>_<YYYYMMDD-HHMMSS>.tar.gz` | one named Docker volume |
| `bind-<dir>_<stamp>.tar.gz` | one app folder under `C:\ServerData` (bind mount) |
| `stacks-config_<stamp>.tar.gz` | every compose file, script and `CLAUDE.md` |

Always stop the app first, and copy the current data aside before overwriting it.

**A named volume** (example: `sonarr_config`):

```bash
cd /c/ServerData/Stacks/streaming && docker compose stop sonarr
MSYS_NO_PATHCONV=1 docker run --rm -v sonarr_config:/t -v /d/Backups:/b valkey/valkey:9 \
  sh -c 'cd /t && tar -xzf /b/sonarr_config_<stamp>.tar.gz'
docker compose start sonarr
```

**An app folder (bind mount):** `tar -xzf /d/Backups/bind-<dir>_<stamp>.tar.gz -C /c/ServerData`, with the app stopped.

**Immich's database: never from its volume archive.** That tar is taken while Postgres
runs, so it is not a consistent copy. Restore from Immich's own nightly dump in
`C:\ServerData\Immich\backups\immich-db-backup-*.sql.gz`, using Immich's documented
database-restore steps (fresh database volume, then load the dump with `psql`).
Photos themselves are files under `C:\ServerData\Immich` and need no database step.

The restore test in the nightly run only extracts `sonarr_config` into a temp folder,
so a real restore of anything else is untested until you do it.

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
