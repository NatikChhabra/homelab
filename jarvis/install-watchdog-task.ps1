# install-watchdog-task.ps1 - register (or remove) the JARVIS Tuya watchdog
# as a Scheduled Task.
#
# WHY A SCHEDULED TASK
# The watchdog exists for the moment the DHCP reservation is lost and the
# switch comes back on a different address. Nothing is watching if the watcher
# depends on someone running it by hand -- the whole point is that it fires
# while Natik is asleep. Task Scheduler runs it unattended.
#
# SHAPE OF THE JOB
#   Check task  : runs every 15 minutes, read-only.
#                 python tuya-watchdog.py --check
#   Repair task : 'Only when a previous task fails' -> but Task Scheduler
#                 triggers on NON-ZERO exit codes, and the script uses them
#                 deliberately: 0 = fine, 1 = device MOVED (repairable),
#                 2 = device offline (NOT repairable). So the repair task runs
#                 the script itself and lets the script decide:
#                 python tuya-watchdog.py --repair --commit
#                 The script returns 1 only if a repair FAILED, and the action
#                 then fails, which flags the task's LastTaskResult for a
#                 human. No repair is attempted while everything is consistent.
#
# EVERY 15 MINUTES
# The switch dropped off WiFi twice in one day. A 15-minute window is an order
# of magnitude shorter than the worst observed gap between a drop and a return
# being noticed, and cheap: a clean --check makes no network noise (ARP cache
# read + one read-only docker exec + a couple of HA REST calls) and writes no
# log line. Every 5 minutes would halve the window but double the chance of
# catching a device mid-reboot and misreading it as a move; every hour would
# leave the lights potentially unreachable for an hour. 15 is the sweet spot.
#
# ELEVATION
# Registering a task that runs as SYSTEM and is not restricted to the current
# user session requires an elevated shell. The script checks and refuses
# rather than half-applying.

[CmdletBinding()]
param(
    [string] $TaskName  = 'JARVIS - Tuya IP Watchdog',
    [string] $PythonExe = 'C:\Users\Naiti\AppData\Local\Programs\Python\Python311\python.exe',
    [string] $Watchdog  = 'C:\ServerData\Stacks\jarvis\tuya-watchdog.py',
    [int]    $Minutes   = 15,

    [switch] $CheckOnly,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "NOT ELEVATED - nothing was changed." -ForegroundColor Red
    Write-Host "Re-open PowerShell as Administrator and run:"
    Write-Host ""
    Write-Host "    powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -ForegroundColor Yellow
    if ($CheckOnly) {
        Write-Host "        -CheckOnly"
    }
    Write-Host ""
    exit 1
}

if ($Uninstall) {
    foreach ($name in @($TaskName, "$TaskName - Repair")) {
        $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "unregistered '$name'." -ForegroundColor Green
        } else {
            Write-Host "task '$name' is not registered; nothing to remove." -ForegroundColor Yellow
        }
    }
    exit 0
}

if (-not (Test-Path $Watchdog)) {
    Write-Host "watchdog script missing at $Watchdog" -ForegroundColor Red
    exit 1
}
if (-not (Test-Path $PythonExe)) {
    Write-Host "python not found at $PythonExe" -ForegroundColor Red
    Write-Host "Pass the right path with -PythonExe (check:  Get-Command python)."
    exit 1
}

# ------------------------------------------------------- register the task ---

# A single action script so that Task Scheduler's trigger matches are trivial
# and there is exactly one place that knows about the repair path. The helper
# runs the check and, unless -CheckOnly, a repair pass; its own exit code is
# the union of the two runs.
$helperPath = Join-Path (Split-Path $Watchdog) 'tuya-watchdog-task.ps1'
$settings = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable `
                -MultipleInstances IgnoreNew `
                -RestartCount 3 `
                -RestartInterval (New-TimeSpan -Minutes 5) `
                -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' `
                                        -LogonType ServiceAccount `
                                        -RunLevel Highest

$helperArgs = "`"$helperPath`" -Watchdog `"$Watchdog`" -PythonExe `"$PythonExe`""
if ($CheckOnly) { $helperArgs += ' -CheckOnly' }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
                                  -Argument "-NoProfile -ExecutionPolicy Bypass -NonInteractive -WindowStyle Hidden -File $helperArgs" `
                                  -WorkingDirectory (Split-Path $Watchdog)

# Omitting -RepetitionDuration means "repeat indefinitely". Do NOT pass
# [TimeSpan]::MaxValue -- it serialises to P99999999DT23H59M59S, which the Task
# Scheduler XML schema rejects outright with "value which is incorrectly
# formatted or out of range" (HRESULT 0x80041318). Hit on 2026-08-08.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
                                    -RepetitionInterval (New-TimeSpan -Minutes $Minutes)

$desc = "Checks that Home Assistant's tuya-local entries still point at the "
$desc += "right IPs for the AZIOT switch and Zoook IR blaster, and repairs a "
$desc += "moved device via HA's own options flow. Read-only unless a move is "
$desc += "detected."
if ($CheckOnly) { $desc += " Check-only mode: never writes." }

# ---------------------------------------------------------------- preflight --

# The scheduled task runs as SYSTEM. The watchdog itself needs no secrets file
# access (it reads tuya-local config from HA), but the HA token used for the
# REST calls is exported by the task helper from secrets.env, so SYSTEM needs
# read access to that file. It usually gets it via SeBackupPrivilege, but the
# file is deliberately ACL-restricted, so verify rather than hope.
$tokenFile = Join-Path (Split-Path (Split-Path $Watchdog)) 'secrets.env'
if (Test-Path $tokenFile) {
    $acl = Get-Acl $tokenFile
    $sysRule = $acl.Access | Where-Object {
        $_.IdentityReference -match 'SYSTEM' -and $_.FileSystemRights -match 'Read|FullControl'
    }
    if ($sysRule) {
        Write-Host "acl: SYSTEM can read $tokenFile"
    } else {
        Write-Host "acl: WARNING - no explicit SYSTEM read ACE on $tokenFile." -ForegroundColor Yellow
        Write-Host "     SYSTEM usually still reads it via SeBackupPrivilege, but if the"
        Write-Host "     task log says 'no token', grant it explicitly:" -ForegroundColor Yellow
        Write-Host "       icacls `"$tokenFile`" /grant `"NT AUTHORITY\SYSTEM:(R)`"" -ForegroundColor Yellow
    }
}

# ------------------------------------------------------------- registration --

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "task '$TaskName' already exists - replacing it."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
                       -Action $action `
                       -Trigger $trigger `
                       -Principal $principal `
                       -Settings $settings `
                       -Description $desc | Out-Null

Write-Host "registered '$TaskName'" -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State,
                  @{n='Trigger';e={'every ' + $Minutes + ' minutes, first run in ~2 min'}},
                  @{n='RunAs';e={$_.Principal.UserId}},
                  @{n='ExecutionLimit';e={'10 min'}} |
    Format-List

# Kick it off now instead of waiting for the trigger, so the first result is
# visible immediately.
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 25
$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "last run: $($info.LastRunTime)   result: $($info.LastTaskResult)   next: $($info.NextRunTime)"
Write-Host ""
Write-Host "task state: $((Get-ScheduledTask -TaskName $TaskName).State)"

$log = Join-Path (Split-Path $Watchdog) 'tuya-watchdog.log'
Write-Host "watchdog log: $log"
Write-Host ""
Write-Host "Verify later with:"
Write-Host "  Get-ScheduledTaskInfo -TaskName '$TaskName'"
Write-Host "  Get-Content `"$log`" -Tail 10"
