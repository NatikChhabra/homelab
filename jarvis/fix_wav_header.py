"""
Repair Kokoro's streaming WAV headers.

The defect, measured 2026-08-09
-------------------------------
Natik reported "crackling in the end" and it is not the voice, the blend, or the
model -- it is a malformed RIFF header.

kokoro-fastapi answers `/v1/audio/speech` with a header written for STREAMING,
where the total length is not knowable when the header goes out. It fills the
`data` chunk size with the unknown-length sentinel 0xFFFFFFFF (4294967295) and
the RIFF size with the same:

    cand_C_gl_fable_light.wav: file 483000 bytes, data chunk DECLARES 4294967295

A player that trusts the header keeps reading past the audio, and whatever it
finds -- uninitialised buffer, silence, the next allocation -- is rendered as
noise. That is the crackle, and it lands at the END precisely because that is
where the real samples stop and the lie begins.

Python's own `wave` module refuses the file outright:
    struct.error: unpack requires a buffer of 4294967294 bytes

Not every sample was affected: `blend_george_lewis.wav` carries a correct header
(data=693330, actual=693330), which is why some auditions sounded clean and
others crackled, and why this read as inconsistent voice quality rather than a
container bug.

The fix
-------
Rewrite both size fields to the real byte counts. This is lossless -- not one
audio sample is touched, only the two integers that describe them. Chunks are
walked properly rather than assuming `data` starts at byte 44, because these
files carry a 26-byte LIST chunk between `fmt ` and `data`.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path


def repair(path: Path) -> tuple[bool, str]:
    """Fix RIFF/data sizes in place. Returns (changed, detail)."""
    b = bytearray(path.read_bytes())
    if bytes(b[0:4]) != b"RIFF" or bytes(b[8:12]) != b"WAVE":
        return False, "not a RIFF/WAVE file"

    # Walk the chunk list to find `data`; it is NOT always at offset 36.
    i, data_off, data_sz = 12, None, None
    while i + 8 <= len(b):
        cid = bytes(b[i:i + 4])
        csz = struct.unpack("<I", b[i + 4:i + 8])[0]
        if cid == b"data":
            data_off, data_sz = i, csz
            break
        # A bogus size on a non-data chunk would desync the walk; guard it.
        if csz > len(b):
            return False, f"chunk {cid!r} has impossible size {csz}"
        i += 8 + csz + (csz % 2)  # chunks are word-aligned

    if data_off is None:
        return False, "no data chunk found"

    actual = len(b) - data_off - 8
    riff_should_be = len(b) - 8
    riff_is = struct.unpack("<I", b[4:8])[0]

    if data_sz == actual and riff_is == riff_should_be:
        return False, f"already correct (data={actual})"

    struct.pack_into("<I", b, data_off + 4, actual)
    struct.pack_into("<I", b, 4, riff_should_be)
    path.write_bytes(bytes(b))
    return True, f"data {data_sz} -> {actual}, riff {riff_is} -> {riff_should_be}"


def verify(path: Path) -> str:
    """Prove the repair by parsing with the stdlib, which rejected it before."""
    import wave
    try:
        with wave.open(str(path), "rb") as w:
            n, fr = w.getnframes(), w.getframerate()
            # Actually read the frames; a correct header that still overruns
            # would fail here rather than silently passing.
            raw = w.readframes(n)
        return f"OK {n / fr:.2f}s, {len(raw)} bytes read"
    except Exception as exc:  # noqa: BLE001
        return f"STILL BROKEN: {type(exc).__name__}: {exc}"


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).parent / "ui" / "static" / "voices"
    files = sorted(root.glob("*.wav"))
    if not files:
        print(f"no wav files under {root}")
        return 1
    fixed = 0
    for f in files:
        changed, detail = repair(f)
        fixed += changed
        print(f"{f.name:<34} {'FIXED ' if changed else 'skip  '} {detail}")
        print(f"{'':<34} verify: {verify(f)}")
    print(f"\n{fixed} of {len(files)} repaired")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
