<#
.SYNOPSIS
    Restart the WSL2 utility VM so a pending .wslconfig change takes effect,
    stopping and restarting every container safely around it.

.DESCRIPTION
    WHY THIS EXISTS: .wslconfig is read ONLY when the utility VM starts. Changes
    sit inert until something restarts it, and on this host that has repeatedly
    meant a setting looked configured and did nothing -- autoMemoryReclaim was
    added 2026-07-28 and had still never taken effect on 2026-09-09.

    `wsl --terminate <distro>` is NOT sufficient: it stops one distro but the
    utility VM survives, so .wslconfig is not re-read. Only `wsl --shutdown`
    tears down the VM. That is why this script is heavier than it looks.

    WHAT IT PROTECTS AGAINST:

    1. Abrupt container death. `wsl --shutdown` is the equivalent of pulling
       power on every container at once. Immich runs PostgreSQL and the *arr
       apps run SQLite; a torn write there is real data loss. Containers are
       therefore stopped gracefully first, with a timeout.

    2. Being orphaned by an SSH drop. This box's sshd keepalives are disabled
       over the Tailscale path, so a long operation can lose its terminal
       mid-flight. Run this DETACHED -- everything is logged to a file and the
       script completes on its own. A dropped session must never be able to
       leave the server with every container down.

    3. Docker Desktop not coming back. Its GUI runs in the INTERACTIVE session
       and cannot be relaunched from an SSH session (Session 0 cannot see the
       desktop). So this script deliberately does NOT quit Docker Desktop -- it
       leaves the GUI running and lets it rebuild the VM, then polls. If the
       engine never returns, that is reported loudly rather than being retried
       into a corner.

.PARAMETER SkipShutdown
    Do everything except the actual restart. Use to rehearse and read the log.
#>
[CmdletBinding()]
param([switch]$SkipShutdown)

$LogFile = 'C:\ServerData\Stacks\wsl-vm-restart.log'
$StacksRoot = 'C:\ServerData\Stacks'

# Brought back up explicitly, in dependency-ish order. Containers carry
# restart:always so most return by themselves, but "most" is not a plan --
# anything still down after the engine returns is started here by name.
$Stacks = @(
    'adguard'      # FIRST: DNS for the whole tailnet. Nothing else matters if
                   # the house cannot resolve a name.
    'jarvis'       # SECOND: the voice loop. The prime directive.
    'homarr', 'portainer', 'vaultwarden', 'uptime-kuma'
    'streaming', 'immich', 'vikunja', 'navidrome', 'filebrowser', 'server-room'
)

function Log {
    param([string]$Msg)
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ'), $Msg
    $line | Tee-Object -FilePath $LogFile -Append
}

function Wait-Engine {
    param([int]$TimeoutSec = 420)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        & docker version --format '{{.Server.Version}}' 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Seconds 5
    }
    return $false
}

Log '============================================================'
Log 'wsl-vm-restart START'
Log ('.wslconfig memory line: ' + ((Get-Content "$env:USERPROFILE\.wslconfig" | Where-Object { $_ -match '^memory=' }) -join ''))

# --- 1. Record what is running, so we can prove it all came back ------------
$before = @(docker ps --format '{{.Names}}' 2>$null)
Log ("containers running before: {0}" -f $before.Count)
$before | ForEach-Object { Log "    $_" }

if ($SkipShutdown) { Log 'SkipShutdown set - stopping here.'; exit 0 }

# --- 2. Graceful stop -------------------------------------------------------
# -t 30 gives PostgreSQL and SQLite time to checkpoint and close cleanly.
Log 'stopping all containers gracefully (timeout 30s each)...'
if ($before.Count -gt 0) {
    & docker stop -t 30 @before 2>&1 | Out-Null
    Log ("docker stop exit: {0}" -f $LASTEXITCODE)
}
$still = @(docker ps --format '{{.Names}}' 2>$null)
Log ("containers still running after stop: {0}" -f $still.Count)

# --- 3. The actual restart --------------------------------------------------
Log 'wsl --shutdown (this is what re-reads .wslconfig)'
& wsl --shutdown 2>&1 | ForEach-Object { Log "    wsl: $_" }
Start-Sleep -Seconds 20

# --- 4. Wait for the engine -------------------------------------------------
Log 'waiting for the Docker engine to return...'
if (-not (Wait-Engine -TimeoutSec 420)) {
    Log 'FATAL  the Docker engine did not come back within 7 minutes.'
    Log 'FATAL  Docker Desktop runs in the INTERACTIVE session and cannot be'
    Log 'FATAL  started from here. Someone must open Docker Desktop on the'
    Log 'FATAL  desktop. EVERY CONTAINER IS DOWN UNTIL THEN, INCLUDING ADGUARD'
    Log 'FATAL  (tailnet DNS) AND THE JARVIS VOICE LOOP.'
    exit 2
}
Log 'engine is back.'
Log ('VM memory now: ' + ((docker run --rm valkey/valkey:9 sh -c 'grep -E "^(MemTotal|MemAvailable)" /proc/meminfo' 2>$null) -join '  '))

# --- 5. Bring everything back -----------------------------------------------
foreach ($s in $Stacks) {
    $dir = Join-Path $StacksRoot $s
    if (-not (Test-Path (Join-Path $dir 'docker-compose.yml'))) { Log "  skip $s (no compose file)"; continue }
    # Sequential. Concurrent docker compose operations corrupt state here.
    & docker compose --project-directory $dir up -d 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { Log "  up   $s" } else { Log "  FAIL $s (exit $LASTEXITCODE)" }
}

# --- 6. Prove it ------------------------------------------------------------
Start-Sleep -Seconds 20
$after = @(docker ps --format '{{.Names}}' 2>$null)
Log ("containers running after: {0} (was {1})" -f $after.Count, $before.Count)
$missing = $before | Where-Object { $_ -notin $after }
if ($missing) { $missing | ForEach-Object { Log "  MISSING $_" } } else { Log '  nothing missing' }

foreach ($critical in @('adguardhome', 'jarvis-voice', 'jarvis-ollama')) {
    $st = & docker inspect -f '{{.State.Status}}' $critical 2>$null
    if ($st -eq 'running') { Log "  OK   $critical is running" }
    else { Log "  FAIL $critical is '$st' - THIS IS THE ONE THAT MATTERS" }
}

Log 'wsl-vm-restart DONE'
Log '============================================================'
