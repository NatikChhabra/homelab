# wol-helper.ps1 - host-side Wake-on-LAN helper for JARVIS.
#
# WHY THIS EXISTS
# Home Assistant runs in Docker Desktop on WSL2. jarvis-hass sits on the bridge
# network 172.30.0.0/16 with broadcast 172.30.255.255. The target PC is on the
# physical LAN 192.168.29.0/24. Those are different L2 segments and the WSL2
# bridge does not forward L2 broadcast, so a magic packet sent from inside the
# container can never reach the PC's NIC. HA therefore calls THIS process, which
# runs on the Windows host and broadcasts on the real LAN adapter.
#
# BINDING / EXPOSURE (measured 2026-08-08, not assumed)
# A probe listener bound to http://+:8099/ was curled from inside jarvis-hass.
# Requests to host.docker.internal and to 192.168.65.254 both arrived with
# LocalEndPoint = 127.0.0.1:8099. Docker Desktop forwards container->host
# traffic onto the host's loopback. So binding LOOPBACK-ONLY is reachable from
# Home Assistant while being unreachable from the LAN and from the internet.
# That is why the default prefix is 127.0.0.1 and why no firewall rule is
# needed. Do not "fix" this by binding to + or to the LAN IP.
#
# SECURITY
# /wake and /status require a bearer token. /health is deliberately
# unauthenticated so liveness can be probed without handing out the secret; it
# returns only uptime and version, never the target's address or state.
# The token is NEVER stored in this script. It comes from $env:JARVIS_WOL_TOKEN
# or from the JARVIS_WOL_TOKEN key in the already-gitignored secrets.env.
# The helper refuses to start if no token is configured (fail closed).
#
# This script never sends shutdown, sleep or reboot. Wake is the only write
# operation it can perform against the PC; everything else is read-only.

[CmdletBinding()]
param(
    [int]    $Port       = 8099,
    [string] $TargetMac  = 'D8:43:AE:17:63:25',
    [string] $TargetIp   = '192.168.29.167',

    # Empty means auto-detect the adapter that owns the default route.
    [string] $SourceIp   = '',
    [string] $Broadcast  = '',

    [string] $TokenFile  = 'C:\ServerData\Stacks\jarvis\secrets.env',
    [string] $LogFile    = 'C:\ServerData\Stacks\jarvis\host-helper\wol-helper.log',

    # Loopback only. See BINDING note above before changing.
    [string[]] $BindHosts = @('127.0.0.1'),

    [int]    $MaxLogBytes = 2MB
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Version   = '1.0.0'
$script:StartedAt = Get-Date

# ---------------------------------------------------------------- logging ---

function Write-Log {
    param([string]$Level, [string]$Message)
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    # MUST be Write-Host, never Write-Output. Write-Output emits into the
    # pipeline, so any function that logs would return its log lines alongside
    # its real value: Get-Token returned @(logline, token) and the caller then
    # measured .Length as 2 and refused to start with a "2 char token".
    Write-Host $line
    try {
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt $MaxLogBytes)) {
            Move-Item $LogFile "$LogFile.1" -Force
        }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch {
        # Never let a logging failure kill the service.
    }
}

# ------------------------------------------------------------------ token ---

function Get-Token {
    if ($env:JARVIS_WOL_TOKEN) {
        Write-Log INFO 'token source: environment variable JARVIS_WOL_TOKEN'
        return $env:JARVIS_WOL_TOKEN
    }
    if (Test-Path $TokenFile) {
        foreach ($raw in (Get-Content $TokenFile -Encoding UTF8)) {
            $line = $raw.Trim()
            if ($line -match '^\s*#' -or $line -eq '') { continue }
            $kv = $line -split '=', 2
            if ($kv.Length -eq 2 -and $kv[0].Trim() -eq 'JARVIS_WOL_TOKEN') {
                $val = $kv[1].Trim().Trim('"').Trim("'")
                if ($val) {
                    Write-Log INFO "token source: $TokenFile (key JARVIS_WOL_TOKEN)"
                    return $val
                }
            }
        }
    }
    return $null
}

# Length-independent comparison, so a wrong token cannot be recovered by timing.
function Test-TokenEqual {
    param([string]$Presented, [string]$Expected)
    if ($null -eq $Presented) { return $false }
    $a = [Text.Encoding]::UTF8.GetBytes($Presented)
    $b = [Text.Encoding]::UTF8.GetBytes($Expected)
    $diff = $a.Length -bxor $b.Length
    $n = [Math]::Max($a.Length, $b.Length)
    for ($i = 0; $i -lt $n; $i++) {
        $x = 0; $y = 0
        if ($i -lt $a.Length) { $x = $a[$i] }
        if ($i -lt $b.Length) { $y = $b[$i] }
        $diff = $diff -bor ($x -bxor $y)
    }
    return ($diff -eq 0)
}

# ------------------------------------------------------------- networking ---

function Resolve-LanInterface {
    # Pick the adapter that actually carries the default route. This host has 8
    # adapters (VirtualBox host-only, Tailscale, WSL vEthernet, two TAP
    # adapters, Wi-Fi), and an unbound broadcast can leave via the wrong one.
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
             Sort-Object RouteMetric |
             Select-Object -First 1
    if (-not $route) { throw 'no default route found; cannot pick a LAN adapter' }

    $addr = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike '169.254.*' } |
            Select-Object -First 1
    if (-not $addr) { throw "no usable IPv4 on interface $($route.InterfaceIndex)" }

    $ipBytes = ([Net.IPAddress]::Parse($addr.IPAddress)).GetAddressBytes()

    # Build the netmask byte-by-byte. Do NOT do this with [uint32]0xFFFFFFFF:
    # Windows PowerShell 5.1 parses that hex literal as Int32 -1, and the cast
    # to UInt32 then throws "Value was either too large or too small".
    $maskBytes = New-Object byte[] 4
    for ($i = 0; $i -lt 4; $i++) {
        $bits = [Math]::Min(8, [Math]::Max(0, $addr.PrefixLength - ($i * 8)))
        $maskBytes[$i] = [byte](((0xFF -shl (8 - $bits)) -band 0xFF))
    }

    $bcastBytes = New-Object byte[] 4
    for ($i = 0; $i -lt 4; $i++) {
        $bcastBytes[$i] = [byte]($ipBytes[$i] -bor (0xFF -bxor $maskBytes[$i]))
    }

    return [pscustomobject]@{
        Alias     = $addr.InterfaceAlias
        Ip        = $addr.IPAddress
        Prefix    = $addr.PrefixLength
        Broadcast = ([Net.IPAddress]$bcastBytes).ToString()
    }
}

function New-MagicPacket {
    param([string]$Mac)
    $clean = ($Mac -replace '[^0-9A-Fa-f]', '')
    if ($clean.Length -ne 12) { throw "invalid MAC address: $Mac" }
    $macBytes = New-Object byte[] 6
    for ($i = 0; $i -lt 6; $i++) {
        $macBytes[$i] = [Convert]::ToByte($clean.Substring($i * 2, 2), 16)
    }
    # 6 x 0xFF, then the MAC repeated 16 times = 102 bytes.
    $pkt = New-Object byte[] 102
    for ($i = 0; $i -lt 6; $i++) { $pkt[$i] = 0xFF }
    for ($r = 0; $r -lt 16; $r++) { [Array]::Copy($macBytes, 0, $pkt, 6 + ($r * 6), 6) }
    return ,$pkt
}

function Send-MagicPacket {
    param([string]$Mac, [string]$FromIp, [string]$BroadcastIp)

    $packet  = New-MagicPacket -Mac $Mac
    $results = @()

    # Directed subnet broadcast first: it is the one that survives a router that
    # drops 255.255.255.255. Ports 9 and 7 are both conventional for WOL.
    $targets = @(
        @{ Addr = $BroadcastIp;      Port = 9 },
        @{ Addr = $BroadcastIp;      Port = 7 },
        @{ Addr = '255.255.255.255'; Port = 9 },
        @{ Addr = '255.255.255.255'; Port = 7 }
    )

    foreach ($t in $targets) {
        $udp = $null
        try {
            # Bind the socket to the LAN IP so the broadcast provably leaves the
            # correct NIC rather than a VPN or hypervisor adapter.
            $ep  = New-Object System.Net.IPEndPoint([Net.IPAddress]::Parse($FromIp), 0)
            $udp = New-Object System.Net.Sockets.UdpClient($ep)
            $udp.EnableBroadcast = $true
            $sent = $udp.Send($packet, $packet.Length, $t.Addr, $t.Port)
            $results += [pscustomobject]@{
                dest = "$($t.Addr):$($t.Port)"; sent = $sent; ok = ($sent -eq 102)
            }
        } catch {
            $results += [pscustomobject]@{
                dest = "$($t.Addr):$($t.Port)"; sent = 0; ok = $false; error = $_.Exception.Message
            }
        } finally {
            if ($udp) { $udp.Close() }
        }
    }
    return $results
}

function Get-TargetState {
    param([string]$Ip, [int[]]$TcpProbePorts = @(445, 3389))

    # ---- signal 1: ICMP. A reply is proof the OS is running. ----
    $awake = $false
    $rttMs = $null
    $via   = $null
    try {
        $ping  = New-Object System.Net.NetworkInformation.Ping
        $reply = $ping.Send($Ip, 1500)
        if ($reply.Status -eq 'Success') {
            $awake = $true; $rttMs = $reply.RoundtripTime; $via = 'icmp'
        }
    } catch { }

    # ---- signal 2: TCP connect, only if ICMP was silent. ----
    # Windows Firewall commonly drops inbound ICMP echo while still accepting
    # SMB/RDP, so ICMP-only detection reports a running PC as asleep. A connect
    # attempt sends no payload and changes nothing on the target: read-only.
    if (-not $awake) {
        foreach ($p in $TcpProbePorts) {
            $client = $null
            try {
                $client = New-Object System.Net.Sockets.TcpClient
                if ($client.ConnectAsync($Ip, $p).Wait(1200)) {
                    $awake = $true; $via = "tcp/$p"
                }
            } catch { } finally { if ($client) { $client.Close() } }
            if ($awake) { break }
        }
    }

    # ---- signal 3: a FRESH ARP resolution. ----
    # A cached entry is worthless here: Windows keeps Stale neighbours for
    # minutes after a machine dies, so reading the cache reports a powered-off
    # PC as merely asleep. Evict the entry, force re-resolution, then read it.
    # Evicting touches only THIS host's neighbour cache, never the target.
    $arpFresh = $false
    try {
        Remove-NetNeighbor -IPAddress $Ip -AddressFamily IPv4 -Confirm:$false -ErrorAction Stop
        $arpFresh = $true
    } catch {
        # Not elevated, or no entry to evict. Fall back to the cached read and
        # downgrade confidence accordingly.
    }
    if ($arpFresh) {
        $t = $null
        try {
            $t = New-Object System.Net.Sockets.TcpClient
            $null = $t.ConnectAsync($Ip, 9).Wait(900)   # forces an ARP request
        } catch { } finally { if ($t) { $t.Close() } }
        Start-Sleep -Milliseconds 400
    }

    $arpMac   = $null
    $arpState = 'none'
    try {
        $n = Get-NetNeighbor -IPAddress $Ip -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.LinkLayerAddress } |
             Select-Object -First 1
        if ($n) { $arpMac = $n.LinkLayerAddress; $arpState = "$($n.State)" }
    } catch { }

    $arpAnswered = [bool]$arpMac -and $arpState -notin @('Unreachable', 'Incomplete')

    # ---- inference ----
    # HONEST LIMIT: "asleep" and "off" cannot be separated with certainty from
    # another host. A NIC armed for Wake-on-LAN keeps PHY link and ARP-offload
    # alive on standby power, and it does that in S5 (full shutdown) just as it
    # does in S3/S4. So an ARP answer proves the NIC has standby power and is
    # listening -- it does NOT prove the machine is merely asleep. Since this
    # target is expected to wake from FULL SHUTDOWN, that ambiguity is the norm
    # here, not an edge case. What the ARP answer genuinely tells us is whether
    # the machine is WAKEABLE, which is the operationally useful bit.
    $state      = 'unknown'
    $confidence = 'low'
    $wakeable   = $null
    $caveat     = $null

    if ($awake) {
        $state = 'awake'; $confidence = 'high'; $wakeable = $false
        $caveat = "confirmed responding via $via"
    }
    elseif ($arpAnswered) {
        $state = 'asleep_or_hibernated'; $confidence = 'low'; $wakeable = $true
        $caveat = 'NIC answered ARP, so it has standby power and is armed. ' +
                  'Cannot distinguish S3/S4 sleep from S5 shutdown-with-WOL-armed ' +
                  'from here; both look identical on the wire. Treat as wakeable.'
    }
    else {
        $state = 'off'; $confidence = 'low'; $wakeable = $false
        $caveat = 'No ICMP, no TCP, no ARP answer. Usually means fully powered ' +
                  'down with the NIC unarmed, unplugged, or on another subnet. ' +
                  'A wake attempt may still succeed if the NIC is armed but ' +
                  'silently drops ARP in S5, which some firmware does.'
        if (-not $arpFresh) {
            $confidence = 'very_low'
            $caveat += ' NOTE: ARP cache could not be evicted (needs admin), so this reading may be stale.'
        }
    }

    return [pscustomobject]@{
        ip = $Ip; awake = $awake; rtt_ms = $rttMs; detected_via = $via
        arp_mac = $arpMac; arp_state = $arpState
        arp_forced_refresh = $arpFresh
        inferred = $state; confidence = $confidence; wakeable = $wakeable
        caveat = $caveat
    }
}

# ------------------------------------------------------------------ start ---

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null

Write-Log INFO "wol-helper $($script:Version) starting (pid $PID)"

$token = Get-Token
if (-not $token) {
    Write-Log FATAL "no token configured. Set `$env:JARVIS_WOL_TOKEN or add JARVIS_WOL_TOKEN=<value> to $TokenFile. Refusing to start."
    exit 1
}
if ($token.Length -lt 16) {
    Write-Log FATAL "token is only $($token.Length) chars; refusing to start with a weak token (want >= 16)."
    exit 1
}

if (-not $SourceIp -or -not $Broadcast) {
    # At boot the task can fire before DHCP has produced a default route, so
    # retry instead of dying. The scheduled task also delays 30s for this.
    $lan = $null
    for ($attempt = 1; $attempt -le 20; $attempt++) {
        try { $lan = Resolve-LanInterface; break }
        catch {
            Write-Log WARN "LAN detect attempt $attempt/20 failed: $($_.Exception.Message)"
            Start-Sleep -Seconds 6
        }
    }
    if (-not $lan) {
        Write-Log FATAL 'no usable LAN adapter after 20 attempts (~2min). Refusing to start.'
        exit 1
    }
    if (-not $SourceIp)  { $SourceIp  = $lan.Ip }
    if (-not $Broadcast) { $Broadcast = $lan.Broadcast }
    Write-Log INFO "LAN adapter '$($lan.Alias)' ip=$($lan.Ip)/$($lan.Prefix) broadcast=$($lan.Broadcast)"
}
Write-Log INFO "target mac=$TargetMac ip=$TargetIp  source=$SourceIp broadcast=$Broadcast"

$listener = New-Object System.Net.HttpListener
foreach ($h in $BindHosts) { $listener.Prefixes.Add("http://${h}:$Port/") }
try {
    $listener.Start()
} catch {
    Write-Log FATAL "could not bind $($BindHosts -join ',') port ${Port}: $($_.Exception.Message)"
    exit 1
}
Write-Log INFO ("listening on " + (($listener.Prefixes) -join ' '))

function Send-Json {
    param($Context, [int]$Status, $Body)
    $json  = ($Body | ConvertTo-Json -Depth 6 -Compress)
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $Context.Response.StatusCode  = $Status
    $Context.Response.ContentType = 'application/json'
    $Context.Response.Headers.Add('Cache-Control', 'no-store')
    $Context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $Context.Response.Close()
    return $json
}

$lastWake = [DateTime]::MinValue

try {
    while ($listener.IsListening) {
        $task = $listener.GetContextAsync()
        while (-not $task.AsyncWaitHandle.WaitOne(1000)) {
            if (-not $listener.IsListening) { break }
        }
        if (-not $task.IsCompleted) { continue }

        $ctx    = $task.Result
        $req    = $ctx.Request
        $path   = $req.Url.AbsolutePath.TrimEnd('/').ToLower()
        if ($path -eq '') { $path = '/' }
        $method = $req.HttpMethod
        $remote = $req.RemoteEndPoint.Address.ToString()

        try {
            # ---- /health : unauthenticated, discloses nothing about the PC ----
            if ($path -eq '/health' -and $method -eq 'GET') {
                $null = Send-Json $ctx 200 @{
                    status  = 'ok'
                    service = 'jarvis-wol-helper'
                    version = $script:Version
                    uptime_seconds = [int]((Get-Date) - $script:StartedAt).TotalSeconds
                }
                Write-Log INFO "GET /health from $remote -> 200"
                continue
            }

            # ---- everything below requires the bearer token ----
            $auth      = $req.Headers['Authorization']
            $presented = $null
            if ($auth -and $auth -match '^(?i)bearer\s+(.+)$') { $presented = $Matches[1].Trim() }

            if (-not (Test-TokenEqual -Presented $presented -Expected $token)) {
                # NOTE: do not try to send a WWW-Authenticate header here.
                # HttpListener rejects it ("must be modified using the
                # appropriate property or method") and the whole request 500s.
                $null = Send-Json $ctx 401 @{ error = 'unauthorized' }
                Write-Log WARN "$method $path from $remote -> 401 unauthorized"
                continue
            }

            switch -Regex ("$method $path") {

                '^GET /status$' {
                    $st  = Get-TargetState -Ip $TargetIp
                    $out = Send-Json $ctx 200 @{
                        ip = $st.ip; awake = $st.awake; state = $st.inferred
                        confidence = $st.confidence; wakeable = $st.wakeable
                        caveat = $st.caveat
                        rtt_ms = $st.rtt_ms; detected_via = $st.detected_via
                        arp_mac = $st.arp_mac; arp_state = $st.arp_state
                        arp_forced_refresh = $st.arp_forced_refresh
                        checked_at = (Get-Date -Format 'o')
                    }
                    Write-Log INFO "GET /status from $remote -> 200 state=$($st.inferred) confidence=$($st.confidence)"
                    break
                }

                # POST is the correct verb, but GET is accepted as an alias.
                # Reason: http.sys rejects a POST that carries neither a body
                # nor Content-Length with "411 Length Required" BEFORE the
                # request reaches this code, and some clients (including a
                # bare HA rest_command with no payload) send exactly that.
                # A GET alias means a misconfigured caller still wakes the PC
                # instead of failing with an error it cannot see.
                '^(POST|GET) /wake$' {
                    # Cheap flood guard. HA retries and a stuck automation should
                    # not machine-gun the LAN with broadcasts.
                    $since = ((Get-Date) - $lastWake).TotalSeconds
                    if ($since -lt 3) {
                        $null = Send-Json $ctx 429 @{ error = 'rate_limited'; retry_after_seconds = [int](3 - $since) }
                        Write-Log WARN "$method /wake from $remote -> 429 (last wake ${since}s ago)"
                        break
                    }
                    $lastWake = Get-Date

                    $before = Get-TargetState -Ip $TargetIp
                    $sends  = Send-MagicPacket -Mac $TargetMac -FromIp $SourceIp -BroadcastIp $Broadcast
                    $okCount = @($sends | Where-Object { $_.ok }).Count

                    $out = Send-Json $ctx 200 @{
                        sent = ($okCount -gt 0)
                        packets_sent = $okCount
                        mac = $TargetMac
                        source_ip = $SourceIp
                        broadcast = $Broadcast
                        state_before = $before.inferred
                        # Wake is fire-and-forget: the NIC never acknowledges a
                        # magic packet. Poll /status to learn whether it worked.
                        note = 'magic packet is unacknowledged; poll /status to confirm'
                        detail = $sends
                        sent_at = (Get-Date -Format 'o')
                    }
                    Write-Log INFO "$method /wake from $remote -> 200 packets=$okCount before=$($before.inferred)"
                    break
                }

                default {
                    $null = Send-Json $ctx 404 @{ error = 'not_found'; path = $path
                        endpoints = @('GET /health', 'GET /status', 'POST /wake') }
                    Write-Log WARN "$method $path from $remote -> 404"
                }
            }
        } catch {
            Write-Log ERROR "$method $path from $remote -> 500 $($_.Exception.Message)"
            try { $null = Send-Json $ctx 500 @{ error = 'internal'; detail = $_.Exception.Message } } catch { }
        }
    }
} finally {
    Write-Log INFO 'shutting down'
    if ($listener.IsListening) { $listener.Stop() }
    $listener.Close()
}
