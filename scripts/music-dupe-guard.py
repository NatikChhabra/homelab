#!/usr/bin/env python3
# music-dupe-guard.py -- undo duplicates that Octo downloads of songs you already own.
#
# Added 2026-11-02. Octo lists "outside" twins of owned songs whose artist differs only in
# order or junk ("From Smart Shuffle • X", "X Official", "X Music"), and its heart-time
# ownership check misses them too: hearting such a twin downloads a second (often lower
# quality) copy. Octo has no setting for this, so this guard cleans up after it, hourly.
#
# Rules (deliberately narrow):
#   - only files in Octo's own download history are ever moved; the owner's library is not
#   - a download is a duplicate when an OLDER library song has the same title (case,
#     punctuation and "(feat. ...)" ignored, so "sped up"/"remix"/"live" stay distinct),
#     a shared artist word once junk words are dropped, and a length within 6 seconds
#   - the duplicate (and its .lrc) moves to music/.octo-trash/dupe-guard/<date>/, never deleted;
#     the kept song becomes a favourite if the duplicate was one (the heart meant "I like it")
# DRY_RUN=1 reports only. Run through scripts/run-py.sh (secrets.env loaded).
import hashlib, json, os, re, secrets, shutil, sys, time, unicodedata, urllib.parse, urllib.request, http.cookiejar

MUSIC = r'D:\Media\music'
TRASH = os.path.join(MUSIC, '.octo-trash', 'dupe-guard', time.strftime('%Y-%m-%d'))
HISTORY = r'C:\ServerData\Octo\config\downloads-history.json'
JUNK = {'from', 'smart', 'shuffle', 'official', 'youtube', 'channel', 'music', 'topic', 'vevo',
        'records', 'and', 'feat', 'ft', 'with', 'the', 'x'}
U, P = os.environ.get('NAVIDROME_USER', ''), os.environ.get('NAVIDROME_PASS', '')


def norm_title(t):
    t = unicodedata.normalize('NFKC', t).lower()
    t = re.sub(r'\((feat|ft|with)\.?[^)]*\)', '', t)
    return re.sub(r'[\W_]+', '', t)


def artist_words(a):
    words = re.findall(r'\w+', unicodedata.normalize('NFKC', a).lower())
    return {w for w in words if w not in JUNK and len(w) > 1}


def same_song(new, old):
    """new/old: dicts with title, artist, duration (seconds or None)."""
    if norm_title(new['title']) != norm_title(old['title']):
        return False
    if not artist_words(new['artist']) & artist_words(old['artist']):
        return False
    a, b = new.get('duration'), old.get('duration')
    return not (a and b) or abs(a - b) <= 6


def demo():
    yours = {'title': 'Suniyan Suniyan', 'artist': 'Juss, MixSingh', 'duration': 201}
    assert same_song({'title': 'Suniyan Suniyan', 'artist': 'MixSingh, Juss', 'duration': 199}, yours)
    assert same_song({'title': 'we fell in love in october', 'artist': 'From Smart Shuffle • girl in red', 'duration': None},
                     {'title': 'we fell in love in october', 'artist': 'girl in red', 'duration': 184})
    assert same_song({'title': 'End of Beginning', 'artist': 'Djo Music', 'duration': 159},
                     {'title': 'End of Beginning', 'artist': 'Djo', 'duration': 159})
    assert not same_song({'title': 'we fell in love in october - sped up', 'artist': 'girl in red', 'duration': 150},
                         {'title': 'we fell in love in october', 'artist': 'girl in red', 'duration': 184})
    assert not same_song({'title': 'Raabta', 'artist': 'Some Cover Band', 'duration': 240},
                         {'title': 'Raabta', 'artist': 'Pritam, Arijit Singh', 'duration': 240})
    assert not same_song({'title': 'Lover', 'artist': 'Taylor Swift', 'duration': 300},
                         {'title': 'Lover', 'artist': 'Taylor Swift', 'duration': 221})
    print('demo ok')


def subsonic(ep, **q):
    salt = secrets.token_hex(6)
    q.update(u=U, t=hashlib.md5((P + salt).encode()).hexdigest(), s=salt, v='1.16.1', c='dupe-guard', f='json')
    with urllib.request.urlopen('http://127.0.0.1:4533/rest/' + ep + '?' + urllib.parse.urlencode(q), timeout=120) as r:
        return json.load(r)['subsonic-response']


def octo_downloads():
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    h = {'Content-Type': 'application/json', 'X-Octo-Admin': '1'}
    op.open(urllib.request.Request('http://127.0.0.1:5274/api/admin/browse/auth', method='POST', headers=h,
                                   data=json.dumps({'username': U, 'password': P}).encode()), timeout=60)
    with op.open(urllib.request.Request('http://127.0.0.1:5274/api/admin/downloads', headers=h), timeout=120) as r:
        api = json.load(r).get('downloads', [])
    # The API returns at most 200 entries (2026-11-03: 219 downloads existed), so add Octo's own
    # complete history file, read-only. Entries there use 'Path' instead of 'path'.
    try:
        hist = json.load(open(HISTORY, encoding='utf-8'))
        hist = hist if isinstance(hist, list) else hist.get('items') or hist.get('downloads') or []
    except (OSError, ValueError):
        hist = []
    return api + [{'path': e.get('Path', '')} for e in hist if isinstance(e, dict)]


def main():
    stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    dry = bool(os.environ.get('DRY_RUN'))
    # Octo writes /music/<file>; Navidrome reports paths relative to the same root.
    octo_files = {d['path'][len('/music/'):] for d in octo_downloads() if d.get('path', '').startswith('/music/')}
    songs, off = [], 0
    while True:
        page = subsonic('search3', query='', songCount=500, songOffset=off, artistCount=0, albumCount=0)['searchResult3'].get('song', [])
        songs += page; off += len(page)
        if len(page) < 500:
            break
    # Navidrome's 'path' is tag-based; map to real files by matching Octo's file names on disk.
    on_disk = {f: os.path.getmtime(os.path.join(MUSIC, f)) for f in os.listdir(MUSIC) if os.path.isfile(os.path.join(MUSIC, f))}
    def real_file(s):
        stem = f"{s['artist'].replace(', ', ';')} - {s['title']}"
        for cand in (stem, f"{s['artist']} - {s['title']}"):
            for f in on_disk:
                if os.path.splitext(f)[0] == cand and not f.endswith('.lrc'):
                    return f
        return None
    moved = 0
    for s in songs:
        f = real_file(s)
        if not f or f not in octo_files:
            continue
        new = {'title': s['title'], 'artist': s['artist'], 'duration': s.get('duration')}
        for o in songs:
            fo = real_file(o)
            if o['id'] == s['id'] or not fo or fo == f or on_disk[fo] >= on_disk[f]:
                continue
            if same_song(new, {'title': o['title'], 'artist': o['artist'], 'duration': o.get('duration')}):
                print(f"{stamp} DUPLICATE {'(dry run) ' if dry else ''}'{f}' of your '{fo}'")
                if not dry:
                    os.makedirs(TRASH, exist_ok=True)
                    for x in (f, os.path.splitext(f)[0] + '.lrc'):
                        if os.path.exists(os.path.join(MUSIC, x)):
                            shutil.move(os.path.join(MUSIC, x), os.path.join(TRASH, x))
                    if 'starred' in s and 'starred' not in o:
                        subsonic('star', id=o['id'])
                    moved += 1
                break
    if moved:
        subsonic('startScan')
    print(f"{stamp} checked {len(songs)} songs, {len(octo_files)} Octo downloads, moved {moved} duplicate(s)")
    print(f"{stamp} SUMMARY checked={len(songs)} moved={moved}")


if __name__ == '__main__':
    demo() if '--demo' in sys.argv else main()
