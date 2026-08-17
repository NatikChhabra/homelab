#!/usr/bin/env python3
"""
Tag Navidrome music library using MusicBrainz.
Writes genre, year, track_number, MusicBrainz IDs to m4a/mp3 files.
Then triggers Navidrome rescan.
"""
import os
import sys
import time
import urllib.request
import urllib.error
import musicbrainzngs
from mutagen.mp4 import MP4
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TYER, TCON, TRCK, TPOS

musicbrainzngs.set_useragent("NavidromeTagger", "1.0", "natik@example.com")

MUSIC_DIR = r"D:\Media\music"
NAVIDROME = "http://localhost:4553"

def get_ext(path):
    return path.lower().rsplit(".", 1)[-1] if "." in path else ""

def is_audio(path):
    return get_ext(path) in {"m4a", "mp3", "flac", "ogg", "wav"}

def parse_path(filepath):
    """Extract artist, title, album from filepath.
    Pattern: /music/Artist/Album/Title.m4a  OR  /music/Artist - Title.m4a
    """
    name = os.path.basename(filepath)
    rel = os.path.relpath(filepath, MUSIC_DIR)
    parts = rel.replace("\\", "/").split("/")

    # Case 1: root-level file like "Artist - Title.m4a"
    if len(parts) == 1:
        base = name.rsplit(".", 1)[0] if "." in name else name
        if " - " in base:
            a, t = base.split(" - ", 1)
            return a.strip(), t.strip(), ""
        return "", base.strip(), ""

    # Case 2: /Artist/Album/Title.m4a or /Artist/Title.m4a
    artist = parts[0].strip()
    if len(parts) >= 3:
        album = parts[-2].strip()
    else:
        album = ""
    base = name.rsplit(".", 1)[0] if "." in name else name
    # If filename starts with artist name, extract title
    if base.lower().startswith(artist.lower()):
        title = base[len(artist):].strip(" -_")
    else:
        title = base.strip()
    return artist, title, album

def search_mb(artist, title, album, limit=5):
    """Search MusicBrainz, return list of release dicts."""
    queries = []
    if artist and title:
        queries.append(f'artist:"{artist}" recording:"{title}"')
    if artist and album:
        queries.append(f'artist:"{artist}" release:"{album}"')
    if title and album:
        queries.append(f'recording:"{title}" release:"{album}"')
    if title:
        queries.append(f'rec:"{title}"')
    if album:
        queries.append(f'release:"{album}"')

    seen = set()
    results = []
    for q in queries:
        try:
            r = musicbrainzngs.search_releases(query=q, limit=limit)
            for rel in r.get("release-list", []):
                rid = rel.get("id", "")
                if rid not in seen:
                    seen.add(rid)
                    results.append(rel)
                    if len(results) >= limit:
                        break
        except Exception:
            continue
        if len(results) >= limit:
            break
    return results

def get_release_info(mb_id):
    try:
        r = musicbrainzngs.get_release_by_id(mb_id, includes=["recordings", "media-formats", "artist-credits"])
        return r.get("release", {})
    except Exception:
        return {}

def write_m4a(path, tags):
    try:
        f = MP4(path)
        map5 = {
            "title": "\xa9nam", "artist": "\xa9ART", "album": "\xa9alb",
            "genre": "\xa9gen", "year": "\xa9day",
        }
        for k, v in tags.items():
            if not v or k not in map5:
                continue
            f[map5[k]] = v
        if "tracknumber" in tags and tags["tracknumber"]:
            f["trkn"] = [(int(tags["tracknumber"]), 999)]
        if "discnumber" in tags and tags["discnumber"]:
            f["disk"] = [(int(tags["discnumber"]), 999)]
        f.save()
        return True
    except Exception as e:
        print(f"    write error: {e}")
        return False

def write_mp3(path, tags):
    try:
        if not os.path.exists(path):
            return False
        audio = MP3(path, ID3=ID3)
        if not audio.tags:
            audio.add_tags()
        mapping = {
            "title": ("TIT2", TIT2), "artist": ("TPE1", TPE1),
            "album": ("TALB", TALB), "year": ("TYER", TYER),
            "genre": ("TCON", TCON), "tracknumber": ("TRCK", TRCK),
            "discnumber": ("TPOS", TPOS),
        }
        for k, v in tags.items():
            if not v or k not in mapping:
                continue
            fname, cls = mapping[k]
            audio.tags.add(cls(encoding=3, text=str(v)))
        audio.save()
        return True
    except Exception as e:
        print(f"    write error: {e}")
        return False

def tag_file(filepath):
    name = os.path.basename(filepath)
    artist, title, album = parse_path(filepath)

    print(f"[{name}]")
    print(f"  artist={artist!r} title={title!r} album={album!r}")

    # Clean up search terms
    clean_artist = artist.replace("'", "").replace("&", "and").strip()
    clean_title = title.replace("'", "").replace(" - ", " ").strip()
    clean_album = album.replace("'", "").strip() if album else ""

    releases = search_mb(clean_artist, clean_title, clean_album, limit=5)
    if not releases:
        releases = search_mb("", clean_title, clean_album, limit=5)
    if not releases:
        releases = search_mb("", clean_title, "", limit=5)
    if not releases:
        print("  NO MusicBrainz match -- skipping")
        return False

    rel = releases[0]
    rel_id = rel.get("id", "")
    rel_name = rel.get("title", "")
    rel_date = rel.get("date", "")
    rel_artist_list = rel.get("artist-credit", [])
    rel_artist = rel_artist_list[0]["name"] if rel_artist_list else ""

    print(f"  MATCH: {rel_name} by {rel_artist} ({rel_date})")

    # Find track number from release media
    track_num = 0
    disc_num = 0
    for medium in rel.get("medium-list", []):
        pos = medium.get("position", 0)
        if pos:
            disc_num = pos
        for track in medium.get("track-list", []):
            rec = track.get("recording", {})
            rec_title = rec.get("title", "")
            if rec_title.lower() == title.lower() or title.lower() in rec_title.lower():
                track_num = track.get("position", 0)
                break
        if track_num:
            break

    # Get genre from release group
    genre = ""
    rg = rel.get("release-group", {})
    for g in rg.get("genre-list", []):
        genre = g.get("name", "")
        break

    tags = {
        "title": title,
        "artist": rel_artist,
        "album": rel_name,
        "genre": genre,
        "year": rel_date[:4] if rel_date else "",
    }
    if track_num:
        tags["tracknumber"] = track_num
    if disc_num:
        tags["discnumber"] = disc_num

    success = write_m4a(filepath, tags) if get_ext(filepath) == "m4a" else write_mp3(filepath, tags)
    print(f"  written: {'OK' if success else 'FAIL'} track={track_num} genre={genre!r} year={tags['year']!r}")
    return success

def trigger_navidrome_scan():
    try:
        req = urllib.request.Request("http://localhost:4533/scan?force=true", method="POST")
        resp = urllib.request.urlopen(req, timeout=5)
        print(f"\nNavidrome scan triggered: {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"\nNavidrome scan endpoint error: {e.code} -- manual restart needed")
    except Exception as e:
        print(f"\nNavidrome scan trigger failed: {e}")

def main():
    print("=" * 60)
    print("Navidrome MusicBrainz Tagging")
    print(f"Library: {MUSIC_DIR}")
    print("=" * 60)

    files = []
    for root, dirs, filenames in os.walk(MUSIC_DIR):
        for fn in filenames:
            fp = os.path.join(root, fn)
            if is_audio(fp):
                files.append(fp)

    print(f"Found {len(files)} audio files\n")

    tagged = 0
    skipped = 0
    errors = 0

    for i, fp in enumerate(files):
        print(f"[{i+1}/{len(files)}]", end=" ")
        try:
            if tag_file(fp):
                tagged += 1
            else:
                skipped += 1
        except Exception as e:
            print(f"ERROR: {e}")
            errors += 1
        time.sleep(0.25)

    print(f"\n{'=' * 60}")
    print(f"Tagged: {tagged} | Skipped (no match): {skipped} | Errors: {errors}")
    print(f"{'=' * 60}")
    trigger_navidrome_scan()

if __name__ == "__main__":
    main()
