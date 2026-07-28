#!/bin/bash
# ============================================================================
# docker-pull.sh -- pull an image when `docker pull` refuses to work.
#
# WHY THIS EXISTS: on this host every `docker pull` fails with
#
#   error getting credentials - err: exit status 1, out: `A specified logon
#   session does not exist. It may already have been terminated.`
#
# including `docker pull alpine:3.20`, so it is not image-specific. It is the
# Docker Desktop CLI credential helper, and it fails even for anonymous pulls
# of public images that need no credentials at all. Restarting Docker Desktop
# does not fix it, removing "credsStore" from ~/.docker/config.json does not fix
# it (Docker Desktop writes it back), and it fails identically inside and
# outside a sandbox.
#
# On 2026-07-28 this blocked starting AdGuard and applying four pending image
# updates -- the server could not be repaired or updated at all.
#
# The DAEMON is fine; only the CLI's credential path is broken. This asks the
# daemon to pull directly over its own API, which needs no client credentials
# for a public image.
#
# Usage:  bash docker-pull.sh adguard/adguardhome:latest
#         bash docker-pull.sh ghcr.io/immich-app/immich-server:release
#
# If `docker pull` ever starts working again, this script becomes redundant --
# it is a workaround for a broken host, not a better way to pull images.
# ============================================================================

set -uo pipefail

REF="${1:-}"
if [ -z "$REF" ]; then
  printf 'usage: %s <image[:tag]>\n' "$0" >&2
  exit 2
fi

# Split the reference into image and tag. Only the LAST colon separates the
# tag, because a registry host may itself contain a port (localhost:5000/foo).
case "${REF##*/}" in
  *:*) TAG="${REF##*:}"; IMAGE="${REF%:*}" ;;
  *)   TAG="latest";     IMAGE="$REF" ;;
esac

printf 'pulling %s:%s via the daemon API...\n' "$IMAGE" "$TAG"

# A local image is needed to run curl against the socket. Any image already on
# the host will do; these are the ones this server always has.
HELPER=""
for cand in lscr.io/linuxserver/sonarr:latest lscr.io/linuxserver/radarr:latest \
            lscr.io/linuxserver/prowlarr:latest; do
  if docker image inspect "$cand" >/dev/null 2>&1; then HELPER="$cand"; break; fi
done
if [ -z "$HELPER" ]; then
  printf 'no local image available to run the API call from\n' >&2
  exit 1
fi

# MSYS_NO_PATHCONV stops Git Bash rewriting /var/run/docker.sock into a Windows
# path, which silently produces "Could not connect to server".
out=$(MSYS_NO_PATHCONV=1 docker run --rm \
        -v /var/run/docker.sock:/var/run/docker.sock \
        --entrypoint sh "$HELPER" -c \
        "curl -sS -X POST --unix-socket /var/run/docker.sock \
         'http://localhost/images/create?fromImage=${IMAGE}&tag=${TAG}' 2>&1" 2>&1)

printf '%s\n' "$out" | tail -3

# The API streams JSON and returns HTTP 200 even when the pull fails, so the
# response body is what has to be checked, not the exit status.
if printf '%s' "$out" | grep -q '"error"'; then
  printf 'PULL FAILED\n' >&2
  printf '%s\n' "$out" | grep -o '"error":"[^"]*"' | head -2 >&2
  exit 1
fi

if docker image inspect "${IMAGE}:${TAG}" >/dev/null 2>&1; then
  printf 'OK: %s:%s is present locally\n' "$IMAGE" "$TAG"
  exit 0
fi

printf 'PULL FAILED: %s:%s is still not present\n' "$IMAGE" "$TAG" >&2
exit 1
