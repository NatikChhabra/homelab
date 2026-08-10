# install-task.ps1 - register (or remove) the JARVIS WOL helper as a boot-time
# Scheduled Task.
#
# WHY A SCHEDULED TASK AND NOT A STARTUP SHORTCUT
# The helper must be running before anyone logs in, because Home Assistant
# starts with Docker at boot and will call /status immediately. A Startup-folder
# shortcut or a HKCU Run key only fires at interactive logon, so the helper
# would be absent on a headless boot. This registers a task that runs as SYSTEM
# with an At Startup trigger, which needs no logon at all.
#
# REQUIRES AN ELEVATED SHELL. Registering a SYSTEM-principal task is an admin
# operation; the script checks and refuses rather than half-failing.

[CmdletBinding()]
param(
    [string] $TaskName   = 'JARVIS - WOL Helper',
    [string] $ScriptPath = 'C:\ServerData\Stacks\jarvis\host-helper\wol-helper.ps1',
    [string] $TokenFile  = 'C:\ServerData\Stacks\jarvis\secrets.env',
    [int]    $Port       = 8099,

    # Wait for the network stack before the helper tries to find the LAN adapter.
    [int]    $StartDelaySeconds = 30,

    # Create a strong token and append it to secrets.env. Deliberately opt-in:
    # the secret is generated on this machine and never printed in full.
    [switch] $GenerateToken,

    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "NOT ELEVATED. Re-open PowerShell as Administrator and run this again." -ForegroundColor Red
    exit 1
}
Write-Host "elevated: yes (running as $($id.Name))"

# ------------------------------------------------------------- uninstall ----

if ($Uninstall) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Host "task '$TaskName' is not registered; nothing to remove." -ForegroundColor Yellow
        exit 0
    }
    Stop-ScheduledTask  -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "unregistered '$TaskName'." -ForegroundColor Green
    Write-Host "Any helper process already running is left alone. To stop it:"
    Write-Host "  Get-CimInstance Win32_Process -Filter `"Name='powershell.exe'`" |"
    Write-Host "    Where-Object { `$_.CommandLine -like '*wol-helper.ps1*' } |"
    Write-Host "    ForEach-Object { Stop-Process -Id `$_.ProcessId -Force }"
    exit 0
}

# --------------------------------------------------------------- preflight --

if (-not (Test-Path $ScriptPath)) {
    Write-Host "helper script missing at $ScriptPath" -ForegroundColor Red
    exit 1
}
Write-Host "helper script: $ScriptPath"

# ------------------------------------------------------------ token setup ---

function Test-TokenPresent {
    if (-not (Test-Path $TokenFile)) { return $false }
    foreach ($raw in (Get-Content $TokenFile -Encoding UTF8)) {
        $line = $raw.Trim()
        if ($line -match '^\s*#' -or $line -eq '') { continue }
        $kv = $line -split '=', 2
        if ($kv.Length -eq 2 -and $kv[0].Trim() -eq 'JARVIS_WOL_TOKEN' -and $kv[1].Trim()) {
            return $true
        }
    }
    return $false
}

$hasToken = Test-TokenPresent

if ($GenerateToken) {
    if ($hasToken) {
        Write-Host "JARVIS_WOL_TOKEN already present in $TokenFile - not overwriting." -ForegroundColor Yellow
    } else {
        # 32 bytes of CSPRNG, hex encoded. Generated locally; only a prefix is shown.
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $token = -join ($bytes | ForEach-Object { $_.ToString('x2') })

        if (-not (Test-Path $TokenFile)) {
            Set-Content -Path $TokenFile -Value '# JARVIS secrets. NEVER COMMIT.' -Encoding utf8
        }
        Add-Content -Path $TokenFile -Encoding utf8 -Value @(
            ''
            '# --- host-side WOL helper --------------------------------------------------'
            '# Bearer token for wol-helper.ps1. Home Assistant sends this as'
            '#   Authorization: Bearer <value>'
            '# Rotate by replacing this value and restarting the "JARVIS - WOL Helper" task.'
            "JARVIS_WOL_TOKEN=$token"
        )
        Write-Host "generated a 64-char token and appended it to $TokenFile" -ForegroundColor Green
        Write-Host "  prefix (for matching against HA config): $($token.Substring(0,8))..."
        $hasToken = $true
    }
}

if (-not $hasToken) {
    Write-Host ""
    Write-Host "NO TOKEN CONFIGURED." -ForegroundColor Red
    Write-Host "The helper fails closed and will not start without one. Either:"
    Write-Host "  1. re-run this script with -GenerateToken, or"
    Write-Host "  2. add a line 'JARVIS_WOL_TOKEN=<at least 16 chars>' to $TokenFile"
    Write-Host "Registering the task anyway would create a job that exits 1 at every boot."
    exit 1
}
Write-Host "token: present in $TokenFile"

# Make sure SYSTEM can actually read the secrets file. SYSTEM normally bypasses
# ACLs, but this file is deliberately ACL-restricted, so verify rather than hope.
$acl = Get-Acl $TokenFile
$sysRule = $acl.Access | Where-Object {
    $_.IdentityReference -match 'SYSTEM' -and $_.FileSystemRights -match 'Read|FullControl'
}
if ($sysRule) {
    Write-Host "acl: NT AUTHORITY\SYSTEM has read access to the token file"
} else {
    Write-Host "acl: WARNING - no explicit SYSTEM read ACE on $TokenFile." -ForegroundColor Yellow
    Write-Host "     SYSTEM usually still reads it via SeBackupPrivilege, but if the task"
    Write-Host "     logs 'no token configured', grant it explicitly:"
    Write-Host "       icacls `"$TokenFile`" /grant `"NT AUTHORITY\SYSTEM:(R)`""
}

# ------------------------------------------------------ register the task ---

$argList = @(
    '-NoProfile'
    '-ExecutionPolicy', 'Bypass'
    '-WindowStyle', 'Hidden'
    '-NonInteractive'
    '-File', "`"$ScriptPath`""
    '-Port', $Port
    '-TokenFile', "`"$TokenFile`""
) -join ' '

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
                                  -Argument $argList `
                                  -WorkingDirectory (Split-Path $ScriptPath)

$trigger = New-ScheduledTaskTrigger -AtStartup
# The LAN adapter and default route may not exist the instant the task fires.
$trigger.Delay = "PT${StartDelaySeconds}S"

# LogonType ServiceAccount = runs with no interactive session and no stored
# password. RunLevel Highest so HttpListener and Remove-NetNeighbor work.
$principal = New-ScheduledTaskPrincipal -UserId 'NT AUTHORITY\SYSTEM' `
                                        -LogonType ServiceAccount `
                                        -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
                -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries `
                -StartWhenAvailable `
                -MultipleInstances IgnoreNew `
                -RestartCount 10 `
                -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero)   # 0 = run forever

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "task '$TaskName' already exists - replacing it."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName `
                       -Action $action `
                       -Trigger $trigger `
                       -Principal $principal `
                       -Settings $settings `
                       -Description ('JARVIS host-side Wake-on-LAN helper. Home Assistant cannot ' +
                                     'broadcast a magic packet from inside Docker/WSL2, so it calls ' +
                                     'this loopback HTTP service instead. Read-only against the ' +
                                     'target except for the wake packet.') | Out-Null

Write-Host ""
Write-Host "registered '$TaskName'" -ForegroundColor Green
Get-ScheduledTask -TaskName $TaskName |
    Select-Object TaskName, State, @{n='Trigger';e={'AtStartup +' + $StartDelaySeconds + 's'}},
                  @{n='RunAs';e={$_.Principal.UserId}} |
    Format-List

Write-Host "starting it now so you do not have to reboot..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 4

$state = (Get-ScheduledTask -TaskName $TaskName).State
$info  = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "task state: $state   lastResult: $($info.LastTaskResult)"

$bound = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($bound) {
    Write-Host "listening on port $Port :" -ForegroundColor Green
    $bound | Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize
    Write-Host "verify:  Invoke-RestMethod http://127.0.0.1:$Port/health"
} else {
    Write-Host "port $Port is NOT bound. Check the log:" -ForegroundColor Yellow
    Write-Host "  Get-Content C:\ServerData\Stacks\jarvis\host-helper\wol-helper.log -Tail 30"
}
