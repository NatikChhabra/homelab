#!/usr/bin/env bash
# scan-secrets.sh — scan a file for leaked secret values from secrets.env
# Run BEFORE writing any report. Exit non-zero if leaks found.
#
# Usage: scripts/scan-secrets.sh <target-file>
#
# Checks two things:
#   1. Full secret values (any value with length >= 6)
#   2. First 8 characters of each value (catches prefix-only leaks)
#
# Never prints secret values, not even a prefix -- only key names and a sha256 fingerprint.
# Values are matched through a process-substitution pattern file, never put on a command line
# (argv is visible to every process on the machine).

set -euo pipefail

STACKS="C:/ServerData/Stacks"
SECRETS="$STACKS/secrets.env"
TARGET="${1:?usage: scan-secrets.sh <file>}"

if [ ! -f "$TARGET" ]; then
  echo "ERROR: target file not found: $TARGET"
  exit 1
fi

if [ ! -f "$SECRETS" ]; then
  echo "ERROR: secrets.env not found at $SECRETS"
  exit 1
fi

KEYS=$(grep -oP '^[A-Z0-9_]+(?==)' "$SECRETS" | sort -u)
FAIL=0
COUNT=0

while IFS= read -r k; do
  [ -z "$k" ] && continue
  # Identifiers, not secrets: matching them only buried real leaks in noise (2026-11-03).
  case "$k" in *_URL|*_IP|*_HOST|*_PORT|*_USER|*_USERNAME) continue ;; esac
  v=$(grep -oP "(?<=^${k}=).*" "$SECRETS" || true)
  # Strip one pair of surrounding quotes, as bash sourcing and run-job.ps1 do. Quoted values
  # (e.g. PROWLARR_KEY) were searched WITH their quotes and so never matched (2026-11-03).
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  [ -z "$v" ] && continue
  [ "${#v}" -lt 6 ] && continue

  # Fingerprint: first 8 hex chars of the value's sha256 (identifies the key's value without revealing it)
  FINGERPRINT=$(printf '%s' "$v" | sha256sum | cut -c1-8)
  PREFIX="${v:0:8}"

  # Check full value match
  if grep -qF -f <(printf '%s\n' "$v") "$TARGET"; then
    echo "LEAK: ${k} full value in ${TARGET} (fingerprint:${FINGERPRINT})"
    COUNT=$((COUNT + 1))
    FAIL=1
  fi

  # Check 8-char prefix match (catches "redacted" patterns that leaked the prefix)
  if [ "${#v}" -ge 8 ] && grep -qF -f <(printf '%s\n' "$PREFIX") "$TARGET"; then
    # 2026-11-03: this line used to echo the 8-char prefix itself -- exactly the leak CLAUDE.md bans
    echo "PARTIAL LEAK: ${k} first 8 chars in ${TARGET} (fingerprint:${FINGERPRINT})"
    COUNT=$((COUNT + 1))
    FAIL=1
  fi
done <<< "$KEYS"

if [ "$FAIL" -eq 0 ]; then
  echo "CLEAN: no secret values or prefixes found in ${TARGET}"
fi
exit $FAIL
