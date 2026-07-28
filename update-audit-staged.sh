#!/bin/bash
# Staged by update-audit.sh on 2026-07-28T04:17:51Z.
# Run from an INTERACTIVE shell — docker pull needs the credential helper,
# which fails under Task Scheduler on this host.
# Only PATCH and MINOR updates are listed. MAJOR / RENAMED are excluded
# by design and need an explicit decision; see update-audit.log.
set -euo pipefail
echo 'Backing up config volumes first...'
bash /c/ServerData/Stacks/backup-volumes.sh

# jellyfin : 10.11.11ubu2404-ls42 -> 10.11.11ubu2604-ls43 (PATCH)
cd 'C:\ServerData\Stacks\streaming' && docker compose pull 'jellyfin' && docker compose up -d 'jellyfin'

# jellyseerr : v3.3.0 -> v3.4.0 (MINOR)
cd 'C:\ServerData\Stacks\streaming' && docker compose pull 'jellyseerr' && docker compose up -d 'jellyseerr'

# vaultwardem-vaultwarden-1 : 1.36.0 -> 1.37.0 (MINOR)
cd 'C:\Users\Naiti\.docker\cagent\working_directories\https-3a-2f-2fai-backend-service-docker-com-2fproxy-2fgordon-agent-3fgordontag-3dv9-26desktopversion-3d4-81-0-26origin-3ddesktop\a0b8a0f7-9dda-4a04-a5b5-48d2bc2bd3b9\default\vaultwarden' && docker compose pull 'vaultwarden' && docker compose up -d 'vaultwarden'

# filebrowser-filebrowser-1 : 2.63.18 -> 2.63.23 (PATCH)
cd '/data/compose/16' && docker compose pull 'filebrowser' && docker compose up -d 'filebrowser'
