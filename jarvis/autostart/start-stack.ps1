# Bring the JARVIS docker stack up at logon.
#
# Docker Desktop starts asynchronously and is NOT ready when the logon trigger
# fires -- `docker compose up` against a dead engine fails instantly and the task
# would report success having done nothing. So this waits for the engine to
# actually answer before doing anything, then verifies containers are running.
#
# Deliberately does NOT run `docker compose pull`. Measured on this host: the
# credential helper needs an interactive session, so an unattended pull fails and
# can leave the stack half-updated. Image updates stay a manual, supervised act.

$ErrorActionPreference = 'Continue'
$Stack = 'C:\ServerData\Stacks\jarvis'
$Log   = Join-Path $Stack 'logs\autostart-stack.log'
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null

function Log($m) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    Add-Content -Path $Log -Value $line -Encoding utf8
}

Log "=== autostart: stack ==="

# Wait up to 10 minutes for the Docker engine. A cold laptop with an HDD and
# WSL2 starting has genuinely taken minutes here.
$deadline = (Get-Date).AddMinutes(10)
$ready = $false
while ((Get-Date) -lt $deadline) {
    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 10
}

if (-not $ready) {
    Log "FAIL docker engine never became ready within 10 minutes"
    exit 1
}
Log "docker engine ready"

Set-Location $Stack
docker compose up -d 2>&1 | ForEach-Object { Log $_ }

Start-Sleep -Seconds 15
$running = (docker ps --filter 'name=jarvis-' --format '{{.Names}}') -split "`n" | Where-Object { $_ }
Log ("containers up: {0} -> {1}" -f $running.Count, ($running -join ', '))

# The voice server is the one that matters for "is JARVIS alive", because it is
# what the browser, Rainmeter and the phone all talk to.
$ok = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest 'http://localhost:3100/healthz' -TimeoutSec 5 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ok = $true; break }
    } catch { Start-Sleep -Seconds 5 }
}
Log $(if ($ok) { "voice server healthy on :3100" } else { "WARN voice server not answering on :3100" })
Log "=== done ==="
