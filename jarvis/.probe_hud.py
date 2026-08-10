import os, urllib.request, json
k = (os.getenv('JELLYFIN_KEY') or '').strip().strip('"').strip("'")
print('JELLYFIN_KEY len in container:', len(k))
def get(url, hdr=None):
    r = urllib.request.Request(url, headers=hdr or {})
    return urllib.request.urlopen(r, timeout=8)
for host in ('host.docker.internal', '172.17.0.1'):
    try:
        resp = get(f'http://{host}:61208/api/4/cpu')
        print(f'GLANCES via {host}:', resp.status)
    except Exception as e:
        print(f'GLANCES via {host} ERR:', type(e).__name__, e)
    try:
        resp = get(f'http://{host}:8096/System/Info', {'Authorization': 'MediaBrowser Token="%s"' % k})
        d = json.load(resp)
        print(f'JELLYFIN via {host}: OK', d.get('ServerName'), d.get('Version'))
    except Exception as e:
        print(f'JELLYFIN via {host} ERR:', type(e).__name__, e)
