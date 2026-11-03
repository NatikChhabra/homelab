# control-room.ps1 -- the server laptop's screen as a control room (2026-11-01).
#
# Replaces the single Glances kiosk: two full-screen Edge app windows, Glances
# and the Homarr board, swapped every $Seconds. Runs at logon from the Startup
# folder (Interactive session -- it needs the desktop).
#
# Why two windows and not one page with iframes: Homarr's login cookie is
# SameSite=Lax, so browsers drop it inside an iframe and the board would sit on
# its login page forever. Separate top-level windows keep the login.
# Why a dedicated --user-data-dir: plain --kiosk runs InPrivate and forgets the
# Homarr login at every restart; this profile remembers it (log in once).
#
# Swapping only minimises one window and restores the other, which Windows
# allows from a background script (stealing focus is not). It pauses while
# anyone has touched the mouse/keyboard in the last $IdlePauseSec, so it never
# yanks the screen away from someone using the laptop.
param([int]$Seconds = 60, [int]$IdlePauseSec = 45)

$log  = 'C:\ServerData\Stacks\logs\control-room.log'
$edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
$prof = 'C:\ServerData\Kiosk\edge-profile'
$urls = [ordered]@{ 'Glances' = 'http://localhost:61208'; 'Homarr' = 'http://localhost:7575/boards/dashboard' }
# Window titles to look for: the Homarr window says "Natik's Server" once logged
# in, but "Homarr"/"Login" before that -- match all or it reopens forever.
$titles = @{ 'Glances' = @('Glances'); 'Homarr' = @("Natik's Server", 'Homarr', 'Login', 'Sign in') }
function Log($m) { "$(Get-Date -Format s) $m" | Add-Content $log }

Add-Type @'
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public static class W {
  delegate bool EnumProc(IntPtr h, IntPtr p);
  [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc f, IntPtr p);
  [DllImport("user32.dll")] static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
  [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [StructLayout(LayoutKind.Sequential)] struct LII { public uint cb; public uint t; }
  [DllImport("user32.dll")] static extern bool GetLastInputInfo(ref LII l);
  public static uint IdleMs() { var l = new LII(); l.cb = 8; GetLastInputInfo(ref l); return (uint)Environment.TickCount - l.t; }
  public static IntPtr Find(string part) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, p) => { if (!IsWindowVisible(h)) return true; var s = new StringBuilder(256); GetWindowText(h, s, 256);
      if (s.ToString().Contains(part)) { found = h; return false; } return true; }, IntPtr.Zero);
    return found;
  }
}
'@

# Only one copy may run: two copies swap at the same moment and cancel out.
# Done FIRST, newest wins (2026-11-02): Startup and Server mode both launch one
# at boot; when this ran after the wait, both woke together and could kill each
# other, leaving no control room at all.
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.CommandLine -like '*-File*control-room.ps1*' -and $_.ProcessId -ne $PID } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# Wait (up to 15 min) for both services -- Docker usually answers in ~5 s, but a
# slow boot must not open windows on an error page (titles would not match).
foreach ($u in $urls.Values) {
  $up = $false
  for ($i = 0; $i -lt 180; $i++) {
    try { Invoke-WebRequest $u -UseBasicParsing -TimeoutSec 5 -MaximumRedirection 0 -ErrorAction Stop | Out-Null; $up = $true; break }
    catch { if ($_.Exception.Response) { $up = $true; break }; Start-Sleep 5 }
  }
  # College mode stops these containers: then there is no control room to show,
  # and opening windows on error pages would cover the user's work.
  if (-not $up) { Log "$u never came up (College mode?) - control room not started"; exit 0 }
}
Log "services up, opening windows"

function FindWin($name) { foreach ($t in $titles[$name]) { $h = [W]::Find($t); if ($h -ne [IntPtr]::Zero) { return $h } }; [IntPtr]::Zero }

# Open only windows that are missing. Never kill existing ones: Edge may not
# have flushed the Homarr login cookie to disk yet.
foreach ($n in $urls.Keys) {
  if ((FindWin $n) -eq [IntPtr]::Zero) {
    Start-Process $edge "--user-data-dir=`"$prof`" --app=$($urls[$n]) --start-fullscreen --no-first-run --disable-features=Translate"
    Start-Sleep 6
  }
}
$names = @($urls.Keys); $cur = 0; $reopens = 0; $hung = 0
while ($true) {
  Start-Sleep $Seconds
  if ([W]::IdleMs() -lt $IdlePauseSec * 1000) { continue }        # someone is using the laptop
  $show = FindWin $names[1 - $cur]; $hide = FindWin $names[$cur]
  if ($show -eq [IntPtr]::Zero) {                                  # window closed or crashed: reopen it
    if (++$reopens -gt 5) { Log "gave up reopening $($names[1 - $cur]) after 5 tries"; $cur = 1 - $cur; continue }
    Log "reopening $($names[1 - $cur])"
    Start-Process $edge "--user-data-dir=`"$prof`" --app=$($urls[$names[1 - $cur]]) --start-fullscreen --no-first-run"
    continue
  }
  # A frozen window (Glances hung, 2026-11-03) used to stay frozen until someone closed it. Hung on two checks in a
  # row: end the kiosk browser (both windows share it); the loop above reopens each window on the next cycles.
  if ([W]::IsHungAppWindow($show)) {
    if (++$hung -ge 2) { $p = [uint32]0; [void][W]::GetWindowThreadProcessId($show, [ref]$p); Log "$($names[1 - $cur]) window frozen - restarting the kiosk browser"; Stop-Process -Id $p -Force -ErrorAction SilentlyContinue; $hung = 0 }
    continue
  }
  $hung = 0
  [W]::ShowWindow($show, 3) | Out-Null                             # SW_MAXIMIZE (stays full-screen)
  if ($hide -ne [IntPtr]::Zero) { [W]::ShowWindow($hide, 6) | Out-Null }  # SW_MINIMIZE
  $cur = 1 - $cur
  $reopens = 0   # only crashes in a row count toward the give-up limit
}
