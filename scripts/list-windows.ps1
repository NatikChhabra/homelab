# Diagnostic: writes visible top-level window titles of the desktop session to logs\windows.txt
Add-Type @'
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public static class LW { delegate bool P(IntPtr h, IntPtr p);
 [DllImport("user32.dll")] static extern bool EnumWindows(P f, IntPtr p); [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h); [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
 public static List<string> All() { var r = new List<string>(); EnumWindows((h,p)=>{ if(IsWindowVisible(h)){var s=new StringBuilder(256);GetWindowText(h,s,256); if(s.Length>0) r.Add((IsIconic(h)?"[min] ":"[shown] ")+s);} return true;}, IntPtr.Zero); return r; } }
'@
[LW]::All() | Set-Content C:\ServerData\Stacks\logs\windows.txt
