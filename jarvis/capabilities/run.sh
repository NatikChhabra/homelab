#!/usr/bin/env bash
# Build and (re)create the capability service container.
#
# `docker restart` re-runs the OLD image -- it recreates nothing -- so a rebuild
# that is only followed by a restart tests exactly the code you just replaced.
# That cost a full test cycle once; this script always does rm + run.
#
# Not added to docker-compose.yml on purpose: the compose file is owned
# elsewhere. The equivalent compose block is in this folder's README, ready to
# paste when the stack owner wants it adopted.
set -euo pipefail
# MSYS_NO_PATHCONV stops Git Bash rewriting /data and /audit inside -v args into
# C:/Program Files/Git/... -- but it also stops it converting /c/... into a real
# Windows path, so every path here is written Windows-style from the start.
export MSYS_NO_PATHCONV=1

HERE="C:/ServerData/Stacks/jarvis/capabilities"
NAME=jarvis-capabilities
PORT="${CAPABILITIES_PORT:-3102}"     # 3100 is jarvis-voice, 3101 taken by another worker

echo "==> building"
docker build -t "$NAME:latest" "$HERE"

echo "==> recreating $NAME on :$PORT"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" \
  --network jarvis_jarvis \
  --restart always \
  -p "${PORT}:${PORT}" \
  -e "PORT=${PORT}" \
  -e TZ=Asia/Kolkata \
  -e PIPER_HOST=jarvis-piper -e PIPER_PORT=10200 -e PIPER_VOICE=en_GB-alan-medium \
  -e GLANCES_URL=http://host.docker.internal:61208 \
  -e VIKUNJA_URL=http://host.docker.internal:3456 \
  -e AUDIT_REPORT=/audit/server-audit-report.txt \
  --add-host host.docker.internal:host-gateway \
  -v "C:/ServerData/Stacks/jarvis/capabilities/data:/data" \
  -v "C:/ServerData/Stacks/server-audit-report.txt:/audit/server-audit-report.txt:ro" \
  "$NAME:latest" >/dev/null

sleep 4
curl -fsS "http://localhost:${PORT}/healthz" && echo
