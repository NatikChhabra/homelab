# Report on the JARVIS Rainmeter skin window from INSIDE session 1.
#
# WHY THIS EXISTS
# Session 0 (where SSH lands) cannot enumerate session 1's windows -- they are
# on different window stations -- so "did the skin load and where is it?" is
# unanswerable from the SSH session. A screenshot is not sufficient either: the
# skin is AlwaysOnTop=-2 (desktop layer), so any maximised window covers it and
# an empty-looking screenshot proves nothing.
#
# This enumerates by window class instead, which is unambiguous, then optionally
# repositions via a Rainmeter bang. Bangs are used rather than editing
# Rainmeter.ini directly because Rainmeter owns that file and rewrites it (as
# UTF-16, re-appending its own defaults) whenever it exits -- an external edit
# while it is running is silently discarded.

param([int]$MoveX = -1, [int]$MoveY = -1)

$ErrorActionPreference = 'Continue'
$out = 'C:\ServerData\Stacks\jarvis\hud-window.txt'

Add-Type @'
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public class WinEnum {
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr p);
  [DllImport("user32.dll")] static extern int GetClassName(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern int GetWindowTextW(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] static extern bool GetWindowRect(IntPtr h, out RECT r);
  public delegate bool EnumProc(IntPtr h, IntPtr p);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int L, T, R, B; }
  public static List<string> Find(string cls) {
    var found = new List<string>();
    EnumWindows((h, p) => {
      var sb = new StringBuilder(256);
      GetClassName(h, sb, sb.Capacity);
      if (sb.ToString().IndexOf(cls, StringComparison.OrdinalIgnoreCase) >= 0) {
        var t = new StringBuilder(256);
        GetWindowTextW(h, t, t.Capacity);
        RECT r; GetWindowRect(h, out r);
        found.Add(string.Format("class={0} title='{1}' visible={2} rect=({3},{4})-({5},{6}) size={7}x{8}",
          sb.ToString(), t.ToString(), IsWindowVisible(h), r.L, r.T, r.R, r.B, r.R - r.L, r.B - r.T));
      }
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
'@

$lines = @()
$lines += "=== session: $((Get-Process -Id $PID).SessionId) ==="

Add-Type -AssemblyName System.Windows.Forms
$s = [System.Windows.Forms.Screen]::PrimaryScreen
$lines += "logical screen : $($s.Bounds.Width)x$($s.Bounds.Height)  workarea=$($s.WorkingArea.Width)x$($s.WorkingArea.Height)"

if ($MoveX -ge 0) {
    $rm = "$env:ProgramFiles\Rainmeter\Rainmeter.exe"
    # Refresh first: Rainmeter owns Rainmeter.ini and rewrites it on its own
    # schedule, so an on-disk edit to JARVIS.ini is not visible to the running
    # skin until it is told to reload. Move/ZPos run after, since a refresh can
    # reset a skin to its last-saved position.
    & $rm '!Refresh' 'JARVIS\HUD'
    Start-Sleep -Seconds 2
    & $rm '!Move' "$MoveX" "$MoveY" 'JARVIS\HUD'
    Start-Sleep -Seconds 2
    & $rm '!ZPos' '-2' 'JARVIS\HUD'
    Start-Sleep -Seconds 1
    $lines += "issued !Refresh, !Move $MoveX $MoveY and !ZPos -2"
}

$w = [WinEnum]::Find('Rainmeter')
if ($w.Count -eq 0) { $lines += 'NO Rainmeter windows found' }
else { $lines += "Rainmeter windows: $($w.Count)"; $w | ForEach-Object { $lines += "  $_" } }

$lines | Out-File -FilePath $out -Encoding utf8
