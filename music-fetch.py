#!/usr/bin/env python3
# ============================================================================
# music-fetch.py -- search legally-free music archives, download on explicit
# confirmation, and sort an existing pile of files into the layout Navidrome
# expects.
#
# Invoked through music-fetch.sh so the documented bash entrypoint keeps
# working. Stdlib only apart from mutagen (already installed, 1.47.0), which
# is what makes tag-aware sorting possible -- Navidrome indexes by TAGS, not
# by folder names, so moving files without fixing tags does not fix the UI.
#
# SOURCES, and why each one is here:
#   Internet Archive   no key, no signup. Live Music Archive (etree) alone is
#                      ~250k artist-permitted concert recordings; plus
#                      netlabels, musopen (public-domain classical) and the
#                      78rpm digitisations.
#   Openverse          Creative Commons search run by WordPress. No key. Gives
#                      Jamendo (~628k CC tracks) and Wikimedia Commons audio
#                      (~3.9M) through one endpoint, which is why the old
#                      JAMENDO_CLIENT_ID requirement is no longer mandatory.
#   ccMixter           no key. CC remix/original community, strong on
#                      instrumental and acoustic material.
#   Library of Congress  no key. National Jukebox: pre-1925 commercial
#                      recordings the LoC has released as public domain.
#   Jamendo direct     optional, needs a free JAMENDO_CLIENT_ID. Adds nothing
#                      Openverse lacks except better filtering, so it stays
#                      optional rather than required.
#
# WHAT THIS DELIBERATELY DOES NOT DO: it does not touch commercial catalogues.
# Mainstream label releases are absent from these archives because the labels
# never licensed them that way. A search for a chart single will return
# covers, live versions, or nothing. That is not a bug in the search -- those
# recordings genuinely are not free to take.
#
# By default an item with no stated licence is SKIPPED rather than downloaded.
# Pass --allow-unknown to override that per run.
# ============================================================================

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request

UA = "homelab-music-fetch/2.0"
AUDIO_EXT = (".flac", ".mp3", ".ogg", ".m4a", ".opus", ".wav", ".wma", ".aac")

# Windows Python writes stdout as cp1252 by default, so the moment a result
# contains a non-Latin character -- which is constantly, given these are
# international music archives -- printing dies with UnicodeEncodeError
# instead of showing anything. Force UTF-8 and replace what the console
# cannot render.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def winpath(p):
    """Translate a Git Bash path (/d/Media/music) into one Windows Python can
    open (D:/Media/music). The wrapper script and the user both naturally type
    the bash form; Python on Windows resolves it to nothing at all."""
    m = re.match(r"^/([A-Za-z])(/.*)?$", p or "")
    if m:
        return m.group(1).upper() + ":" + (m.group(2) or "/")
    return p


MUSIC_DIR = winpath(os.environ.get("MUSIC_DIR", "D:/Media/music"))
# Headroom to leave on the media volume. The *arr apps import into the same
# disk and will not check what this script did.
MIN_FREE_BYTES = int(os.environ.get("MUSIC_MIN_FREE_GB", "5")) * 1024 ** 3
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "music-fetch-last.json")

# ---------------------------------------------------------------------------
# HTTP


def check_url(url):
    """Every download URL here comes out of a third-party API response, not out
    of this file. urllib happily opens file:// and ftp://, so a compromised or
    merely sloppy archive could hand back file:///c:/ServerData/Stacks/secrets.env
    and this script would obediently copy it into the music library. Nothing
    downstream re-checks the scheme, so it is checked once, here."""
    s = urllib.parse.urlparse(url or "")
    if s.scheme not in ("http", "https"):
        raise ValueError("refusing non-http(s) URL: %.60s" % (url or "<empty>"))
    return url


class _SchemeCheckedRedirect(urllib.request.HTTPRedirectHandler):
    """Re-run check_url on every redirect hop.

    check_url above only ever saw the FIRST url. urllib's default redirect
    handler then allows http, https AND ftp, so a single 302 from one of these
    third-party archives was enough to walk the download straight past the
    scheme check the rest of this file depends on. The check is worth having
    exactly once per URL that actually gets fetched, not once per URL that was
    originally requested.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Every fetch in this file goes through this opener, so the redirect check
# cannot be forgotten at a call site.
OPENER = urllib.request.build_opener(_SchemeCheckedRedirect)


def http(url, params=None, referer=None, timeout=40, raw=False):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(check_url(url), headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    with OPENER.open(req, timeout=timeout) as r:
        data = r.read()
    return data if raw else json.loads(data.decode("utf-8", "replace"))


def download_to(url, path, referer=None, timeout=900):
    """Stream a file to disk instead of building it in memory.

    The previous version did `data = r.read()` and then wrote `data`, which
    holds the ENTIRE file in RAM. A single etree concert set runs to hundreds
    of megabytes per FLAC, and this box has 15.4 GB of RAM shared with 23
    containers and a WSL VM already sitting at ~8 GB. One `get` of a large
    lossless item was enough to push the host into swap.

    Writes to a .part file and renames on success, so an interrupted download
    can never leave a truncated file that the 'skip (have)' check would then
    treat as complete on the next run.
    """
    req = urllib.request.Request(check_url(url), headers={"User-Agent": UA})
    if referer:
        req.add_header("Referer", referer)
    tmp = path + ".part"
    total = 0
    try:
        with OPENER.open(req, timeout=timeout) as r, open(tmp, "wb") as fh:
            since_check = 0
            while True:
                chunk = r.read(1024 * 256)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
                # Enforce the headroom DURING the transfer, not just before it.
                # The pre-flight check in download() compares against a declared
                # size, and the declared size is routinely 0 -- LoC and Jamendo
                # hardcode it, Openverse often returns null, and the
                # content_length fallback multiplies the first file's length by
                # the file count, which for a concert set of unequal tracks is a
                # guess rather than a bound. A pre-flight check against an
                # unknown size is not a limit, and D: is shared with the *arr
                # imports that will not notice this script filling it.
                since_check += len(chunk)
                if since_check >= 32 * 1024 * 1024:
                    since_check = 0
                    if free_bytes(os.path.dirname(tmp)) < MIN_FREE_BYTES:
                        raise IOError(
                            "stopped at %s: free space fell below the %s floor"
                            % (human(total), human(MIN_FREE_BYTES)))
        os.replace(tmp, path)
        return total
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def free_bytes(path):
    """Free space on the volume that holds path, walking up to the first
    directory that exists -- the artist/album folders are created after this
    check runs."""
    p = os.path.abspath(path)
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return 0
        p = parent
    try:
        return shutil.disk_usage(p).free
    except Exception:
        return 0


def try_http(label, *a, **kw):
    """A dead source must not take the whole search down with it. Report and
    carry on -- these are five independent third-party services and at least
    one of them is having a bad day at any given time."""
    try:
        return http(*a, **kw)
    except Exception as e:
        print("  (%s unavailable: %s)" % (label, str(e)[:70]))
        return None


def content_length(url, referer=None):
    try:
        # check_url here too. This was the one fetch in the file that built a
        # Request straight from a third-party URL without it.
        req = urllib.request.Request(check_url(url), headers={"User-Agent": UA},
                                     method="HEAD")
        if referer:
            req.add_header("Referer", referer)
        with OPENER.open(req, timeout=25) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


def human(n):
    if not n:
        return "size unknown"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0


def same_content(a, b):
    """Is b byte-identical to a, near enough to call it a duplicate?

    organize --apply used to decide this on FILE SIZE ALONE, which is not an
    identity test: two different tracks of equal byte length were reported as
    'dup' and the source file was quietly left where it was. Nothing was
    deleted, so this never lost data by itself -- but it told the user the move
    was complete, and someone clearing the source folder afterwards on that
    basis would lose the file for real.

    Size plus the head and tail of the content. Not a full hash: these are
    multi-megabyte audio files on a spinning disk and a full read of both sides
    of every collision would dominate the run. Two distinct audio files sharing
    a size, a first 256 KB and a last 256 KB is not a case worth engineering
    against.
    """
    try:
        sa, sb = os.path.getsize(a), os.path.getsize(b)
    except OSError:
        return False
    if sa != sb:
        return False

    def edges(path, size):
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            h.update(fh.read(262144))
            if size > 524288:
                fh.seek(-262144, os.SEEK_END)
                h.update(fh.read(262144))
        return h.digest()

    try:
        return edges(a, sa) == edges(b, sb)
    except OSError:
        return False


def safe(s, limit=60):
    """Windows rejects <>:"/\\|?* in filenames, and a trailing dot or space
    makes a directory that Explorer can create but not delete."""
    s = re.sub(r'[<>:"/\\|?*]', "_", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:limit].strip(" .")


# ---------------------------------------------------------------------------
# Licence handling

CC_NAMES = {
    "by": "CC BY", "by-sa": "CC BY-SA", "by-nc": "CC BY-NC",
    "by-nd": "CC BY-ND", "by-nc-sa": "CC BY-NC-SA",
    "by-nc-nd": "CC BY-NC-ND", "cc0": "CC0 (public domain)",
    "pdm": "Public Domain Mark", "publicdomain": "Public domain",
}

# Internet Archive collections whose membership rules already guarantee the
# material is free to take, so an item in one of them without an explicit
# licenceurl field is still safe. etree only admits bands that have publicly
# permitted taping; musopen commissions and releases public-domain classical;
# 78rpm holds pre-1929 discs.
IA_FREE_COLLECTIONS = {"etree", "musopen", "netlabels", "78rpm", "georgeblood"}


def nice_licence(url_or_text):
    t = str(url_or_text or "").strip()
    if not t:
        return ""
    m = re.search(r"creativecommons\.org/(?:licenses|publicdomain)/([a-z0-9-]+)", t)
    if m:
        key = m.group(1)
        return "%s (%s)" % (CC_NAMES.get(key, key.upper()), t)
    return t


# ---------------------------------------------------------------------------
# Sources. Each returns a list of dicts:
#   source, id, title, artist, album, year, licence, page, files=[{url,name}]
# files may be empty, in which case resolve() fills it in at download time.


# Spoken-word shapes that survive the music-collection filter. opensource_audio
# and audio_music both accept community uploads, and podcasters file their back
# catalogue there constantly -- a search for "josh woodward" returned
# "episode-585-the-flan-in-the-system" ahead of his actual albums, and ccMixter
# returns its weekly radio show alongside tracks. Collection filtering alone was
# not enough; this is the second pass.
SPOKEN = re.compile(
    r"(^|\b)(episode[\s\-_]*\d+|ep[\s\-_]?\d{1,4}\b|podcast|radio\s*(show|hour)|"
    r"sermon|lecture|interview|audiobook|talk\s*show|news\s*(cast|hour))", re.I)


def looks_spoken(*fields):
    return any(SPOKEN.search(str(f or "")) for f in fields)


def s_archive(q, limit):
    # mediatype:audio alone is not enough. archive.org indexes podcast
    # transcripts and show notes as full text, so a search for a musician
    # returns every episode that CREDITED them rather than their music --
    # "kevin macleod" produced eight aviation podcasts and zero tracks.
    # Restricting to the music collections is what makes results be music.
    cols = "etree OR audio_music OR netlabels OR opensource_audio OR musopen OR 78rpm"
    d = try_http("Internet Archive", "https://archive.org/advancedsearch.php", {
        "q": "(%s) AND mediatype:(audio) AND collection:(%s)" % (q, cols),
        "fl[]": "identifier", "rows": limit, "page": 1, "output": "json",
    })
    if not d:
        return []
    out = []
    for x in d.get("response", {}).get("docs", []):
        # advancedsearch only reliably returns the fields it feels like
        # returning, so title/creator get filled from /metadata at resolve
        # time. The identifier is the only thing that matters here.
        ident = x.get("identifier")
        if ident and not looks_spoken(ident):
            out.append({"source": "archive", "id": ident, "title": ident,
                        "artist": "", "album": "", "year": "", "licence": None,
                        "page": "https://archive.org/details/" + ident,
                        "files": []})
    return out


def s_archive_fill(items):
    """One extra scrape pass to turn identifiers into readable titles. Doing
    it here rather than per-item keeps the listing informative without a
    metadata call per result blowing the search out to a minute."""
    if not items:
        return
    ids = " OR ".join('"%s"' % i["id"] for i in items)
    d = try_http("Internet Archive titles", "https://archive.org/advancedsearch.php", {
        "q": "identifier:(%s)" % ids,
        "fl[]": "identifier", "rows": len(items), "output": "json",
    })
    # advancedsearch cannot return creator/title reliably in one shot for a
    # large OR query; when it does not, the identifier stands in. Details are
    # always shown from /metadata before anything downloads, so nothing is
    # decided on a missing title.
    if not d:
        return
    for x in d.get("response", {}).get("docs", []):
        for i in items:
            if i["id"] == x.get("identifier"):
                t = x.get("title")
                if t:
                    i["title"] = str(t)


def s_openverse(q, limit):
    # freesound is excluded on purpose: it is a sound-effects library, so a
    # search for a genre returns door slams and rain loops mixed into the
    # music results.
    d = try_http("Openverse", "https://api.openverse.org/v1/audio/", {
        "q": q, "page_size": limit, "source": "jamendo,wikimedia_audio",
    })
    if not d:
        return []
    out = []
    for r in d.get("results", []):
        url = r.get("url")
        if not url:
            continue
        aset = r.get("audio_set") or {}
        lic = r.get("license_url") or ""
        if not lic and r.get("license"):
            lic = "https://creativecommons.org/licenses/%s/%s/" % (
                r["license"], r.get("license_version") or "4.0")
        ext = "." + (r.get("filetype") or "mp3").lower().lstrip(".")
        out.append({
            "source": "openverse/" + str(r.get("source") or "?"),
            "id": str(r.get("id")),
            "title": str(r.get("title") or "untitled"),
            "artist": str(r.get("creator") or ""),
            "album": str(aset.get("title") or ""),
            "year": "", "licence": lic,
            "page": r.get("foreign_landing_url") or "",
            "files": [{"url": url,
                       "name": safe(r.get("title") or "track") + ext,
                       "size": r.get("filesize") or 0}],
        })
    return out


def s_ccmixter(q, limit):
    d = try_http("ccMixter", "https://ccmixter.org/api/query", {
        "f": "json", "limit": limit, "search": q, "type": "tracks",
    })
    if not d:
        return []
    out = []
    for u in d if isinstance(d, list) else []:
        files = []
        for f in u.get("files", []):
            url = f.get("download_url")
            name = f.get("file_name") or ""
            if url and name.lower().endswith(AUDIO_EXT):
                files.append({"url": url, "name": name,
                              "size": int(f.get("file_size") or 0)})
        if not files or looks_spoken(u.get("upload_name")):
            continue
        # A ccMixter upload is ONE track, published as an mp3 plus one or more
        # FLAC variants (_1, _2 ... are alternate masters, not extra songs).
        # Taking every file put four copies of "Xtended Chords" in the library
        # and showed it as a four-track album. Keep the single best file:
        # lossless if offered, largest of those, else the mp3.
        flac = [f for f in files if f["name"].lower().endswith((".flac", ".wav"))]
        files = [max(flac or files, key=lambda f: f.get("size") or 0)]
        out.append({
            "source": "ccmixter", "id": str(u.get("upload_id")),
            "title": str(u.get("upload_name") or "untitled"),
            "artist": str(u.get("user_name") or ""), "album": "",
            "year": str(u.get("upload_date") or "")[:4],
            "licence": u.get("license_url") or u.get("license_name") or "",
            "page": u.get("file_page_url") or "",
            # ccMixter returns 403 on a bare GET to its content host. It is
            # not blocking scripts, it is blocking hotlinking -- sending the
            # site as Referer is all it wants.
            "referer": "https://ccmixter.org/",
            "files": files,
        })
    return out


def s_loc(q, limit):
    d = try_http("Library of Congress", "https://www.loc.gov/audio/", {
        "q": q, "fo": "json", "c": limit, "at": "results",
    })
    if not d:
        return []
    out = []
    for r in d.get("results", []) if isinstance(d, dict) else []:
        # Only the National Jukebox items are unambiguously public domain.
        # loc.gov/audio also indexes rights-restricted broadcast archives, and
        # nothing in the JSON marks those clearly enough to trust, so filter
        # to jukebox rather than guess.
        ident = str(r.get("id") or "")
        if "jukebox" not in ident.lower():
            continue
        if r.get("access_restricted"):
            continue
        files = []
        for res in r.get("resources", []) or []:
            m = res.get("media")
            if m and str(m).lower().endswith((".mp3", ".wav")):
                files.append({"url": m, "name": safe(r.get("title") or "track") + ".mp3",
                              "size": 0})
        if not files:
            continue
        out.append({
            "source": "loc-jukebox", "id": ident.rstrip("/").split("/")[-1],
            "title": str(r.get("title") or "untitled"),
            "artist": ", ".join(r.get("contributor") or [])[:60],
            "album": "National Jukebox",
            "year": str((r.get("dates") or [""])[0])[:4],
            "licence": "Public domain (Library of Congress National Jukebox)",
            "page": ident, "files": files[:1],
        })
    return out


def s_jamendo(q, limit):
    cid = os.environ.get("JAMENDO_CLIENT_ID")
    if not cid:
        return []
    d = try_http("Jamendo", "https://api.jamendo.com/v3.0/tracks", {
        "client_id": cid, "format": "json", "limit": limit, "search": q,
        "audiodownload_allowed": "true",
    })
    if not d:
        return []
    out = []
    for t in d.get("results", []):
        url = t.get("audiodownload")
        if not url:
            continue
        out.append({
            "source": "jamendo", "id": str(t.get("id")),
            "title": str(t.get("name") or "untitled"),
            "artist": str(t.get("artist_name") or ""),
            "album": str(t.get("album_name") or ""),
            "year": str(t.get("releasedate") or "")[:4],
            "licence": t.get("license_ccurl") or "",
            "page": t.get("shareurl") or "",
            "files": [{"url": url, "name": safe(t.get("name") or "track") + ".mp3",
                       "size": 0}],
        })
    return out


SOURCES = [
    ("Internet Archive (public domain / artist-permitted / CC)", s_archive),
    ("Openverse -> Jamendo + Wikimedia Commons (Creative Commons)", s_openverse),
    ("ccMixter (Creative Commons)", s_ccmixter),
    ("Library of Congress National Jukebox (public domain)", s_loc),
    ("Jamendo direct (Creative Commons, needs JAMENDO_CLIENT_ID)", s_jamendo),
]


# ---------------------------------------------------------------------------
# Resolve: turn a search hit into concrete files + a real licence


def resolve(item):
    if item["source"] != "archive":
        return item
    meta = try_http("archive metadata", "https://archive.org/metadata/" + item["id"])
    if not meta:
        return item
    m = meta.get("metadata", {}) or {}
    c = m.get("creator") or ""
    if isinstance(c, list):
        c = c[0] if c else ""
    item["title"] = str(m.get("title") or item["title"])
    item["artist"] = str(c)
    item["album"] = str(m.get("album") or m.get("title") or item["title"])
    item["year"] = str(m.get("year") or m.get("date") or "")[:4]

    lic = m.get("licenseurl") or m.get("rights") or m.get("usage") or ""
    cols = m.get("collection") or []
    if isinstance(cols, str):
        cols = [cols]
    if not lic and set(cols) & IA_FREE_COLLECTIONS:
        which = sorted(set(cols) & IA_FREE_COLLECTIONS)[0]
        lic = "Free by collection policy (archive.org/%s)" % which
    item["licence"] = lic

    files = [f for f in meta.get("files", [])
             if str(f.get("name", "")).lower().endswith(AUDIO_EXT)]
    # Prefer lossless when the item carries both, otherwise every concert
    # downloads twice the bytes for no gain.
    flac = [f for f in files if str(f["name"]).lower().endswith(".flac")]
    files = flac or files
    item["files"] = [{"url": "https://archive.org/download/%s/%s" % (
        item["id"], urllib.parse.quote(f["name"])),
        "name": os.path.basename(f["name"]),
        "size": int(f.get("size") or 0)} for f in files]
    return item


# ---------------------------------------------------------------------------
# Search / listing


def cmd_search(args):
    q = " ".join(args.query).strip()
    if not q:
        sys.exit("usage: music-fetch.sh search \"query\"")

    results = []
    for label, fn in SOURCES:
        hits = fn(q, args.limit)
        if fn is s_archive:
            s_archive_fill(hits)
        if fn is s_jamendo and not hits and not os.environ.get("JAMENDO_CLIENT_ID"):
            print("\n=== %s ===" % label)
            print("  skipped - no key set. Free key: developer.jamendo.com")
            continue
        print("\n=== %s ===" % label)
        if not hits:
            print("  no results")
            continue
        for h in hits:
            results.append(h)
            n = len(results)
            who = h["artist"] or "unknown artist"
            lic = nice_licence(h["licence"]) if h["licence"] else "licence checked before download"
            print("  [%2d] %s" % (n, h["title"][:66]))
            print("       %s  %s" % (who[:40], h["year"]))
            print("       %s" % lic[:78])

    if not results:
        print("\nNothing found. These archives hold public-domain and Creative")
        print("Commons material only, so commercial releases will not appear.")
        return

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"query": q, "results": results}, f)

    print("\n%d result(s). Nothing has been downloaded." % len(results))
    if not sys.stdin.isatty():
        print("Download with:  bash music-fetch.sh get <number>")
        return

    # The whole point of the prompt: searching never writes anything to disk,
    # downloading is always a separate, explicit yes.
    print("Download which? numbers like  3  or  1,4,7   /  a = all  /  n = none")
    try:
        pick = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if not pick or pick in ("n", "no"):
        print("Nothing downloaded.")
        return
    if pick in ("a", "all"):
        chosen = list(range(1, len(results) + 1))
    else:
        chosen = []
        for part in re.split(r"[,\s]+", pick):
            if part.isdigit() and 1 <= int(part) <= len(results):
                chosen.append(int(part))
        if not chosen:
            print("Did not understand '%s'. Nothing downloaded." % pick)
            return
    for n in chosen:
        download(results[n - 1], args)


# ---------------------------------------------------------------------------
# Download


def download(item, args):
    item = resolve(item)
    lic = item.get("licence") or ""
    dest_artist = safe(item["artist"] or "Unknown Artist", 50) or "Unknown Artist"
    dest_album = safe(item["album"] or item["title"] or "Singles", 60) or "Singles"
    dest = os.path.join(MUSIC_DIR, dest_artist, dest_album)
    total = sum(f.get("size") or 0 for f in item["files"])
    if not total and item["files"]:
        total = content_length(item["files"][0]["url"], item.get("referer")) * len(item["files"])

    print("\n" + "-" * 72)
    print("  title   : %s" % item["title"][:66])
    print("  artist  : %s" % (item["artist"] or "unknown"))
    print("  source  : %s" % item["source"])
    print("  page    : %s" % (item["page"] or "-"))
    print("  licence : %s" % (nice_licence(lic) if lic else "NOT STATED"))
    print("  files   : %d audio file(s), %s" % (len(item["files"]), human(total)))
    print("  dest    : %s" % dest)

    if not item["files"]:
        print("  -> no audio files in this item, skipping")
        return

    if not lic:
        # Absence of a licence field is not permission. An unlicensed item is
        # exactly the case this script exists to avoid downloading by accident.
        print("  -> SKIPPED: no licence stated, so this is not verifiably free.")
        print("     Check %s yourself, then re-run with --allow-unknown to take it."
              % (item["page"] or "the source page"))
        if not args.allow_unknown:
            return
        print("     --allow-unknown given, continuing anyway.")

    if not args.yes:
        if not sys.stdin.isatty():
            print("  -> not a terminal and --yes not given, skipping")
            return
        try:
            ans = input("  Download this? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if ans not in ("y", "yes"):
            print("  -> skipped")
            return

    # D: is 450 GB and shared with the whole media library, and nothing here
    # asks Sonarr or qBittorrent what they are about to write. Refusing at 5 GB
    # of headroom keeps a large lossless item from being the thing that fills
    # the disk out from under the *arr imports.
    free = free_bytes(dest)
    if free and free < MIN_FREE_BYTES:
        print("  -> REFUSED: only %s free on the music volume (need %s headroom)"
              % (human(free), human(MIN_FREE_BYTES)))
        return
    if total and free and total > free - MIN_FREE_BYTES:
        print("  -> REFUSED: this item is %s and only %s is free (keeping %s headroom)"
              % (human(total), human(free), human(MIN_FREE_BYTES)))
        return

    os.makedirs(dest, exist_ok=True)
    ok = fail = 0
    for f in item["files"]:
        out = os.path.join(dest, safe(f["name"], 120))
        if os.path.exists(out) and os.path.getsize(out) > 0:
            print("  skip (have)  %s" % f["name"][:56])
            continue
        # Re-checked per file as well as per 32 MB inside download_to, so a
        # multi-file item stops at the floor instead of working through the
        # rest of the track list once the disk is already gone.
        if free_bytes(dest) < MIN_FREE_BYTES:
            print("  -> STOPPED: only %s free, at the %s floor. %d file(s) not fetched."
                  % (human(free_bytes(dest)), human(MIN_FREE_BYTES),
                     len(item["files"]) - ok - fail))
            break
        try:
            n = download_to(f["url"], out, referer=item.get("referer"), timeout=900)
            print("  ok   %-10s %s" % (human(n), f["name"][:52]))
            tag_provenance(out, item)
            ok += 1
        except Exception as e:
            print("  FAILED       %s  (%s)" % (f["name"][:40], str(e)[:40]))
            if os.path.exists(out):
                os.remove(out)
            fail += 1
    print("  -> %d downloaded, %d failed" % (ok, fail))


def tag_provenance(path, item):
    """Stamp artist/album/title and the licence into the file itself. Two
    reasons: Navidrome reads tags rather than paths, and the licence needs to
    travel with the file -- a folder name is not evidence that something was
    free to take."""
    try:
        import mutagen
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3, TXXX
    except Exception:
        return
    try:
        try:
            audio = EasyID3(path)
        except Exception:
            audio = mutagen.File(path, easy=True)
        if audio is None:
            return
        if not audio.get("artist") and item.get("artist"):
            audio["artist"] = item["artist"]
        # Falling back to the item title rather than leaving this empty:
        # Navidrome files an untagged track under "[Unknown Album]", which is
        # where every single-track download used to land.
        if not audio.get("album"):
            audio["album"] = item.get("album") or item.get("title") or "Singles"
        # The archive's own title, not the filename. Straight from the source
        # this reads "Xtended Chords"; from the filename it read
        # "Javolenus_-_Xtended_Chords_2", which is what shipped before.
        if not audio.get("title"):
            audio["title"] = item.get("title") or \
                os.path.splitext(os.path.basename(path))[0]
        audio.save()
        if path.lower().endswith(".mp3") and item.get("licence"):
            t = ID3(path)
            t.add(TXXX(encoding=3, desc="LICENCE",
                       text="%s | %s" % (nice_licence(item["licence"])[:120],
                                         item.get("page") or item["source"])))
            t.save()
    except Exception:
        pass


def cmd_get(args):
    ref = args.what
    if ref.isdigit():
        if not os.path.exists(STATE_FILE):
            sys.exit("no previous search. Run: music-fetch.sh search \"query\"")
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        rs = st.get("results", [])
        n = int(ref)
        if not (1 <= n <= len(rs)):
            sys.exit("result %d not in the last search (1-%d)" % (n, len(rs)))
        download(rs[n - 1], args)
    else:
        # Backwards compatible with the old "get <archive-identifier>" form.
        download({"source": "archive", "id": ref, "title": ref, "artist": args.artist or "",
                  "album": "", "year": "", "licence": None,
                  "page": "https://archive.org/details/" + ref, "files": []}, args)


# ---------------------------------------------------------------------------
# Organize: sort an existing pile into Artist/Album and fix the tags

# Filename shapes seen in the wild, most specific first. Each returns
# (artist, title) -- album comes from tags or the parent folder.
FN_PATTERNS = [
    # "01 - Artist - Title", "01. Artist - Title"
    (re.compile(r"^\s*\d{1,3}\s*[-._)]\s*(?P<artist>.+?)\s+-\s+(?P<title>.+)$"), True),
    # "Artist - Title"
    (re.compile(r"^\s*(?P<artist>.+?)\s+-\s+(?P<title>.+)$"), True),
    # "01 - Title", "01. Title", "01 Title"  -- artist unknown, comes from folder
    (re.compile(r"^\s*\d{1,3}\s*[-._)]?\s+(?P<title>.+)$"), False),
]

JUNK = re.compile(
    r"\b(official\s*(music\s*)?video|lyrics?|audio|hd|hq|4k|720p|1080p|"
    r"free\s*download|youtube|mp3juices?|downloaded\s*from)\b", re.I)


def clean_title(s):
    s = re.sub(r"[\[\(]\s*\]?\s*[\)\]]?", " ", JUNK.sub(" ", s))
    s = re.sub(r"[_]+", " ", s)
    s = re.sub(r"\s*[\[\(]\s*[\)\]]\s*", " ", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -_.")


def read_tags(path):
    try:
        import mutagen
        a = mutagen.File(path, easy=True)
        if not a:
            return {}
        g = lambda k: (a.get(k) or [""])[0].strip()
        return {"artist": g("albumartist") or g("artist"), "album": g("album"),
                "title": g("title"), "track": g("tracknumber").split("/")[0]}
    except Exception:
        return {}


def guess(path):
    """Work out artist/album/title for one file. Tags win; the filename and
    the parent folder fill the gaps. Jumbled collections are usually 'Artist -
    Song.mp3' loose in one folder, or a folder named 'Artist - Album'."""
    t = read_tags(path)
    artist, album, title, track = (t.get("artist", ""), t.get("album", ""),
                                   t.get("title", ""), t.get("track", ""))

    stem = clean_title(os.path.splitext(os.path.basename(path))[0])
    parent = os.path.basename(os.path.dirname(path))

    if not (artist and title):
        for rx, has_artist in FN_PATTERNS:
            m = rx.match(stem)
            if not m:
                continue
            if has_artist and not artist:
                artist = clean_title(m.group("artist"))
            if not title:
                title = clean_title(m.group("title"))
            break
    if not title:
        title = stem
    # "Artist - Album" folder, the other common jumbled shape
    if not artist or not album:
        pm = re.match(r"^\s*(?P<a>.+?)\s+-\s+(?P<b>.+)$", parent)
        if pm:
            artist = artist or clean_title(pm.group("a"))
            album = album or clean_title(pm.group("b"))
        elif parent and parent.lower() not in ("music", "downloads", "songs", "new folder"):
            artist = artist or clean_title(parent)

    m = re.match(r"^\s*(\d{1,3})\b", os.path.basename(path))
    if not track and m:
        track = m.group(1)

    artist = safe(artist, 60) or "Unknown Artist"
    # Loose singles do not belong in a fake album -- Navidrome shows a
    # one-track album per file otherwise, which is worse than one bucket.
    album = safe(album, 60) or "Singles"
    title = safe(title, 90) or "Untitled"
    return artist, album, title, track


def cmd_organize(args):
    src = winpath(args.path or MUSIC_DIR)
    if not os.path.isdir(src):
        sys.exit("not a directory: %s" % src)

    files = []
    for root, _dirs, names in os.walk(src):
        for n in names:
            if n.lower().endswith(AUDIO_EXT):
                files.append(os.path.join(root, n))
    if not files:
        print("No audio files under %s" % src)
        return

    print("Scanning %d audio file(s) under %s" % (len(files), src))
    print("Target layout: %s\\<Artist>\\<Album>\\<track> <Title>.<ext>\n" % MUSIC_DIR)

    plan, skipped = [], 0
    for p in sorted(files):
        artist, album, title, track = guess(p)
        ext = os.path.splitext(p)[1].lower()
        prefix = ("%02d " % int(track)) if track.isdigit() else ""
        target = os.path.join(MUSIC_DIR, artist, album, safe(prefix + title, 110) + ext)
        if os.path.normcase(os.path.abspath(target)) == os.path.normcase(os.path.abspath(p)):
            skipped += 1
            continue
        plan.append((p, target, artist, album, title))

    if not plan:
        print("Everything is already in place (%d file(s))." % skipped)
        return

    for p, target, artist, album, title in plan:
        print("  %s" % os.path.relpath(p, src))
        print("    -> %s\\%s\\%s" % (artist, album, os.path.basename(target)))
    print("\n%d file(s) to move, %d already correct." % (len(plan), skipped))

    if not args.apply:
        # Dry run is the default because this moves real files. Nothing here
        # deletes anything, but a bad guess still scatters a library.
        print("\nDRY RUN - nothing was moved.")
        print("Re-run with --apply to do it:  bash music-fetch.sh organize %s --apply"
              % (args.path or ""))
        return

    if not args.yes and sys.stdin.isatty():
        try:
            if input("\nMove these %d file(s)? [y/N] " % len(plan)).strip().lower() \
                    not in ("y", "yes"):
                print("Nothing moved.")
                return
        except (EOFError, KeyboardInterrupt):
            print()
            return

    moved = failed = 0
    for p, target, artist, album, title in plan:
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            final = target
            i = 2
            while os.path.exists(final):
                if same_content(p, final):
                    break  # genuinely the same file already there
                base, ext = os.path.splitext(target)
                final = "%s (%d)%s" % (base, i, ext)
                i += 1
            if os.path.exists(final) and same_content(p, final):
                # Left in place on purpose rather than deleted -- this script
                # moves and never removes. Say where it still is, so 'dup' is
                # not mistaken for 'handled, safe to clear the source folder'.
                print("  dup  %s  (left at %s)"
                      % (os.path.basename(p)[:50], os.path.relpath(p, src)))
                continue
            shutil.move(p, final)
            if not args.no_fix_tags:
                write_tags(final, artist, album, title)
            moved += 1
        except Exception as e:
            print("  FAILED %s (%s)" % (os.path.basename(p)[:50], str(e)[:50]))
            failed += 1

    # Empty leftovers only. rmdir refuses on anything non-empty, which is the
    # safety property that matters -- no recursive delete anywhere here.
    for root, dirs, _f in os.walk(src, topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass

    print("\nMoved %d, failed %d." % (moved, failed))
    print("Navidrome rescans hourly. To index now:  docker restart navidrome")


def write_tags(path, artist, album, title):
    """Navidrome groups by tags. A file moved into the right folder but still
    tagged 'Track 01' by an unknown artist shows up wrong in the UI, so the
    move and the retag have to happen together."""
    try:
        import mutagen
        a = mutagen.File(path, easy=True)
        if a is None:
            return
        if not (a.get("artist") or [""])[0].strip():
            a["artist"] = artist
        if not (a.get("album") or [""])[0].strip():
            a["album"] = album
        if not (a.get("title") or [""])[0].strip():
            a["title"] = title
        a.save()
    except Exception:
        pass


def cmd_sources(_args):
    print("Sources this script searches, all free-licence only:\n")
    for label, fn in SOURCES:
        state = "ready"
        if fn is s_jamendo and not os.environ.get("JAMENDO_CLIENT_ID"):
            state = "disabled (set JAMENDO_CLIENT_ID, free at developer.jamendo.com)"
        print("  %-62s %s" % (label, state))
    print("\nLibrary root : %s" % MUSIC_DIR)
    print("Last search  : %s" % (STATE_FILE if os.path.exists(STATE_FILE) else "none yet"))


# ---------------------------------------------------------------------------


def main():
    p = argparse.ArgumentParser(prog="music-fetch", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-y", "--yes", action="store_true",
                        help="do not ask before downloading/moving")
    common.add_argument("--allow-unknown", action="store_true",
                        help="allow items with no stated licence (off by default)")

    ps = sub.add_parser("search", parents=[common])
    ps.add_argument("query", nargs="+")
    ps.add_argument("--limit", type=int, default=8, help="results per source (default 8)")
    ps.set_defaults(func=cmd_search)

    pg = sub.add_parser("get", parents=[common])
    pg.add_argument("what", help="result number from the last search, or an archive.org identifier")
    pg.add_argument("artist", nargs="?", default="")
    pg.set_defaults(func=cmd_get)

    po = sub.add_parser("organize", parents=[common])
    po.add_argument("path", nargs="?", default="",
                    help="folder to sort (default: the Navidrome library root)")
    po.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    po.add_argument("--no-fix-tags", action="store_true", help="move files but leave tags alone")
    po.set_defaults(func=cmd_organize)

    pl = sub.add_parser("sources")
    pl.set_defaults(func=cmd_sources)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
