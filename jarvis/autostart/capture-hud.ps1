# Capture ONLY the Rainmeter skin window, even while it is covered.
#
# WHY NOT A NORMAL SCREENSHOT
# The skin is AlwaysOnTop=-2 (desktop layer), so any maximised window -- on this
# box, a full-screen glances terminal -- sits over it. A full-desktop capture
# therefore shows the covering window and proves nothing about the skin, and
# minimising the user's foreground window to get a clean shot is not acceptable
# on a machine someone is using.
#
# PrintWindow asks the window to render itself into a supplied DC, independent
# of what is on top of it. PW_RENDERFULLCONTENT (0x2) is the load-bearing flag:
# Rainmeter skins are LAYERED windows (WS_EX_LAYERED), and without that flag
# PrintWindow returns a blank or black bitmap for them -- which would look
# exactly like "the meters are empty", the very failure being tested for.

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class Grab {
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  public static IntPtr Handle = IntPtr.Zero;
  public static RECT Rect;
  public static void FindSkin() {
    EnumWindows((h, p) => {
      var sb = new StringBuilder(256);
      GetClassName(h, sb, sb.Capacity);
      if (sb.ToString() == "RainmeterMeterWindow" && IsWindowVisible(h)) {
        RECT r; GetWindowRect(h, out r);
        if (r.R - r.L > 50) { Handle = h; Rect = r; return false; }
      }
      return true;
    }, IntPtr.Zero);
  }
}
'@

[Grab]::FindSkin()
$log = 'C:\ServerData\Stacks\jarvis\hud-shot.txt'
if ([Grab]::Handle -eq [IntPtr]::Zero) {
    'no visible RainmeterMeterWindow found' | Out-File $log -Encoding utf8
    return
}
$r = [Grab]::Rect
$w = $r.R - $r.L
$h = $r.B - $r.T
$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
# Paint a known background first: the skin is semi-transparent, so an
# uninitialised bitmap would leave undefined pixels behind the panel.
$g.Clear([System.Drawing.Color]::FromArgb(255, 25, 30, 40))
$dc = $g.GetHdc()
$okFull = [Grab]::PrintWindow([Grab]::Handle, $dc, 2)   # PW_RENDERFULLCONTENT
$g.ReleaseHdc($dc)
$g.Dispose()
$bmp.Save('C:\ServerData\Stacks\jarvis\hud-shot.png', [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
"skin ${w}x${h} at ($($r.L),$($r.T)) printwindow_full=$okFull" | Out-File $log -Encoding utf8
