#!/usr/bin/env python3
"""
Patch tuya-local so an IR blaster can be selected as a gateway.

The problem
-----------
tuya-local decides what may act as a gateway from HUB_CATEGORIES, a hardcoded
list of Tuya category codes. Natik's Zoook IR blaster reports a category that is
not on that list, so `Hub count: 0` and the hub dropdown offered only "None".
Its four DIY sub-remotes (AirCon, TV, fan, DIY) are sub-devices with no IP of
their own, and choose_device refuses those with `needs_hub` until a gateway is
named -- so they were unaddable, permanently.

The wrong fix (do not repeat)
-----------------------------
Deriving the parent from the index key. The index is built as

    "<the device's OWN id>/<its node_id>"

so the PARENT's id never appears in it. Splitting on "/" yields the children's
own ids, which match nothing, and the result is `Hub count: 0` all over again --
indistinguishable from the unpatched bug. That cost a restart and a rescan.

The fix
-------
Tuya marks children with `sub=True`. If any sub-device exists at all, then every
top-level device is a candidate gateway for it, and the dropdown exists so a
human picks the right one. That needs no category knowledge and no parent link.

This is upstream-worthy: the whitelist will keep missing rebadged blasters.

Run this after every tuya-local upgrade -- the integration replaces its own
files. Verify with:  docker logs jarvis-hass | grep "Hub count"
"""

import subprocess
import sys

TARGET = "/config/custom_components/tuya_local/cloud.py"
CONTAINER = "jarvis-hass"

PY = r'''
p = "/config/custom_components/tuya_local/cloud.py"
s = open(p).read()

WRONG = """        # A device that other devices hang off IS a gateway, whatever Tuya
        # calls its category. HUB_CATEGORIES is a hardcoded list and it misses
        # this Zoook IR blaster, which reports a category that is not in it.
        # The result was an empty hub dropdown and DIY sub-remotes (AC, TV,
        # fan) that could not be added at all, because choose_device refuses
        # them with needs_hub until a gateway is named. Derive hub-ness from
        # the data instead of the category whitelist: any device that appears
        # as the parent half of a "<parent_id>/<node_id>" index is a hub.
        parent_ids = {k.split("/")[0] for k in cloud_devices if "/" in k}
        for k, dev in cloud_devices.items():
            if "/" not in k and dev["id"] in parent_ids:
                dev["is_hub"] = True

        return cloud_devices"""

RIGHT = """        # HUB_CATEGORIES is a hardcoded whitelist and it misses this Zoook
        # IR blaster, whose category is not on it. That left the hub dropdown
        # empty and made the DIY sub-remotes (AirCon, TV, fan) unaddable,
        # because choose_device refuses a sub-device with needs_hub until a
        # gateway is named.
        #
        # Do NOT try to recover the parent from the index key: the index is
        # "<the device's OWN id>/<its node_id>", so the parent id never
        # appears in it. An earlier attempt at that produced Hub count: 0 and
        # looked identical to the unpatched bug.
        #
        # Tuya marks children with sub=True. If any sub-device exists, every
        # top-level device is a possible gateway for it; the dropdown is there
        # for a human to pick the right one.
        if any(dev.get("sub") for dev in cloud_devices.values()):
            for dev in cloud_devices.values():
                if not dev.get("sub"):
                    dev["is_hub"] = True

        return cloud_devices"""

PLAIN = """            cloud_devices[index] = cloud_device

        return cloud_devices"""

if RIGHT in s:
    print("already patched")
elif WRONG in s:
    s = s.replace(WRONG, RIGHT)
    print("replaced the broken index-based patch")
elif PLAIN in s:
    s = s.replace(PLAIN, PLAIN.replace("\n        return cloud_devices",
                                       "\n" + RIGHT[RIGHT.index("        #"):]))
    print("applied to clean upstream file")
else:
    raise SystemExit("no known anchor found -- cloud.py has changed upstream")

SUB_OLD = '                "support_local": device.support_local,'
SUB_NEW = ('                "support_local": device.support_local,\n'
           '                "sub": device.sub if hasattr(device, "sub") else False,')
if '"sub": device.sub' not in s:
    if s.count(SUB_OLD) != 1:
        raise SystemExit("cannot anchor the sub flag")
    s = s.replace(SUB_OLD, SUB_NEW)
    print("added sub flag to the device dict")

open(p, "w").write(s)

import ast
ast.parse(open(p).read())
print("syntax OK")
'''


def main():
    r = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "python3", "-"],
        input=PY, capture_output=True, text=True, timeout=120,
    )
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        sys.exit(r.returncode)

    # Stale bytecode would shadow the edit.
    subprocess.run(
        ["docker", "exec", CONTAINER, "sh", "-c",
         "find /config/custom_components/tuya_local/__pycache__ "
         "-name 'cloud*' -delete 2>/dev/null || true"],
        timeout=60,
    )
    print("bytecode cache cleared -- restart jarvis-hass to load it")


if __name__ == "__main__":
    main()
