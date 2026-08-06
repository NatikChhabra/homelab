# ============================================================================
# mount-watchdog.ps1 -- detect and repair a dead D: media mount.
#
# WHY THIS EXISTS
#   The media drive is USB-attached. On 2026-07-28 Windows spun it down after
#   30 seconds idle (SUB_DISK DISKIDLE was 0x1e), and when it slept, Docker
#   Desktop's VM mount to D: died and never came back on its own. Every one of
#   sonarr/radarr/bazarr/jellyfin/qbittorrent reported "No such device" on
#   /data: Jellyfin showed an empty library, imports could not write, and
#   nothing in the stack raised an error, because from Docker's point of view
#   the containers were perfectly healthy.
#
#   The power settings were fixed (disk idle + USB selective suspend both set to
#   never, device-level power saving disabled on 13 devices), which should stop
#   it recurring. This watchdog exists because "should" is not "will" -- a USB
#   drive can still drop on a cable glitch or a controller reset, and a silent
#   media outage is exactly the failure the owner would not notice for days.
#
# WHAT IT DOES
#   Checks whether /data inside a container is a real mount. If it is dead, and
#   the drive is healthy on the host, restarts Docker Desktop and re-checks.
#   Read-only when everything is fine -- it only acts on a confirmed failure.
#
# Exit: 0 = healthy or repaired, 1 = repair failed, 2 = host drive itself gone.
# ============================================================================

$ErrorActionPreference = 'Stop'
$LogFile   = 'C:\ServerData\Stacks\mount-watchdog.log'
$HostPath  = 'D:\Media\library'
$Probe     = 'sonarr'
$MinSizeGB = 100

# ---- Uptime Kuma heartbeat (added 2026-07-28) ------------------------------
# This watchdog guards the failure it was written for, but nothing guarded the
# watchdog. It runs every 10 minutes from Task Scheduler, and if the task itself
# stopped -- disabled, erroring on startup, machine rebooted into a bad state --
# the media mount would be unmonitored again and nobody would know. That is the
# same silent-failure shape the script exists to prevent.
#
# 127.0.0.1, not localhost: PowerShell resolves localhost to ::1 first and
# Uptime Kuma only listens on IPv4, so the heartbeat would silently time out.
$KumaBase = 'http://127.0.0.1:3001'
$PushToken = ''
try {
    foreach ($line in (Get-Content 'C:\ServerData\Stacks\secrets.env' -ErrorAction Stop)) {
        if ($line -match '^\s*KUMA_PUSH_MOUNT_WATCHDOG\s*=\s*(.*?)\s*$') {
            $PushToken = $Matches[1].Trim("'`"")
        }
    }
} catch {}

function Send-Heartbeat {
    param([string]$State, [string]$Message)
    # Never let a monitoring failure become a watchdog failure: if Kuma is down
    # this must not stop the mount from being repaired.
    if ([string]::IsNullOrWhiteSpace($PushToken)) {
        Log 'WARN  no KUMA_PUSH_MOUNT_WATCHDOG in secrets.env - heartbeat skipped'
        return
    }
    # Built OUTSIDE the try: if the expansion itself threw, the catch below
    # referenced an unassigned $u and logged an empty url= field.
    #
    # $($PushToken), not $PushToken: PowerShell treats '?' as part of an
    # unbraced variable name, so "$PushToken?status=" parsed as a variable
    # called PushToken?status, expanded to nothing, and produced
    # /api/push/=up -- a 404 that the old silent catch hid completely.
    $u = "$KumaBase/api/push/$($PushToken)?status=$State&msg=$([uri]::EscapeDataString($Message))"
    try {
        Invoke-RestMethod -Uri $u -TimeoutSec 15 | Out-Null
    } catch {
        # Logged rather than swallowed. A silent catch here is what made a
        # non-firing heartbeat look identical to a healthy one during setup.
        #
        # The token is REDACTED out of the logged URL. This log is plain text on
        # C: and backup-volumes.sh copies it to D: nightly, so logging the push
        # URL whole wrote a live credential into every backup -- the same thing
        # CLAUDE.md forbids for hardcoded keys, and one that server-audit.sh's
        # "no credentials hardcoded in scripts" check cannot see because the
        # secret is emitted at runtime rather than written in the source. The
        # path shape is what makes a 404 diagnosable; the token is not.
        $safeUrl = $u -replace '/api/push/[^?]*', '/api/push/<redacted>'
        Log ('WARN  heartbeat failed: {0}  url={1}' -f $_.Exception.Message, $safeUrl)
    }
}

function Log($msg) {
    $line = ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ'), $msg)
    Add-Content -Path $LogFile -Value $line
    Write-Output $line
}

# Keep the log bounded.
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 1MB)) {
    Move-Item $LogFile "$LogFile.1" -Force
}

function Test-DataMount {
    # Returns $true only when /data is a real, large, populated mount. Checking
    # that the path merely exists is useless -- a dead mount still has the dir.
    try {
        $out = & docker exec $Probe sh -c 'df -BG /data 2>/dev/null | tail -1' 2>$null
        if (-not $out) { return $false }
        if ($out -match 'No such device') { return $false }
        $sizeField = ($out -split '\s+') | Where-Object { $_ -match '^\d+G$' } | Select-Object -First 1
        if (-not $sizeField) { return $false }
        $gb = [int]($sizeField -replace 'G','')
        if ($gb -lt $MinSizeGB) { return $false }
        $ls = & docker exec $Probe sh -c 'ls /data 2>/dev/null' 2>$null
        return ($ls -match 'library')
    } catch { return $false }
}

if (Test-DataMount) {
    Log 'OK  /data mount healthy'
    Send-Heartbeat -State 'up' -Message '/data mount healthy'
    exit 0
}

Log 'PROBLEM  /data is not a live mount inside the containers'

# If the drive is gone on the host too, restarting Docker fixes nothing and the
# problem is physical -- a pulled cable or a dead enclosure. Say so and stop.
if (-not (Test-Path $HostPath)) {
    Log ('FATAL  {0} is not present on the HOST either - check the USB cable/enclosure. Not restarting Docker.' -f $HostPath)
    try {
        Write-EventLog -LogName Application -Source 'ServerDataJobs' -EntryType Error -EventId 1010 `
            -Message "mount-watchdog: D: media drive missing on the host. Physical problem - Docker restart skipped."
    } catch {}
    Send-Heartbeat -State 'down' -Message 'D: missing on the HOST - physical problem, check cable/enclosure'
    exit 2
}

Log 'host drive is healthy - the Docker VM mount died. Restarting Docker Desktop.'
try {
    Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 25
    Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
} catch {
    Log ('ERROR  could not restart Docker Desktop: {0}' -f $_.Exception.Message)
}

# Wait for the engine, then for the mount.
$deadline = (Get-Date).AddMinutes(8)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds 20
    try { & docker ps 2>&1 | Out-Null; if ($LASTEXITCODE -eq 0) { break } } catch {}
}
Start-Sleep -Seconds 45

if (Test-DataMount) {
    Log 'REPAIRED  /data mount is live again after Docker Desktop restart'
    try {
        Write-EventLog -LogName Application -Source 'ServerDataJobs' -EntryType Warning -EventId 1011 `
            -Message "mount-watchdog: D: media mount had died and was repaired by restarting Docker Desktop."
    } catch {}
    # 'up', but the message carries the repair so it is visible in Kuma's
    # history rather than looking like an uneventful cycle.
    Send-Heartbeat -State 'up' -Message 'REPAIRED - mount had died, fixed by Docker Desktop restart'
    exit 0
}

Log 'FAILED  mount still dead after restarting Docker Desktop - needs a human'
try {
    Write-EventLog -LogName Application -Source 'ServerDataJobs' -EntryType Error -EventId 1012 `
        -Message "mount-watchdog: D: media mount still dead after a Docker Desktop restart."
} catch {}
Send-Heartbeat -State 'down' -Message 'mount STILL DEAD after Docker Desktop restart - needs a human'
exit 1
