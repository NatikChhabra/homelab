# Homelab

Docker Compose stacks and automation for a Windows-hosted home server: media
library, photos, passwords, tasks, dashboards, and the scheduled jobs that keep
them honest.

This repository is the *configuration* for that server. It deliberately contains
no application data, no databases, and no credentials.

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
  53   adguard (DNS)      6767  bazarr           8222  vaultwarden
3001   uptime-kuma        6881  qbittorrent      8989  sonarr
3080   adguard (web UI)   7575  homarr           9443  portainer
3456   vikunja            7878  radarr           9696  prowlarr
4533   navidrome
5055   jellyseerr         8080  qbittorrent      11011 cleanuparr
6246   maintainerr        8095  filebrowser      61208 glances
2283   immich             8096  jellyfin         8191  flaresolverr
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
running, that every photo in the database still has its original file on disk,
that backups are current and actually extract, that the photo backup is not
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
