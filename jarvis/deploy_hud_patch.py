"""
Apply the HUD hooks to a copy of the LIVE voice_server.py.

Why not just copy the repo file in
---------------------------------
MEASURED 2026-08-08: the running jarvis-voice image predates the Kokoro switch.
Its /app/voice_server.py has no `import tts_engine` and there is no
tts_engine.py in the image. Copying the repo file into the container would
therefore either crash the server on import, or -- if tts_engine.py were copied
too -- silently swap the live TTS engine as a side effect of a UI change.
Neither is acceptable for a change that is supposed to be additive.

So the same hooks are applied textually to the live file. Every replacement is
asserted: if the live source has drifted so a hook no longer matches, this exits
non-zero rather than writing a half-patched server.

Usage:  python deploy_hud_patch.py <live-copy.py> <output.py>
"""

import sys

EDITS = [
    # 1. import
    ("import wyoming_client as wy\nfrom router import",
     "import wyoming_client as wy\nimport hud\nfrom router import"),

    # 2. Session keeps the app, only so a directive can reach every open HUD.
    ("        verifier: Any = None,\n    ) -> None:\n"
     "        self.ws = ws\n        self.http = http\n        self.verifier = verifier",
     "        verifier: Any = None,\n        app: web.Application | None = None,\n    ) -> None:\n"
     "        self.ws = ws\n        self.http = http\n        self.verifier = verifier\n"
     "        # Held only so a display directive can be broadcast to every\n"
     "        # connected HUD, not just the one that spoke.\n"
     "        self.app = app"),

    # 3. show()
    ("    async def send_json(self, **payload: Any) -> None:\n"
     "        if not self.ws.closed:\n"
     "            await self.ws.send_json(payload)\n",
     "    async def send_json(self, **payload: Any) -> None:\n"
     "        if not self.ws.closed:\n"
     "            await self.ws.send_json(payload)\n"
     "\n"
     "    async def show(self, text: str) -> None:\n"
     '        """Answer a spoken request with a VISUAL, if it asked for one.\n'
     "\n"
     "        A compiled regex over the transcript: no I/O, no model, and every\n"
     "        failure swallowed. Nothing here may cost the voice loop a\n"
     '        millisecond or take it down."""\n'
     "        try:\n"
     "            directive = hud.directive_for(text)\n"
     "            if not directive:\n"
     "                return\n"
     '            directive["reason"] = "utterance"\n'
     "            if self.app is not None:\n"
     "                await hud.broadcast_display(self.app, directive)\n"
     "            else:\n"
     "                await self.send_json(type='display', **directive)\n"
     "        except Exception:\n"
     '            log.exception("display directive failed (voice loop unaffected)")\n'),

    # 4. spoken path -- fired before the model runs, so the visual beats speech
    ('            await self.send_json(type="transcript", text=text, stt_ms=stt_ms)\n',
     '            await self.send_json(type="transcript", text=text, stt_ms=stt_ms)\n'
     "            await self.show(text)\n"),

    # 5. typed path
    ('                        issued_at = time.time()\n'
     '                        reply, route = await route_utterance(',
     '                        issued_at = time.time()\n'
     '                        await session.show(cmd.get("text", ""))\n'
     '                        reply, route = await route_utterance('),

    # 6. client registry
    ('    session = Session(ws, request.app["http"], request.app.get("verifier"))',
     '    session = Session(ws, request.app["http"], request.app.get("verifier"), request.app)\n'
     '    request.app.setdefault("clients", set()).add(ws)'),

    # 7 + 8. deregister on both exits
    ('        await ws.close()\n        return ws',
     '        await ws.close()\n        request.app["clients"].discard(ws)\n        return ws'),
    ('    finally:\n        await session.close()',
     '    finally:\n        request.app["clients"].discard(ws)\n        await session.close()'),

    # 9. routes
    ('        web.static("/static", STATIC_DIR),\n    ])\n    return app',
     '        web.static("/static", STATIC_DIR),\n    ])\n'
     '    # Panels, the display-directive channel and the allowlisted actuation\n'
     '    # endpoint. Every route it adds is under /api/hud/.\n'
     '    hud.register(app)\n    return app'),
]


def main() -> int:
    src_path, out_path = sys.argv[1], sys.argv[2]
    with open(src_path, encoding="utf-8") as fh:
        src = fh.read()

    if "import hud" in src:
        print("ALREADY PATCHED — refusing to double-apply")
        return 2

    for i, (old, new) in enumerate(EDITS, 1):
        count = src.count(old)
        if count != 1:
            print(f"EDIT {i} FAILED: expected exactly 1 match, found {count}")
            print(f"  looking for: {old[:70]!r}")
            return 1
        src = src.replace(old, new)
        print(f"edit {i}: ok")

    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(src)
    print(f"wrote {out_path} ({len(src)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
