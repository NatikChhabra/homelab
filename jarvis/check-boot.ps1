# ============================================================================
# check-boot.ps1 -- did the whole JARVIS stack actually come back after a boot?
#
# WHY THIS EXISTS: every part of JARVIS is set to restart itself, so the stack
# LOOKS unattended, but the pieces come back at three different times and only
# two of them are automatic:
#
#   * tailscaled is a Windows service (Automatic, LocalSystem). It restores the
#     :8443 serve mapping ~12 s after boot with nobody logged in.
#   * Docker Desktop is a per-user desktop app. It starts from the HKCU Run key,
#     which means it starts AT LOGON, NOT AT BOOT. Measured on 2026-08-08: the
#     machine booted 06:53:01 and Docker Desktop did not start until 11:36:30 --
#     a 4 h 43 m window in which https://<tailnet>:8443 answered, proxied to a
#     dead localhost:3100, and JARVIS was entirely down.
#   * The containers only exist once Docker is up.
#
# The failure that costs the most time is the middle one, because it does not
# look like a boot problem. The Tailscale URL still resolves and still has a
# valid certificate, so the browser reaches something; it is the mic that
# breaks, and a broken mic reads as a JARVIS bug rather than "Docker is not
# running". This script names that difference in one line.
#
#   powershell -ExecutionPolicy Bypass -File check-boot.ps1
#   powershell -ExecutionPolicy Bypass -File check-boot.ps1 -Quiet
#
# STRICTLY READ-ONLY. It starts nothing, restarts nothing, pulls nothing, and
# is safe to run at any time, including while a voice command is in flight.
# Exit 0 = everything answered, 1 = warnings only, 2 = at least one FAIL.
# ============================================================================

[CmdletBinding()]
param(
    # Print only the summary line. For a scheduled/unattended caller.
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

$script:OK = 0; $script:WARN = 0; $script:FAIL = 0

function ok   { param([string]$m) $script:OK++;   if (-not $Quiet) { Write-Host "  OK    $m" -ForegroundColor Green } }
function warn { param([string]$m) $script:WARN++; Write-Host "  WARN  $m" -ForegroundColor Yellow }
function fail { param([string]$m) $script:FAIL++; Write-Host "  FAIL  $m" -ForegroundColor Red }
function sec  { param([string]$m) if (-not $Quiet) { Write-Host ""; Write-Host "== $m ==" -ForegroundColor Cyan } }

# Wyoming is a raw TCP protocol, NOT HTTP. `curl http://host:10300/` against a
# Wyoming server always fails and proves nothing. A socket connect is the
# cheapest probe that distinguishes "published port with nothing behind it"
# from "actually serving".
function Test-TcpPort {
    param([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 5000)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($iar)
        return $client.Connected
    } catch { return $false } finally { $client.Close() }
}

function Get-Http {
    param([string]$Uri, [int]$TimeoutSec = 20)
    try {
        return Invoke-WebRequest -Uri $Uri -TimeoutSec $TimeoutSec -UseBasicParsing -ErrorAction Stop
    } catch { return $null }
}

Write-Host ""
Write-Host "JARVIS boot check  --  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')" -ForegroundColor White

# ---------------------------------------------------------------------------
sec 'Boot timeline'
# Catches THE headline failure: the box rebooted (power cut, Windows Update)
# and came up to the logon screen, so Docker never launched and JARVIS is down
# while every "restart: always" policy is still perfectly satisfied.
# ---------------------------------------------------------------------------
$boot = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
$uptime = (Get-Date) - $boot
if (-not $Quiet) { Write-Host ("  ..    booted {0}  (up {1:N1} h)" -f $boot.ToString('yyyy-MM-dd HH:mm:ss'), $uptime.TotalHours) }

$dockerProc = Get-Process -Name 'com.docker.backend' -ErrorAction SilentlyContinue |
              Sort-Object StartTime | Select-Object -First 1
if (-not $dockerProc) {
    fail "Docker Desktop is NOT running. Nothing in JARVIS is up. This is almost always 'the machine rebooted and nobody logged in' -- Docker Desktop starts from the per-user Run key at LOGON, not at boot."
} else {
    $lag = $dockerProc.StartTime - $boot
    if ($lag.TotalMinutes -gt 5) {
        warn ("Docker started {0:N0} min AFTER boot ({1}) -- that whole window was JARVIS downtime. Expected if a human had to log in first." -f $lag.TotalMinutes, $dockerProc.StartTime.ToString('HH:mm:ss'))
    } else {
        ok ("Docker started {0:N0} min after boot -- consistent with an unattended start" -f $lag.TotalMinutes)
    }
}

# ---------------------------------------------------------------------------
sec 'Containers'
# Catches a container that died and exhausted its restart backoff, and the
# subtler case of a container that is "Up" but whose healthcheck never passed.
# "Running" is not "answering" -- every probe below hits a real endpoint.
# ---------------------------------------------------------------------------
$expected = @('jarvis-ollama','jarvis-hass','jarvis-whisper','jarvis-piper','jarvis-wake-word','jarvis-voice')
$dockerUp = $false
try {
    $psOut = & docker ps --format '{{.Names}}|{{.Status}}' 2>$null
    if ($LASTEXITCODE -eq 0) { $dockerUp = $true }
} catch { $dockerUp = $false }

if (-not $dockerUp) {
    fail "the Docker engine did not answer 'docker ps' -- container state is UNKNOWN, which is not the same as down"
} else {
    $running = @{}
    foreach ($line in $psOut) {
        if ($line -match '^([^|]+)\|(.*)$') { $running[$Matches[1]] = $Matches[2] }
    }
    foreach ($name in $expected) {
        if (-not $running.ContainsKey($name)) {
            fail "$name is NOT running"
        } elseif ($running[$name] -match 'unhealthy') {
            fail "$name is running but UNHEALTHY: $($running[$name])"
        } elseif ($running[$name] -match 'health: starting') {
            warn "$name is still inside its healthcheck start_period: $($running[$name])"
        } else {
            ok "$name  $($running[$name])"
        }
    }
    # A container that keeps dying and being restarted reads as "Up 4 seconds"
    # forever. Restart counts make that visible without waiting to catch it.
    foreach ($name in $expected) {
        if ($running.ContainsKey($name)) {
            $rc = & docker inspect --format '{{.RestartCount}}' $name 2>$null
            if ($rc -and [int]$rc -gt 3) { warn "$name has restarted $rc times -- it is crash-looping, not stable" }
        }
    }
}

# ---------------------------------------------------------------------------
sec 'Service endpoints'
# Catches the case the compose healthchecks were rewritten for on 2026-08-08:
# a probe must hit something UNAUTHENTICATED and real. The old HA healthcheck
# curled /api/, which needs a bearer token, so it returned 401 forever -- the
# container sat "unhealthy" while HA was fine and every probe was logged as a
# failed login. /manifest.json needs no token.
# ---------------------------------------------------------------------------
$r = Get-Http 'http://127.0.0.1:8123/manifest.json'
if ($r -and $r.StatusCode -eq 200) { ok "Home Assistant answers /manifest.json (unauthenticated)" }
else { fail "Home Assistant did not answer http://127.0.0.1:8123/manifest.json" }

$r = Get-Http 'http://127.0.0.1:3100/healthz'
if ($r -and $r.Content -match 'ok') { ok "jarvis-voice answers /healthz -- this is what :8443 proxies to" }
else { fail "jarvis-voice did not answer http://127.0.0.1:3100/healthz -- the UI will load nothing" }

# Wyoming: socket connect only. See Test-TcpPort above for why not HTTP.
foreach ($w in @(@('piper',10200), @('whisper',10300), @('openwakeword',10400))) {
    if (Test-TcpPort -TargetHost '127.0.0.1' -Port $w[1]) { ok "wyoming-$($w[0]) accepts TCP on $($w[1])" }
    else { fail "wyoming-$($w[0]) did not accept a TCP connection on $($w[1]) -- the voice loop will fail at that stage" }
}

# ---------------------------------------------------------------------------
sec 'Ollama model'
# Catches two distinct regressions that both look like "JARVIS got slow":
#  1) the model is installed but not RESIDENT, so the first command after idle
#     pays a 3-8 s cold load and blows the sub-3s voice target. OLLAMA_KEEP_ALIVE=-1
#     in the compose file is supposed to make this permanent.
#  2) the model is resident but on CPU. That is the RTX 3050 driver fault from
#     2026-08-08 coming back: WSL only projects the host driver libraries into
#     the VM at VM boot, so a reboot with a broken driver silently drops to CPU.
# /api/tags lists what is INSTALLED and would pass in both cases; /api/ps is
# the one that reports what is actually loaded, and its size_vram.
# ---------------------------------------------------------------------------
$r = Get-Http 'http://127.0.0.1:11434/api/ps' -TimeoutSec 15
if (-not $r) {
    fail "Ollama did not answer /api/ps on 11434"
} else {
    $ps = $r.Content | ConvertFrom-Json
    if (-not $ps.models -or $ps.models.Count -eq 0) {
        fail "Ollama is up but NO model is loaded -- the first voice command will pay a cold-load stall despite OLLAMA_KEEP_ALIVE=-1"
    } else {
        foreach ($m in $ps.models) {
            if ($m.size_vram -gt 0) {
                ok ("model {0} resident, {1:N1} GB in VRAM, context {2}" -f $m.name, ($m.size_vram / 1GB), $m.context_length)
            } else {
                fail ("model {0} is loaded on CPU, not GPU (size_vram=0) -- check the NVIDIA driver; WSL only picks it up at VM boot" -f $m.name)
            }
        }
    }
}

# ---------------------------------------------------------------------------
sec 'Tailscale HTTPS (:8443)'
# Catches the silent one. getUserMedia refuses to hand over a microphone
# outside a secure context, so the UI MUST be reached over HTTPS; that HTTPS is
# Tailscale's, on 8443 because Vaultwarden already owns 443. If the mapping is
# gone the page may still load over plain http and the mic just never turns on,
# with no error that names the cause.
# ---------------------------------------------------------------------------
$svc = Get-Service -Name 'Tailscale' -ErrorAction SilentlyContinue
if (-not $svc) { fail "the Tailscale service is not installed" }
elseif ($svc.Status -ne 'Running') { fail "the Tailscale service is $($svc.Status) -- :8443 cannot answer" }
elseif ($svc.StartType -ne 'Automatic') { warn "the Tailscale service start type is $($svc.StartType), not Automatic -- :8443 will not come back on its own after a reboot" }
else { ok "Tailscale service Running / Automatic (comes up at boot, no logon needed)" }

$tsExe = 'C:\Program Files\Tailscale\tailscale.exe'
if (-not (Test-Path $tsExe)) {
    warn "tailscale.exe not found at $tsExe -- skipping the serve-mapping check"
} else {
    $serveRaw = & $tsExe serve status --json 2>$null
    $dnsName = $null
    try { $dnsName = ((& $tsExe status --json 2>$null) | ConvertFrom-Json).Self.DNSName.TrimEnd('.') } catch {}

    $mapped = $false
    if ($serveRaw) {
        try {
            $serve = ($serveRaw | Out-String) | ConvertFrom-Json
            foreach ($p in $serve.Web.PSObject.Properties) {
                if ($p.Name -match ':8443$') {
                    $target = $p.Value.Handlers.'/'.Proxy
                    if ($target -match '3100') { $mapped = $true; ok "serve maps $($p.Name) -> $target (persisted daemon config, restored at service start)" }
                    else { fail "serve maps $($p.Name) -> $target, which is NOT jarvis-voice on 3100" }
                }
            }
        } catch { warn "could not parse 'tailscale serve status --json'" }
    }
    if (-not $mapped -and $script:FAIL -eq 0) {
        fail "no :8443 serve mapping exists -- re-create it with: tailscale serve --bg --https 8443 http://localhost:3100  (without --bg it dies with the shell)"
    }

    # The mapping existing is not the same as the mapping working. Fetch the
    # real URL end to end: this is the exact path the browser takes, and it is
    # the only check that proves TLS, the proxy hop, and the backend together.
    if ($dnsName) {
        $r = Get-Http "https://${dnsName}:8443/healthz" -TimeoutSec 25
        if ($r -and $r.StatusCode -eq 200 -and $r.Content -match 'ok') {
            ok "https://${dnsName}:8443/healthz returned 200 -- secure context is live, getUserMedia will be allowed"
        } elseif ($r) {
            fail "https://${dnsName}:8443/healthz returned $($r.StatusCode) -- the mapping is up but the backend behind it is not"
        } else {
            fail "https://${dnsName}:8443/healthz did not answer -- the mic will fail silently in the browser"
        }
    } else {
        warn "could not read this node's tailnet DNS name -- skipped the end-to-end HTTPS fetch"
    }
}

# ---------------------------------------------------------------------------
Write-Host ""
$verdict = if ($script:FAIL -gt 0) { 'FAILURES' } elseif ($script:WARN -gt 0) { 'WARNINGS' } else { 'ALL CLEAR' }
$colour  = if ($script:FAIL -gt 0) { 'Red' } elseif ($script:WARN -gt 0) { 'Yellow' } else { 'Green' }
Write-Host (" SUMMARY  ok={0}  warnings={1}  failures={2}   {3}" -f $script:OK, $script:WARN, $script:FAIL, $verdict) -ForegroundColor $colour
Write-Host ""

if ($script:FAIL -gt 0) { exit 2 } elseif ($script:WARN -gt 0) { exit 1 } else { exit 0 }
