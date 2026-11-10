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

# Every docker call goes through here with a hard timeout (2026-11-10). On
# 2026-11-10 the engine hung (API 500s); untimed `docker inspect` calls blocked
# each run for ~4 min, longer than the 3-min interval, and runs died with no log
# line. Returns trimmed stdout, or $null on timeout; exit code in $script:DockerExit.
function Invoke-DockerT([string[]]$DockerArgs, [int]$Sec = 30) {
    $script:DockerExit = -1
    $psi = New-Object System.Diagnostics.ProcessStartInfo 'docker'
    $psi.Arguments = ($DockerArgs | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } }) -join ' '
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    try { $p = [System.Diagnostics.Process]::Start($psi) } catch { return $null }
    $out = $p.StandardOutput.ReadToEndAsync(); $null = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($Sec * 1000)) { try { $p.Kill() } catch {}; return $null }
    $script:DockerExit = $p.ExitCode
    return $out.Result.Trim()
}

# Probe: sonarr checks /data (Server mode). In College mode sonarr is stopped and navidrome is the one
# service left, so it checks its own D: mount, /music, instead (2026-11-03).
$Probe     = 'sonarr'; $MountPath = '/data'; $Expect = 'library'
foreach ($c in 'sonarr', 'navidrome') {
    $r = Invoke-DockerT @('inspect', '-f', '{{.State.Running}}', $c) 15
    if ($r -eq 'true') { $Probe = $c; if ($c -eq 'navidrome') { $MountPath = '/music'; $Expect = '.' }; break }
}
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
    # 2026-11-03: was local IST time with a 'Z' suffix, 5h30 off from every other log.
    $line = ('{0} {1}' -f (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'), $msg)
    # Retry, never throw: a log file briefly held by an overlapping run used to
    # kill the whole watchdog under ErrorActionPreference=Stop (2026-11-10).
    for ($i = 0; $i -lt 5; $i++) {
        try { Add-Content -Path $LogFile -Value $line -ErrorAction Stop; break } catch { Start-Sleep -Milliseconds 400 }
    }
    Write-Output $line
}

# Keep the log bounded.
if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 1MB)) {
    try { Move-Item $LogFile "$LogFile.1" -Force } catch {}   # held? next run retries
}

function Test-DataMount {
    # Returns $true only when /data is a real, large, populated mount. Checking
    # that the path merely exists is useless -- a dead mount still has the dir.
    try {
        # df -kP prints the same columns in GNU (sonarr) and BusyBox (navidrome); BusyBox ignores the G in -BG.
        $out = Invoke-DockerT @('exec', $Probe, 'sh', '-c', "df -kP $MountPath 2>/dev/null | tail -1")
        # $null = timed out (slow engine), not a dead mount: route it to the
        # strike-counted branch below instead of an instant restart (review 2026-11-11).
        if ($null -eq $out) { $script:ProbeTimedOut = $true; return $false }
        if (-not $out) { return $false }
        if ($out -match 'No such device') { return $false }
        $kb = ($out -split '\s+') | Where-Object { $_ -match '^\d+$' } | Select-Object -First 1
        if (-not $kb) { return $false }
        $gb = [int]([int64]$kb / 1048576)
        if ($gb -lt $MinSizeGB) { return $false }
        $ls = Invoke-DockerT @('exec', $Probe, 'sh', '-c', "ls $MountPath 2>/dev/null")
        if ($null -eq $ls) { $script:ProbeTimedOut = $true; return $false }
        return [bool]($ls -match $Expect)
    } catch { return $false }
}

$EngineDownFile = 'C:\ServerData\Stacks\logs\engine-down.json'
$script:ProbeTimedOut = $false

# Healthy: reset the strike count, but keep a restart from the last 30 min so a
# flapping engine cannot be force-restarted every ~15 min (review 2026-11-11).
function Clear-EngineDown {
    try {
        $j = Get-Content $EngineDownFile -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($j.lastRestartUtc -and ((Get-Date).ToUniversalTime() - ([datetime]$j.lastRestartUtc).ToUniversalTime()).TotalMinutes -lt 30) {
            if ([int]$j.count -ne 0) { @{ count = 0; firstUtc = $null; lastRestartUtc = $j.lastRestartUtc } | ConvertTo-Json | Set-Content $EngineDownFile }
            return
        }
    } catch {}
    Remove-Item $EngineDownFile -ErrorAction SilentlyContinue
}

# Full Docker Desktop restart. Stopping only 'Docker Desktop' was NOT enough on
# 2026-11-10 ("Docker Desktop is unable to start", VM stopped): killing the
# backend too and `wsl --shutdown` is what brought the engine back.
function Restart-DockerFull {
    Get-Process 'Docker Desktop', 'com.docker.backend', 'com.docker.build' -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 8
    try { & wsl.exe --shutdown 2>$null | Out-Null } catch {}
    Start-Sleep -Seconds 8
    Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    # Wait for the engine (up to 8 min).
    $deadline = (Get-Date).AddMinutes(8)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 20
        $v = Invoke-DockerT @('version', '-f', '{{.Server.Version}}') 15
        if ($v) { return $true }
    }
    return $false
}

if (Test-DataMount) {
    Clear-EngineDown
    Log 'OK  /data mount healthy'
    # Job freshness (added 2026-11-01). Kuma 1.23 restarts every push monitor's
    # timer when it starts, and Docker restarts often here, so a nightly job
    # that silently stopped could stay "up" forever. This heartbeat fires every
    # 10 min and its monitor does alert, so it carries the check: any run-job
    # status file that is overdue or failed turns this monitor DOWN, naming it.
    $maxHours = @{ 'backup-volumes' = 27; 'update-audit' = 27; 'shrink-library' = 27; 'lb-discovery' = 27; 'music-dupe-guard' = 3; 'offline-playlists' = 1; 'rolling-window' = 2; 'orphan-cleanup' = 194; 'server-audit' = 194 }
    $problems = foreach ($job in $maxHours.Keys) {
        $f = "C:\ServerData\Stacks\logs\$job-status.json"
        if (-not (Test-Path $f)) { continue }
        try { $st = Get-Content $f -Raw | ConvertFrom-Json } catch { "$job (status unreadable)"; continue }
        $age = ((Get-Date).ToUniversalTime() - ([datetime]$st.finishedUtc).ToUniversalTime()).TotalHours
        # Age only: a job that ran and FAILED already pushes its own monitor down
        # (run-job.ps1). Re-flagging failures here made this monitor and the
        # weekly audit hold each other DOWN (seen 2026-11-01). Overdue = silent.
        if ($age -gt $maxHours[$job]) { "$job overdue ($([int]$age) h since last run)" }
    }
    if ($problems) {
        Log ('JOBS  ' + ($problems -join '; '))
        Send-Heartbeat -State 'down' -Message ('mount OK, but jobs need attention: ' + ($problems -join '; '))
        exit 0
    }
    Send-Heartbeat -State 'up' -Message '/data mount healthy; scheduled jobs fresh'
    exit 0
}

# A dead mount leaves the containers RUNNING. If the engine is not answering or
# the probe container is not running, this is a boot, a Docker start or a
# planned container restart -- not a dead mount. Restarting Docker Desktop then
# only takes AdGuard (family DNS) down for ~2 min; it did exactly that after all
# 3 boots on 2026-11-01. Skip; a real dead mount is caught on the next run.
# The server version is empty until the engine answers ('docker info' prints client text even when it is
# down, and under ErrorActionPreference=Stop its stderr killed this script at every boot).
$engine = Invoke-DockerT @('version', '-f', '{{.Server.Version}}') 20
if (-not $engine -or $script:ProbeTimedOut) {
    # HUNG-ENGINE ESCALATION (2026-11-10). This branch used to SKIP forever: on
    # 2026-11-10 the engine hung at 23:56 IST and nothing recovered it (AdGuard,
    # every app and Kuma itself were down ~55 min). Its 'down' heartbeat can never arrive, because Kuma runs in the
    # same engine. So: count consecutive cycles; after 4 (~12 min, longer than
    # any normal Docker start) and once Docker has been running 15+ min, do a
    # full restart. At most one restart per 30 min.
    $st = @{ count = 0; firstUtc = (Get-Date).ToUniversalTime().ToString('o'); lastRestartUtc = $null }
    if (Test-Path $EngineDownFile) {
        try { $j = Get-Content $EngineDownFile -Raw | ConvertFrom-Json; $st.count = [int]$j.count; if ($j.firstUtc) { $st.firstUtc = $j.firstUtc }; $st.lastRestartUtc = $j.lastRestartUtc } catch {}
    }
    $st.count++
    # Gate on how long Docker itself has been running, NOT on OS uptime (review
    # 2026-11-11): auto-logon is off and Docker Desktop starts only at an
    # interactive logon, so after a reboot with nobody logged in there is no
    # Docker process at all. That is 'not started', never 'hung' -- the 67-min
    # SKIP on 2026-11-03 was exactly that (logon at 20:47 IST). A fresh start
    # under 15 min old is also left alone.
    $dd = Get-Process 'com.docker.backend', 'Docker Desktop' -ErrorAction SilentlyContinue | Sort-Object StartTime | Select-Object -First 1
    $ddMin = if ($dd) { ((Get-Date) - $dd.StartTime).TotalMinutes } else { 0 }
    $sinceRestart = if ($st.lastRestartUtc) { ((Get-Date).ToUniversalTime() - ([datetime]$st.lastRestartUtc).ToUniversalTime()).TotalMinutes } else { 9999 }
    if ($st.count -ge 4 -and $ddMin -ge 15 -and $sinceRestart -ge 30) {
        Log ("ENGINE HUNG  not answering for {0} consecutive checks since {1} - full Docker Desktop restart" -f $st.count, $st.firstUtc)
        $st.lastRestartUtc = (Get-Date).ToUniversalTime().ToString('o')
        $st | ConvertTo-Json | Set-Content $EngineDownFile
        $ok = Restart-DockerFull
        $mins = [int]((Get-Date).ToUniversalTime() - ([datetime]$st.firstUtc).ToUniversalTime()).TotalMinutes
        if ($ok) {
            Log "ENGINE RECOVERED  after a full restart (engine was down ~$mins min)"
            try { Write-EventLog -LogName Application -Source 'ServerDataJobs' -EntryType Warning -EventId 1013 -Message "mount-watchdog: Docker engine was hung ~$mins min; recovered by a full Docker Desktop restart." } catch {}
            # Down on purpose: Kuma is back now, so this alert actually reaches the
            # owner. The next healthy cycle (3 min) sends 'up'.
            Start-Sleep -Seconds 60
            Send-Heartbeat -State 'down' -Message "Docker engine was hung ~$mins min - auto-restarted, recovering"
            exit 0
        }
        Log 'ENGINE FAILED  still not answering after a full restart - needs a human'
        try { Write-EventLog -LogName Application -Source 'ServerDataJobs' -EntryType Error -EventId 1014 -Message 'mount-watchdog: Docker engine hung and a full restart did not bring it back.' } catch {}
        exit 1
    }
    $st | ConvertTo-Json | Set-Content $EngineDownFile
    $why = if ($script:ProbeTimedOut) { 'mount probe timed out (engine slow)' } else { 'docker engine not answering' }
    Log ("SKIP  {0} ({1} in a row; full restart at 4 once Docker is 15+ min old) - mount not checked" -f $why, $st.count)
    Send-Heartbeat -State 'down' -Message 'docker engine not answering - mount not checked'
    exit 0
}
Clear-EngineDown
$probeRunning = Invoke-DockerT @('inspect', '-f', '{{.State.Running}}', $Probe) 15
if ($probeRunning -ne 'true') {
    Log ("SKIP  probe container {0} not running - mount not checked, Docker not restarted" -f $Probe)
    Send-Heartbeat -State 'up' -Message "probe $Probe not running - mount check skipped"
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
    # The engine ANSWERS here, so first the gentle restart that repaired ~20 USB
    # drops: only the Docker Desktop front end. The full restart (backend kill +
    # wsl --shutdown = pulling power on every container mid-write) only if the
    # engine does not come back from that (review 2026-11-11).
    Get-Process 'Docker Desktop' -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 25
    Start-Process 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
    $back = $false; $deadline = (Get-Date).AddMinutes(8)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 20
        if (Invoke-DockerT @('version', '-f', '{{.Server.Version}}') 15) { $back = $true; break }
    }
    if (-not $back) { Log 'engine did not return from the gentle restart - full restart'; $null = Restart-DockerFull }
} catch {
    Log ('ERROR  could not restart Docker Desktop: {0}' -f $_.Exception.Message)
}
# Engine is up (or timed out); give the containers time to start, then re-check the mount.
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
