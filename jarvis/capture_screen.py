"""
Periodic desktop screenshot capture for JARVIS HUD.

Writes a timestamped PNG to jarvis/screenshots/ every 30 seconds.
The jarvis-voice container serves the latest via /api/hud/screen.
Rainmeter's Image meter reads it directly from :3100 with Cache-Control: no-cache.

Run this as a Windows Scheduled Task every 30 seconds, or invoke it manually
to take a one-shot screenshot. The script uses PIL (Pillow) if available,
falling back to a built-in PowerShell screenshot via subprocess.

Usage:
    python capture_screen.py          # one-shot
    (scheduled task runs this periodically)
"""

import os
import time
from datetime import datetime

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "screenshots")
os.makedirs(OUTPUT_DIR, exist_ok=True)


def take_screenshot_pil():
    """Capture using Pillow (fast, high quality)."""
    from PIL import ImageGrab
    img = ImageGrab.grab()
    return img


def take_screenshot_ps():
    """Fallback: PowerShell screenshot via System.Windows.Forms."""
    import subprocess
    ps = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$s = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$img = New-Object System.Drawing.Bitmap $s.Width, $s.Height
$g = [System.Drawing.Graphics]::FromImage($img)
$g.CopyFromScreen($s.Location, [System.Drawing.Point]::Empty, $s.Size)
$img.Save("$OUTPUT_DIR\fallback.png", [System.Drawing.Imaging.ImageFormat]::Png)
'''
    ps = ps.replace("$OUTPUT_DIR", OUTPUT_DIR)
    subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-Command", ps],
                   capture_output=True, timeout=15)


def main():
    now = datetime.now()
    fname = now.strftime("%Y%m%d_%H%M%S") + ".png"
    fpath = os.path.join(OUTPUT_DIR, fname)

    try:
        img = take_screenshot_pil()
        img.save(fpath, "PNG")
        size = os.path.getsize(fpath)
        print(f"Captured {img.size[0]}x{img.size[1]} -> {fpath} ({size//1024}KB)")
    except Exception as e:
        print(f"PIL capture failed: {e}")
        try:
            take_screenshot_ps()
            fb = os.path.join(OUTPUT_DIR, "fallback.png")
            if os.path.exists(fb):
                os.replace(fb, fpath)
                print(f"Fallback capture -> {fpath}")
        except Exception as e2:
            print(f"Both capture methods failed: {e2}")
            return 1
    return 0


def prune(max_age_hours=24):
    """Delete screenshots older than max_age_hours.

    WHY THIS EXISTS: this script wrote a PNG every 30 seconds and never deleted
    one. By 2026-09-09 that was 40,266 files and 3.7 GB -- 95% of the entire
    Stacks directory -- on the C: SSD that also holds every app database, at
    85% full. Nothing ever reads an old screenshot: the HUD serves only the
    newest via /api/hud/screen, so every file older than the last one is dead
    weight the moment it is written.

    Pruning here rather than in a separate scheduled job on purpose -- a
    cleanup task that has to be registered separately is a cleanup task that
    silently stops running.
    """
    cutoff = time.time() - max_age_hours * 3600
    removed = freed = 0
    try:
        for name in os.listdir(OUTPUT_DIR):
            if not name.endswith(".png"):
                continue
            fp = os.path.join(OUTPUT_DIR, name)
            try:
                st = os.stat(fp)
                if st.st_mtime < cutoff:
                    freed += st.st_size
                    os.remove(fp)
                    removed += 1
            except OSError:
                # A file vanishing mid-prune is normal, not an error.
                continue
    except OSError as e:
        print(f"prune skipped: {e}")
        return
    if removed:
        print(f"Pruned {removed} screenshot(s), freed {freed // 1048576} MB")


if __name__ == "__main__":
    rc = main()
    prune()
    exit(rc)
