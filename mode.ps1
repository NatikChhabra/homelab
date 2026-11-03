# mode.ps1 -- flip this laptop between Server (home) and College (portable).
#
# Run via the desktop shortcuts "Server Mode" / "College Mode". They call the
# scheduled tasks "Mode - Server" / "Mode - College", which run elevated, so no
# UAC prompt. Log: C:\ServerData\Stacks\logs\mode.log
#
# College mode MUST reset DNS: every adapter points at 127.0.0.1 (AdGuard in
# Docker), so stopping Docker without resetting DNS = no internet at all.
# Other tailnet devices that use this box as DNS lose it too while you're away;
# see the report for the fallback.

param([Parameter(Mandatory)][ValidateSet('Server','College')][string]$Mode)
$ErrorActionPreference = 'Continue'
$log = 'C:\ServerData\Stacks\logs\mode.log'
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
function Log($m) { Add-Content $log ("{0}  [{1}] {2}" -f (Get-Date -Format s), $Mode, $m) }

# Instant feedback (2026-11-02): a switch takes 15-30 s and the popup below only
# comes at the end, so it looked like nothing was happening.
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$toast = New-Object System.Windows.Forms.NotifyIcon
$toast.Icon = [System.Drawing.SystemIcons]::Information; $toast.Visible = $true
$toast.ShowBalloonTip(15000, 'Mode switch', "Switching to $Mode mode... (about 20 s)", 'Info')
$note = ''

$PERF = '27fa6203-3987-4dcc-918d-748559d549ec'   # ASUS Performance
$BAL  = '381b4222-f694-41f0-9685-ff5bb260df2e'   # Balanced
$nics = Get-NetAdapter -Physical | ? { $_.Name -match 'Wi-Fi|Ethernet' }
$winlogon = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon'
$run = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$dockerExe = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$serverTasks = Get-ScheduledTask | ? { $_.TaskName -like 'ServerData - *' }

if ($Mode -eq 'Server') {
    powercfg /setactive $PERF
    powercfg /change standby-timeout-ac 0; powercfg /change hibernate-timeout-ac 0; powercfg /change monitor-timeout-ac 0
    # CPU floor 5% on AC (was 100%: clocks pinned 24/7; boost still reaches max). 2026-11-01 audit.
    powercfg /setacvalueindex $PERF SUB_PROCESSOR PROCTHROTTLEMIN 5
    powercfg /setacvalueindex $PERF SUB_PROCESSOR PERFEPP 33   # 2026-11-01: idle cores stop pinning max clock; boost still aggressive
    powercfg /setacvalueindex $PERF SUB_BUTTONS LIDACTION 0; powercfg /setactive $PERF
    # College mode gives Balanced sleep timers; zero them again so no scheme can
    # sleep the server if something switches plans (audit section "Host stays awake").
    foreach ($v in 'setacvalueindex','setdcvalueindex') {
        powercfg /$v $BAL SUB_SLEEP STANDBYIDLE 0; powercfg /$v $BAL SUB_VIDEO VIDEOIDLE 0
        powercfg /$v $BAL SUB_SLEEP HIBERNATEIDLE 0; powercfg /$v $BAL SUB_BUTTONS LIDACTION 0
    }
    $nics | % { Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ServerAddresses '127.0.0.1','94.140.14.14' }
    tailscale set --accept-dns=true
    Set-ItemProperty $run 'Docker Desktop' "`"$dockerExe`""
    # Windows forgot the auto-login account on 2026-11-02; owner chose to leave it
    # off (password needed after a power cut). Logged, no popup.
    if ((Get-ItemProperty $winlogon).DefaultUserName) { Set-ItemProperty $winlogon AutoAdminLogon '1' }
    else { Log 'auto-login not configured (owner chose to leave it off, 2026-11-02)' }
    $serverTasks | % { Enable-ScheduledTask -InputObject $_ | Out-Null }
    if (-not (Get-Process 'Docker Desktop' -ea 0)) { Start-Process $dockerExe }
    # College mode set restart=no on everything but navidrome; every compose
    # file here declares restart: always (all of them), so
    # putting that back is exact.
    # 'docker info' prints client text even with the daemon down, so it always passed at once (2026-11-03 check).
    # The server version is empty until the daemon really answers.
    for ($i = 0; $i -lt 60 -and -not (docker version -f '{{.Server.Version}}' 2>$null); $i++) { Start-Sleep 5 }
    $all = docker ps -aq
    if ($all) { docker update --restart=always $all | Out-Null; docker start $all | Out-Null }
    $down = docker ps -a --filter status=exited -q
    if (-not $all) { Log 'WARN docker not ready after 5 min - containers NOT started'; $note = ' Docker did not start: containers are down.' }
    elseif ($down) { Log "WARN $(@($down).Count) container(s) still stopped after start"; $note = " $(@($down).Count) container(s) did not start." }
    Start-ScheduledTask -TaskName 'ServerData - Keep Awake'
    # Control room back on screen. Via explorer.exe so it runs unelevated (this
    # task runs at Highest; an elevated Edge would be wrong). 2026-11-01.
    Start-Process explorer.exe 'C:\ServerData\Stacks\scripts\control-room.vbs'
    if (-not $note) { Log 'server mode on; all containers started' }
} else {
    Stop-ScheduledTask -TaskName 'ServerData - Keep Awake' -ea 0
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | ? CommandLine -match 'keep-awake\.ps1' | % { Stop-Process -Id $_.ProcessId -Force }
    # Control room (server screen) must not keep popping full-screen windows over
    # college work: stop its script and the kiosk Edge profile (2026-11-01).
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | ? CommandLine -match 'control-room\.ps1' | % { Stop-Process -Id $_.ProcessId -Force }
    Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | ? CommandLine -match 'ServerData.Kiosk.edge-profile' | % { Stop-Process -Id $_.ProcessId -Force -ea 0 }
    # The mount watchdog stays on: navidrome keeps running in College mode and needs its D: mount watched.
    # The light music jobs stay on too (2026-11-03): navidrome and Octo keep running, so a song hearted
    # at college still reaches the phone's 'Everything' playlist and the dupe guard still cleans up.
    $keepTasks = 'ServerData - Mount Watchdog', 'ServerData - Offline Playlists', 'ServerData - Music Dupe Guard', 'ServerData - LB Discovery'
    $serverTasks | ? { $_.TaskName -notin $keepTasks } | % { Disable-ScheduledTask -InputObject $_ | Out-Null }
    Set-ItemProperty $winlogon AutoAdminLogon '0'   # stolen laptop must not boot to an open desktop
    # DNS before AdGuard goes down, or there is a window with no resolver at all
    $nics | % { Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ResetServerAddresses }
    # At HOME the router hands out this laptop (AdGuard, about to stop) as DNS, and
    # the fixed-IP Ethernet gets none: every lookup waited on a dead server first
    # (2026-11-02). There, use AdGuard's public DNS (still ad-free). Away from home
    # the network's own DNS stays, so captive portals keep working.
    $mine = @((Get-NetIPAddress -AddressFamily IPv4).IPAddress) + '127.0.0.1'
    # Only a fixed-IP card is pinned: a pinned DHCP Wi-Fi card kept these servers at college and broke its
    # login page (2026-11-03 check). Wi-Fi is pinned only when there is no Ethernet to carry home lookups.
    $ethUp = $nics | ? { $_.Name -match 'Ethernet' -and $_.Status -eq 'Up' }
    $nics | % {
        $dhcp = (Get-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ea 0).Dhcp -eq 'Enabled'
        if ($dhcp -and $ethUp) { return }
        if ($dhcp) { Log "WARN no Ethernet: pinned DNS on $($_.Name); it stays pinned if this laptop leaves home in College mode" }
        $s = (Get-DnsClientServerAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4).ServerAddresses
        if (-not $s -or ($s | ? { $_ -in $mine })) { Set-DnsClientServerAddress -InterfaceIndex $_.ifIndex -ServerAddresses '94.140.14.14','94.140.15.15' }
    }
    tailscale set --accept-dns=false
    # Everything stops except the music: navidrome plus the Octo stack. The library
    # is on D: of this laptop, and since 2026-11-03 the phone (Octo app) and Feishin
    # both reach it THROUGH Octo (:5274), so stopping Octo cut them off at college.
    # Octo + yt-dlp-shim + slskd idle at ~1.1 GB and ~0% CPU. restart=no keeps the
    # stopped ones down across reboots until Server mode puts it back.
    $keep = ' (navidrome|octo|yt-dlp-shim|slskd)$'
    $others = docker ps -a --format '{{.ID}} {{.Names}}' | ? { $_ -notmatch $keep } | % { ($_ -split ' ')[0] }
    if ($others) { docker update --restart=no $others | Out-Null; docker stop $others | Out-Null }
    powercfg /setactive $BAL
    powercfg /change standby-timeout-ac 30; powercfg /change standby-timeout-dc 10
    powercfg /change monitor-timeout-ac 10; powercfg /change monitor-timeout-dc 3
    powercfg /change hibernate-timeout-dc 60
    powercfg /setacvalueindex $BAL SUB_BUTTONS LIDACTION 1; powercfg /setdcvalueindex $BAL SUB_BUTTONS LIDACTION 1; powercfg /setactive $BAL
    Log 'college mode on; only music running (navidrome + octo stack), DNS automatic, sleep enabled'
}

# Self-closing popup (8 s). A modal MessageBox kept the task "running" until
# someone clicked OK, so the next switch was refused (2026-10-25).
$toast.Dispose()
(New-Object -ComObject WScript.Shell).Popup("$Mode mode is on.$note", $(if ($note) { 30 } else { 8 }), 'Mode switch', 64) | Out-Null
