#!/usr/bin/env python3
# lb-discovery.py -- ListenBrainz recommendations -> Octo imports. Replaces Explo.
# Runs daily through scripts/run-py.sh (secrets.env loaded). Stdlib only.
#   "Discover Weekly" = newest ListenBrainz Weekly Exploration (new to you, LB refreshes Mondays)
#   "Daily Discovery" = never-listened CF recommendations, a different slice each day. OFF by
#   default (LB_DAILY=0): 25/day downloaded would grow the library ~2 GB/week; the daily list
#   is Octo's own "Your Radio" (previews, heart to keep). Set LB_DAILY=25 to turn it on.
# Both go to Octo as ONE CSV upload. Octo matches them to the library, keeps each as a
# Navidrome playlist and fetches the missing songs (Soulseek FLAC first, YouTube last).
# Same list name -> same Octo list, updated in place. A list LB has nothing for is left as it was.
# DRY_RUN=1 prints the CSV and uploads nothing. LB_USER overrides the ListenBrainz user.
import csv, datetime, http.cookiejar, io, json, os, sys, urllib.error, urllib.parse, urllib.request, uuid

LB = "https://api.listenbrainz.org/1"
OCTO = "http://127.0.0.1:5274"
USER = os.environ.get("LB_USER", "Natik Chhabra")
DAILY = int(os.environ.get("LB_DAILY", "0"))
UA = {"User-Agent": "lb-discovery/1 (home server)"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.load(r) if r.status == 200 else None  # 204 = LB has computed nothing yet


def mbid(ident):
    ident = ident[0] if isinstance(ident, list) else ident
    return ident.rstrip("/").rsplit("/", 1)[-1]


def patch(p):
    ext = p["extension"]["https://musicbrainz.org/doc/jspf#playlist"]
    return ext.get("additional_metadata", {}).get("algorithm_metadata", {}).get("source_patch")


u = urllib.parse.quote(USER)
rows, seen = [], set()

lists = [p["playlist"] for p in (get(f"{LB}/user/{u}/playlists/createdfor?count=100") or {}).get("playlists", [])]
weekly = sorted((p for p in lists if patch(p) == "weekly-exploration"), key=lambda p: p["date"])
if weekly:
    for t in get(f"{LB}/playlist/{mbid(weekly[-1]['identifier'])}")["playlist"]["track"]:
        seen.add(mbid(t["identifier"]))
        rows.append(["Discover Weekly", t["title"], t["creator"], t.get("album", ""), t.get("duration", ""), ""])

recs = ((get(f"{LB}/cf/recommendation/user/{u}/recording?count=1000") or {}).get("payload") or {}).get("mbids", [])
fresh = [r["recording_mbid"] for r in recs if not r.get("latest_listened_at") and r["recording_mbid"] not in seen]
if fresh and DAILY:
    # ponytail: stateless rotation by date; a slice can repeat once the pool is used up.
    start = datetime.date.today().toordinal() * DAILY % len(fresh)
    pick = (fresh * 2)[start:start + min(DAILY, len(fresh))]
    meta = get(f"{LB}/metadata/recording/?recording_mbids={','.join(pick)}&inc=artist%20release") or {}
    for m in pick:
        d = meta.get(m)
        if d and d.get("recording") and d.get("artist"):
            rec = d["recording"]
            rows.append(["Daily Discovery", rec["name"], d["artist"]["name"], (d.get("release") or {}).get("name", ""),
                         rec.get("length") or "", (rec.get("isrcs") or [""])[0]])

stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%FT%TZ")
if not rows:
    print(f"{stamp} ListenBrainz has no Weekly Exploration or recommendations for {USER} yet; nothing sent.")
    sys.exit(0)

buf = io.StringIO()
w = csv.writer(buf)
w.writerow(["Playlist Name", "Track Name", "Artist Name(s)", "Album Name", "Duration (ms)", "ISRC"])
w.writerows(rows)
if os.environ.get("DRY_RUN"):
    sys.stdout.buffer.write(buf.getvalue().encode())
    sys.exit(0)

op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def post(path, data, ctype):
    req = urllib.request.Request(OCTO + path, data=data, headers={"Content-Type": ctype, "X-Octo-Admin": "1"})
    try:
        with op.open(req, timeout=180) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


status, _ = post("/api/admin/browse/auth", json.dumps(
    {"username": os.environ["NAVIDROME_USER"], "password": os.environ["NAVIDROME_PASS"]}).encode(), "application/json")
if status != 200:
    print(f"{stamp} Octo sign-in failed: HTTP {status}")
    sys.exit(1)
b = uuid.uuid4().hex
body = (f'--{b}\r\nContent-Disposition: form-data; name="file"; filename="listenbrainz.csv"\r\n'
        f"Content-Type: text/csv\r\n\r\n{buf.getvalue()}\r\n--{b}--\r\n").encode()
status, raw = post("/api/admin/imports/file", body, f"multipart/form-data; boundary={b}")
res = json.loads(raw or b"{}")
print(f"{stamp} HTTP {status}: {res.get('message')} ({len(rows)} songs sent)")
if not res.get("ok"):
    sys.exit(1)
# Keep each list as a Navidrome playlist and fetch what is missing (FLAC first). Idempotent:
# Octo keeps both flags across re-uploads; fetch is refused until a list is matched once.
import hashlib, time
for name in sorted({r[0] for r in rows}):
    lid = "file-" + hashlib.sha256(name.lower().encode()).hexdigest()[:12]
    for flag in ("playlist", "fetch"):
        for _ in range(6):
            st, _b = post(f"/api/admin/imports/lists/{lid}/{flag}", b'{"on":true}', "application/json")
            if st == 200:
                break
            time.sleep(10)
        print(f"{stamp} {name}: {flag} on -> HTTP {st}")
        if st != 200:
            sys.exit(1)
sys.exit(0)
