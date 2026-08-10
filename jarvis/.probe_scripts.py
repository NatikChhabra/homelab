import os, urllib.request, json
t = os.getenv('JARVIS_HASS_TOKEN') or os.getenv('HA_API_TOKEN')
H = {'Authorization': 'Bearer ' + t, 'Content-Type': 'application/json'}


def get(p):
    r = urllib.request.Request('http://jarvis-hass:8123' + p, headers=H)
    return json.load(urllib.request.urlopen(r, timeout=15))


for s in ('ac_set_temperature', 'fan_set', 'ac_apply', 'tv_toggle', 'fan_boost'):
    try:
        cfg = get('/api/config/script/config/' + s)
        print('===', s, '===')
        print(json.dumps(cfg)[:800])
    except Exception as e:
        print('===', s, 'ERR', type(e).__name__, e)
