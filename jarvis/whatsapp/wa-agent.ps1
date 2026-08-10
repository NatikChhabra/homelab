# wa-agent.ps1 - authenticated local HTTP front-end for wa_uia.ps1.
#
# WHY THIS EXISTS
# JARVIS (Home Assistant, in Docker on the server laptop) needs WhatsApp, but
# the WhatsApp session lives in Natik's desktop session on the main PC. UI
# automation MUST run there, in session 1, as him. So HA calls this agent over
# the LAN/tailnet and the agent drives the local UI.
#
# HOW THIS DIFFERS FROM host-helper/wol-helper.ps1  -- read before "fixing" the bind
# The WOL helper binds LOOPBACK ONLY, because it runs on the same host as
# Docker and Docker Desktop forwards container->host traffic onto the host's
# loopback. That property does NOT apply here: this agent runs on a DIFFERENT
# machine (192.168.29.167) from the caller (192.168.29.108), so loopback is
# unreachable and a real network bind is unavoidable. That is a genuinely
# larger exposure than the WOL helper has, and it is compensated for by:
#   1. Preferring the Tailscale interface when present. Tailscale traffic is
#      WireGuard-encrypted, which also fixes the plaintext problem below.
#   2. A mandatory bearer token, fail-closed, constant-time compared.
#   3. A source-IP allowlist checked before authentication.
#   4. Binding one explicit address, never "+" and never 0.0.0.0.
#
# PLAINTEXT WARNING, and it is a real one:
# On a plain LAN bind this is HTTP, so "read" responses carry WhatsApp message
# bodies in cleartext across the network. Anyone with the Wi-Fi password could
# read them. Over Tailscale that is not true. This is the single strongest
# argument for putting the main PC on the tailnet, and it is why -RequireTailscale
# exists.
#
# CONCURRENCY
# UI automation is not reentrant: two overlapping requests would interleave
# keystrokes into the same window and could deliver half of one message to the
# wrong chat. Every request that touches the UI takes a global mutex; a caller
# that loses the race gets 409 busy and never a silent partial action.
#
# RATE LIMITING
# By construction, not by policy: one recipient per call, a minimum gap between
# sends, and an hourly ceiling. This is what keeps a confused 3B model looping a
# tool call from turning into a broadcast.

[CmdletBinding()]
param(
    [int]    $Port = 8098,

    # Empty = auto-select: Tailscale address if present, else the LAN adapter
    # that owns the default route. Never binds "+".
    [string] $BindAddress = '',

    [switch] $RequireTailscale,

    # CIDRs / addresses permitted to talk to this agent at all. Default covers
    # the tailnet CGNAT range plus the server laptop's LAN address.
    [string[]] $AllowFrom = @('100.64.0.0/10', '192.168.29.108/32', '127.0.0.1/32'),

    [string] $TokenFile = 'C:\ServerData\Stacks\jarvis\secrets.env',
    [string] $TokenKey  = 'JARVIS_WHATSAPP_TOKEN',

    # Left empty and resolved in the body. MEASURED: $PSScriptRoot is EMPTY
    # while param defaults are bound under `powershell.exe -File`, so a default
    # of "$PSScriptRoot\wa_uia.ps1" becomes the literal "\wa_uia.ps1" and the
    # agent dies with "automation script missing at \wa_uia.ps1".
    [string] $UiaScript = '',
    [string] $LogFile   = '',

    # Send pacing. Human-paced by design.
    [int] $MinSendIntervalSeconds = 5,
    [int] $MaxSendsPerHour = 20,

    # Hard ceiling on one UI operation before the child is killed.
    [int] $OperationTimeoutSeconds = 45,

    [switch] $AllowedContactsFile,

    # Start the HTTP layer even outside an interactive desktop session. FOR
    # TESTING THE TRANSPORT ONLY. This weakens nothing: wa_uia.ps1 performs its
    # own independent session check and will still refuse every UI action with
    # no_interactive_session. Two guards, and this bypasses only the outer one.
    [switch] $SkipSessionCheck
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

# Resolve script-relative paths HERE, not in the param block. See the note on
# $UiaScript: $PSScriptRoot is not populated during parameter binding under
# `powershell.exe -File`, but it is correct by the time the body runs.
$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Definition }
if (-not $UiaScript) { $UiaScript = Join-Path $here 'wa_uia.ps1' }
if (-not $LogFile)   { $LogFile   = Join-Path $here 'wa-agent.log' }

# Normalise the allowlist. MEASURED: `powershell.exe -File script.ps1 -AllowFrom
# a,b,c` binds the WHOLE string "a,b,c" as ONE element, and space-separated
# values bind only the first. The scheduled task launches this with -File, so
# without this split the allowlist would be one malformed entry, [int] would
# throw on the bogus prefix length, and every request would 500. Splitting here
# makes both a real array and a comma-joined string work.
$AllowFrom = @($AllowFrom |
    ForEach-Object { $_ -split ',' } |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ })

$script:Version = '1.0.0'
$script:StartedAt = Get-Date
$script:SendTimes = New-Object System.Collections.ArrayList
$script:LastSend = [DateTime]::MinValue
# Local\ not Global\: creating a Global\ named object needs SeCreateGlobalPrivilege,
# which a normal non-elevated user process does not hold, and this agent runs
# non-elevated on purpose. Local\ is per-session, which is exactly the right
# scope anyway since UI automation is confined to one desktop session.
$script:Mutex = New-Object System.Threading.Mutex($false, 'Local\JarvisWhatsAppAgent')

# ----------------------------------------------------------------- logging ---

function Write-Log {
    param([string]$Level, [string]$Message)
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    # Write-Host, never Write-Output. See the note in wol-helper.ps1: a logging
    # function that uses Write-Output returns its log lines as part of its value.
    Write-Host $line
    try {
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 2MB)) {
            Move-Item $LogFile "$LogFile.1" -Force
        }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }
}

# ------------------------------------------------------------------ token ---

function Get-Token {
    $envVal = [Environment]::GetEnvironmentVariable($TokenKey)
    if ($envVal) {
        Write-Log INFO "token source: environment variable $TokenKey"
        return $envVal
    }
    if (Test-Path $TokenFile) {
        foreach ($raw in (Get-Content $TokenFile -Encoding UTF8)) {
            $line = $raw.Trim()
            if ($line -match '^\s*#' -or $line -eq '') { continue }
            $kv = $line -split '=', 2
            if ($kv.Length -eq 2 -and $kv[0].Trim() -eq $TokenKey) {
                $val = $kv[1].Trim().Trim('"').Trim("'")
                if ($val) {
                    Write-Log INFO "token source: $TokenFile (key $TokenKey)"
                    return $val
                }
            }
        }
    }
    return $null
}

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

# --------------------------------------------------------------- allowlist ---

function Test-IpAllowed {
    param([string]$Ip)
    if (-not $AllowFrom -or $AllowFrom.Count -eq 0) { return $true }
    $addr = $null
    if (-not [Net.IPAddress]::TryParse($Ip, [ref]$addr)) { return $false }
    # IPv4-mapped IPv6 (::ffff:192.168.29.108) must compare as IPv4.
    if ($addr.IsIPv4MappedToIPv6) { $addr = $addr.MapToIPv4() }
    if ($addr.AddressFamily -ne 'InterNetwork') { return $false }
    $ipInt = [BitConverter]::ToUInt32(($addr.GetAddressBytes()[3..0]), 0)

    foreach ($cidr in $AllowFrom) {
        $parts = $cidr -split '/'
        $netAddr = $null
        if (-not [Net.IPAddress]::TryParse($parts[0], [ref]$netAddr)) { continue }
        if ($netAddr.AddressFamily -ne 'InterNetwork') { continue }
        # Parse the prefix length defensively. A malformed entry must be SKIPPED
        # (fail closed for that rule), never allowed to throw: an exception here
        # would surface as a 500 on every single request and would be very hard
        # to trace back to one bad allowlist string.
        $bits = 32
        if ($parts.Length -ge 2) {
            $parsed = 0
            if (-not [int]::TryParse($parts[1], [ref]$parsed)) {
                Write-Log WARN "allowlist entry '$cidr' has an unparseable prefix length; ignoring it."
                continue
            }
            $bits = $parsed
        }
        if ($bits -lt 0 -or $bits -gt 32) {
            Write-Log WARN "allowlist entry '$cidr' has an out-of-range prefix length; ignoring it."
            continue
        }
        $netInt = [BitConverter]::ToUInt32(($netAddr.GetAddressBytes()[3..0]), 0)
        if ($bits -eq 0) { return $true }
        # Build the mask from an Int64 literal (the L suffix is required).
        # MEASURED on PS 5.1.26100: [uint64]0xFFFFFFFF fails, because the hex
        # literal is parsed as Int32 -1 FIRST and the cast to an unsigned type
        # then throws "Value was either too large or too small". 0xFFFFFFFFL is
        # parsed as Int64 and shifts correctly. Verified for /8 /10 /24 /32.
        $mask = [uint32]((0xFFFFFFFFL -shl (32 - $bits)) -band 0xFFFFFFFFL)
        if (($ipInt -band $mask) -eq ($netInt -band $mask)) { return $true }
    }
    return $false
}

# ------------------------------------------------------------------- bind ---

function Resolve-BindAddress {
    $ts = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
          Where-Object { $_.IPAddress -like '100.*' -and $_.InterfaceAlias -match 'Tailscale' } |
          Select-Object -First 1
    if ($ts) {
        Write-Log INFO "bind target: Tailscale $($ts.IPAddress) (traffic is WireGuard-encrypted)"
        return [pscustomobject]@{ Ip = $ts.IPAddress; Kind = 'tailscale' }
    }
    if ($RequireTailscale) {
        throw 'RequireTailscale was set but no Tailscale IPv4 address was found. Refusing to fall back to a cleartext LAN bind.'
    }
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
             Sort-Object RouteMetric | Select-Object -First 1
    if (-not $route) { throw 'no default route found; cannot pick a bind address' }
    $addr = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1
    if (-not $addr) { throw "no usable IPv4 on interface $($route.InterfaceIndex)" }
    Write-Log WARN "bind target: LAN $($addr.IPAddress) - responses are PLAINTEXT HTTP on the LAN. Message bodies from 'read' are readable by anyone on this network. Prefer Tailscale."
    return [pscustomobject]@{ Ip = $addr.IPAddress; Kind = 'lan' }
}

# -------------------------------------------------------------- invoke uia ---

function Invoke-UiaScript {
    param(
        [string] $UiaAction,
        [string] $Contact = '',
        [string] $Message = '',
        [int]    $Count = 10
    )
    # Run as a child process rather than dot-sourcing, for two reasons:
    #  1. A wedged Chromium UIA call can be killed on a timeout instead of
    #     permanently hanging the agent.
    #  2. Each request starts from clean state, so no leaked focus or stale
    #     element reference from a previous call can affect the next one.
    $args = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass', '-NonInteractive',
        '-File', $UiaScript,
        '-Action', $UiaAction,
        '-Count', $Count
    )
    if ($Contact) { $args += @('-Contact', $Contact) }
    # PRIVACY: never put the body on the command line; it would be world-visible
    # via Win32_Process.CommandLine. The switch tells the child to read stdin.
    if ($Message) { $args += '-MessageFromStdin' }

    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = (Get-Command powershell.exe).Source
    $psi.Arguments = ($args | ForEach-Object {
        if ($_ -is [string] -and $_ -match '\s') { '"' + $_ + '"' } else { "$_" }
    }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $psi.CreateNoWindow = $true

    $proc = [System.Diagnostics.Process]::Start($psi)
    if ($Message) {
        $proc.StandardInput.Write($Message)
    }
    $proc.StandardInput.Close()

    $stdout = $proc.StandardOutput.ReadToEndAsync()
    $stderr = $proc.StandardError.ReadToEndAsync()

    if (-not $proc.WaitForExit($OperationTimeoutSeconds * 1000)) {
        try { $proc.Kill() } catch { }
        return @{ ok = $false; error = 'operation_timeout'
                  detail = "The UI operation exceeded ${OperationTimeoutSeconds}s and was killed. WhatsApp may be slow to build its accessibility tree, or a dialog is blocking it. Whether a send completed is UNKNOWN - check WhatsApp before retrying." }
    }

    $out = $stdout.Result
    $err = $stderr.Result
    if ($err) { Write-Log WARN "uia stderr: $($err.Trim())" }

    # The script prints exactly one JSON document. Anything else means it died
    # before reaching Out-Result, and we must not invent a success.
    $jsonLine = ($out -split "`n" | Where-Object { $_.Trim().StartsWith('{') } | Select-Object -Last 1)
    if (-not $jsonLine) {
        return @{ ok = $false; error = 'uia_no_output'
                  detail = "wa_uia.ps1 produced no JSON (exit $($proc.ExitCode)). stderr: $($err.Trim())" }
    }
    try {
        $parsed = $jsonLine | ConvertFrom-Json
    } catch {
        return @{ ok = $false; error = 'uia_bad_output'; detail = "unparseable JSON from wa_uia.ps1: $($_.Exception.Message)" }
    }
    return $parsed
}

# ---------------------------------------------------------------- throttle ---

function Test-SendThrottle {
    $now = Get-Date
    $since = ($now - $script:LastSend).TotalSeconds
    if ($since -lt $MinSendIntervalSeconds) {
        return @{ ok = $false; error = 'rate_limited'
                  detail = "Minimum $MinSendIntervalSeconds s between sends; last was $([int]$since) s ago."
                  retry_after_seconds = [int]($MinSendIntervalSeconds - $since) }
    }
    $cut = $now.AddHours(-1)
    $recent = @($script:SendTimes | Where-Object { $_ -gt $cut })
    $script:SendTimes.Clear()
    foreach ($t in $recent) { [void]$script:SendTimes.Add($t) }
    if ($recent.Count -ge $MaxSendsPerHour) {
        return @{ ok = $false; error = 'hourly_limit_reached'
                  detail = "$MaxSendsPerHour sends in the last hour is the ceiling. This limit exists so a looping tool call cannot become a broadcast."
                  retry_after_seconds = 300 }
    }
    return @{ ok = $true }
}

# ------------------------------------------------------------------- start ---

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
Write-Log INFO "wa-agent $($script:Version) starting (pid $PID)"

$sid = (Get-Process -Id $PID).SessionId
if ($sid -eq 0 -and -not $SkipSessionCheck) {
    Write-Log FATAL 'Running in session 0. UI automation cannot work from session 0: it has no visible desktop, UIA enumerates zero windows and keystrokes reach nothing. This agent must run as the logged-in user in the interactive session. Refusing to start and pretend otherwise. (Use -SkipSessionCheck to test the HTTP layer only.)'
    exit 1
}
if ($sid -eq 0) {
    Write-Log WARN 'session 0 with -SkipSessionCheck: HTTP layer only. Every UI action will fail with no_interactive_session.'
}
Write-Log INFO "session $sid, user $env:USERNAME"

if (-not (Test-Path $UiaScript)) {
    Write-Log FATAL "automation script missing at $UiaScript"
    exit 1
}

$token = Get-Token
if (-not $token) {
    Write-Log FATAL "no token configured. Set `$env:$TokenKey or add $TokenKey=<value> to $TokenFile. Refusing to start."
    exit 1
}
if ($token.Length -lt 16) {
    Write-Log FATAL "token is only $($token.Length) chars; refusing to start with a weak token (want >= 16)."
    exit 1
}

if (-not $BindAddress) {
    try { $bind = Resolve-BindAddress; $BindAddress = $bind.Ip }
    catch { Write-Log FATAL $_.Exception.Message; exit 1 }
}
Write-Log INFO "allowlist: $($AllowFrom -join ', ')"

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://${BindAddress}:$Port/")
try {
    $listener.Start()
} catch {
    Write-Log FATAL "could not bind ${BindAddress}:${Port}: $($_.Exception.Message). A non-loopback bind needs a urlacl reservation; install-agent.ps1 adds one."
    exit 1
}
Write-Log INFO "listening on http://${BindAddress}:$Port/"

function Send-Json {
    param($Context, [int]$Status, $Body)
    $json = ($Body | ConvertTo-Json -Depth 10 -Compress)
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $Context.Response.StatusCode = $Status
    $Context.Response.ContentType = 'application/json'
    $Context.Response.Headers.Add('Cache-Control', 'no-store')
    $Context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $Context.Response.Close()
}

try {
    while ($listener.IsListening) {
        $task = $listener.GetContextAsync()
        while (-not $task.AsyncWaitHandle.WaitOne(1000)) {
            if (-not $listener.IsListening) { break }
        }
        if (-not $task.IsCompleted) { continue }

        $ctx = $task.Result
        $req = $ctx.Request
        $path = $req.Url.AbsolutePath.TrimEnd('/').ToLower()
        if ($path -eq '') { $path = '/' }
        $method = $req.HttpMethod
        $remote = $req.RemoteEndPoint.Address.ToString()

        try {
            # ---- source allowlist, checked before anything else ----
            if (-not (Test-IpAllowed -Ip $remote)) {
                Send-Json $ctx 403 @{ ok = $false; error = 'forbidden_source'
                                      detail = 'Your address is not in this agent''s allowlist.' }
                Write-Log WARN "$method $path from $remote -> 403 source not allowed"
                continue
            }

            # ---- /health : unauthenticated, discloses nothing about WhatsApp ----
            # Deliberately says nothing about contacts, messages or login state.
            if ($path -eq '/health' -and $method -eq 'GET') {
                Send-Json $ctx 200 @{ ok = $true; service = 'jarvis-whatsapp-agent'
                    version = $script:Version
                    uptime_seconds = [int]((Get-Date) - $script:StartedAt).TotalSeconds }
                Write-Log INFO "GET /health from $remote -> 200"
                continue
            }

            # ---- bearer token required below this line ----
            $auth = $req.Headers['Authorization']
            $presented = $null
            if ($auth -and $auth -match '^(?i)bearer\s+(.+)$') { $presented = $Matches[1].Trim() }
            if (-not (Test-TokenEqual -Presented $presented -Expected $token)) {
                # Do NOT add a WWW-Authenticate header: HttpListener rejects it
                # and turns the whole response into a 500. wol-helper hit this.
                Send-Json $ctx 401 @{ ok = $false; error = 'unauthorized' }
                Write-Log WARN "$method $path from $remote -> 401"
                continue
            }

            # ---- parse parameters from query string and/or JSON body ----
            # BOTH are accepted, and GET is an alias for POST everywhere, because
            # http.sys answers a body-less POST with "411 Length Required" before
            # this script ever sees the request. A body-less HA rest_command
            # would otherwise fail with an error nobody can see.
            $q = $req.QueryString
            $p = @{ action = $q['action']; contact = $q['contact']
                    message = $q['message']; count = $q['count'] }
            if ($req.HasEntityBody) {
                $reader = New-Object IO.StreamReader($req.InputStream, $req.ContentEncoding)
                $raw = $reader.ReadToEnd(); $reader.Close()
                if ($raw -and $raw.Trim().StartsWith('{')) {
                    try {
                        $b = $raw | ConvertFrom-Json
                        foreach ($k in @('action', 'contact', 'message', 'count')) {
                            if ($b.PSObject.Properties.Name -contains $k -and $b.$k) { $p[$k] = [string]$b.$k }
                        }
                    } catch {
                        Send-Json $ctx 400 @{ ok = $false; error = 'bad_json'; detail = $_.Exception.Message }
                        Write-Log WARN "$method $path from $remote -> 400 bad json"
                        continue
                    }
                }
            }

            if ($path -eq '/whatsapp' -or $path -eq '/') {
                $act = if ($p.action) { $p.action.ToLower().Trim() } else { '' }
                if ($act -notin @('send', 'read', 'preview', 'status')) {
                    Send-Json $ctx 400 @{ ok = $false; error = 'bad_action'
                        detail = "action must be one of send|read|preview|status; got '$act'"
                        allowed = @('send', 'read', 'preview', 'status') }
                    Write-Log WARN "$method $path from $remote -> 400 bad action '$act'"
                    continue
                }

                $count = 10
                if ($p.count) { [void][int]::TryParse($p.count, [ref]$count) }

                # ---- serialise: UI automation is not reentrant ----
                if (-not $script:Mutex.WaitOne(0)) {
                    Send-Json $ctx 409 @{ ok = $false; error = 'busy'
                        detail = 'Another WhatsApp UI operation is in progress. Overlapping operations would interleave keystrokes into the same window, so this request was refused rather than risked.' }
                    Write-Log WARN "$method $path from $remote -> 409 busy"
                    continue
                }
                try {
                    if ($act -eq 'send') {
                        $thr = Test-SendThrottle
                        if (-not $thr.ok) {
                            Send-Json $ctx 429 $thr
                            Write-Log WARN "send from $remote -> 429 $($thr.error)"
                            continue
                        }
                        if (-not $p.message) {
                            Send-Json $ctx 400 @{ ok = $false; error = 'message_required'
                                                  detail = 'action=send needs a message.' }
                            Write-Log WARN "send from $remote -> 400 no message"
                            continue
                        }
                    }

                    Write-Log INFO "$act from $remote contact='$($p.contact)'$(if($p.message){" msgchars=$($p.message.Length)"})"
                    $result = Invoke-UiaScript -UiaAction $act -Contact $p.contact `
                                               -Message $p.message -Count $count

                    $isOk = $false
                    try { $isOk = [bool]$result.ok } catch { }
                    $rerr = ''
                    try { if ($result.error) { $rerr = [string]$result.error } } catch { }
                    if ($act -eq 'send' -and $isOk) {
                        $script:LastSend = Get-Date
                        [void]$script:SendTimes.Add($script:LastSend)
                    }
                    # MEASURED: PowerShell 5.1 has no inline if-EXPRESSION, so
                    # `Send-Json $ctx (if (...) {200} else {502})` fails at
                    # runtime with "The term 'if' is not recognized". Assign to
                    # a variable first. (It works inside "$( )" in a string,
                    # which is why the log line below is fine.)
                    $status = 502
                    if ($isOk) { $status = 200 }
                    Send-Json $ctx $status $result
                    Write-Log INFO "$act from $remote -> $(if($isOk){'200 ok'}else{"502 $rerr"})"
                } finally {
                    $script:Mutex.ReleaseMutex()
                }
                continue
            }

            Send-Json $ctx 404 @{ ok = $false; error = 'not_found'; path = $path
                endpoints = @('GET /health', 'GET|POST /whatsapp?action=send|read|preview|status') }
            Write-Log WARN "$method $path from $remote -> 404"

        } catch {
            Write-Log ERROR "$method $path from $remote -> 500 $($_.Exception.Message)"
            try { Send-Json $ctx 500 @{ ok = $false; error = 'internal'; detail = $_.Exception.Message } } catch { }
        }
    }
} finally {
    Write-Log INFO 'shutting down'
    if ($listener.IsListening) { $listener.Stop() }
    $listener.Close()
    try { $script:Mutex.Dispose() } catch { }
}


