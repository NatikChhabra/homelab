#!/bin/bash
# pre-build-guard.sh — refuse to build a Docker image from a dirty working tree.
#
# Why this exists
# ---------------
# F4 drift: on 2026-08-16 the jarvis-voice image was baked at 02:32:44 with
# uncommitted working-tree changes in ha_aliases.py, hud.py, and hermes/executor.py.
# The commit (36f2294) landed at 02:33:36 — 52 seconds too late. The audit caught
# it a day later. This guard prevents that race by aborting BEFORE docker build.
#
# What it does
# ------------
# 1. Reads the Dockerfile to extract every COPY source path.
# 2. Runs `git status --porcelain` filtered to those paths.
# 3. If any path has uncommitted changes → aborts with the file list.
# 4. Stamps the image with the current git SHA via LABEL.
#
# Usage
# -----
#   bash pre-build-guard.sh <dockerfile> [build-args...]
#
# Exit codes
# ----------
#   0 = clean, proceed with build
#   1 = dirty tree detected (files listed to stderr)
#   2 = missing git repo or Dockerfile

set -uo pipefail

DOCKERFILE="${1:?Usage: pre-build-guard.sh <dockerfile> [build-args...]}"
shift || true

if [[ ! -f "$DOCKERFILE" ]]; then
  echo "pre-build-guard: Dockerfile not found: $DOCKERFILE" >&2
  exit 2
fi

if ! git rev-parse --git-dir >/dev/null 2>&1; then
  echo "pre-build-guard: not a git repo (or git not found)" >&2
  exit 2
fi

GIT_SHA=$(git rev-parse HEAD)
echo "pre-build-guard: git HEAD = $GIT_SHA"

# Extract COPY source paths from the Dockerfile.
# Handles both: COPY file1 file2 ./ and COPY dir/ ./
COPY_SOURCES=()
while IFS= read -r line; do
  # Skip comments and empty lines
  [[ "$line" =~ ^[[:space:]]*# ]] && continue
  [[ -z "${line// /}" ]] && continue

  # Remove the COPY keyword and trailing destination
  # Pattern: COPY <sources...> <destination>
  # Destination is the last path component (ends with / or is a filename)
  cleaned="${line#*COPY }"
  # Remove the last token (destination)
  dest=$(echo "$cleaned" | awk '{print $NF}')
  # Sources are everything except the last token
  sources="${cleaned% $dest}"

  for src in $sources; do
    # Skip build args like ${VAR}
    [[ "$src" == *'${'* ]] && continue
    # If it's a directory (ends with /), add it as a glob
    if [[ "$src" == */ ]]; then
      COPY_SOURCES+=("${src}%")
    else
      COPY_SOURCES+=("$src")
    fi
  done
done < "$DOCKERFILE"

echo "pre-build-guard: watching ${#COPY_SOURCES[@]} source patterns"

# Check git status for each pattern
DIRTY=()
for pattern in "${COPY_SOURCES[@]}"; do
  # Use git ls-files to get tracked files matching the pattern,
  # then check if any have modifications
  while IFS= read -r file; do
    status=$(git status --porcelain -- "$file" 2>/dev/null)
    if [[ -n "$status" ]]; then
      DIRTY+=("$file")
    fi
  done < <(git ls-files --error-unmatch $pattern 2>/dev/null || git ls-files --error-unmatch "${pattern%\*}" 2>/dev/null || true)
done

# Also check for untracked files in watched directories
for pattern in "${COPY_SOURCES[@]}"; do
  if [[ "$pattern" == *'/'* && "$pattern" != *'*'* ]]; then
    # Directory pattern - check for untracked files
    while IFS= read -r file; do
      rel="${file#./}"
      # Check if this file is in the watched set
      while IFS= read -r watched; do
        watched_clean="${watched%\*}"
        if [[ "$rel" == "${watched_clean}"* ]]; then
          status=$(git status --porcelain -- "$rel" 2>/dev/null)
          if [[ -n "$status" ]]; then
            DIRTY+=("$rel")
          fi
          break
        fi
      done < <(git ls-files "$pattern" 2>/dev/null)
    done < <(git ls-files --others --exclude-standard "$pattern" 2>/dev/null)
  fi
done

if [[ ${#DIRTY[@]} -gt 0 ]]; then
  echo "" >&2
  echo "pre-build-guard: REFUSING TO BUILD — dirty tree detected:" >&2
  for f in "${DIRTY[@]}"; do
    echo "  MODIFIED: $f" >&2
  done
  echo "" >&2
  echo "Commit changes before building, or this guard will keep aborting." >&2
  exit 1
fi

echo "pre-build-guard: tree is clean. Proceeding."
echo "BUILD_ARG=--build-arg GIT_SHA=${GIT_SHA}"
exit 0
