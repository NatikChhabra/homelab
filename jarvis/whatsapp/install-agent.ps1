# install-agent.ps1 - install (or remove) the JARVIS WhatsApp agent on the MAIN PC.
#
# RUN THIS ON THE MAIN PC (192.168.29.167), NOT ON THE SERVER LAPTOP.
# The WhatsApp session lives in Natik's desktop session there, and UI automation
# cannot cross a machine or a session boundary.
#
# WHY AT-LOGON AND NOT AT-STARTUP  (this is the opposite of wol-helper's task)
# wol-helper runs as SYSTEM with an At Startup trigger so it works before anyone
# logs in. This agent MUST NOT do that. MEASURED on the server laptop: a process
# in session 0 enumerates ZERO windows through UI Automation, because session 0
# has no visible desktop. A SYSTEM service therefore cannot see or drive
# WhatsApp at all. The trigger is At Logon and the principal is the interactive
# user, which is the only configuration in which this can work.
#
# WHY NOT ELEVATED
# Deliberately RunLevel Limited. UIPI blocks a lower-integrity process from
# sending input to a higher-integrity window, but WhatsApp runs unelevated, so
# an unelevated agent is sufficient. Running it as admin would add privilege for
# no capability. The one thing elevation would have bought - binding a
# non-loopback HTTP prefix - is granted instead by a narrow urlacl reservation
# made once, here, by this script.
#
# CONSEQUENCE YOU MUST ACCEPT: there is no auto-logon on this machine, so the
# agent does not exist until Natik logs in. JARVIS cannot message anyone while
# the PC sits at the login screen. That is a real limitation, not an oversight.

[CmdletBinding()]
param(
    [string] $TaskName = 'JARVIS - WhatsApp Agent',
    [int]    $Port = 8098,

    # Empty = auto (Tailscale if present, else the default-route LAN adapter).
    [string] $BindAddress = '',

    [string[]] $AllowFrom = @('100.64.0.0/10', '192.168.29.108/32', '127.0.0.1/32'),

    # Token shared with Home Assistant. PREFERRED: generate it on the server
    # laptop, put it in that machine's secrets.env, and pass the same value here
    # with -Token, so the secret never has to be read off a console.
    [string] $Token = '',
    [switch] $GenerateToken,

    [string] $TokenFile = "$env:LOCALAPPDATA\jarvis\whatsapp-secrets.env",

    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Definition }
$agentScript = Join-Path $here 'wa-agent.ps1'
$uiaScript   = Join-Path $here 'wa_uia.ps1'
$fwRuleName  = 'JARVIS WhatsApp Agent'

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
$elevated = $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# --------------------------------------------------------------- uninstall ---

if ($Uninstall) {
    Write-Host "Removing the JARVIS WhatsApp agent." -ForegroundColor Cyan

    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($t) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "  removed scheduled task '$TaskName'" -ForegroundColor Green
    } else {
        Write-Host "  scheduled task '$TaskName' was not registered"
    }

    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*wa-agent.ps1*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  stopped running agent (pid $($_.ProcessId))" -ForegroundColor Green
        }

    if ($elevated) {
        foreach ($a in @($BindAddress, '+')) {
            if (-not $a) { continue }
            $url = "http://${a}:$Port/"
            $null = netsh http delete urlacl url=$url 2>&1
        }
        Write-Host "  removed urlacl reservations for port $Port" -ForegroundColor Green
        if (Get-NetFirewallRule -DisplayName $fwRuleName -ErrorAction SilentlyContinue) {
            Remove-NetFirewallRule -DisplayName $fwRuleName
            Write-Host "  removed firewall rule '$fwRuleName'" -ForegroundColor Green
        }
    } else {
        Write-Host "  NOT ELEVATED: urlacl and firewall rule were left in place." -ForegroundColor Yellow
        Write-Host "  To finish, re-run this from an elevated shell with -Uninstall."
    }

    Write-Host ""
    Write-Host "The token file was NOT deleted, in case you want to reuse it:" -ForegroundColor Yellow
    Write-Host "  $TokenFile"
    Write-Host "Remove it with:  Remove-Item '$TokenFile'"
    Write-Host ""
    Write-Host "Nothing was installed system-wide, so there is nothing else to undo."
    Write-Host "WhatsApp itself is untouched: this never linked a device or changed your account."
    exit 0
}

# --------------------------------------------------------------- preflight ---

Write-Host "JARVIS WhatsApp agent installer" -ForegroundColor Cyan
Write-Host ""

$sid = (Get-Process -Id $PID).SessionId
if ($sid -eq 0) {
    Write-Host "FATAL: this shell is in session 0, which has no desktop." -ForegroundColor Red
    Write-Host "Run this from a normal interactive PowerShell window on the main PC."
    exit 1
}
Write-Host "  session:  $sid (interactive), user $env:USERNAME"

if (-not (Test-Path $agentScript)) { Write-Host "FATAL: missing $agentScript" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $uiaScript))   { Write-Host "FATAL: missing $uiaScript" -ForegroundColor Red; exit 1 }
Write-Host "  scripts:  found wa-agent.ps1 and wa_uia.ps1"

if (-not $elevated) {
    Write-Host ""
    Write-Host "NOT ELEVATED." -ForegroundColor Red
    Write-Host "A one-time elevated run is needed to reserve the HTTP URL and add a"
    Write-Host "firewall rule. The AGENT ITSELF will run unelevated afterwards."
    Write-Host "Re-open PowerShell as Administrator and run this again."
    exit 1
}
Write-Host "  elevated: yes (one-time, for urlacl + firewall only)"

# WhatsApp presence is reported, not required: the agent is useful before
# WhatsApp is set up, and 'status' is what tells you what is missing.
$waPkg = Get-AppxPackage -Name '*WhatsApp*' -ErrorAction SilentlyContinue
if ($waPkg) {
    Write-Host "  whatsapp: installed ($($waPkg.Name) $($waPkg.Version))" -ForegroundColor Green
} else {
    Write-Host "  whatsapp: NOT found as an Appx package." -ForegroundColor Yellow
    Write-Host "            If you use the .exe build that is fine; 'status' will confirm."
}

# ------------------------------------------------------------------ token ---

function Test-TokenPresent {
    if (-not (Test-Path $TokenFile)) { return $false }
    foreach ($raw in (Get-Content $TokenFile -Encoding UTF8)) {
        $kv = $raw.Trim() -split '=', 2
        if ($kv.Length -eq 2 -and $kv[0].Trim() -eq 'JARVIS_WHATSAPP_TOKEN' -and $kv[1].Trim()) { return $true }
    }
    return $false
}

New-Item -ItemType Directory -Force -Path (Split-Path $TokenFile) | Out-Null

if ($Token) {
    if ($Token.Length -lt 16) {
        Write-Host "FATAL: supplied token is $($Token.Length) chars; minimum is 16." -ForegroundColor Red
        exit 1
    }
    Set-Content -Path $TokenFile -Encoding utf8 -Value @(
        '# JARVIS WhatsApp agent secret. NEVER COMMIT THIS FILE.'
        '# Must match JARVIS_WHATSAPP_TOKEN in the server laptop''s Stacks/secrets.env.'
        "JARVIS_WHATSAPP_TOKEN=$Token"
    )
    Write-Host "  token:    written to $TokenFile (from -Token)" -ForegroundColor Green
}
elseif ($GenerateToken -and -not (Test-TokenPresent)) {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $gen = -join ($bytes | ForEach-Object { $_.ToString('x2') })
    Set-Content -Path $TokenFile -Encoding utf8 -Value @(
        '# JARVIS WhatsApp agent secret. NEVER COMMIT THIS FILE.'
        "JARVIS_WHATSAPP_TOKEN=$gen"
    )
    Write-Host "  token:    generated and written to $TokenFile" -ForegroundColor Green
    Write-Host ""
    Write-Host "  COPY THIS INTO THE SERVER LAPTOP'S Stacks/secrets.env, then clear your scrollback:" -ForegroundColor Yellow
    Write-Host "    JARVIS_WHATSAPP_TOKEN=$gen"
    Write-Host ""
}
elseif (-not (Test-TokenPresent)) {
    Write-Host ""
    Write-Host "NO TOKEN CONFIGURED." -ForegroundColor Red
    Write-Host "The agent fails closed and will not start without one. Either:"
    Write-Host "  1. re-run with -Token <the value already in the laptop's secrets.env>   (preferred), or"
    Write-Host "  2. re-run with -GenerateToken and copy the printed value to the laptop."
    exit 1
}
else {
    Write-Host "  token:    already present in $TokenFile"
}

# Restrict the token file to this user. It is outside the repo by design:
# C:\ServerData is the laptop's layout and does not exist on this machine.
try {
    $acl = New-Object System.Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $env:USERNAME, 'FullControl', 'Allow')))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        'Administrators', 'FullControl', 'Allow')))
    Set-Acl -Path $TokenFile -AclObject $acl
    Write-Host "  acl:      token file restricted to $env:USERNAME and Administrators" -ForegroundColor Green
} catch {
    Write-Host "  acl:      WARNING could not restrict $TokenFile - $($_.Exception.Message)" -ForegroundColor Yellow
}

# ------------------------------------------------------------ bind address ---

if (-not $BindAddress) {
    $ts = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          Where-Object { $_.IPAddress -like '100.*' -and $_.InterfaceAlias -match 'Tailscale' } |
          Select-Object -First 1
    if ($ts) {
        $BindAddress = $ts.IPAddress
        Write-Host "  bind:     $BindAddress (Tailscale - traffic is WireGuard-encrypted)" -ForegroundColor Green
    } else {
        $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
                 Sort-Object RouteMetric | Select-Object -First 1
        $addr = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 |
                Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1
        $BindAddress = $addr.IPAddress
        Write-Host "  bind:     $BindAddress (LAN)" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  PLAINTEXT WARNING: this is HTTP on the LAN, so 'read' responses carry" -ForegroundColor Yellow
        Write-Host "  WhatsApp message text in the clear. Anyone with the Wi-Fi password could" -ForegroundColor Yellow
        Write-Host "  capture it. Putting this PC on Tailscale removes that exposure entirely." -ForegroundColor Yellow
        Write-Host ""
    }
}

# --------------------------------------------------- urlacl + firewall rule ---

# A non-loopback HttpListener prefix normally requires admin. Reserving the URL
# for this specific user lets the agent bind while running unelevated, which is
# the whole point: least privilege for the process that drives the keyboard.
$url = "http://${BindAddress}:$Port/"
$null = netsh http delete urlacl url=$url 2>&1
$acct = "$env:USERDOMAIN\$env:USERNAME"
$res = netsh http add urlacl url=$url user=$acct 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "  urlacl:   reserved $url for $acct" -ForegroundColor Green
} else {
    Write-Host "  urlacl:   FAILED - $res" -ForegroundColor Red
    Write-Host "            The agent will not be able to bind. Fix this before continuing."
    exit 1
}

# Scope the firewall opening as tightly as the allowlist allows: this port, TCP,
# private profile, and only from the addresses that are permitted anyway.
if (Get-NetFirewallRule -DisplayName $fwRuleName -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -DisplayName $fwRuleName
}
$remoteAddrs = @($AllowFrom | Where-Object { $_ -notlike '127.*' })
if (-not $remoteAddrs) { $remoteAddrs = @('LocalSubnet') }
New-NetFirewallRule -DisplayName $fwRuleName `
    -Description 'Inbound for the JARVIS WhatsApp agent. Restricted to the JARVIS server and the tailnet.' `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -RemoteAddress $remoteAddrs -Profile Private, Domain -Enabled True | Out-Null
Write-Host "  firewall: allow TCP $Port inbound from $($remoteAddrs -join ', ') (private/domain only)" -ForegroundColor Green

# ------------------------------------------------------ register the task ---

$argList = @(
    '-NoProfile'
    '-ExecutionPolicy', 'Bypass'
    '-WindowStyle', 'Hidden'
    '-File', "`"$agentScript`""
    '-Port', $Port
    '-BindAddress', $BindAddress
    '-TokenFile', "`"$TokenFile`""
    '-AllowFrom', ($AllowFrom -join ',')
) -join ' '

$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $argList -WorkingDirectory $here
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$trigger.Delay = 'PT20S'

# InteractiveToken = runs only when this user is logged on, inside their desktop
# session, with no stored password. RunLevel Limited = NOT elevated.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -StartWhenAvailable -MultipleInstances IgnoreNew `
                -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) `
                -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "  task:     replaced existing '$TaskName'"
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description ('JARVIS WhatsApp agent. Drives the WhatsApp desktop app via UI Automation ' +
                  'inside the interactive session, because UI automation cannot work from ' +
                  'session 0. Unelevated by design. Never links a device to the account.') | Out-Null
Write-Host "  task:     registered '$TaskName' (At Logon, unelevated, +20s)" -ForegroundColor Green

Write-Host ""
Write-Host "Starting it now so you do not have to log out." -ForegroundColor Cyan
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 8

$bound = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($bound) {
    Write-Host "  listening on port ${Port}:" -ForegroundColor Green
    $bound | Select-Object LocalAddress, LocalPort, OwningProcess | Format-Table -AutoSize
} else {
    Write-Host "  port $Port is NOT bound. Check the log:" -ForegroundColor Yellow
    Write-Host "    Get-Content '$here\wa-agent.log' -Tail 30"
    exit 1
}

Write-Host ""
Write-Host "=== VERIFY, in this order ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "1. On THIS PC, liveness (no token needed, reveals nothing):"
Write-Host "     Invoke-RestMethod http://127.0.0.1:$Port/health"
Write-Host ""
Write-Host "2. On THIS PC, what the automation can actually see. Read-only: it does"
Write-Host "   not steal focus, type, or send. Run it with WhatsApp open."
Write-Host "     `$t = (Get-Content '$TokenFile' | Select-String 'JARVIS_WHATSAPP_TOKEN=').ToString().Split('=')[1]"
Write-Host "     Invoke-RestMethod ""http://127.0.0.1:$Port/whatsapp?action=status"" -Headers @{Authorization=""Bearer `$t""} | ConvertTo-Json -Depth 5"
Write-Host ""
Write-Host "   Look for composer_found / chat_list_found / message_list_found = True."
Write-Host "   If they are False, the accessibility names differ on your build. Run:"
Write-Host "     powershell -File '$here\wa_uia.ps1' -Action dump | ConvertFrom-Json | Select -Expand elements | Format-Table"
Write-Host "   and copy the real names into '$here\selectors.json'."
Write-Host ""
Write-Host "3. From the SERVER LAPTOP, prove the network path:"
Write-Host "     Invoke-RestMethod http://${BindAddress}:$Port/health"
Write-Host ""
Write-Host "4. ONLY when 1-3 pass, do a dry run against a real chat. Resolves and"
Write-Host "   verifies the target but types nothing:"
Write-Host "     .../whatsapp?action=preview&contact=<exact chat name>"
Write-Host ""
Write-Host "5. First real send: message YOURSELF. In WhatsApp your own chat is"
Write-Host "   usually named 'Message yourself'. Never test on a real contact."
Write-Host ""
Write-Host "Uninstall completely:  .\install-agent.ps1 -Uninstall   (elevated)"

