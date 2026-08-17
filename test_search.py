#!/usr/bin/env python3
"""Quick test of fixed search logic."""
import os
import time
import musicbrainzngs
musicbrainzngs.set_useragent('NavidromeTagger', '1.0', 'natik@example.com')

MUSIC_DIR = r'D:\Media\music'

def parse_path(filepath):
    name = os.path.basename(filepath)
    rel = os.path.relpath(filepath, MUSIC_DIR)
    parts = rel.replace("\\", "/").split("/")
    if len(parts) == 1:
        base = name.rsplit(".", 1)[0] if "." in name else name
        if " - " in base:
            a, t = base.split(" - ", 1)
            return a.strip(), t.strip(), ""
        return "", base.strip(), ""
    artist = parts[0].strip()
    album = parts[-2].strip() if len(parts) >= 3 else ""
    base = name.rsplit(".", 1)[0] if "." in name else name
    if base.lower().startswith(artist.lower()):
        title = base[len(artist):].strip(" -_")
    else:
        title = base.strip()
    return artist, title, album

def search_mb(artist, title, album, limit=5):
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
        except Exception as e:
            print(f"    ERROR: {e}")
            continue
        if len(results) >= limit:
            break
    return results

files = []
for root, dirs, filenames in os.walk(MUSIC_DIR):
    for fn in filenames:
        if fn.lower().endswith(('.m4a', '.mp3')):
            files.append(os.path.join(root, fn))

print(f"Testing {len(files)} files...\n")
matched = 0
for i, fp in enumerate(files[:15]):
    name = os.path.basename(fp)
    artist, title, album = parse_path(fp)
    clean_artist = artist.replace("'", "").replace("&", "and").strip()
    clean_title = title.replace("'", "").replace(" - ", " ").strip()
    clean_album = album.replace("'", "").strip() if album else ""

    releases = search_mb(clean_artist, clean_title, clean_album, limit=5)
    if releases:
        matched += 1
        rel = releases[0]
        print(f"{i+1}. {artist} - {title[:40]}: MATCH -> {rel['title']} ({rel.get('date','')[:4]})")
    else:
        print(f"{i+1}. {artist} - {title[:40]}: NO MATCH")
    time.sleep(1.1)

print(f"\nMatched {matched}/15 test files")
