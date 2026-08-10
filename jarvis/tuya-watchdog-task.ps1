# tuya-watchdog-task.ps1 - the thing Task Scheduler actually runs.
#
# Registered by install-watchdog-task.ps1. Kept separate from it so the
# scheduled action is a small, readable script rather than a long quoted
# command line buried in the task XML -- an unreadable action is one nobody
# audits.
#
# EXIT CODES FROM tuya-watchdog.py, and why the branching matters:
#   0  everything consistent                     -> done, exit 0
#   1  a device has MOVED                        -> repair, because this is
#                                                   exactly the failure the
#                                                   watchdog exists for
#   2  a device is not on the LAN at all         -> do NOT repair. The device
#                                                   is powered off or off WiFi.
#                                                   Repair cannot help and
#                                                   would write a wrong address.
#   3  environment error (HA down, no token)     -> do NOT repair, surface it
#
# The check runs first and unconditionally, so the common case does nothing at
# all and costs one ARP-cache read.

[CmdletBinding()]
param(
    [string] $Watchdog  = 'C:\ServerData\Stacks\jarvis\tuya-watchdog.py',
    [string] $PythonExe = 'C:\Users\Naiti\AppData\Local\Programs\Python\Python311\python.exe',
    [switch] $CheckOnly
)

$ErrorActionPreference = 'Continue'

$stamp = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
$dir   = Split-Path $Watchdog
$runLog = Join-Path $dir 'tuya-watchdog-task.log'

function Write-RunLog([string] $msg) {
    Add-Content -Path $runLog -Encoding utf8 -Value "$stamp $msg"
}

# The HA token lives in Stacks/secrets.env (one level up from the jarvis dir).
# Export it so the watchdog can reach the REST API. Never echoed.
$secrets = Join-Path (Split-Path $dir) 'secrets.env'
if (Test-Path $secrets) {
    foreach ($raw in (Get-Content $secrets -Encoding UTF8)) {
        $line = $raw.Trim()
        if ($line -match '^\s*#' -or $line -eq '' -or $line -notmatch '=') { continue }
        $kv = $line -split '=', 2
        if ($kv[0].Trim() -eq 'JARVIS_HASS_TOKEN' -and $kv[1].Trim()) {
            $env:JARVIS_HASS_TOKEN = $kv[1].Trim()
        }
    }
}
if (-not $env:JARVIS_HASS_TOKEN) {
    Write-RunLog "no JARVIS_HASS_TOKEN in $secrets - repair would be unavailable"
}

# ------------------------------------------------------------------ check ---

$checkOut = & $PythonExe $Watchdog --check 2>&1
$checkRc  = $LASTEXITCODE

switch ($checkRc) {
    0 { exit 0 }                                     # quiet, common case
    2 {
        Write-RunLog "check rc=2 - a device is not on the LAN. Not repairing."
        Write-RunLog ($checkOut -join "`n")
        exit 2
    }
    3 {
        Write-RunLog "check rc=3 - environment error. Not repairing."
        Write-RunLog ($checkOut -join "`n")
        exit 3
    }
}

# rc=1: a device moved.
Write-RunLog "check rc=1 - a device has MOVED."
Write-RunLog ($checkOut -join "`n")

if ($CheckOnly) {
    Write-RunLog "CheckOnly mode - not repairing. Run manually:"
    Write-RunLog "  $PythonExe $Watchdog --repair --commit"
    exit 1
}

# ----------------------------------------------------------------- repair ---

$repairOut = & $PythonExe $Watchdog --repair --commit 2>&1
$repairRc  = $LASTEXITCODE
Write-RunLog "repair rc=$repairRc"
Write-RunLog ($repairOut -join "`n")

if ($repairRc -ne 0) {
    Write-RunLog "REPAIR FAILED - Home Assistant may have lost local control."
    exit $repairRc
}

# Confirm the repair actually stuck rather than trusting the exit code. A
# repair that reports success but leaves the entity unavailable is the failure
# mode most worth catching.
Start-Sleep -Seconds 10
$verifyOut = & $PythonExe $Watchdog --check 2>&1
$verifyRc  = $LASTEXITCODE
Write-RunLog "post-repair verify rc=$verifyRc"
if ($verifyRc -ne 0) {
    Write-RunLog ($verifyOut -join "`n")
}
exit $verifyRc
