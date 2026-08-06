# ============================================================================
# run-job.ps1 -- Task Scheduler entry point for the unattended server jobs.
#
#   powershell -ExecutionPolicy Bypass -File run-job.ps1 -Job rolling-window
#   powershell -ExecutionPolicy Bypass -File run-job.ps1 -Job update-audit
#
# For every run it:
#   * runs the job's bash script under Git Bash, capturing all output
#   * writes a per-run transcript to logs\<job>\<timestamp>.log
#   * writes logs\<job>-status.json with the last result (readable at a glance)
#   * pings the matching Uptime Kuma push monitor with up/down + the summary,
#     so a job that stops running entirely shows as a missed heartbeat
#   * writes a Windows Application event log entry on failure
#   * refuses to start if a previous run of the same job is still going
# ============================================================================

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('rolling-window', 'update-audit', 'orphan-cleanup', 'server-audit', 'backup-volumes')]
    [string]$Job
)

$ErrorActionPreference = 'Stop'

$Root     = 'C:\ServerData\Stacks'
$BashExe  = 'C:\Program Files\Git\bin\bash.exe'
$LogRoot  = Join-Path $Root 'logs'
# 127.0.0.1, not localhost: PowerShell resolves localhost to ::1 first and
# Uptime Kuma only listens on IPv4, so the heartbeat silently times out.
$KumaBase = 'http://127.0.0.1:3001'
$EventSrc = 'ServerDataJobs'

# Uptime Kuma push tokens used to be hardcoded below. They moved to secrets.env
# on 2026-07-28 when this tree went into version control -- a push token lets
# anyone who has it forge an "up" heartbeat, which would hide a job that had
# actually stopped running. secrets.env is ACL-restricted and gitignored.
#
# Parsed rather than dot-sourced: secrets.env is shell syntax (KEY=value), not
# PowerShell, so executing it would fail.
$SecretsFile = Join-Path $Root 'secrets.env'
$Secrets = @{}
if (Test-Path $SecretsFile) {
    foreach ($line in (Get-Content $SecretsFile)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$') {
            $Secrets[$Matches[1]] = $Matches[2].Trim("'`"")
        }
    }
} else {
    throw "cannot read $SecretsFile -- refusing to run without credentials"
}

# Per-job definition. Env vars are passed through to the bash script.
$Jobs = @{
    'rolling-window' = @{
        Script    = '/c/ServerData/Stacks/rolling-window.sh'
        PushTokenName = 'KUMA_PUSH_ROLLING_WINDOW'
        Env       = @{
            # LIVE as of 2026-07-26, explicitly approved after the cross-season
            # dry-run output was reviewed. Deletions are real from here on.
            'RW_DRY_RUN'         = 'false'
            # WINDOW REMOVED 2026-07-28 at the user's instruction: requesting a
            # season now downloads the WHOLE season, not three episodes at a
            # time. 0 means unlimited.
            #
            # This line used to say '3', and it is the one that actually
            # mattered -- it overrode the script's own default, so changing the
            # default alone would have left every scheduled run still capped at
            # three episodes while appearing to be fixed.
            'RW_WINDOW_SIZE'     = '0'
            # Deleting unwatched files existed only to keep disk usage matched
            # to the 3-episode window. With no window there is nothing to
            # enforce, and leaving it on would delete episodes the moment they
            # arrived. Explicit rather than relying on the default.
            'RW_DELETE_UNWATCHED' = 'false'
            # UNCHANGED, deliberately: watched episodes are still deleted after
            # viewing. That behaviour works and was explicitly not to be touched.
            'RW_DELETE_WATCHED'  = 'true'
            'RW_LOG_FILE'        = '/c/ServerData/Stacks/rolling-window.log'
        }
        Timeout   = 900
    }
    'update-audit'   = @{
        Script    = '/c/ServerData/Stacks/update-audit.sh'
        PushTokenName = 'KUMA_PUSH_UPDATE_AUDIT'
        Env       = @{
            'UA_LOG_FILE'   = '/c/ServerData/Stacks/update-audit.log'
            'UA_STAGE_FILE' = '/c/ServerData/Stacks/update-audit-staged.sh'
        }
        Timeout   = 1800
    }
    'backup-volumes' = @{
        Script    = '/c/ServerData/Stacks/backup-volumes.sh'
        # Was '' until 2026-07-28, meaning the nightly backup ran with NO
        # monitoring at all -- Send-Heartbeat returns early on an empty token,
        # so a backup that stopped running entirely would have gone unnoticed
        # until someone needed a restore. That is the one job where silent
        # failure is worst.
        PushTokenName = 'KUMA_PUSH_DAILY_BACKUP'
        Env       = @{
            # D: on purpose -- a different physical disk from the C: SSD that
            # holds the live data. Same-disk backups do not survive a disk
            # failure, which is the case backups exist for.
            'BV_BACKUP_DIR'     = '/d/Backups'
            'BV_RETENTION_DAYS' = '14'
            'BV_LOG_FILE'       = '/c/ServerData/Stacks/backup-volumes.log'
        }
        Timeout   = 3600
    }
    'server-audit'   = @{
        Script    = '/c/ServerData/Stacks/server-audit.sh'
        # Also '' until 2026-07-28. The weekly audit is what surfaces every
        # other silent failure on this box, so it going quiet is a compounding
        # problem rather than a cosmetic one.
        PushTokenName = 'KUMA_PUSH_WEEKLY_AUDIT'
        Env       = @{
            # Read-only audit of the whole server. Warnings are advisory, so
            # only a real FAIL (exit 2) marks the job failed -- otherwise a
            # merely superseded download would raise an alert every week.
            'RW_AUDIT_OK_ON_WARN' = 'true'
            'RW_AUDIT_REPORT'     = '/c/ServerData/Stacks/server-audit-report.txt'
        }
        Timeout   = 1800
    }
    'orphan-cleanup' = @{
        Script    = '/c/ServerData/Stacks/orphan-cleanup.sh'
        PushTokenName = 'KUMA_PUSH_ORPHAN_CLEANUP'
        Env       = @{
            # Live. Every run still logs the complete would-delete list before
            # touching anything, and refuses to act at all if qBittorrent cannot
            # be reached to build the protected-torrent list.
            'OC_DRY_RUN'  = 'false'
            'OC_LOG_FILE' = '/c/ServerData/Stacks/orphan-cleanup.log'
        }
        Timeout   = 3600
    }
}

$Def     = $Jobs[$Job]

# Resolve the push token AFTER the job is selected, and record whether the
# lookup succeeded.
#
# This used to read `PushToken = $Secrets['KUMA_PUSH_...']` inside the job
# table. A PowerShell hashtable returns $null for a key it does not have, so a
# renamed, misspelled or not-yet-added entry in secrets.env produced an empty
# token -- and Send-Heartbeat treats an empty token as "this job has no monitor
# on purpose" and reports success. The result would be a job that runs, passes,
# and quietly never pings Kuma, which is precisely the silent-monitoring
# failure the tokens were added on 2026-07-28 to remove. A configuration
# mistake must not be indistinguishable from a configuration choice.
#
# The job still RUNS when the token is missing -- a backup not taken is worse
# than a backup taken unmonitored -- but the mistake is surfaced three ways:
# an event log Error, a field in the status file, and the Kuma monitor itself
# going down at its next expected heartbeat.
$PushToken       = ''
$PushTokenBroken = $false
if ($Def.ContainsKey('PushTokenName') -and -not [string]::IsNullOrWhiteSpace($Def.PushTokenName)) {
    if ($Secrets.ContainsKey($Def.PushTokenName) -and -not [string]::IsNullOrWhiteSpace($Secrets[$Def.PushTokenName])) {
        $PushToken = $Secrets[$Def.PushTokenName]
    } else {
        $PushTokenBroken = $true
    }
}

$JobLogs = Join-Path $LogRoot $Job
if (-not (Test-Path $JobLogs)) { New-Item -ItemType Directory -Force -Path $JobLogs | Out-Null }

$Stamp      = Get-Date -Format 'yyyyMMdd-HHmmss'
$RunLog     = Join-Path $JobLogs "$Stamp.log"
$StatusFile = Join-Path $LogRoot "$Job-status.json"
$LockFile   = Join-Path $LogRoot "$Job.lock"

function Write-Status {
    param([string]$State, [int]$Code, [string]$Summary, [double]$Seconds)
    $payload = [ordered]@{
        job          = $Job
        state        = $State
        exitCode     = $Code
        summary      = $Summary
        durationSec  = [math]::Round($Seconds, 1)
        # false means this job expects an Uptime Kuma monitor but its token
        # could not be resolved from secrets.env, so no heartbeat was sent.
        heartbeatConfigured = (-not $PushTokenBroken)
        finishedUtc  = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
        finishedLocal= (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
        runLog       = $RunLog
    }
    $payload | ConvertTo-Json | Out-File -FilePath $StatusFile -Encoding utf8
}

function Send-Heartbeat {
    param([string]$State, [string]$Message, [double]$Seconds)
    # A job that declares no PushTokenName has no Uptime Kuma monitor on
    # purpose. Report success so the caller does not log a "heartbeat could not
    # be sent" warning on every run for a monitor that was never meant to
    # exist. A job that DOES declare one but whose token could not be resolved
    # is a different case entirely and is reported as a failure to send.
    if ($PushTokenBroken) { return $false }
    if ([string]::IsNullOrWhiteSpace($PushToken)) { return $true }
    # A failure to reach Kuma must never fail the job itself.
    try {
        $url = "$KumaBase/api/push/$($PushToken)?status=$State&msg=$([uri]::EscapeDataString($Message))&ping=$([math]::Round($Seconds * 1000))"
        Invoke-RestMethod -Uri $url -TimeoutSec 20 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Write-JobEvent {
    param([string]$EntryType, [string]$Message, [int]$EventId)
    try {
        if (-not [System.Diagnostics.EventLog]::SourceExists($EventSrc)) {
            New-EventLog -LogName Application -Source $EventSrc
        }
        Write-EventLog -LogName Application -Source $EventSrc -EntryType $EntryType `
                       -EventId $EventId -Message $Message
    } catch {
        # Event log unavailable (no admin rights) -- the status file and the
        # missed Kuma heartbeat still make the failure visible.
    }
}

# ---- monitoring configuration --------------------------------------------
# Reported before the job runs, so the reason is in the transcript even if the
# job itself then fails for an unrelated reason.
if ($PushTokenBroken) {
    $msg = "$Job declares Uptime Kuma token '$($Def.PushTokenName)' but secrets.env has no usable value for it. " +
           "The job will still run, but NO heartbeat will be sent and the Kuma monitor will go down at its next expected beat."
    Add-Content -Path $RunLog -Value $msg
    Write-JobEvent -EntryType Error -Message $msg -EventId 1005
}

# ---- overlap guard ---------------------------------------------------------
if (Test-Path $LockFile) {
    $lockAge = (Get-Date) - (Get-Item $LockFile).LastWriteTime
    if ($lockAge.TotalSeconds -lt $Def.Timeout) {
        $msg = "Previous $Job run still in progress (lock is $([math]::Round($lockAge.TotalMinutes,1)) min old). Skipping this cycle."
        Add-Content -Path $RunLog -Value $msg
        Write-JobEvent -EntryType Warning -Message $msg -EventId 1002
        exit 0
    }
    # Stale lock: the previous run died without cleaning up. That is itself a failure.
    $msg = "Stale lock for $Job ($([math]::Round($lockAge.TotalMinutes,1)) min old) -- previous run did not finish cleanly. Continuing."
    Add-Content -Path $RunLog -Value $msg
    Write-JobEvent -EntryType Warning -Message $msg -EventId 1003
    Remove-Item $LockFile -Force
}

$started = Get-Date
"$Job started $($started.ToString('yyyy-MM-dd HH:mm:ss'))" | Out-File -FilePath $LockFile -Encoding utf8

$exitCode = 0
$summary  = ''
try {
    foreach ($k in $Def.Env.Keys) { Set-Item -Path "Env:$k" -Value $Def.Env[$k] }

    if (-not (Test-Path $BashExe)) { throw "Git Bash not found at $BashExe" }

    $header = @(
        "=== $Job ===",
        "started : $($started.ToString('yyyy-MM-dd HH:mm:ss'))",
        "script  : $($Def.Script)",
        "env     : " + (($Def.Env.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join '  '),
        ''
    )
    # -Append, because $RunLog may already hold the PushTokenBroken warning that
    # the monitoring-configuration block wrote before the job started. Without
    # it, Out-File truncated the file and erased that warning -- so the comment
    # promising the reason would be "in the transcript" was false. $RunLog is
    # timestamped per run, so appending never mixes two runs together.
    $header | Out-File -FilePath $RunLog -Encoding utf8 -Append

    $output = & $BashExe $Def.Script *>&1
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) { $exitCode = 0 }
    $output | Out-File -FilePath $RunLog -Append -Encoding utf8

    # The bash scripts both end with a SUMMARY line; surface it as the heartbeat
    # message. Read it from the captured output, not the file, which is still
    # being written at this point.
    $summaryLine = $output | Where-Object { $_ -match 'SUMMARY' } | Select-Object -Last 1
    if ($summaryLine) { $summary = ([string]$summaryLine -replace '^\S+\s+', '').Trim() }
    if (-not $summary) { $summary = "exit $exitCode" }
}
catch {
    $exitCode = 99
    $summary  = "wrapper error: $($_.Exception.Message)"
    Add-Content -Path $RunLog -Value $summary
}
finally {
    if (Test-Path $LockFile) { Remove-Item $LockFile -Force }
}

$elapsed = ((Get-Date) - $started).TotalSeconds

if ($exitCode -eq 0) {
    Write-Status -State 'ok' -Code $exitCode -Summary $summary -Seconds $elapsed
    $sent = Send-Heartbeat -State 'up' -Message $summary -Seconds $elapsed
    if (-not $sent) {
        Write-JobEvent -EntryType Warning -EventId 1004 `
            -Message "$Job succeeded but its Uptime Kuma heartbeat could not be sent. Kuma may be down."
    }
    Add-Content -Path $RunLog -Value "`nOK  exit=$exitCode  $([math]::Round($elapsed,1))s  heartbeat=$sent"
} else {
    Write-Status -State 'failed' -Code $exitCode -Summary $summary -Seconds $elapsed
    Send-Heartbeat -State 'down' -Message $summary -Seconds $elapsed | Out-Null
    Write-JobEvent -EntryType Error -EventId 1001 `
        -Message "$Job FAILED with exit code $exitCode.`n$summary`nTranscript: $RunLog"
    Add-Content -Path $RunLog -Value "`nFAILED  exit=$exitCode  $([math]::Round($elapsed,1))s"
}

# Keep the last 200 transcripts per job so this never fills the disk.
Get-ChildItem -Path $JobLogs -Filter '*.log' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 200 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exitCode
