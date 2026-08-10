# Start the JARVIS WhatsApp bridge, or restart it cleanly.
#
# WHY NOT JUST `node server.js`
# The bridge holds port 8097 and a WhatsApp WebSocket. Starting a second copy
# fails with EADDRINUSE *after* the old one is still happily linked, and killing
# by name (`pkill -f "node server.js"`) is unreliable here -- measured
# 2026-08-09, it left pid 18168 alive and holding the port. Killing by PORT OWNER
# is the only method that actually works, and it is the same lesson as
# host-helper/restart-agent.ps1.
#
# THE AUTH DIRECTORY IS THE LINK, AND IT MUST SURVIVE RESTARTS
# wa/auth/ holds the linked-device credentials. Deleting it logs the device out
# and forces a fresh QR scan on the phone. Never clear it as a troubleshooting
# step unless a re-link is actually intended.

#
# -Ensure IS THE MODE THE SCHEDULED TASK USES
# "JARVIS - WhatsApp" runs this with -Ensure at logon and every 5 minutes. It is
# a supervisor with no supervisor process: healthy means exit immediately and
# cost nothing, unhealthy means replace it.
#
# It deliberately tests /health rather than the port. A node process that has
# wedged still HOLDS port 8097 while answering nothing, so a port check alone
# would see a listener, call it healthy, and leave the bridge dead for as long
# as nobody spoke to it. This project has already been caught twice by treating
# "something is bound to the port" as "the service works" -- once on 8099, where
# the old WOL helper answered 200 for hours while the PC agent was not running
# at all.

param([switch]$Restart, [switch]$Ensure, [switch]$ShowQr)

$ErrorActionPreference = 'Continue'
$dir = 'C:\ServerData\Stacks\jarvis\wa'
$log = Join-Path $dir 'wa-bridge.log'

function Get-PortOwner {
    $c = Get-NetTCPConnection -LocalPort 8097 -State Listen -ErrorAction SilentlyContinue
    if ($c) { return $c[0].OwningProcess }
    return $null
}

function Test-Healthy {
    try {
        $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8097/health' -TimeoutSec 5
        return ($h.service -eq 'jarvis-whatsapp')
    } catch { return $false }
}

if ($Ensure) {
    if (Test-Healthy) {
        "healthy (pid $(Get-PortOwner)) - nothing to do"
        exit 0
    }
    # Not answering. If something still holds the port it is wedged, and it must
    # go or the replacement cannot bind.
    $owner = Get-PortOwner
    if ($owner) {
        Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        "unhealthy but holding 8097 - killed pid $owner"
        Start-Sleep -Seconds 2
    } else {
        "not running"
    }
}

if ($Restart) {
    $owner = Get-PortOwner
    if ($owner) {
        Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue
        "stopped pid $owner"
        foreach ($i in 1..25) {
            if (-not (Get-PortOwner)) { break }
            Start-Sleep -Milliseconds 200
        }
    }
}

if (Get-PortOwner) {
    "already running (pid $(Get-PortOwner)) - use -Restart to replace it"
} else {
    # Token comes from secrets.env, never the command line: a command line is
    # visible to every process on the box via Win32_Process.
    $tok = (Select-String -Path 'C:\ServerData\Stacks\jarvis\secrets.env' -Pattern '^JARVIS_WA_TOKEN=(.+)$').Matches.Groups[1].Value.Trim()
    if (-not $tok) { "ERROR: JARVIS_WA_TOKEN missing from secrets.env"; exit 1 }
    $env:WA_TOKEN = $tok
    $env:WA_PORT  = '8097'
    # The QR page must be openable from Natik's main PC, so the bridge also
    # listens on this machine's Tailscale address -- serving /qr and /health
    # there and nothing else. Read live rather than hard-coded: a tailnet
    # address can change, and a stale one means the listener silently never
    # binds.
    try {
        $ts = (& 'C:\Program Files\Tailscale\tailscale.exe' ip -4 2>$null | Select-Object -First 1)
        if ($ts) { $env:WA_TAILNET_IP = $ts.Trim() }
    } catch { }
    # -RedirectStandardOutput on a LONG-LIVED process makes Start-Process hold the
    # pipe, and this script then blocks until the child exits -- which for a
    # server is never. Measured 2026-08-09: the call hung past a 2-minute timeout
    # while the bridge itself was up and healthy the whole time. cmd /c with a
    # shell redirect detaches properly and the log still gets written.
    Start-Process -FilePath 'cmd.exe' `
        -ArgumentList '/c', "node server.js > `"$log`" 2>&1" `
        -WorkingDirectory $dir -WindowStyle Hidden
    Start-Sleep -Seconds 12
    $owner = Get-PortOwner
    if ($owner) { "started (pid $owner)" } else { "FAILED to start - see $log" }
}

try {
    $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8097/health' -TimeoutSec 5
    "connection=$($h.connection) linked=$($h.linked) contacts=$($h.contacts)"
    if ($h.awaiting_qr -and $ShowQr) {
        ""
        "Scan this in WhatsApp > Settings > Linked devices > Link a device:"
        ""
        Get-Content $log | Select-Object -Last 40
    } elseif ($h.awaiting_qr) {
        "QR is waiting - run with -ShowQr to print it"
    }
} catch {
    "health check failed: $($_.Exception.Message)"
}
