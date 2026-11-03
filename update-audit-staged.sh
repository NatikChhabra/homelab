#!/bin/bash
# Staged by update-audit.sh on 2026-11-02T23:00:02Z.
# Run from an INTERACTIVE shell — docker pull needs the credential helper,
# which fails under Task Scheduler on this host.
# Only PATCH and MINOR updates are listed. MAJOR / RENAMED are excluded
# by design and need an explicit decision; see update-audit.log.
set -euo pipefail
echo 'Backing up config volumes first...'
bash /c/ServerData/Stacks/backup-volumes.sh

# vaultwardem-vaultwarden-1 : 1.37.4 -> 1.37.5 (PATCH)
cd 'C:\ServerData\Stacks\vaultwarden' && docker compose pull 'vaultwarden' && docker compose up -d 'vaultwarden'
