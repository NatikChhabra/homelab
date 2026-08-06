#!/bin/bash
# Staged by update-audit.sh on 2026-08-06T23:00:02Z.
# Run from an INTERACTIVE shell — docker pull needs the credential helper,
# which fails under Task Scheduler on this host.
# Only PATCH and MINOR updates are listed. MAJOR / RENAMED are excluded
# by design and need an explicit decision; see update-audit.log.
set -euo pipefail
echo 'Backing up config volumes first...'
bash /c/ServerData/Stacks/backup-volumes.sh

# glances : v4.5.5 -> v4.5.6 (PATCH)
cd 'C:\ServerData\Stacks\server-room' && docker compose pull 'glances' && docker compose up -d 'glances'

# vaultwardem-vaultwarden-1 : 1.37.0 -> 1.37.1 (PATCH)
cd 'C:\ServerData\Stacks\vaultwarden' && docker compose pull 'vaultwarden' && docker compose up -d 'vaultwarden'

# jellyseerr : v3.4.0 -> v3.4.1 (PATCH)
cd 'C:\ServerData\Stacks\streaming' && docker compose pull 'jellyseerr' && docker compose up -d 'jellyseerr'

# immich-immich-server-1 : v3.0.3 -> v3.1.0 (MINOR)
cd 'C:\ServerData\Stacks\immich' && docker compose pull 'immich-server' && docker compose up -d 'immich-server'

# immich-immich-machine-learning-1 : v3.0.3 -> v3.1.0 (MINOR)
cd 'C:\ServerData\Stacks\immich' && docker compose pull 'immich-machine-learning' && docker compose up -d 'immich-machine-learning'

# vikunja-vikunja-1 : 2.4.0 -> 2.5.0 (MINOR)
cd 'C:\ServerData\Stacks\vikunja' && docker compose pull 'vikunja' && docker compose up -d 'vikunja'
