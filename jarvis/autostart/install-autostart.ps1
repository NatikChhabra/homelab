# Bring JARVIS up automatically at logon.
#
# WHAT THIS SOLVES
# Measured on this machine: the laptop booted 06:53:01 and Docker did not start
# until 11:36:27 -- a 4 h 43 m window with zero JARVIS containers running. Two
# unexpected shutdowns in 30 days; one recovered in 24 minutes, the other took
# about 32 hours. For an assistant that is supposed to be always on, that is the
# single largest source of downtime in the project, larger than any bug.
#
# WHY LOGON AND NOT BOOT
# Every autostart mechanism that can reach Docker Desktop, the microphone and the
# audio endpoints is per-user. Docker Desktop itself only starts in an interactive
# session, and both the mic and the Echo Dot live in the user's audio session -- a
# SYSTEM task cannot use either. So "autostart" here honestly means "starts when
# Natik logs in".
#
# Natik's decision, 2026-08-09, when told auto-logon might not be possible:
# "its fine if this does not work it should just start i can manually type the
# password". So this deliberately does NOT require auto-logon. He types a password
# if Windows asks, and JARVIS is running by the time the desktop is usable. Enabling
# auto-logon later is an independent choice with a real security cost (FOR-NATIK.md
# item 5) and this build does not quietly depend on it.
#
# WHY A SCHEDULED TASK AND NOT THE STARTUP FOLDER
# The Startup folder gives no retry, no logging, and pops console windows in the
# user's face. A task can wait for the network, retry, and run hidden.
#
# Idempotent: re-running replaces the tasks rather than stacking duplicates.

[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'

$JarvisDir = 'C:\ServerData\Stacks\jarvis'
$LogDir    = Join-Path $JarvisDir 'logs'
$Python    = (Get-Command python -ErrorAction SilentlyContinue).Source
$TaskPath  = '\JARVIS\'

$Tasks = @(
    @{ Name = 'JARVIS - Stack';   Script = 'autostart\start-stack.ps1'; Delay = 'PT30S' }
    @{ Name = 'JARVIS - Ears';    Script = 'autostart\start-ears.ps1';  Delay = 'PT2M'  }
    @{ Name = 'JARVIS - Speaker'; Script = 'autostart\start-speaker.ps1'; Delay = 'PT2M' }
)

function Remove-JarvisTasks {
    foreach ($t in $Tasks) {
        $existing = Get-ScheduledTask -TaskName $t.Name -TaskPath $TaskPath -ErrorAction SilentlyContinue
        if ($existing) {
            Unregister-ScheduledTask -TaskName $t.Name -TaskPath $TaskPath -Confirm:$false
            Write-Host "removed: $($t.Name)" -ForegroundColor Yellow
        }
    }
}

if ($Uninstall) {
    Remove-JarvisTasks
    Write-Host "JARVIS autostart removed." -ForegroundColor Green
    return
}

if (-not $Python) { throw "python not found on PATH" }
if (-not (Test-Path $JarvisDir)) { throw "jarvis dir not found: $JarvisDir" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

Write-Host "python:  $Python"
Write-Host "jarvis:  $JarvisDir"
Write-Host "logs:    $LogDir`n"

Remove-JarvisTasks

foreach ($t in $Tasks) {
    $script = Join-Path $JarvisDir $t.Script
    if (-not (Test-Path $script)) {
        Write-Host "SKIP $($t.Name): missing $script" -ForegroundColor Yellow
        continue
    }

    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`"" `
        -WorkingDirectory $JarvisDir

    # AtLogOn with a delay. The stack needs Docker Desktop to be up before
    # `docker compose up` will do anything, and the voice services need the
    # stack -- hence the staggered delays rather than firing all three at once.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $trigger.Delay = $t.Delay

    # RunOnlyIfNetworkAvailable is deliberately NOT set: the Tuya devices and
    # containers are all local, and on a laptop the network can come up after
    # logon, which would make the task skip entirely rather than wait.
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 2) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)   # zero = no limit; these are daemons
    # NOTE: never pass [TimeSpan]::MaxValue anywhere in a task definition -- it
    # serialises to P99999999DT23H59M59S and Task Scheduler rejects the whole
    # registration. Zero is the documented "no limit" value.

    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    if ($WhatIf) {
        Write-Host "WOULD REGISTER: $($t.Name) -> $script (delay $($t.Delay))" -ForegroundColor Cyan
        continue
    }

    Register-ScheduledTask -TaskName $t.Name -TaskPath $TaskPath `
        -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "JARVIS autostart. See Stacks/jarvis/autostart/." | Out-Null
    Write-Host "registered: $($t.Name)  (logon + $($t.Delay))" -ForegroundColor Green
}

Write-Host "`nRegistered tasks:" -ForegroundColor Cyan
Get-ScheduledTask -TaskPath $TaskPath -ErrorAction SilentlyContinue |
    Select-Object TaskName, State | Format-Table -AutoSize

Write-Host @"

Start now without waiting for a logon:
    Start-ScheduledTask -TaskPath '$TaskPath' -TaskName 'JARVIS - Stack'
    Start-ScheduledTask -TaskPath '$TaskPath' -TaskName 'JARVIS - Speaker'
    Start-ScheduledTask -TaskPath '$TaskPath' -TaskName 'JARVIS - Ears'

Logs land in $LogDir.
Remove everything again with:  .\install-autostart.ps1 -Uninstall
"@ -ForegroundColor Gray
