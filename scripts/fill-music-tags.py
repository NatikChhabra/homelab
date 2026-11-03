"""fill-music-tags.py -- fill ONLY missing genre / year / album-artist on .m4a files.

Added 2026-11-01 (audit finding: 15 tracks had none of the three). Looks each
track up in Apple's public iTunes Search API by artist + title, accepts a match
only when both artist and title agree, writes to a temp copy, re-reads the copy,
and only then replaces the original (D: is a flaky USB disk -- never edit in
place). Never touches title/album/artist. Re-runnable: tagged files are skipped.

    python fill-music-tags.py            # dry run: show what it would write
    python fill-music-tags.py --apply
"""
import json, os, re, shutil, sys, tempfile, time, unicodedata, urllib.parse, urllib.request
from mutagen.mp4 import MP4

sys.stdout.reconfigure(encoding='utf-8')
MUSIC = r'D:\Media\music'
APPLY = '--apply' in sys.argv

def norm(s):
    s = unicodedata.normalize('NFKC', s or '').lower()
    s = re.sub(r'\(.*?\)|\[.*?\]| - cover| feat\..*', '', s)
    return re.sub(r'[^a-z0-9]+', '', s)

def lookup(artist, title):
    q = urllib.parse.quote(f'{artist} {title}')
    with urllib.request.urlopen(f'https://itunes.apple.com/search?term={q}&entity=song&limit=10', timeout=20) as r:
        res = json.load(r).get('results', [])
    a0 = norm(artist.split(',')[0]); t0 = norm(title)
    for x in res:
        if a0 and a0 in norm(x.get('artistName')) and t0 and (t0 in norm(x.get('trackName')) or norm(x.get('trackName')) in t0):
            return x
    return None

done = skipped = nomatch = 0
for f in sorted(os.listdir(MUSIC)):
    if not f.lower().endswith('.m4a'):
        continue
    p = os.path.join(MUSIC, f)
    tags = MP4(p).tags or {}
    have = {k: tags.get(k) for k in ('\xa9gen', '\xa9day', 'aART')}
    if all(have.values()):
        continue
    artist = (tags.get('\xa9ART') or [''])[0]; title = (tags.get('\xa9nam') or [''])[0]
    if not artist or not title:
        m = re.match(r'(.+?) - (.+)\.m4a$', f); artist, title = (m.group(1), m.group(2)) if m else ('', '')
    x = lookup(artist, title); time.sleep(1)
    if not x:
        print(f'NO MATCH  {f}'); nomatch += 1; continue
    new = {}
    if not have['\xa9gen'] and x.get('primaryGenreName'): new['\xa9gen'] = [x['primaryGenreName']]
    if not have['\xa9day'] and x.get('releaseDate'): new['\xa9day'] = [x['releaseDate'][:4]]
    if not have['aART'] and x.get('artistName'): new['aART'] = [x['artistName']]
    print(f'{"WRITE" if APPLY else "WOULD"}  {f}  ->  ' + ', '.join(f'{k.strip(chr(0xa9))}={v[0]}' for k, v in new.items()))
    if not APPLY or not new:
        skipped += 1; continue
    tmp = os.path.join(tempfile.mkdtemp(), f)
    shutil.copy2(p, tmp)
    m = MP4(tmp); m.tags.update(new); m.save()
    chk = MP4(tmp)
    if chk.info.length > 1 and all(chk.tags.get(k) == v for k, v in new.items()) and chk.tags.get('\xa9nam') == tags.get('\xa9nam'):
        shutil.copy2(tmp, p); done += 1
    else:
        print(f'  verify FAILED, original kept: {f}')
    shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)
print(f'\nupdated {done} | dry/unchanged {skipped} | no match {nomatch}')
