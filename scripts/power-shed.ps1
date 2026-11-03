# power-shed.ps1 -- shed non-essential load while the laptop runs on battery.
#
# Added 2026-11-01 (audit): 9 unclean shutdowns since Sept; on 10-31 mains was
# out 3h47m and the battery (68% health, capped at 80%) lasted 1h53m. Family DNS
# (AdGuard), Jellyfin and Vaultwarden must outlive a power cut as long as
# possible, so the heavy, non-urgent containers pause on battery and resume on
# AC. Fired by the task "ServerData - Power Shed" on Kernel-Power event 105
# (power source changed) and once at logon (in case it booted on battery).
#
#   -DryRun   print what would happen, change nothing
param([switch]$DryRun)

$log  = 'C:\ServerData\Stacks\logs\power-shed.log'
$shed = 'immich-immich-machine-learning-1', 'qbittorrent', 'slskd'   # slskd: P2P, added 2026-11-02
function Log($m) { "$(Get-Date -Format s) $m" | Add-Content $log; $m }

Add-Type -AssemblyName System.Windows.Forms
# At logon Docker may not be up yet (2026-11-01 verify): wait up to 5 min so the
# booted-on-battery case still sheds, and the back-on-AC case still restarts.
# (the server version, not 'docker info': that prints client text even with the daemon down)
for ($i = 0; $i -lt 60 -and -not (& docker version -f '{{.Server.Version}}' 2>$null); $i++) { Start-Sleep 5 }
$onBattery = [System.Windows.Forms.SystemInformation]::PowerStatus.PowerLineStatus -eq 'Offline'
$pct = [int]([System.Windows.Forms.SystemInformation]::PowerStatus.BatteryLifePercent * 100)

foreach ($c in $shed) {
    $state = (& docker inspect -f '{{.State.Status}}' $c 2>$null)
    if (-not $state) { continue }
    if ($onBattery -and $state -eq 'running') {
        if ($DryRun) { "would stop $c (on battery, $pct%)" } else { & docker stop $c | Out-Null; Log "on battery ($pct%): stopped $c" }
    } elseif (-not $onBattery -and $state -ne 'running') {
        if ($DryRun) { "would start $c (AC back)" } else { & docker start $c | Out-Null; Log "on AC: started $c" }
    }
}
if ($onBattery -and -not $DryRun) {
    # Never encode on battery: end tonight's shrinker run if it is going.
    if ((Get-ScheduledTask 'ServerData - Shrink Library' -ErrorAction SilentlyContinue).State -eq 'Running') {
        Stop-ScheduledTask 'ServerData - Shrink Library'; Log 'on battery: stopped the running shrink-library job'
    }
    # Ending the task does not stop the encode: ffmpeg runs inside the jellyfin
    # container. Kill the shrinker's ffmpeg (identified by its .shrinking. temp
    # output; Jellyfin's own transcodes never write that name) and remove the
    # half-written temp file. The original file is untouched until a finished
    # encode passes its checks, so nothing is lost.
    # End the host loop first, or it starts the next file. Then match ffmpeg by its own path: a bare
    # 'shrinking\.' pattern also matched this very sh -c line, so pkill killed its own shell before find ran.
    Get-CimInstance Win32_Process -Filter "Name='bash.exe'" | ? CommandLine -match 'shrink-library\.sh' | % { Stop-Process -Id $_.ProcessId -Force }
    $k = & docker exec jellyfin sh -c "pkill -f '^/usr/lib/jellyfin-ffmpeg/ffmpeg .*[.]shrinking[.]' && echo killed; sleep 2; find /data/library -name '*.shrinking.*' -delete -print" 2>$null
    if ($k) { Log "on battery: stopped shrinker ffmpeg / removed temp: $($k -join ' ')" }
}
if ($DryRun) { "power: $(if ($onBattery) {'BATTERY'} else {'AC'}) $pct% -> " + $(if ($onBattery) {'would shed load'} else {'all shed containers should run'}) }
