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
# Never prints secret values — only reports locations and fingerprints.

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
  v=$(grep -oP "(?<=^${k}=).*" "$SECRETS" || true)
  [ -z "$v" ] && continue
  [ "${#v}" -lt 6 ] && continue

  # Fingerprint: first 8 chars + sha256 of full value (full hash never printed)
  FINGERPRINT=$(printf '%s' "$v" | sha256sum | cut -c1-8)
  PREFIX="${v:0:8}"

  # Check full value match
  if grep -qF -- "$v" "$TARGET"; then
    echo "LEAK: ${k} full value in ${TARGET} (fingerprint:${FINGERPRINT})"
    COUNT=$((COUNT + 1))
    FAIL=1
  fi

  # Check 8-char prefix match (catches "redacted" patterns that leaked the prefix)
  if [ "${#v}" -ge 8 ] && grep -qF -- "$PREFIX" "$TARGET"; then
    echo "PARTIAL LEAK: ${k} prefix (${PREFIX}...) in ${TARGET} (fingerprint:${FINGERPRINT})"
    COUNT=$((COUNT + 1))
    FAIL=1
  fi
done <<< "$KEYS"

if [ "$FAIL" -eq 0 ]; then
  echo "CLEAN: no secret values or prefixes found in ${TARGET}"
fi
exit $FAIL
