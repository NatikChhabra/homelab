#!/usr/bin/env python3
# offline-playlists.py -- keep the two playlists the phone keeps downloaded in step with the library.
#
# Added 2026-11-03. The Octo phone app downloads a playlist for offline ("Keep downloaded") but
# skips read-only smart playlists, so these are plain playlists this job rewrites:
#   - "Everything"  = every song in the library, newest first. Kept downloaded on the phone, so a
#                     new song lands on the phone at its next sync (how Symfonium worked).
#   - "Liked Songs" = the favourites, newest first. Pinned to Octo's Home so they are easy to see.
# A playlist is only rewritten when its songs or order changed, and keeps its id (the phone's
# Keep downloaded switch is tied to the id). DRY_RUN=1 reports only. Run through scripts/run-py.sh.
import hashlib, json, os, secrets, sys, urllib.parse, urllib.request

U, P = os.environ.get('NAVIDROME_USER', ''), os.environ.get('NAVIDROME_PASS', '')
BASE = 'http://localhost:4533/rest/'
DRY = os.environ.get('DRY_RUN') == '1'


def call(ep, pairs=()):
    s = secrets.token_hex(6)
    auth = [('u', U), ('t', hashlib.md5((P + s).encode()).hexdigest()), ('s', s), ('v', '1.16.1'), ('c', 'offline-playlists'), ('f', 'json')]
    data = urllib.parse.urlencode(auth + list(pairs)).encode()   # POST: Everything is hundreds of ids
    r = json.loads(urllib.request.urlopen(BASE + ep, data=data, timeout=120).read())['subsonic-response']
    if r['status'] != 'ok':
        raise RuntimeError(f"{ep}: {r.get('error')}")
    return r


def library():
    songs, off = [], 0
    while True:
        page = call('search3', [('query', ''), ('songCount', 500), ('songOffset', off), ('artistCount', 0), ('albumCount', 0)]).get('searchResult3', {}).get('song', [])
        songs += page; off += 500
        if len(page) < 500:
            return songs


def sync(name, want, playlists):
    mine = [p for p in playlists if p['name'] == name and p.get('owner') == U and not p.get('readonly')]
    if not mine:
        print(f'{name}: creating with {len(want)} songs')
        if not DRY:
            call('createPlaylist', [('name', name)] + [('songId', i) for i in want])
        return
    pid = mine[0]['id']
    have = [e['id'] for e in call('getPlaylist', [('id', pid)])['playlist'].get('entry', [])]
    if have == want:
        print(f'{name}: up to date ({len(want)} songs)')
        return
    print(f'{name}: {len(have)} -> {len(want)} songs (+{len(set(want) - set(have))} -{len(set(have) - set(want))})')
    if not DRY:
        call('createPlaylist', [('playlistId', pid)] + [('songId', i) for i in want])   # replaces the songs, same id


def main():
    if not U or not P:
        sys.exit('NAVIDROME_USER/NAVIDROME_PASS not set')
    songs = library()
    if len(songs) < 50:   # a half-mounted music drive must not empty the phone's downloads
        sys.exit(f'only {len(songs)} songs in the library -- refusing to rewrite playlists')
    everything = [s['id'] for s in sorted(songs, key=lambda s: s.get('created', ''), reverse=True)]
    liked = call('getStarred2').get('starred2', {}).get('song', [])
    liked = [s['id'] for s in sorted(liked, key=lambda s: s.get('starred', ''), reverse=True)]
    playlists = call('getPlaylists').get('playlists', {}).get('playlist', [])
    sync('Everything', everything, playlists)
    sync('Liked Songs', liked, playlists)


if __name__ == '__main__':
    main()
