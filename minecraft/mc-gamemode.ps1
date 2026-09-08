<#
.SYNOPSIS
    Frees host memory for the Minecraft server by stopping non-essential stacks.

.DESCRIPTION
    WHY THIS EXISTS: this host has 15.4 GB of RAM in 2x8GB (both SODIMM slots
    full, board maxes at 32 GB). The WSL2 VM that runs Docker is capped in
    .wslconfig, and 33 containers already occupy ~7.0 GB of it. Tensura
    Evolutions asks for 8-12 GB. That memory does not exist while the full
    stack is running, so something has to yield for the duration of a session.

    Measured on 2026-09-09, this reclaims ~3.9 GB:
        streaming    ~1983 MB   (sonarr radarr bazarr prowlarr qbittorrent
                                 flaresolverr jellyfin jellyseerr maintainerr
                                 cleanuparr)
        immich        ~776 MB   (server + postgres + machine-learning + redis)
        server-room   ~756 MB   (glances)
        uptime-kuma   ~134 MB   (also stops it alerting on everything below)
        navidrome      ~63 MB
        vikunja        ~62 MB
        portainer      ~53 MB
        filebrowser    ~30 MB

    NOT STOPPED by a plain -On, each for a specific reason:
        jarvis      - the voice loop. Stopped ONLY with the explicit
                      -IncludeJarvis switch, never by default. It is ~2.75 GB
                      (mostly jarvis-ollama) and is the largest single block of
                      memory on the box, which is why the switch exists at all.
        adguard     - DNS for every device on the tailnet. Stopping it takes
                      the whole house off the internet, not just this box.
        homarr      - the dashboard, and the thing you would use to notice
                      something is down. Also how you find your way back.
        vaultwarden - only 40 MB, and needing a password mid-session with the
                      vault down is a bad trade for 40 MB.
        minecraft   - the point of the exercise.

    Uses `docker compose stop`, not `down`: containers are stopped in place,
    not removed, so -Off is fast and no volume or network is touched.

    Stacks are handled STRICTLY SEQUENTIALLY. Concurrent `docker compose`
    operations corrupt state on this host; see Stacks/CLAUDE.md section 12.

.PARAMETER On
    Stop the non-essential stacks. Run before playing.

.PARAMETER Off
    Start them again. Run when finished.

.PARAMETER Status
    Show what is running and the current memory picture. Changes nothing.

.EXAMPLE
    .\mc-gamemode.ps1 -Status
    .\mc-gamemode.ps1 -On
    .\mc-gamemode.ps1 -Off
#>
[CmdletBinding(DefaultParameterSetName = 'Status')]
param(
    [Parameter(ParameterSetName = 'On')]     [switch]$On,
    [Parameter(ParameterSetName = 'Off')]    [switch]$Off,
    [Parameter(ParameterSetName = 'Status')] [switch]$Status,

    # Also stop the JARVIS stack -- roughly 2.75 GB, most of it jarvis-ollama.
    #
    # THIS BREAKS THE VOICE LOOP. Wake word, speech-to-text, the LLM and
    # speech-out all stop answering until -Off. It exists because Natik asked
    # for it explicitly on 2026-09-09 to fit Minecraft, and it is the largest
    # single block of memory on the box. It is deliberately opt-in and never
    # part of a plain -On, because the voice loop is the one thing that is
    # supposed to survive everything else.
    #
    # Does NOT touch the "JARVIS - Keep Awake" scheduled task, which is what
    # stops the host entering Modern Standby. Killing that risks the machine
    # sleeping and taking the server off the network entirely -- the exact
    # outage this box has lost hours to before.
    [Parameter(ParameterSetName = 'On')]
    [Parameter(ParameterSetName = 'Off')]
    [switch]$IncludeJarvis
)

$ErrorActionPreference = 'Stop'
$StacksRoot = 'C:\ServerData\Stacks'

# Order matters on -On: heaviest first, so if something fails partway the
# largest saving has already landed. -Off reverses it.
$Optional = @(
    'streaming'
    'immich'
    'server-room'
    'uptime-kuma'
    'navidrome'
    'vikunja'
    'portainer'
    'filebrowser'
)

$Protected = @('jarvis', 'adguard', 'homarr', 'vaultwarden', 'minecraft')

# -IncludeJarvis moves jarvis out of Protected and onto the end of the stop
# list. AdGuard stays protected regardless: it is DNS for every device on the
# tailnet, so stopping it takes the house off the internet rather than just
# degrading this box.
if ($IncludeJarvis) {
    $Protected = $Protected | Where-Object { $_ -ne 'jarvis' }
    $Optional  = $Optional + 'jarvis'
}

function Get-MemPicture {
    $os    = Get-CimInstance Win32_OperatingSystem
    $free  = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
    $tot   = [math]::Round($os.TotalVisibleMemorySize / 1MB, 2)
    $avail = (Get-Counter '\Memory\Available MBytes' -ErrorAction SilentlyContinue).CounterSamples[0].CookedValue
    [pscustomobject]@{
        HostTotalGB     = $tot
        HostFreeGB      = $free
        HostAvailableMB = [int]$avail
    }
}

function Show-Status {
    Write-Host ''
    Write-Host '=== Host memory ===' -ForegroundColor Cyan
    Get-MemPicture | Format-List

    Write-Host '=== WSL2 VM memory (where the containers actually live) ===' -ForegroundColor Cyan
    # Uses an image already present locally. A registry pull would fail on this
    # host because the credential helper is broken; see docker-pull.sh.
    try { docker run --rm valkey/valkey:9 free -m 2>$null | Select-Object -First 3 }
    catch { Write-Host '  (could not read VM memory)' -ForegroundColor Yellow }

    Write-Host ''
    Write-Host '=== Optional stacks ===' -ForegroundColor Cyan
    foreach ($s in $Optional) {
        $n = @(docker compose --project-directory "$StacksRoot\$s" ps -q 2>$null).Count
        $state = if ($n -gt 0) { "RUNNING ($n)" } else { 'stopped' }
        $col   = if ($n -gt 0) { 'Yellow' } else { 'Green' }
        Write-Host ("  {0,-14} {1}" -f $s, $state) -ForegroundColor $col
    }
    Write-Host ''
    Write-Host '=== Protected (never stopped by this script) ===' -ForegroundColor Cyan
    foreach ($s in $Protected) {
        $n = @(docker compose --project-directory "$StacksRoot\$s" ps -q 2>$null).Count
        $state = if ($n -gt 0) { "running ($n)" } else { 'STOPPED - unexpected' }
        $col   = if ($n -gt 0) { 'Green' } else { 'Red' }
        Write-Host ("  {0,-14} {1}" -f $s, $state) -ForegroundColor $col
    }
    Write-Host ''
}

function Invoke-Stacks {
    param([string[]]$Stacks, [ValidateSet('stop', 'start')][string]$Action)

    $before = Get-MemPicture
    Write-Host ("Available before: {0} MB" -f $before.HostAvailableMB) -ForegroundColor Cyan

    $failed = @()
    foreach ($s in $Stacks) {
        $dir = Join-Path $StacksRoot $s
        if (-not (Test-Path (Join-Path $dir 'docker-compose.yml'))) {
            Write-Host ("  SKIP {0} - no docker-compose.yml" -f $s) -ForegroundColor Yellow
            continue
        }
        # Guard rail: refuse to touch a protected stack even if the list above
        # is edited carelessly later.
        if ($Protected -contains $s) {
            Write-Host ("  REFUSED {0} - protected stack" -f $s) -ForegroundColor Red
            continue
        }
        Write-Host ("  {0} {1} ..." -f $Action, $s) -NoNewline
        # Sequential on purpose. Do not parallelise docker compose here.
        #
        # Do NOT write this as `... 2>&1 | Out-Null`. Under Windows PowerShell
        # 5.1, redirecting a NATIVE command's stderr wraps each line in an
        # ErrorRecord (NativeCommandError). docker compose writes its ordinary
        # progress ("Container maintainerr Stopping") to stderr, so with
        # $ErrorActionPreference = 'Stop' the script threw on a perfectly
        # successful stop -- it aborted midway through the streaming stack on
        # 2026-09-09 having actually stopped everything it reported. Exit code
        # is the truth here, not the stream.
        $prevEAP = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & docker compose --project-directory $dir $Action 2>$null | Out-Null
        $rc = $LASTEXITCODE
        $ErrorActionPreference = $prevEAP
        if ($rc -eq 0) { Write-Host ' ok' -ForegroundColor Green }
        else { Write-Host (' FAILED (exit {0})' -f $rc) -ForegroundColor Red; $failed += $s }
    }

    Start-Sleep -Seconds 3
    $after = Get-MemPicture
    $delta = $after.HostAvailableMB - $before.HostAvailableMB
    Write-Host ("Available after:  {0} MB  (delta {1} MB)" -f $after.HostAvailableMB, $delta) -ForegroundColor Cyan

    if ($failed.Count) {
        Write-Host ("FAILED stacks: {0}" -f ($failed -join ', ')) -ForegroundColor Red
        exit 1
    }
}

if ($On) {
    Write-Host 'GAME MODE ON - stopping non-essential stacks' -ForegroundColor Magenta
    Invoke-Stacks -Stacks $Optional -Action stop
    Write-Host ''
    if ($IncludeJarvis) {
        Write-Host 'JARVIS IS DOWN - the voice loop will not answer until -Off.' -ForegroundColor Yellow
        Write-Host 'AdGuard, Homarr and Vaultwarden are still up.' -ForegroundColor Green
    } else {
        Write-Host 'JARVIS, AdGuard, Homarr and Vaultwarden are still up.' -ForegroundColor Green
    }
    Write-Host 'Run  .\mc-gamemode.ps1 -Off  when you finish playing.' -ForegroundColor Green
}
elseif ($Off) {
    Write-Host 'GAME MODE OFF - restarting stacks' -ForegroundColor Magenta
    # Reverse order: lightest first, so the dashboard and monitoring come back
    # before the heavy media stack starts competing for disk I/O.
    $rev = $Optional.Clone()
    [array]::Reverse($rev)
    Invoke-Stacks -Stacks $rev -Action start
    Write-Host ''
    Write-Host 'All stacks restarted. Verify with:  .\mc-gamemode.ps1 -Status' -ForegroundColor Green
}
else {
    Show-Status
}
