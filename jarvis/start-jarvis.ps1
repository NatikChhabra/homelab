# start-jarvis.ps1 -- one-command startup for the full JARVIS stack
#
# USAGE:
#   powershell -ExecutionPolicy Bypass -File start-jarvis.ps1
#   powershell -ExecutionPolicy Bypass -File start-jarvis.ps1 -Stop
#   powershell -ExecutionPolicy Bypass -File start-jarvis.ps1 -StatusOnly
#
# What it does:
#   1. Starts the Docker stack (waits for engine, compose up -d, health check)
#   2. Starts ears.py if not already running
#   3. Starts speaker.py if not already running
#   4. Starts pc-agent if not already running
#   5. Prints a status table
#
# Scheduled tasks handle reboot recovery. This script is for manual starts
# and for the 'one-command' requirement in B4.

[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$StatusOnly
)

$ErrorActionPreference = 'Continue'
$Stack = 'C:\ServerData\Stacks\jarvis'
$Python = 'C:\Users\Naiti\AppData\Local\Programs\Python\Python311\python.exe'
$Log = Join-Path $Stack 'logs\start-jarvis.log'
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null

function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -Path $Log -Value $line -Encoding utf8
    Write-Host $line
}

function Get-ProcInfo {
    param([string]$Exe, [string]$Pattern)
    $wmi = Get-WmiObject Win32_Process -ErrorAction SilentlyContinue |
           Where-Object { $_.ExecutablePath -like "*$Exe*" -and $_.CommandLine -like "*$Pattern*" } |
           Select-Object -First 1
    if ($wmi) {
        $start = [System.Management.ManagementDateTimeConverter]::ToDateTime($wmi.CreationDate)
        $uptime = (Get-Date) - $start
        return "RUNNING (PID {0}, up {1:N1}m)" -f $wmi.ProcessId, $uptime.TotalMinutes
    }
    return "NOT RUNNING"
}

Log "=== start-jarvis.ps1 ==="

if ($Stop) {
    Log "Stopping all JARVIS services..."
    Get-Process python*, node* -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like '*ears.py' -or
        $_.CommandLine -like '*speaker.py' -or
        $_.CommandLine -like '*pc-agent*' -or
        $_.CommandLine -like '*baileys*'
    } | Stop-Process -Force -ErrorAction SilentlyContinue
    Log "Manual process instances stopped. Scheduled tasks will restart as needed."
    exit 0
}

if ($StatusOnly) {
    Log "JARVIS Status:"
    Log ("  ears.py:        {0}" -f (Get-ProcInfo 'python' 'ears.py'))
    Log ("  speaker.py:     {0}" -f (Get-ProcInfo 'python' 'speaker.py'))
    Log ("  pc-agent:       {0}" -f (Get-ProcInfo 'python' 'pc-agent'))
    Log ("  wa-bridge:      {0}" -f (Get-ProcInfo 'node' 'baileys'))
    $containers = docker ps --filter 'name=jarvis-' --format '{{.Names}}: {{.Status}}' 2>$null
    foreach ($c in $containers) { Log ("  Docker {0}" -f $c) }
    exit 0
}

# --- Phase 1: Docker stack ---
Log "Phase 1: Docker stack..."
$deadline = (Get-Date).AddMinutes(10)
$ready = $false
while ((Get-Date) -lt $deadline) {
    docker info 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 10
}
if (-not $ready) {
    Log "FAIL: Docker engine not ready within 10 minutes"
    exit 1
}
Log "Docker engine ready"

Set-Location $Stack
docker compose up -d 2>$null | ForEach-Object { Log $_ }
Log "Docker compose up complete"

# Wait for voice container health
Log "Waiting for voice server..."
$ok = $false
for ($i = 0; $i -lt 24; $i++) {
    try {
        $r = Invoke-WebRequest 'http://localhost:3100/healthz' -TimeoutSec 5 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { Start-Sleep -Seconds 5 }
}
if ($ok) { Log "Voice server healthy on :3100" } else { Log "WARN: voice server not answering" }

# --- Phase 2: Host services ---
Log "Phase 2: Host services..."

# ears.py
$earsInfo = Get-ProcInfo 'python' 'ears.py'
if ($earsInfo -match 'NOT RUNNING') {
    Log "Starting ears.py via scheduled task..."
    Start-ScheduledTask -TaskPath '\JARVIS\' -TaskName 'JARVIS - Ears' -ErrorAction SilentlyContinue
} else {
    Log ("ears.py {0}" -f $earsInfo)
}

# speaker.py
$speakerInfo = Get-ProcInfo 'python' 'speaker.py'
if ($speakerInfo -match 'NOT RUNNING') {
    Log "Starting speaker.py via scheduled task..."
    Start-ScheduledTask -TaskPath '\JARVIS\' -TaskName 'JARVIS - Speaker' -ErrorAction SilentlyContinue
} else {
    Log ("speaker.py {0}" -f $speakerInfo)
}

# pc-agent
$pcagentInfo = Get-ProcInfo 'python' 'pc-agent'
if ($pcagentInfo -match 'NOT RUNNING') {
    Log "Starting pc-agent via scheduled task..."
    Start-ScheduledTask -TaskPath '\JARVIS\' -TaskName 'JARVIS - PC Agent' -ErrorAction SilentlyContinue
} else {
    Log ("pc-agent {0}" -f $pcagentInfo)
}

# --- Phase 3: Status table ---
Log ""
Log "=== JARVIS Status ==="
Log ("  ears.py:        {0}" -f (Get-ProcInfo 'python' 'ears.py'))
Log ("  speaker.py:     {0}" -f (Get-ProcInfo 'python' 'speaker.py'))
Log ("  pc-agent:       {0}" -f (Get-ProcInfo 'python' 'pc-agent'))
Log ("  wa-bridge:      {0}" -f (Get-ProcInfo 'node' 'baileys'))
$containers = docker ps --filter 'name=jarvis-' --format '{{.Names}}: {{.Status}}' 2>$null
foreach ($c in $containers) { Log ("  Docker {0}" -f $c) }
Log "=== done ==="
