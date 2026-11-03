"""warp-pull.py -- pull a registry image through the WARP tunnel into a docker-load tar.

Added 2026-11-01. Airtel throttles GitHub's container CDN (ghcr.io blobs ran at
~10 KB/s direct vs ~10 MB/s through WARP), so daemon pulls of ghcr images
(homarr, immich, lscr.io/linuxserver/*) stall forever. Use the wrapper:

    bash /c/ServerData/Stacks/warp-pull.sh ghcr.io/homarr-labs/homarr:latest

Writes an OCI layout holding the registry's ORIGINAL index + manifest bytes
(linux/amd64 blobs only). Docker Desktop uses the containerd image store, which
keeps those digests on `docker load`, so RepoDigests match the registry exactly
like a normal pull -- update-audit.sh compares RepoDigests and would otherwise
flag every warp-pulled image as outdated forever.

Runs INSIDE a python container on the streaming network (so http://warp:1080
resolves); stdlib only. Writes /out/image.tar.
"""
import io, json, re, sys, tarfile, urllib.request

ref = sys.argv[1]
reg, rest = ref.split('/', 1) if '.' in ref.split('/')[0] else ('docker.io', ref)
if reg == 'docker.io':  # Docker Hub: real API host, and official images live under library/
    reg = 'registry-1.docker.io'
    rest = rest if '/' in rest else 'library/' + rest
repo, tag = rest.rsplit(':', 1) if ':' in rest.split('/')[-1] else (rest, 'latest')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https': 'http://warp:1080', 'http': 'http://warp:1080'}))
ACCEPT = ','.join(['application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
                   'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json'])
token = None

def get(path, accept=ACCEPT):
    global token
    for _ in range(2):
        h = {'Accept': accept}
        if token:
            h['Authorization'] = 'Bearer ' + token
        try:
            return opener.open(urllib.request.Request(f'https://{reg}/v2/{repo}/{path}', headers=h), timeout=120)
        except urllib.error.HTTPError as e:
            if e.code != 401 or token:
                raise
            # anonymous bearer token from the realm the registry names
            p = dict(re.findall(r'(\w+)="([^"]*)"', e.headers['WWW-Authenticate']))
            url = f"{p['realm']}?service={p.get('service', '')}&scope=repository:{repo}:pull"
            token = json.load(opener.open(url, timeout=60))['token']

with tarfile.open('/out/image.tar', 'w') as out:
    def add(name, fileobj, size):
        ti = tarfile.TarInfo(name); ti.size = size; out.addfile(ti, fileobj)
    def add_bytes(name, b):
        add(name, io.BytesIO(b), len(b))
    def blob(digest, r, size):
        add('blobs/sha256/' + digest.split(':')[1], r, size)

    r = get(f'manifests/{tag}')
    top_raw, top_type, top_dig = r.read(), r.headers['Content-Type'], r.headers['Docker-Content-Digest']
    top = json.loads(top_raw)
    blob(top_dig, io.BytesIO(top_raw), len(top_raw))
    m = top
    if 'manifests' in top:  # multi-arch index -> fetch only linux/amd64
        d = next(x['digest'] for x in top['manifests'] if x.get('platform', {}).get('architecture') == 'amd64' and x['platform'].get('os') == 'linux')
        mr = get(f'manifests/{d}'); m_raw = mr.read(); m = json.loads(m_raw)
        blob(d, io.BytesIO(m_raw), len(m_raw))
    cfg = get(f"blobs/{m['config']['digest']}").read()
    blob(m['config']['digest'], io.BytesIO(cfg), len(cfg))
    for i, l in enumerate(m['layers']):
        blob(l['digest'], get(f"blobs/{l['digest']}"), l['size'])  # streamed, no temp copies
        print(f'  layer {i + 1}/{len(m["layers"])} {l["size"] // 1048576} MB', flush=True)
    add_bytes('oci-layout', b'{"imageLayoutVersion":"1.0.0"}')
    full = ref if '.' in ref.split('/')[0] else 'docker.io/' + (ref if '/' in ref.split(':')[0] else 'library/' + ref)
    add_bytes('index.json', json.dumps({'schemaVersion': 2, 'manifests': [{
        'mediaType': top_type, 'digest': top_dig, 'size': len(top_raw),
        'annotations': {'io.containerd.image.name': full, 'org.opencontainers.image.ref.name': tag}}]}).encode())
print('wrote image.tar for', ref, top_dig[:19])
