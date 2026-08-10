import asyncio, json, os, sys
import aiohttp

TOK = os.environ.get("TOK", "")
BASE = "http://localhost:8123"


def load_prompt():
    try:
        d = json.load(open("/config/.storage/core.config_entries"))
    except Exception:
        return ""
    for e in d["data"]["entries"]:
        if e.get("domain") != "ollama":
            continue
        for s in e.get("subentries") or []:
            p = (s.get("data") or {}).get("prompt")
            if p:
                return p
    return ""


def prompt_script_names(prompt):
    """Names the prompt tells the model to call, from the whole-room block.

    Only the block after the "Whole-room requests" marker is read: the comfort
    rules above it also use "->" but their right-hand sides are descriptions
    ("AC 22 and fan speed 4"), not script names.
    """
    lines = prompt.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if "Whole-room requests" in ln:
            start = i + 1
            break
    if start is None:
        return None
    names = []
    for ln in lines[start:]:
        if not ln.strip():
            break
        if "->" not in ln:
            continue
        if ln.lstrip().startswith("-"):
            continue
        rhs = ln.split("->", 1)[1].strip().rstrip(".").strip()
        if rhs:
            names.append(rhs)
    return names


async def main():
    out = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(BASE + "/api/websocket") as ws:
            await ws.receive_json()
            await ws.send_json({"type": "auth", "access_token": TOK})
            a = await ws.receive_json()
            if a.get("type") != "auth_ok":
                print("probe|autherror")
                return
            await ws.send_json({"id": 1, "type": "homeassistant/expose_entity/list"})
            r = await ws.receive_json()
            if not r.get("success"):
                print("probe|listerror")
                return
            ee = r["result"]["exposed_entities"]
        exposed = sorted(k for k, v in ee.items() if v.get("conversation"))

        hdr = {"Authorization": "Bearer " + TOK}
        async with s.get(BASE + "/api/states", headers=hdr) as resp:
            states = await resp.json()

    names = {}
    for st in states:
        names[st["entity_id"]] = (st.get("attributes") or {}).get("friendly_name") or ""

    out.append("exposed|%d" % len(exposed))

    exposed_names = {}
    for eid in exposed:
        exposed_names[(names.get(eid) or "").strip().lower()] = eid
    all_script_names = {}
    for eid, fn in names.items():
        if eid.startswith("script."):
            all_script_names[(fn or "").strip().lower()] = eid

    prompt = load_prompt()
    refs = prompt_script_names(prompt)
    if refs is None:
        out.append("promptblock|notfound")
    else:
        out.append("promptrefs|%d" % len(refs))
        for n in refs:
            key = n.strip().lower()
            if key in exposed_names:
                continue
            if key in all_script_names:
                out.append("notexposed|%s|%s" % (n, all_script_names[key]))
            else:
                out.append("missing|%s" % n)
    print("\n".join(out))


asyncio.run(main())
