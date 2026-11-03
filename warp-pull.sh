#!/bin/bash
# warp-pull.sh -- pull an image through WARP when the ISP throttles the registry.
# Added 2026-11-01: Airtel crawls ghcr.io blobs at ~10 KB/s (daemon pulls of
# homarr/immich never finish); through the warp container they run ~10 MB/s.
# See warp-pull.py for the mechanics. Usage:
#   bash /c/ServerData/Stacks/warp-pull.sh ghcr.io/homarr-labs/homarr:latest
set -euo pipefail
export MSYS_NO_PATHCONV=1
REF=${1:?usage: warp-pull.sh <image:tag>}
TMP='C:\ServerData\Stacks\logs\warp-pull'
mkdir -p /c/ServerData/Stacks/logs/warp-pull
docker run --rm --network streaming_default -v 'C:\ServerData\Stacks\warp-pull.py:/warp-pull.py:ro' -v "$TMP:/out" \
  python:3-alpine python -u /warp-pull.py "$REF"
docker load -i "$TMP\image.tar"
rm -f /c/ServerData/Stacks/logs/warp-pull/image.tar
