#!/bin/bash
# Staged by update-audit.sh on 2026-07-28T13:42:45Z.
# Run from an INTERACTIVE shell — docker pull needs the credential helper,
# which fails under Task Scheduler on this host.
# Only PATCH and MINOR updates are listed. MAJOR / RENAMED are excluded
# by design and need an explicit decision; see update-audit.log.
set -euo pipefail
echo 'Backing up config volumes first...'
bash /c/ServerData/Stacks/backup-volumes.sh
echo 'Nothing staged — no safe updates were found.'
