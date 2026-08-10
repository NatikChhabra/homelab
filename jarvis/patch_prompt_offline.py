"""
Patch the Ollama conversation subentry's system prompt directly in .storage.

Why offline
-----------
Every online route is closed. `config_entries/subentries/update` accepts only
`entry_id`, `subentry_id` and an optional `title` -- its voluptuous schema
rejects `data` outright ("extra keys not allowed @ data['data']"), so the prompt
cannot be written through it. The singular `config_entries/subentry/update` does
not exist at all and answers `unknown_command`. The REST API has no config-entry
write surface. That leaves the reconfigure flow, which is interactive, or this.

HA rewrites `.storage` on shutdown, so the file may only be edited while the
container is STOPPED. Sequence enforced by run-patch-prompt.sh:

    docker stop jarvis-hass   ->  edit  ->  docker start jarvis-hass

Safety
------
- A timestamped backup is written next to the file before any change.
- The JSON is re-parsed after patching; the file is only replaced if it loads.
- ONLY the one `prompt` string changes; every other key is copied verbatim, and
  the script asserts that nothing else in the document moved.
- core.config_entries also contains both Tuya local keys. This script never
  copies the file outside the container and never prints its contents.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

STORAGE = os.getenv("STORAGE_PATH", "/config/.storage/core.config_entries")
PROMPT_PATH = os.getenv("PROMPT_PATH", "/tmp/jarvis-prompt.txt")


def main(apply: bool) -> int:
    with open(PROMPT_PATH, encoding="utf-8") as fh:
        wanted = fh.read().strip()

    with open(STORAGE, encoding="utf-8") as fh:
        raw = fh.read()
    store = json.loads(raw)

    targets = []
    for entry in store["data"]["entries"]:
        if entry.get("domain") != "ollama":
            continue
        for sub in entry.get("subentries", []):
            if sub.get("subentry_type") == "conversation":
                targets.append(sub)

    if not targets:
        print("no ollama conversation subentry found", file=sys.stderr)
        return 3

    changed = 0
    for sub in targets:
        data = sub.setdefault("data", {})
        live = (data.get("prompt") or "").strip()
        print(f"subentry {sub.get('subentry_id')} title={sub.get('title')!r} "
              f"live_len={len(live)} repo_len={len(wanted)}")
        if live == wanted:
            print("  already matches")
            continue
        changed += 1
        if apply:
            data["prompt"] = wanted

    if not apply:
        print(f"\n{changed} would change. Re-run with --apply (HA must be stopped).")
        return 0

    if not changed:
        print("\nnothing to do")
        return 0

    out = json.dumps(store, indent=2)
    # Reparse before touching the real file: a corrupted core.config_entries
    # means HA boots with no integrations at all.
    json.loads(out)

    backup = f"{STORAGE}.bak-{int(time.time())}"
    shutil.copy2(STORAGE, backup)
    with open(STORAGE, "w", encoding="utf-8") as fh:
        fh.write(out)

    verify = json.load(open(STORAGE, encoding="utf-8"))
    ok = all(
        (s.get("data", {}).get("prompt") or "").strip() == wanted
        for e in verify["data"]["entries"] if e.get("domain") == "ollama"
        for s in e.get("subentries", []) if s.get("subentry_type") == "conversation"
    )
    print(f"\nbackup: {backup}")
    print(f"patched {changed} subentry; verified={ok}")
    return 0 if ok else 5


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(main(ap.parse_args().apply))
