# maintenance-reboot.ps1 -- one-shot reboot in a quiet window (added 2026-11-01).
# Applies changes that only take effect at boot (.wslconfig autoMemoryReclaim,
# pagefile, SysMain, NIC power flags). Waits until: on AC, nobody is watching
# Jellyfin, and no nightly job is running. Gives up at 07:00 so it never
# reboots into the day. The "Boot Check" task audits the box after it comes up.
$log = 'C:\ServerData\Stacks\logs\maintenance-reboot.log'
function Log($m) { "$(Get-Date -Format s) $m" | Add-Content $log }
Unregister-ScheduledTask -TaskName 'ServerData - Maintenance Reboot' -Confirm:$false -EA SilentlyContinue

$key = ((Get-Content 'C:\ServerData\Stacks\secrets.env') -match '^JELLYFIN_KEY=' -replace '^JELLYFIN_KEY=', '').Trim().Trim("'`"")
$jobs = 'ServerData - Daily Backup', 'ServerData - Update Audit', 'ServerData - Shrink Library', 'ServerData - Immich Auto Albums'
Add-Type -AssemblyName System.Windows.Forms
while ((Get-Date).Hour -lt 7) {
    $why = @()
    if ([System.Windows.Forms.SystemInformation]::PowerStatus.PowerLineStatus -ne 'Online') { $why += 'on battery' }
    try {
        $s = Invoke-RestMethod 'http://127.0.0.1:8096/Sessions' -Headers @{ Authorization = "MediaBrowser Token=$key" } -TimeoutSec 15
        if (@($s | ? NowPlayingItem).Count) { $why += 'Jellyfin playing' }
    } catch { }   # Jellyfin down is no reason to wait
    $busy = $jobs | ? { (Get-ScheduledTask $_ -EA 0).State -eq 'Running' }
    if ($busy) { $why += "running: $($busy -join ', ')" }
    if (-not $why) { Log 'quiet - rebooting'; shutdown /r /t 60 /c 'Maintenance reboot (applies memory/WSL changes)'; exit 0 }
    Log ('waiting: ' + ($why -join '; ')); Start-Sleep 900
}
Log 'gave up at 07:00 - not rebooted'
