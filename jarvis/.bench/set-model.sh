#!/bin/sh
# Switch the Ollama conversation subentry's model (and optionally the upstream
# URL) with Home Assistant stopped, because HA rewrites .storage on shutdown
# and would clobber a live edit.
#
#   set-model.sh <model-tag> [url]
set -e

MODEL="$1"
URL="$2"

echo "== stopping HA =="
docker stop jarvis-hass >/dev/null

docker run --rm -i -v hass_config:/config python:3.12-slim python3 - "$MODEL" "$URL" <<'PY'
import json, sys
model, url = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
p = "/config/.storage/core.config_entries"
d = json.load(open(p))
n = 0
for e in d["data"]["entries"]:
    if e.get("domain") != "ollama":
        continue
    if url:
        e["data"]["url"] = url
    for sub in e.get("subentries", []):
        if sub.get("subentry_type") == "conversation":
            sub["data"]["model"] = model
            n += 1
    print("entry url =", e["data"]["url"])
json.dump(d, open(p, "w"), indent=2)
print("patched", n, "conversation subentries -> model", model)
PY

echo "== starting HA =="
docker start jarvis-hass >/dev/null

# Wait for HA to actually answer, not just for the container to exist.
for i in $(seq 1 90); do
  code=$(docker exec jarvis-hass curl -s -o /dev/null -w '%{http_code}' -m 3 http://127.0.0.1:8123/ 2>/dev/null || echo 000)
  if [ "$code" = "200" ] || [ "$code" = "302" ]; then
    echo "HA up after ${i}s (HTTP $code)"
    exit 0
  fi
  sleep 1
done
echo "HA DID NOT COME UP" >&2
exit 1
