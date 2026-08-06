#!/bin/bash
# ============================================================================
# music-fetch.sh -- search and download legally-free music into the Navidrome
# library, and sort an existing pile of files into the layout it expects.
#
# This is a thin wrapper. The logic lives in music-fetch.py next to it: five
# archive APIs, licence checking, an interactive confirmation prompt and
# tag-aware sorting stopped fitting into bash-with-inline-python somewhere
# around the third source.
#
# Usage:
#   bash music-fetch.sh sources                    # what it searches, and why
#   bash music-fetch.sh search "kevin macleod"     # search, then asks what to take
#   bash music-fetch.sh get 3                      # download result 3, with a prompt
#   bash music-fetch.sh get 3 -y                   # ...without the prompt
#   bash music-fetch.sh get <archive-identifier>   # old form, still works
#   bash music-fetch.sh organize                   # dry run: show how it would sort
#   bash music-fetch.sh organize /d/Downloads/mp3s # sort from somewhere else
#   bash music-fetch.sh organize --apply           # actually move the files
#
# Searching never writes to disk. Downloading always asks first unless -y is
# given, and an item with no stated licence is skipped rather than taken.
#
# Downloads land in D:/Media/music/<Artist>/<Album>/, which is what Navidrome
# reads. It rescans hourly (ND_SCANSCHEDULE=1h); to index immediately:
#   docker restart navidrome
# ============================================================================

set -uo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
command -v python >/dev/null 2>&1 || die "python not found"

# Windows Python writes stdout as cp1252 by default, so a result containing a
# non-Latin character -- constantly, given these are international music
# archives -- would kill the whole run with UnicodeEncodeError.
export PYTHONIOENCODING="utf-8:replace"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HERE/music-fetch.py" ] || die "music-fetch.py missing from $HERE"

exec python "$HERE/music-fetch.py" "$@"
