"""immich-auto-albums.py -- sort every Immich photo into albums by what is IN it.

Added 2026-11-01. Uses Immich's own SigLIP2 image embeddings (smart_search table)
and its ML service for the text side, then a per-category similarity threshold
picked by eye from contact sheets (see --calibrate). Albums it owns carry the
description below; it only ever ADDS photos, never removes one you put there.

Nightly task "ServerData - Immich Auto Albums" runs it, so new photos sort
themselves. Usage:
    python immich-auto-albums.py              # sort (idempotent)
    python immich-auto-albums.py --calibrate  # print score spreads + contact sheets
Credentials: IMMICH_EMAIL / IMMICH_PASS from secrets.env (never printed).
"""
import json, os, subprocess, sys, urllib.request

sys.stdout.reconfigure(encoding='utf-8')
MODEL = 'ViT-B-16-SigLIP2__webli'
TAG = 'Auto-sorted by your server'
API = 'http://localhost:2283/api'
env = dict(os.environ, MSYS_NO_PATHCONV='1')

# album name -> (prompts, threshold). A photo joins when its best prompt score
# >= threshold. Thresholds were set from contact sheets on 2026-11-01.
CATEGORIES = {
    'Selfies':           (['a selfie of a young man', 'a mirror selfie', 'a close-up selfie photo of a face'], 0.135),
    'Friends & Groups':  (['a group of friends posing together', 'a group photo of people smiling'], 0.095),
    'Food & Cafes':      (['a photo of food on a plate', 'a meal at a restaurant', 'a cup of coffee in a cafe', 'street food'], 0.076),
    'Nature & Sky':      (['a landscape with mountains', 'a sunset sky', 'trees and nature', 'the beach and the sea'], 0.092),
    'Animals & Zoo':     (['a photo of a dog', 'a photo of a cat', 'an animal', 'birds at a zoo'], 0.090),
    'Notes & Documents': (['a photo of a printed document', 'handwritten notes on paper', 'a page of a book', 'a whiteboard with writing'], 0.130),
    'Festivals':         (['diwali lights and diyas', 'holi festival colours', 'an indian wedding', 'a festival celebration'], 0.099),
    'Chats & Memes':     (['a meme with text', 'a screenshot of a chat conversation'], 0.131),
}
# Dropped after looking at contact sheets (2026-11-01): Travel (maps + game
# screenshots), Cars & Bikes (mostly GTA + number plates), Parties (too mixed).

# A photo is NOT put in a real-world album when it looks more like one of these.
NEGATIVE = ['a cartoon illustration', 'a screenshot of a phone screen', 'a video game screenshot', 'a logo or graphic design']
NO_NEGATIVE = {'Chats & Memes', 'Notes & Documents'}

def sh(cmd, stdin=None):
    return subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding='utf-8', env=env).stdout

def embed(text):
    out = sh(['docker', 'exec', 'immich-immich-server-1', 'sh', '-c',
              'curl -s -m 60 -X POST http://immich-machine-learning:3003/predict '
              f'-F \'entries={{"clip":{{"textual":{{"modelName":"{MODEL}"}}}}}}\' -F "text=$1"', '_', text])
    v = json.loads(out)['clip']
    return json.loads(v) if isinstance(v, str) else v

def scores(vec):
    """asset id -> cosine similarity, for every visible, non-deleted photo."""
    sql = (f"select s.\"assetId\", 1 - (s.embedding <=> '{json.dumps(vec)}') from smart_search s "
           "join asset a on a.id = s.\"assetId\" where a.\"deletedAt\" is null and a.visibility = 'timeline';")
    rows = sh(['docker', 'exec', '-i', 'immich-database-1', 'sh', '-c', 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|"'], sql)
    return {r.split('|')[0]: float(r.split('|')[1]) for r in rows.splitlines() if '|' in r}

def api(path, method='GET', body=None, token=None):
    h = {'Content-Type': 'application/json'}
    if token:
        h['Authorization'] = 'Bearer ' + token
    req = urllib.request.Request(API + path, json.dumps(body).encode() if body is not None else None, h, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        t = r.read()
        return json.loads(t) if t else None

def category_scores():
    out = {}
    neg = {}
    for p in NEGATIVE:
        for a, sc in scores(embed(p)).items():
            neg[a] = max(neg.get(a, -1), sc)
    out['_neg'] = neg
    for name, (prompts, _) in CATEGORIES.items():
        best = {}
        # (negatives are applied in the sort step, not here)
        for p in prompts:
            for a, s in scores(embed(p)).items():
                best[a] = max(best.get(a, -1), s)
        out[name] = best
    return out

if __name__ == '__main__':
    cat = category_scores()
    if '--calibrate' in sys.argv:
        tok = api('/auth/login', 'POST', {'email': os.environ['IMMICH_EMAIL'], 'password': os.environ['IMMICH_PASS']})['accessToken']
        from PIL import Image
        import io
        outdir = sys.argv[sys.argv.index('--calibrate') + 1] if len(sys.argv) > sys.argv.index('--calibrate') + 1 else '.'
        for name, best in cat.items():
            if name == '_neg':
                continue
            ranked = sorted(best.items(), key=lambda x: -x[1])
            v = [s for _, s in ranked]
            print(f'{name:20} top {v[0]:.3f} | #10 {v[9]:.3f} | #25 {v[24]:.3f} | #50 {v[49]:.3f} | #100 {v[99]:.3f} | median {v[len(v)//2]:.3f}')
            # contact sheet: 40 best, 8 per row, score written under each
            sheet = Image.new('RGB', (8 * 160, 5 * 175), (20, 20, 30))
            from PIL import ImageDraw
            d = ImageDraw.Draw(sheet)
            for i, (aid, s) in enumerate(ranked[:40]):
                req = urllib.request.Request(f'{API}/assets/{aid}/thumbnail?size=thumbnail', headers={'Authorization': 'Bearer ' + tok})
                try:
                    im = Image.open(io.BytesIO(urllib.request.urlopen(req, timeout=30).read())).convert('RGB')
                    im.thumbnail((156, 156))
                    sheet.paste(im, ((i % 8) * 160 + 2, (i // 8) * 175 + 2))
                except Exception:
                    pass
                d.text(((i % 8) * 160 + 4, (i // 8) * 175 + 159), f'{i+1}: {s:.3f}', fill=(255, 255, 120))
            sheet.save(os.path.join(outdir, 'cal-' + name.split()[0].lower() + '.jpg'), quality=80)
        api('/auth/logout', 'POST', {}, tok)
        sys.exit(0)

    tok = api('/auth/login', 'POST', {'email': os.environ['IMMICH_EMAIL'], 'password': os.environ['IMMICH_PASS']})['accessToken']
    try:
        albums = {a['albumName']: a for a in api('/albums', token=tok)}
        for name, (_, thr) in CATEGORIES.items():
            if thr <= 0:
                continue  # not calibrated yet
            ids = [a for a, s in cat[name].items()
                   if s >= thr and (name in NO_NEGATIVE or s > cat['_neg'].get(a, -1))]
            if not ids:
                continue
            if name not in albums:
                albums[name] = api('/albums', 'POST', {'albumName': name, 'description': TAG}, tok)
            alb = albums[name]
            if alb.get('description') != TAG:
                print(f'skip {name}: an album with that name exists and is not ours'); continue
            # Immich v3 no longer returns an album's assets; list them via search.
            have, page = set(), 1
            while page:
                r = api('/search/metadata', 'POST', {'albumIds': [alb['id']], 'size': 1000, 'page': page}, tok)['assets']
                have |= {x['id'] for x in r['items']}
                page = int(r['nextPage']) if r.get('nextPage') else None
            new = [i for i in ids if i not in have]
            for k in range(0, len(new), 500):
                api(f"/albums/{alb['id']}/assets", 'PUT', {'ids': new[k:k + 500]}, tok)
            print(f'{name:20} {len(ids):4} photos ({len(new)} new)')
    finally:
        api('/auth/logout', 'POST', {}, tok)
