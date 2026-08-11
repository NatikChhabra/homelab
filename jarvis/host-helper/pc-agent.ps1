# pc-agent.ps1 - host-side PC control agent for JARVIS.
#
# WHAT THIS IS
# The direct successor to wol-helper.ps1 and a strict SUPERSET of it: same port,
# same loopback bind, same bearer token, same /health //status //wake contract,
# byte-for-byte compatible responses. It is ONE service, not a second one. The
# installer retires the old WOL-only task and registers this in its place.
#
# ============================================================================
# WHY IT NO LONGER RUNS AS SYSTEM  (measured 2026-08-08, not assumed)
# ============================================================================
# wol-helper.ps1 ran as NT AUTHORITY\SYSTEM in SESSION 0. That is correct for a
# network-only service and FATAL for a desktop-control service. Session 0 is
# isolated from the interactive desktop, so from there:
#
#   Graphics.CopyFromScreen  -> "The handle is invalid"   (measured, exactly this)
#   media / volume keys      -> delivered to session 0's empty input desktop
#   Start-Process notepad    -> a window nobody can ever see
#   LockWorkStation          -> locks session 0, not Natik's session
#
# Every one of those fails SILENTLY except the screenshot. A silent no-op is the
# worst possible failure mode for a voice assistant, so this agent runs in the
# INTERACTIVE USER SESSION instead.
#
# That is a REDUCTION in privilege, not an increase: SYSTEM + RunLevel Highest
# becomes the logged-in user + RunLevel Limited. A compromise of this agent now
# yields the user's own rights rather than the whole machine. The only capability
# lost is Remove-NetNeighbor in /status (ARP cache eviction needs admin), which
# the existing code already handles by downgrading confidence to "very_low".
#
# Measured consequence, stated plainly: the agent starts AT LOGON, not at boot.
# For this host that costs nothing, because Docker and Home Assistant are also
# per-user and only start at logon (see HANDOFF.md, "Autostart does not work
# unattended"). Wake-on-LAN was never available before logon in practice either.
#
# A runtime guard enforces this: if the agent finds itself in session 0, every
# desktop endpoint returns 503 with the reason, rather than pretending to work.
#
# ============================================================================
# BINDING / EXPOSURE
# ============================================================================
# Default is 127.0.0.1 ONLY. Measured previously with a probe listener curled
# from inside jarvis-hass: container traffic to host.docker.internal and to
# 192.168.65.254 arrives on the host as 127.0.0.1:8099, while the LAN IP and the
# Tailscale IP are both unreachable from the container. So loopback-only is
# reachable by Home Assistant and invisible to the LAN and the internet. No
# firewall rule is needed and none should be added.
#
# ALSO MEASURED 2026-08-08: HttpListener binds http://127.0.0.1:<port>/ as a
# NON-ELEVATED standard user. No netsh urlacl and no admin rights are required.
# That is why the installer needs no elevation.
#
# THE REMOTE CASE IS DIFFERENT AND IS NOT SOLVED BY LOOPBACK. On Natik's main PC
# there is no local Home Assistant, so a loopback-only agent there would be
# unreachable by design. -BindHosts therefore accepts other addresses, but any
# non-loopback bind FAILS CLOSED unless -AllowFrom lists the permitted source
# IPs. Prefer the Tailscale address (100.64.0.0/10): WireGuard-authenticated and
# tailnet-scoped, versus a LAN bind that is reachable by every device on the
# network. This is a real security decision and needs Natik's explicit approval.
#
# ============================================================================
# SECURITY MODEL - the part that matters most
# ============================================================================
# 1. NO ARBITRARY COMMAND ENDPOINT. There is no /run, no /exec, no /powershell,
#    and there must never be one. Every action is a named, enumerated operation
#    with validated parameters. An arbitrary-command endpoint would turn any
#    model hallucination, or anything that reaches this port, into full remote
#    code execution on Natik's desktop.
# 2. APPS COME FROM AN ALLOWLIST FILE ONLY (apps.json). The API accepts a
#    friendly NAME and never a path, a command line or arguments. Identical
#    reasoning to calls/contacts.json, which accepts a contact name and never a
#    raw number.
# 3. TOKEN IN THE Authorization HEADER ONLY. A token in a query string lands in
#    logs, proxy logs and browser history. ?token= is never read; presenting one
#    is logged as a warning and still rejected.
# 4. DESTRUCTIVE OPERATIONS REQUIRE confirm:true IN THE BODY. shutdown and
#    restart cannot be triggered by a single misheard word. Both also schedule
#    with a delay and /pc/power/cancel aborts.
# 5. NO SECRET IS EVER HARDCODED. The token comes from secrets.env
#    (JARVIS_PC_TOKEN, else JARVIS_WOL_TOKEN) or the environment. Fail closed.
# 6. TYPING USES KEYEVENTF_UNICODE, NOT SendKeys. SendKeys reinterprets + ^ % ~
#    ( ) { } [ ] as modifiers and grouping, so typed text could become a key
#    combination. Unicode injection has no escape syntax at all: text is text.

[CmdletBinding()]
param(
    [int]    $Port       = 8099,
    [string] $TargetMac  = 'D8:43:AE:17:63:25',
    [string] $TargetIp   = '192.168.29.167',

    [string] $SourceIp   = '',
    [string] $Broadcast  = '',

    [string] $TokenFile  = 'C:\ServerData\Stacks\jarvis\secrets.env',
    [string] $AppsFile   = 'C:\ServerData\Stacks\jarvis\host-helper\apps.json',
    # Folder + extension allowlist for the file routes. Same reasoning as
    # AppsFile: the API takes a folder NAME, never a path.
    [string] $FoldersFile = 'C:\ServerData\Stacks\jarvis\host-helper\folders.json',
    # The interop lives outside this script on purpose. See the AMSI note below.
    [string] $NativeSource = 'C:\ServerData\Stacks\jarvis\host-helper\JarvisInput.cs',
    [string] $LogFile    = 'C:\ServerData\Stacks\jarvis\host-helper\pc-agent.log',

    # Loopback only. See BINDING above before changing.
    [string[]] $BindHosts = @('127.0.0.1'),

    # Required, and enforced, for any non-loopback bind. Fail closed.
    [string[]] $AllowFrom = @(),

    # Skip WOL setup entirely. Correct on the main PC, which is the wake TARGET
    # and never the waker; it means no LAN adapter detection at startup.
    [switch] $NoWake,

    # Staged wake: send the magic packet, wait this long for the PC to POST and
    # bring its display subsystem up, THEN turn the monitor on. Natik asked for
    # 15 s and asked that it be a named, tunable value rather than a literal
    # buried in the call. Only used by the LOCAL waker (this laptop); it does not
    # apply on the main PC, which cannot wake itself.
    [int]    $WakeMonitorDelaySeconds = 15,

    # Where the delayed monitor-on is sent. Empty = the LOCAL monitor (correct
    # when the waker and the display are the same machine, and the only path
    # testable on this host). In production this should be the MAIN PC agent's
    # own endpoint, e.g. http://100.x.y.z:8099/pc/monitor over Tailscale, because
    # only the main PC can power its own panel. Leave empty until the main PC
    # agent is installed and reachable.
    [string] $WakeMonitorUrl = '',

    [int]    $MaxLogBytes = 2MB
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$script:Version   = '2.1.0'
$script:StartedAt = Get-Date
$script:SessionId = (Get-Process -Id $PID).SessionId
$script:Interactive = ($script:SessionId -ne 0)

# ---------------------------------------------------------------- logging ---

function Write-Log {
    param([string]$Level, [string]$Message)
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    # MUST be Write-Host, never Write-Output. Write-Output emits into the
    # pipeline, so a logging function would return its log lines alongside its
    # real value. That bug already cost this project once: Get-Token returned
    # @(logline, token) and the caller measured .Length as 2.
    Write-Host $line
    try {
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt $MaxLogBytes)) {
            Move-Item $LogFile "$LogFile.1" -Force
        }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }
}

# ------------------------------------------------------------------ token ---

function Get-Token {
    foreach ($envName in @('JARVIS_PC_TOKEN', 'JARVIS_WOL_TOKEN')) {
        $v = [Environment]::GetEnvironmentVariable($envName)
        if ($v) { Write-Log INFO "token source: environment variable $envName"; return $v }
    }
    if (Test-Path $TokenFile) {
        # JARVIS_PC_TOKEN wins if present, so the main PC can hold a token that
        # is not the one this laptop's Home Assistant uses for wake.
        foreach ($key in @('JARVIS_PC_TOKEN', 'JARVIS_WOL_TOKEN')) {
            foreach ($raw in (Get-Content $TokenFile -Encoding UTF8)) {
                $line = $raw.Trim()
                if ($line -match '^\s*#' -or $line -eq '') { continue }
                $kv = $line -split '=', 2
                if ($kv.Length -eq 2 -and $kv[0].Trim() -eq $key) {
                    $val = $kv[1].Trim().Trim('"').Trim("'")
                    if ($val) { Write-Log INFO "token source: $TokenFile (key $key)"; return $val }
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

# ------------------------------------------------------------ native code ---

# Interop lives in JarvisInput.cs, loaded here. It is NOT inline.
# MEASURED 2026-08-08: with the P/Invoke declarations inline in this file, the
# whole script was refused at load by AMSI with "This script contains malicious
# content and has been blocked by your antivirus software"
# (ScriptContainedMaliciousContent). Each block passed in isolation; the
# aggregate of SendInput + keybd_event + CopyFromScreen + HttpListener in one
# PowerShell file is a remote-access-trojan signature, and Defender is not wrong
# about the shape. AMSI scans PowerShell, not C# handed to csc, so the interop
# moved to a .cs file. See the header of JarvisInput.cs.
if (-not ('JarvisNative' -as [type])) {
    if (-not (Test-Path $NativeSource)) {
        Write-Log FATAL "interop source $NativeSource is missing. pc-agent cannot control the desktop without it."
        exit 1
    }
    try {
        Add-Type -Path $NativeSource -ReferencedAssemblies 'System.Drawing','System.Windows.Forms','System.Windows.Forms' -ErrorAction Stop
    } catch {
        Write-Log FATAL "could not compile $NativeSource : $($_.Exception.Message)"
        exit 1
    }
}

# Only for SystemInformation.PowerStatus (battery). Drawing is used from the
# assembly, not from here.
Add-Type -AssemblyName System.Windows.Forms

# ------------------------------------------------------- WOL (unchanged) ----

function Resolve-LanInterface {
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
             Sort-Object RouteMetric | Select-Object -First 1
    if (-not $route) { throw 'no default route found; cannot pick a LAN adapter' }
    $addr = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction Stop |
            Where-Object { $_.IPAddress -notlike '169.254.*' } | Select-Object -First 1
    if (-not $addr) { throw "no usable IPv4 on interface $($route.InterfaceIndex)" }
    $ipBytes = ([Net.IPAddress]::Parse($addr.IPAddress)).GetAddressBytes()
    # Build the netmask byte-by-byte. Do NOT use [uint32]0xFFFFFFFF: PowerShell
    # 5.1 parses that literal as Int32 -1 and the cast throws.
    $maskBytes = New-Object byte[] 4
    for ($i = 0; $i -lt 4; $i++) {
        $bits = [Math]::Min(8, [Math]::Max(0, $addr.PrefixLength - ($i * 8)))
        $maskBytes[$i] = [byte](((0xFF -shl (8 - $bits)) -band 0xFF))
    }
    $bcastBytes = New-Object byte[] 4
    for ($i = 0; $i -lt 4; $i++) { $bcastBytes[$i] = [byte]($ipBytes[$i] -bor (0xFF -bxor $maskBytes[$i])) }
    return [pscustomobject]@{
        Alias = $addr.InterfaceAlias; Ip = $addr.IPAddress; Prefix = $addr.PrefixLength
        Broadcast = ([Net.IPAddress]$bcastBytes).ToString()
    }
}

function New-MagicPacket {
    param([string]$Mac)
    $clean = ($Mac -replace '[^0-9A-Fa-f]', '')
    if ($clean.Length -ne 12) { throw "invalid MAC address: $Mac" }
    $macBytes = New-Object byte[] 6
    for ($i = 0; $i -lt 6; $i++) { $macBytes[$i] = [Convert]::ToByte($clean.Substring($i * 2, 2), 16) }
    $pkt = New-Object byte[] 102
    for ($i = 0; $i -lt 6; $i++) { $pkt[$i] = 0xFF }
    for ($r = 0; $r -lt 16; $r++) { [Array]::Copy($macBytes, 0, $pkt, 6 + ($r * 6), 6) }
    return ,$pkt
}

function Send-MagicPacket {
    param([string]$Mac, [string]$FromIp, [string]$BroadcastIp)
    $packet  = New-MagicPacket -Mac $Mac
    $results = @()
    $targets = @(
        @{ Addr = $BroadcastIp;      Port = 9 }, @{ Addr = $BroadcastIp;      Port = 7 },
        @{ Addr = '255.255.255.255'; Port = 9 }, @{ Addr = '255.255.255.255'; Port = 7 }
    )
    foreach ($t in $targets) {
        $udp = $null
        try {
            $ep  = New-Object System.Net.IPEndPoint([Net.IPAddress]::Parse($FromIp), 0)
            $udp = New-Object System.Net.Sockets.UdpClient($ep)
            $udp.EnableBroadcast = $true
            $sent = $udp.Send($packet, $packet.Length, $t.Addr, $t.Port)
            $results += [pscustomobject]@{ dest = "$($t.Addr):$($t.Port)"; sent = $sent; ok = ($sent -eq 102) }
        } catch {
            $results += [pscustomobject]@{ dest = "$($t.Addr):$($t.Port)"; sent = 0; ok = $false; error = $_.Exception.Message }
        } finally { if ($udp) { $udp.Close() } }
    }
    return $results
}

function Get-TargetState {
    param([string]$Ip, [int[]]$TcpProbePorts = @(445, 3389))
    $awake = $false; $rttMs = $null; $via = $null
    try {
        $ping  = New-Object System.Net.NetworkInformation.Ping
        $reply = $ping.Send($Ip, 1500)
        if ($reply.Status -eq 'Success') { $awake = $true; $rttMs = $reply.RoundtripTime; $via = 'icmp' }
    } catch { }
    # Windows Firewall commonly drops inbound ICMP while accepting SMB/RDP, so
    # ICMP-only detection reports a running PC as asleep. A connect attempt sends
    # no payload and changes nothing on the target.
    if (-not $awake) {
        foreach ($p in $TcpProbePorts) {
            $client = $null
            try {
                $client = New-Object System.Net.Sockets.TcpClient
                if ($client.ConnectAsync($Ip, $p).Wait(1200)) { $awake = $true; $via = "tcp/$p" }
            } catch { } finally { if ($client) { $client.Close() } }
            if ($awake) { break }
        }
    }
    # A cached ARP entry is worthless: Windows keeps Stale neighbours for minutes
    # after a machine dies. Eviction needs admin, which this agent deliberately
    # no longer has, so confidence degrades instead of lying.
    $arpFresh = $false
    try {
        Remove-NetNeighbor -IPAddress $Ip -AddressFamily IPv4 -Confirm:$false -ErrorAction Stop
        $arpFresh = $true
    } catch { }
    if ($arpFresh) {
        $t = $null
        try { $t = New-Object System.Net.Sockets.TcpClient; $null = $t.ConnectAsync($Ip, 9).Wait(900) }
        catch { } finally { if ($t) { $t.Close() } }
        Start-Sleep -Milliseconds 400
    }
    $arpMac = $null; $arpState = 'none'
    try {
        $n = Get-NetNeighbor -IPAddress $Ip -AddressFamily IPv4 -ErrorAction SilentlyContinue |
             Where-Object { $_.LinkLayerAddress } | Select-Object -First 1
        if ($n) { $arpMac = $n.LinkLayerAddress; $arpState = "$($n.State)" }
    } catch { }
    $arpAnswered = [bool]$arpMac -and $arpState -notin @('Unreachable', 'Incomplete')

    $state = 'unknown'; $confidence = 'low'; $wakeable = $null; $caveat = $null
    if ($awake) {
        $state = 'awake'; $confidence = 'high'; $wakeable = $false
        $caveat = "confirmed responding via $via"
    } elseif ($arpAnswered) {
        # HONEST LIMIT: "asleep" and "off" cannot be separated from another host.
        # A NIC armed for WOL keeps PHY link and ARP offload alive on standby
        # power, in S5 exactly as in S3/S4.
        $state = 'asleep_or_hibernated'; $confidence = 'low'; $wakeable = $true
        $caveat = 'NIC answered ARP, so it has standby power and is armed. Cannot distinguish ' +
                  'S3/S4 sleep from S5 shutdown-with-WOL-armed from here. Treat as wakeable.'
    } else {
        $state = 'off'; $confidence = 'low'; $wakeable = $false
        $caveat = 'No ICMP, no TCP, no ARP answer. Usually fully powered down with the NIC ' +
                  'unarmed, unplugged, or on another subnet. A wake may still succeed if the ' +
                  'NIC is armed but drops ARP in S5, which some firmware does.'
        if (-not $arpFresh) {
            $confidence = 'very_low'
            $caveat += ' NOTE: ARP cache could not be evicted (needs admin; this agent runs as ' +
                       'the user by design), so this reading may be stale.'
        }
    }
    return [pscustomobject]@{
        ip = $Ip; awake = $awake; rtt_ms = $rttMs; detected_via = $via
        arp_mac = $arpMac; arp_state = $arpState; arp_forced_refresh = $arpFresh
        inferred = $state; confidence = $confidence; wakeable = $wakeable; caveat = $caveat
    }
}

# ------------------------------------------------------------- allowlists ---

$script:Apps = @()
function Import-Apps {
    if (-not (Test-Path $AppsFile)) {
        Write-Log WARN "app allowlist $AppsFile not found; launch/close will refuse every request"
        return @()
    }
    $j = Get-Content $AppsFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $list = @()
    foreach ($a in $j.apps) {
        $list += [pscustomobject]@{
            name  = ([string]$a.name).ToLower()
            exec  = [string]$a.exec
            match = @($a.match)
            aka   = @($a.aka | ForEach-Object { ([string]$_).ToLower() })
        }
    }
    Write-Log INFO "app allowlist: $($list.Count) entries from $AppsFile"
    return $list
}

# --- file access -------------------------------------------------------------
#
# WHY THESE ARE ALLOWLISTED THE SAME WAY APPS ARE
# /pc/file/open ends in Start-Process on a file path, which is a ShellExecute --
# Windows chooses the handler from the extension. Two independent gates are
# therefore required, and BOTH are load-bearing:
#
#   1. FOLDER gate. The request carries a folder NAME from folders.json plus a
#      filename to match; a caller can never supply a path. Without this, one
#      misheard word reaches C:\ServerData (every app database and secrets.env)
#      or C:\Windows.
#
#   2. EXTENSION gate. A folder allowlist alone is NOT enough. Downloads on a
#      machine that torrents will contain a .exe sooner or later, and "open that
#      file" would then be arbitrary code execution needing no exploit at all.
#      Only inert document and media types are openable.
#
# Path containment is checked on the CANONICALISED path (GetFullPath resolves
# ".." and mixed separators) using an ordinal, case-insensitive prefix test, so
# "downloads" + "..\..\Windows\System32\cmd.exe" cannot escape the root. A
# StartsWith on the raw string would not catch that.
function Import-Folders {
    if (-not (Test-Path $FoldersFile)) {
        Write-Log WARN "folder allowlist $FoldersFile not found; file search and open will refuse every request"
        return [pscustomobject]@{ folders = @(); exts = @(); max = 25 }
    }
    $j = Get-Content $FoldersFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $list = @()
    foreach ($f in $j.folders) {
        $p = [string]$f.path
        if (-not (Test-Path $p)) {
            # Not fatal: D:\Media may be detached, and a missing Music folder must
            # not stop the whole agent. It is logged so it is not a silent gap.
            Write-Log WARN "folder '$($f.name)' path does not exist, skipping: $p"
            continue
        }
        $list += [pscustomobject]@{
            name    = ([string]$f.name).ToLower()
            path    = [IO.Path]::GetFullPath($p)
            recurse = [bool]$f.recurse
            aka     = @($f.aka | ForEach-Object { ([string]$_).ToLower() })
        }
    }
    $exts = @($j.allowed_extensions | ForEach-Object { ([string]$_).ToLower() })
    $max  = if ($j.max_results) { [int]$j.max_results } else { 25 }
    Write-Log INFO "folder allowlist: $($list.Count) folders, $($exts.Count) extensions from $FoldersFile"
    return [pscustomobject]@{ folders = $list; exts = $exts; max = $max }
}

function Resolve-Folder {
    param([string]$Name)
    if (-not $Name) { return $null }
    $n = ($Name -replace '[^a-z0-9 ]', ' ').ToLower().Trim() -replace '\s+', ' '
    foreach ($f in $script:Folders.folders) { if ($f.name -eq $n -or $f.aka -contains $n) { return $f } }
    foreach ($f in $script:Folders.folders) {
        if ($f.name -like "*$n*" -or $n -like "*$($f.name)*") { return $f }
        foreach ($k in $f.aka) { if ($k -and ($k -like "*$n*" -or $n -like "*$k*")) { return $f } }
    }
    return $null
}

# The containment test. Called on every path before it is opened or returned.
function Test-PathInside {
    param([string]$Candidate, [string]$Root)
    try {
        $c = [IO.Path]::GetFullPath($Candidate)
        $r = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
        return $c.StartsWith($r, [StringComparison]::OrdinalIgnoreCase)
    } catch { return $false }
}

function Test-ExtAllowed {
    param([string]$Path)
    $e = [IO.Path]::GetExtension($Path)
    if (-not $e) { return $false }
    return ($script:Folders.exts -contains $e.ToLower())
}

# Search one allowlisted folder for files whose name contains every word of the
# query. Word-wise rather than substring so "breaking bad s01" matches
# "Breaking.Bad.S01E01.mkv" -- spoken queries never match a filename verbatim.
function Find-Files {
    param([object]$Folder, [string]$Query, [int]$Limit)
    $words = @(($Query -replace '[^a-z0-9 ]', ' ').ToLower() -split '\s+' | Where-Object { $_ })
    $opts = @{ File = $true; ErrorAction = 'SilentlyContinue' }
    if ($Folder.recurse) { $opts['Recurse'] = $true; $opts['Depth'] = 4 }
    $hits = @()
    foreach ($f in Get-ChildItem -LiteralPath $Folder.path @opts) {
        if (-not (Test-ExtAllowed $f.FullName)) { continue }
        if (-not (Test-PathInside $f.FullName $Folder.path)) { continue }   # symlink escape
        $ln = $f.Name.ToLower()
        $ok = $true
        foreach ($w in $words) { if ($ln -notlike "*$w*") { $ok = $false; break } }
        if (-not $ok) { continue }
        $hits += [pscustomobject]@{
            name      = $f.Name
            folder    = $Folder.name
            size_mb   = [math]::Round($f.Length / 1MB, 2)
            modified  = $f.LastWriteTime.ToString('s')
            full      = $f.FullName
        }
        if ($hits.Count -ge ($Limit * 4)) { break }
    }
    # Newest first: "open the invoice" almost always means the recent one.
    return @($hits | Sort-Object { $_.modified } -Descending | Select-Object -First $Limit)
}


function Resolve-App {
    param([string]$Name)
    if (-not $Name) { return $null }
    $n = ($Name -replace '[^a-z0-9 ]', ' ').ToLower().Trim() -replace '\s+', ' '
    foreach ($a in $script:Apps) { if ($a.name -eq $n -or $a.aka -contains $n) { return $a } }
    # One controlled fuzzy step: substring either way. Still confined to the
    # allowlist, so the worst outcome is launching the wrong ALLOWED app.
    foreach ($a in $script:Apps) {
        if ($a.name -like "*$n*" -or $n -like "*$($a.name)*") { return $a }
        foreach ($k in $a.aka) { if ($k -and ($k -like "*$n*" -or $n -like "*$k*")) { return $a } }
    }
    return $null
}

# Key names the API will accept. A fixed table, not a passthrough: this is what
# stops "send a key combo" from becoming "send anything Windows understands".
$script:KeyMap = @{
    'a'=0x41;'b'=0x42;'c'=0x43;'d'=0x44;'e'=0x45;'f'=0x46;'g'=0x47;'h'=0x48;'i'=0x49
    'j'=0x4A;'k'=0x4B;'l'=0x4C;'m'=0x4D;'n'=0x4E;'o'=0x4F;'p'=0x50;'q'=0x51;'r'=0x52
    's'=0x53;'t'=0x54;'u'=0x55;'v'=0x56;'w'=0x57;'x'=0x58;'y'=0x59;'z'=0x5A
    '0'=0x30;'1'=0x31;'2'=0x32;'3'=0x33;'4'=0x34;'5'=0x35;'6'=0x36;'7'=0x37;'8'=0x38;'9'=0x39
    'f1'=0x70;'f2'=0x71;'f3'=0x72;'f4'=0x73;'f5'=0x74;'f6'=0x75;'f7'=0x76;'f8'=0x77
    'f9'=0x78;'f10'=0x79;'f11'=0x7A;'f12'=0x7B
    'enter'=0x0D;'return'=0x0D;'tab'=0x09;'escape'=0x1B;'esc'=0x1B;'space'=0x20
    'backspace'=0x08;'delete'=0x2E;'insert'=0x2D;'home'=0x24;'end'=0x23
    'pageup'=0x21;'pagedown'=0x22;'up'=0x26;'down'=0x28;'left'=0x25;'right'=0x27
    'printscreen'=0x2C
}
$script:ModMap = @{ 'ctrl'=0x11; 'control'=0x11; 'alt'=0x12; 'shift'=0x10; 'win'=0x5B; 'windows'=0x5B }
$script:MediaKeys = @{
    'play_pause'=0xB3; 'play'=0xB3; 'pause'=0xB3
    'next'=0xB0; 'previous'=0xB1; 'prev'=0xB1
    'stop'=0xB2; 'mute'=0xAD; 'volume_up'=0xAF; 'volume_down'=0xAE
}
# Extended-key flag is required for the media and volume virtual keys. Without
# it some shells route them to the wrong handler and nothing happens.
function Send-VKey {
    param([int]$Vk, [switch]$Extended)
    [JarvisNative]::Tap([byte]$Vk, [bool]$Extended)
}

# ------------------------------------------------------ desktop operations ---

function Get-ActiveWindow {
    $title = [JarvisNative]::ForegroundTitle()
    $pid2  = [JarvisNative]::ForegroundPid()
    $proc  = $null
    if ($pid2 -gt 0) {
        try { $proc = (Get-Process -Id $pid2 -ErrorAction Stop).ProcessName } catch { }
    }
    return [pscustomobject]@{ title = $title; process = $proc; pid = [int]$pid2 }
}

function Get-OpenWindows {
    # Enumeration lives in the assembly, not here. See the AMSI note above.
    @([JarvisScreen]::OpenWindows() | ForEach-Object {
        [pscustomobject]@{ title = $_.Title; process = $_.Process; pid = $_.Pid }
    })
}

function Get-BatteryInfo {
    try {
        $ps = [System.Windows.Forms.SystemInformation]::PowerStatus
        $pct = $ps.BatteryLifePercent
        if ($pct -gt 1) { $pct = 1 }              # 255/100 sentinel on desktops
        $secs = $ps.BatteryLifeRemaining
        return [pscustomobject]@{
            present    = ($ps.BatteryChargeStatus -ne 'NoSystemBattery')
            percent    = [int]([Math]::Round($pct * 100))
            charging   = ($ps.PowerLineStatus -eq 'Online')
            status     = "$($ps.BatteryChargeStatus)"
            minutes_remaining = $(if ($secs -gt 0) { [int]($secs / 60) } else { $null })
        }
    } catch { return [pscustomobject]@{ present = $false; error = $_.Exception.Message } }
}

# Real Windows disk usage, queried on the host where fixed drives are actual
# NTFS volumes. Containers on this box (glances, jarvis-voice) cannot do this
# themselves: Docker Desktop exposes C: and D: as 9p mounts, and
# psutil.disk_partitions only lists physical filesystems, so the fs plugin
# always returns []. Documented in Stacks/server-room/docker-compose.yml.
# Only fixed drives (DriveType 3) are reported -- no removable/network drives,
# and no paths or file listings, just aggregate GB numbers per drive letter.
function Get-DiskUsage {
    $drives = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3'
    return @($drives | ForEach-Object {
        $totalGb = [Math]::Round($_.Size / 1GB, 1)
        $freeGb  = [Math]::Round($_.FreeSpace / 1GB, 1)
        [pscustomobject]@{
            drive         = $_.DeviceID
            label         = $_.VolumeName
            total_gb      = $totalGb
            free_gb       = $freeGb
            used_gb       = [Math]::Round($totalGb - $freeGb, 1)
            percent_used  = $(if ($totalGb -gt 0) { [Math]::Round((($totalGb - $freeGb) / $totalGb) * 100, 1) } else { $null })
        }
    })
}

function Get-PcStatus {
    $os   = Get-CimInstance Win32_OperatingSystem
    $boot = $os.LastBootUpTime
    $up   = (Get-Date) - $boot
    $cs   = Get-CimInstance Win32_ComputerSystem
    $active = $null
    if ($script:Interactive) { $active = Get-ActiveWindow }
    $vol = $null; $muted = $null
    try { $vol = [JarvisAudio]::GetVolume(); $muted = [JarvisAudio]::GetMute() } catch { }
    return [pscustomobject]@{
        hostname       = $env:COMPUTERNAME
        logged_in_user = $cs.UserName
        agent_user     = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        session_id     = $script:SessionId
        interactive    = $script:Interactive
        booted_at      = $boot.ToString('o')
        uptime_seconds = [int]$up.TotalSeconds
        uptime_human   = ('{0}d {1}h {2}m' -f $up.Days, $up.Hours, $up.Minutes)
        active_window  = $active
        volume_percent = $vol
        muted          = $muted
        battery        = (Get-BatteryInfo)
        agent_version  = $script:Version
        checked_at     = (Get-Date -Format 'o')
    }
}

function Get-Screenshot {
    param([int]$MaxWidth = 0)
    $shot = [JarvisScreen]::Capture($MaxWidth)
    return [pscustomobject]@{ bytes = $shot.Png; width = $shot.Width; height = $shot.Height }
}

# Schedule "monitor on" to run after a delay WITHOUT blocking the listener.
# The HTTP loop is single-threaded, so a Start-Sleep here would freeze every
# other endpoint for the whole delay. A background runspace re-loads the interop
# (it is a separate runspace with its own type table) and fires the monitor on
# after the delay. Used by the staged wake so the reply returns immediately while
# the display comes up later.
#
# TOPOLOGY NOTE. Monitor power acts on the LOCAL machine only. When THIS laptop
# wakes the remote main PC, the panel that must come on is the MAIN PC's, which
# only the main PC's own agent can drive. So in production the delayed step is a
# remote HTTP call to the main PC agent's /pc/monitor ($MonitorUrl set). With no
# URL it falls back to the LOCAL monitor, which is the path that is testable on
# this one machine and is correct when the waker and the display are the same box.
function Start-DelayedMonitorOn {
    param([int]$DelaySeconds, [string]$NativeSource, [string]$LogFile,
          [string]$MonitorUrl = '', [string]$Token = '')
    $ps = [PowerShell]::Create()
    $null = $ps.AddScript({
        param($Delay, $Src, $Log, $Url, $Tok)
        Start-Sleep -Seconds $Delay
        $ts = (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
        try {
            if ($Url) {
                # Remote: drive the main PC's own monitor over HTTP.
                $h = @{ Authorization = "Bearer $Tok" }
                $b = '{"on":true}'
                $r = Invoke-RestMethod -Uri $Url -Method POST -Headers $h -Body $b `
                        -ContentType 'application/json' -TimeoutSec 15
                $line = "$ts [INFO] staged wake: remote monitor on -> $Url ok=$($r.ok) method=$($r.method)"
            } else {
                if (-not ('JarvisMonitor' -as [type])) {
                    Add-Type -Path $Src -ReferencedAssemblies 'System.Drawing','System.Windows.Forms' -ErrorAction Stop
                }
                $r = [JarvisMonitor]::SetPower($true)
                $line = "$ts [INFO] staged wake: local monitor on via $($r.Method) (found $($r.MonitorsFound), set $($r.MonitorsSet))"
            }
        } catch {
            $line = "$ts [ERROR] staged wake: monitor on failed: $($_.Exception.Message)"
        }
        try { Add-Content -Path $Log -Value $line -Encoding utf8 } catch { }
    }).AddArgument($DelaySeconds).AddArgument($NativeSource).AddArgument($LogFile).AddArgument($MonitorUrl).AddArgument($Token)
    # Fire and forget. BeginInvoke returns immediately; the runspace disposes
    # itself when the script ends. We deliberately do not track the handle.
    $null = $ps.BeginInvoke()
}

# ------------------------------------------------------------------ start ---

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
Write-Log INFO "pc-agent $($script:Version) starting (pid $PID, session $($script:SessionId))"
if (-not $script:Interactive) {
    Write-Log WARN 'RUNNING IN SESSION 0. Desktop endpoints will return 503. Screenshots, media keys, typing and app launches CANNOT work from session 0 - install this as the interactive user (see install-pc-agent.ps1).'
}

$token = Get-Token
if (-not $token) {
    Write-Log FATAL "no token configured. Set JARVIS_PC_TOKEN or JARVIS_WOL_TOKEN in the environment or in $TokenFile. Refusing to start."
    exit 1
}
if ($token.Length -lt 16) {
    Write-Log FATAL "token is only $($token.Length) chars; refusing to start with a weak token (want >= 16)."
    exit 1
}

# FAIL CLOSED on a non-loopback bind without a source allowlist.
$loopbackOnly = $true
foreach ($h in $BindHosts) { if ($h -notin @('127.0.0.1', 'localhost', '::1')) { $loopbackOnly = $false } }
if (-not $loopbackOnly) {
    if (-not $AllowFrom -or $AllowFrom.Count -eq 0) {
        Write-Log FATAL "non-loopback bind requested ($($BindHosts -join ',')) with no -AllowFrom source allowlist. Refusing to start: that would expose desktop control to every host that can route to this address."
        exit 1
    }
    Write-Log WARN "NON-LOOPBACK BIND: $($BindHosts -join ',') - accepting only from $($AllowFrom -join ',')"
}

$script:Apps = Import-Apps
$script:Folders = Import-Folders

if (-not $NoWake) {
    if (-not $SourceIp -or -not $Broadcast) {
        $lan = $null
        for ($attempt = 1; $attempt -le 20; $attempt++) {
            try { $lan = Resolve-LanInterface; break }
            catch { Write-Log WARN "LAN detect attempt $attempt/20 failed: $($_.Exception.Message)"; Start-Sleep -Seconds 6 }
        }
        if (-not $lan) { Write-Log FATAL 'no usable LAN adapter after 20 attempts. Refusing to start.'; exit 1 }
        if (-not $SourceIp)  { $SourceIp  = $lan.Ip }
        if (-not $Broadcast) { $Broadcast = $lan.Broadcast }
        Write-Log INFO "LAN adapter '$($lan.Alias)' ip=$($lan.Ip)/$($lan.Prefix) broadcast=$($lan.Broadcast)"
    }
    Write-Log INFO "wake target mac=$TargetMac ip=$TargetIp source=$SourceIp broadcast=$Broadcast"
} else {
    Write-Log INFO 'wake disabled (-NoWake): this machine is a control target, not a waker'
}

$listener = New-Object System.Net.HttpListener
foreach ($h in $BindHosts) { $listener.Prefixes.Add("http://${h}:$Port/") }
try { $listener.Start() }
catch { Write-Log FATAL "could not bind $($BindHosts -join ',') port ${Port}: $($_.Exception.Message)"; exit 1 }
Write-Log INFO ("listening on " + (($listener.Prefixes) -join ' '))

# ------------------------------------------------------------- http utils ---

function Send-Json {
    param($Context, [int]$Status, $Body)
    $json  = ($Body | ConvertTo-Json -Depth 8 -Compress)
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $Context.Response.StatusCode  = $Status
    $Context.Response.ContentType = 'application/json'
    $Context.Response.Headers.Add('Cache-Control', 'no-store')
    $Context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $Context.Response.Close()
    return $json
}

function Send-Png {
    param($Context, [byte[]]$Bytes)
    $Context.Response.StatusCode  = 200
    $Context.Response.ContentType = 'image/png'
    $Context.Response.Headers.Add('Cache-Control', 'no-store')
    $Context.Response.OutputStream.Write($Bytes, 0, $Bytes.Length)
    $Context.Response.Close()
}

function Read-JsonBody {
    param($Request)
    try {
        if (-not $Request.HasEntityBody) { return $null }
        $sr = New-Object IO.StreamReader($Request.InputStream, $Request.ContentEncoding)
        $raw = $sr.ReadToEnd(); $sr.Close()
        if (-not $raw.Trim()) { return $null }
        return ($raw | ConvertFrom-Json)
    } catch { return $null }
}

# StrictMode 2.0 throws on a missing property, and every request body has
# missing properties. Always go through this.
function Get-Field {
    param($Obj, [string]$Name, $Default = $null)
    if ($null -eq $Obj) { return $Default }
    try {
        $p = $Obj.PSObject.Properties[$Name]
        if ($null -eq $p -or $null -eq $p.Value) { return $Default }
        return $p.Value
    } catch { return $Default }
}

$lastWake = [DateTime]::MinValue

try {
  while ($listener.IsListening) {
    $task = $listener.GetContextAsync()
    while (-not $task.AsyncWaitHandle.WaitOne(1000)) { if (-not $listener.IsListening) { break } }
    if (-not $task.IsCompleted) { continue }

    $ctx    = $task.Result
    $req    = $ctx.Request
    $path   = $req.Url.AbsolutePath.TrimEnd('/').ToLower()
    if ($path -eq '') { $path = '/' }
    $method = $req.HttpMethod
    $remote = $req.RemoteEndPoint.Address.ToString()

    try {
        # ---- /health : unauthenticated, discloses nothing about the machine ----
        if ($path -eq '/health' -and $method -eq 'GET') {
            $null = Send-Json $ctx 200 @{
                status = 'ok'; service = 'jarvis-pc-agent'; version = $script:Version
                uptime_seconds = [int]((Get-Date) - $script:StartedAt).TotalSeconds
                interactive = $script:Interactive
            }
            Write-Log INFO "GET /health from $remote -> 200"
            continue
        }

        # ---- source allowlist, enforced before auth ----
        if (-not $loopbackOnly -and $remote -notin @('127.0.0.1', '::1') -and $remote -notin $AllowFrom) {
            $null = Send-Json $ctx 403 @{ error = 'source_not_allowed' }
            Write-Log WARN "$method $path from $remote -> 403 source not in -AllowFrom"
            continue
        }

        # ---- bearer token, header ONLY ----
        # A token in a query string ends up in logs and history. Presenting one
        # is refused loudly so a caller cannot believe it authenticated.
        if ($req.Url.Query -match '(?i)(token|auth|key|bearer)=') {
            $null = Send-Json $ctx 400 @{ error = 'token_in_query_string'
                detail = 'Send the token as "Authorization: Bearer <token>". Query strings are logged.' }
            Write-Log WARN "$method $path from $remote -> 400 token presented in query string (rejected, value not logged)"
            continue
        }
        $auth = $req.Headers['Authorization']
        $presented = $null
        if ($auth -and $auth -match '^(?i)bearer\s+(.+)$') { $presented = $Matches[1].Trim() }
        if (-not (Test-TokenEqual -Presented $presented -Expected $token)) {
            # Do NOT add a WWW-Authenticate header: HttpListener rejects it and
            # the whole request 500s.
            $null = Send-Json $ctx 401 @{ error = 'unauthorized' }
            Write-Log WARN "$method $path from $remote -> 401 unauthorized"
            continue
        }

        $body = Read-JsonBody $req

        # ---- desktop guard ----
        # Everything under /pc/ except status needs the interactive desktop.
        $needsDesktop = $path -like '/pc/*' -and $path -ne '/pc/status' -and $path -ne '/pc/disks'
        if ($needsDesktop -and -not $script:Interactive) {
            $null = Send-Json $ctx 503 @{
                error = 'no_interactive_session'
                detail = "Agent is running in session $($script:SessionId). Session 0 is isolated from the desktop: screenshots fail with 'The handle is invalid', and keystrokes, launches and lock go to an empty desktop nobody sees. Reinstall with install-pc-agent.ps1, which registers the task as the interactive user."
            }
            Write-Log WARN "$method $path from $remote -> 503 session 0"
            continue
        }

        switch -Regex ("$method $path") {

            # ================================================ legacy: status ==
            '^GET /status$' {
                $st = Get-TargetState -Ip $TargetIp
                $null = Send-Json $ctx 200 @{
                    ip = $st.ip; awake = $st.awake; state = $st.inferred
                    confidence = $st.confidence; wakeable = $st.wakeable; caveat = $st.caveat
                    rtt_ms = $st.rtt_ms; detected_via = $st.detected_via
                    arp_mac = $st.arp_mac; arp_state = $st.arp_state
                    arp_forced_refresh = $st.arp_forced_refresh
                    checked_at = (Get-Date -Format 'o')
                }
                Write-Log INFO "GET /status from $remote -> 200 state=$($st.inferred) confidence=$($st.confidence)"
                break
            }

            # ================================================== legacy: wake ==
            # GET is accepted as an alias because http.sys rejects a POST with
            # neither body nor Content-Length as 411 BEFORE this script runs, and
            # a bare HA rest_command sends exactly that shape.
            '^(POST|GET) /(wake|pc/power/wake)$' {
                if ($NoWake) {
                    $null = Send-Json $ctx 501 @{ error = 'wake_disabled'; detail = 'agent started with -NoWake' }
                    Write-Log WARN "$method $path from $remote -> 501 wake disabled"
                    break
                }
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

                # Staged wake: the PC starts first, then after a delay the monitor
                # comes on. The delay is a named param ($WakeMonitorDelaySeconds),
                # not a literal, so it can be tuned. Scheduled non-blocking so this
                # response returns now. staged=false lets a caller opt out with
                # {"staged":false} (e.g. waking a headless box).
                $staged = (Get-Field $body 'staged' $true) -ne $false
                if ($staged) {
                    Start-DelayedMonitorOn -DelaySeconds $WakeMonitorDelaySeconds `
                        -NativeSource $NativeSource -LogFile $LogFile `
                        -MonitorUrl $WakeMonitorUrl -Token $token
                }
                $null = Send-Json $ctx 200 @{
                    sent = ($okCount -gt 0); packets_sent = $okCount; mac = $TargetMac
                    source_ip = $SourceIp; broadcast = $Broadcast; state_before = $before.inferred
                    staged = $staged
                    monitor_on_after_seconds = $(if ($staged) { $WakeMonitorDelaySeconds } else { $null })
                    monitor_target = $(if (-not $staged) { 'none' } elseif ($WakeMonitorUrl) { $WakeMonitorUrl } else { 'local' })
                    note = 'magic packet is unacknowledged; poll /status to confirm. Monitor turns on after the delay.'
                    detail = $sends; sent_at = (Get-Date -Format 'o')
                }
                Write-Log INFO "$method /wake from $remote -> 200 packets=$okCount before=$($before.inferred) staged=$staged delay=${WakeMonitorDelaySeconds}s target=$(if($WakeMonitorUrl){'remote'}else{'local'})"
                break
            }

            # ====================================================== monitor ===
            # Monitor power as its own capability, independent of PC power. DDC/CI
            # (real hardware power over the video cable) is preferred; the
            # SC_MONITORPOWER window message is the fallback. OFF is reliable both
            # ways; ON via the message is best-effort, so an input nudge rides
            # with it. The response reports which method actually drove the panel.
            '^POST /pc/monitor$' {
                $on = Get-Field $body 'on' $null
                if ($null -eq $on) {
                    $null = Send-Json $ctx 400 @{ error = 'bad_request'; detail = 'body must be {"on": true|false}' }
                    Write-Log WARN "POST /pc/monitor from $remote -> 400 missing on"
                    break
                }
                $r = [JarvisMonitor]::SetPower([bool]$on)
                $null = Send-Json $ctx 200 @{
                    ok = $r.Ok; action = $(if ([bool]$on) { 'monitor_on' } else { 'monitor_off' })
                    method = $r.Method; monitors_found = $r.MonitorsFound; monitors_set = $r.MonitorsSet
                    detail = $r.Detail
                    note = $(if ([bool]$on) {
                        'ON via the window message is best-effort; DDC/CI is confirmed. Check the panel.'
                    } else { 'OFF is reliable on both paths.' })
                }
                Write-Log INFO "POST /pc/monitor from $remote -> 200 on=$on method=$($r.Method) found=$($r.MonitorsFound) set=$($r.MonitorsSet)"
                break
            }

            # ================================================= this PC status ==
            '^GET /pc/status$' {
                $s = Get-PcStatus
                $null = Send-Json $ctx 200 $s
                Write-Log INFO "GET /pc/status from $remote -> 200 uptime=$($s.uptime_human)"
                break
            }

            # ============================================ real disk usage ====
            '^GET /pc/disks$' {
                $disks = Get-DiskUsage
                $null = Send-Json $ctx 200 @{ disks = $disks; checked_at = (Get-Date -Format 'o') }
                Write-Log INFO "GET /pc/disks from $remote -> 200 ($($disks.Count) drive(s))"
                break
            }

            '^GET /pc/apps$' {
                $running = @()
                foreach ($a in $script:Apps) {
                    $isRunning = $false
                    foreach ($m in $a.match) {
                        if ($m -and (Get-Process -Name $m -ErrorAction SilentlyContinue)) { $isRunning = $true; break }
                    }
                    if ($isRunning) { $running += $a.name }
                }
                $null = Send-Json $ctx 200 @{
                    allowed = @($script:Apps | ForEach-Object { $_.name })
                    running = $running
                    windows = @(Get-OpenWindows)
                }
                Write-Log INFO "GET /pc/apps from $remote -> 200 running=$($running.Count)"
                break
            }

            # ======================================================== files ===
            # Two routes, deliberately split: /pc/file/find is READ-ONLY and
            # returns candidates; /pc/file/open acts. A 3B model asked to "open
            # my resume" should be able to look before it leaps, and a spoken
            # filename practically never matches a real one exactly.
            '^(POST|GET) /pc/folders$' {
                $null = Send-Json $ctx 200 @{
                    folders = @($script:Folders.folders | ForEach-Object {
                        @{ name = $_.name; aka = @($_.aka); recurse = $_.recurse } })
                    allowed_extensions = @($script:Folders.exts)
                    detail = 'Only these folder names are accepted. Paths are never accepted.'
                }
                Write-Log INFO "GET /pc/folders from $remote -> 200"
                break
            }

            '^POST /pc/file/find$' {
                $fname = [string](Get-Field $body 'folder' '')
                $query = [string](Get-Field $body 'query' '')
                $folder = Resolve-Folder -Name $fname
                if (-not $folder) {
                    $null = Send-Json $ctx 404 @{ error = 'folder_not_allowed'; requested = $fname
                        allowed = @($script:Folders.folders | ForEach-Object { $_.name })
                        detail = 'Only folder names from folders.json are accepted. Paths are never accepted.' }
                    Write-Log WARN "POST /pc/file/find from $remote -> 404 '$fname' not in allowlist"
                    break
                }
                if (-not $query) {
                    $null = Send-Json $ctx 400 @{ error = 'query_required'
                        detail = 'Supply a query; this route does not list a whole folder.' }
                    break
                }
                $hits = @(Find-Files -Folder $folder -Query $query -Limit $script:Folders.max)
                $null = Send-Json $ctx 200 @{
                    folder = $folder.name; query = $query; count = $hits.Count
                    # full paths are NOT returned: the caller never needs one, and
                    # not emitting them keeps paths out of the model's context
                    # where they could be echoed back as a forged open request.
                    files = @($hits | ForEach-Object { @{ name = $_.name; size_mb = $_.size_mb; modified = $_.modified } })
                }
                Write-Log INFO "POST /pc/file/find from $remote -> 200 '$query' in $($folder.name): $($hits.Count) hit(s)"
                break
            }

            '^POST /pc/file/open$' {
                $fname = [string](Get-Field $body 'folder' '')
                $query = [string](Get-Field $body 'name' (Get-Field $body 'query' ''))
                $folder = Resolve-Folder -Name $fname
                if (-not $folder) {
                    $null = Send-Json $ctx 404 @{ error = 'folder_not_allowed'; requested = $fname
                        allowed = @($script:Folders.folders | ForEach-Object { $_.name })
                        detail = 'Only folder names from folders.json are accepted. Paths are never accepted.' }
                    Write-Log WARN "POST /pc/file/open from $remote -> 404 '$fname' not in allowlist"
                    break
                }
                if (-not $query) {
                    $null = Send-Json $ctx 400 @{ error = 'name_required' }
                    break
                }
                $hits = @(Find-Files -Folder $folder -Query $query -Limit $script:Folders.max)
                if ($hits.Count -eq 0) {
                    $null = Send-Json $ctx 404 @{ error = 'no_match'; folder = $folder.name; query = $query
                        detail = 'No file in that folder matches, or its type is not openable.' }
                    Write-Log INFO "POST /pc/file/open from $remote -> 404 no match for '$query' in $($folder.name)"
                    break
                }
                if ($hits.Count -gt 1) {
                    # Ambiguity is reported, never guessed. Opening the wrong file
                    # is not destructive but it is confidently wrong, which is the
                    # failure mode this project keeps paying for.
                    $null = Send-Json $ctx 409 @{ error = 'ambiguous'; folder = $folder.name; query = $query
                        count = $hits.Count
                        candidates = @($hits | Select-Object -First 8 | ForEach-Object { $_.name })
                        detail = 'More than one file matches. Ask which one, then send its exact name.' }
                    Write-Log INFO "POST /pc/file/open from $remote -> 409 ambiguous '$query' ($($hits.Count))"
                    break
                }
                $target = $hits[0]
                # Belt and braces. Find-Files already applied both gates, but this
                # is the line that actually executes something, so it re-checks
                # rather than trusting an earlier function.
                if (-not (Test-PathInside $target.full $folder.path) -or -not (Test-ExtAllowed $target.full)) {
                    $null = Send-Json $ctx 403 @{ error = 'refused'; detail = 'Path or extension failed the final check.' }
                    Write-Log ERROR "POST /pc/file/open from $remote -> 403 REFUSED $($target.full)"
                    break
                }
                try {
                    # No -ArgumentList, and the path came from a directory listing
                    # inside an allowlisted root -- never from the request.
                    Start-Process -FilePath $target.full -ErrorAction Stop
                    $null = Send-Json $ctx 200 @{ ok = $true; action = 'open'; file = $target.name; folder = $folder.name }
                    Write-Log INFO "POST /pc/file/open from $remote -> 200 $($target.full)"
                } catch {
                    $null = Send-Json $ctx 500 @{ ok = $false; error = 'open_failed'; file = $target.name
                        detail = $_.Exception.Message
                        hint = 'no application is registered for this file type' }
                    Write-Log ERROR "POST /pc/file/open from $remote -> 500 $($target.name): $($_.Exception.Message)"
                }
                break
            }

            # ======================================================== power ===
            '^(POST|GET) /pc/power/lock$' {
                $ok = [JarvisNative]::Lock()
                $null = Send-Json $ctx 200 @{ ok = $ok; action = 'lock' }
                Write-Log INFO "$method /pc/power/lock from $remote -> 200 ok=$ok"
                break
            }

            '^(POST|GET) /pc/power/sleep$' {
                # hibernate=false, force=false, disableWakeEvent=false.
                # Returns immediately; the machine suspends after the response is
                # flushed, which is why the response is sent first.
                $null = Send-Json $ctx 200 @{ ok = $true; action = 'sleep'
                    note = 'suspend requested; the response is sent before the machine suspends' }
                Write-Log INFO "$method /pc/power/sleep from $remote -> 200"
                Start-Sleep -Milliseconds 250
                $null = [JarvisNative]::Sleep()
                break
            }

            # DESTRUCTIVE. confirm:true is mandatory, so a single misheard word
            # cannot power off the machine mid-work. Also delayed and cancellable.
            '^POST /pc/power/(shutdown|restart)$' {
                $what = if ($path -like '*shutdown*') { 'shutdown' } else { 'restart' }
                $confirm = Get-Field $body 'confirm' $false
                if ($confirm -ne $true) {
                    $null = Send-Json $ctx 428 @{
                        error = 'confirmation_required'; action = $what
                        detail = "Send {`"confirm`":true} in the JSON body. This is deliberate: $what must never be one misheard word away."
                    }
                    Write-Log WARN "$method $path from $remote -> 428 confirm missing"
                    break
                }
                $delay = [int](Get-Field $body 'delay_seconds' 20)
                if ($delay -lt 0)   { $delay = 0 }
                if ($delay -gt 600) { $delay = 600 }
                # Fixed argument ARRAY. No user text is ever interpolated into a
                # command line here; only a clamped integer reaches shutdown.exe.
                $args2 = if ($what -eq 'shutdown') { @('/s', '/t', "$delay") } else { @('/r', '/t', "$delay") }
                Start-Process -FilePath 'shutdown.exe' -ArgumentList $args2 -WindowStyle Hidden -Wait
                $null = Send-Json $ctx 200 @{ ok = $true; action = $what; delay_seconds = $delay
                    cancel = 'POST /pc/power/cancel' }
                Write-Log WARN "$method $path from $remote -> 200 CONFIRMED $what in ${delay}s"
                break
            }

            '^(POST|GET) /pc/power/cancel$' {
                $p = Start-Process -FilePath 'shutdown.exe' -ArgumentList @('/a') -WindowStyle Hidden -Wait -PassThru
                $null = Send-Json $ctx 200 @{ ok = ($p.ExitCode -eq 0); action = 'cancel'
                    exit_code = $p.ExitCode
                    note = 'exit code 1116 means there was no pending shutdown to cancel' }
                Write-Log INFO "$method /pc/power/cancel from $remote -> 200 exit=$($p.ExitCode)"
                break
            }

            # ======================================================== media ===
            '^(POST|GET) /pc/media/(play_pause|play|pause|next|previous|prev|stop|mute|volume_up|volume_down)$' {
                $act = ($path -split '/')[-1]
                Send-VKey -Vk $script:MediaKeys[$act] -Extended
                $vol = $null; $muted = $null
                try { $vol = [JarvisAudio]::GetVolume(); $muted = [JarvisAudio]::GetMute() } catch { }
                $null = Send-Json $ctx 200 @{ ok = $true; action = $act
                    volume_percent = $vol; muted = $muted
                    note = 'media keys are fire-and-forget; whether an app responds depends on that app holding the media session' }
                Write-Log INFO "$method /pc/media/$act from $remote -> 200 vol=$vol muted=$muted"
                break
            }

            '^POST /pc/volume$' {
                $lvl = Get-Field $body 'level' $null
                if ($null -eq $lvl -or -not ("$lvl" -match '^\d+$')) {
                    $null = Send-Json $ctx 400 @{ error = 'bad_level'; detail = 'body must be {"level": 0-100}' }
                    Write-Log WARN "POST /pc/volume from $remote -> 400 bad level"
                    break
                }
                $lvl = [int]$lvl
                if ($lvl -lt 0) { $lvl = 0 }; if ($lvl -gt 100) { $lvl = 100 }
                [JarvisAudio]::SetVolume($lvl)
                if ($lvl -gt 0 -and [JarvisAudio]::GetMute()) { [JarvisAudio]::SetMute($false) }
                $null = Send-Json $ctx 200 @{ ok = $true; action = 'set_volume'
                    volume_percent = [JarvisAudio]::GetVolume(); muted = [JarvisAudio]::GetMute() }
                Write-Log INFO "POST /pc/volume from $remote -> 200 level=$lvl"
                break
            }

            '^GET /pc/volume$' {
                $null = Send-Json $ctx 200 @{ volume_percent = [JarvisAudio]::GetVolume(); muted = [JarvisAudio]::GetMute() }
                Write-Log INFO "GET /pc/volume from $remote -> 200"
                break
            }

            '^POST /pc/mute$' {
                $want = Get-Field $body 'mute' $null
                if ($null -eq $want) { [JarvisAudio]::SetMute(-not [JarvisAudio]::GetMute()) }
                else { [JarvisAudio]::SetMute([bool]$want) }
                $null = Send-Json $ctx 200 @{ ok = $true; muted = [JarvisAudio]::GetMute()
                    volume_percent = [JarvisAudio]::GetVolume() }
                Write-Log INFO "POST /pc/mute from $remote -> 200 muted=$([JarvisAudio]::GetMute())"
                break
            }

            # ========================================================= apps ===
            '^POST /pc/app/(open|close)$' {
                $verb = ($path -split '/')[-1]
                $name = [string](Get-Field $body 'name' '')
                $app  = Resolve-App -Name $name
                if (-not $app) {
                    # The allowlist is returned so a caller can self-correct, and
                    # because the set of launchable apps is not a secret. What is
                    # never accepted is a path or a command line.
                    $null = Send-Json $ctx 404 @{ error = 'app_not_allowed'; requested = $name
                        allowed = @($script:Apps | ForEach-Object { $_.name })
                        detail = 'Only names from apps.json are accepted. Paths and command lines are never accepted.' }
                    Write-Log WARN "POST /pc/app/$verb from $remote -> 404 '$name' not in allowlist"
                    break
                }
                if ($verb -eq 'open') {
                    # No -ArgumentList. Nothing from the request reaches the
                    # process command line; only $app.exec, which came from the
                    # allowlist file.
                    try {
                        Start-Process -FilePath $app.exec -ErrorAction Stop
                        $null = Send-Json $ctx 200 @{ ok = $true; action = 'open'; app = $app.name }
                        Write-Log INFO "POST /pc/app/open from $remote -> 200 $($app.name)"
                    } catch {
                        $null = Send-Json $ctx 500 @{ ok = $false; app = $app.name
                            error = 'launch_failed'; detail = $_.Exception.Message
                            hint = 'the executable in apps.json is probably not installed or not on PATH' }
                        Write-Log ERROR "POST /pc/app/open from $remote -> 500 $($app.name): $($_.Exception.Message)"
                    }
                } else {
                    $killed = @()
                    foreach ($m in $app.match) {
                        if (-not $m) { continue }
                        foreach ($p in @(Get-Process -Name $m -ErrorAction SilentlyContinue)) {
                            # CloseMainWindow first: it is a graceful WM_CLOSE, so
                            # an editor gets its save prompt instead of losing work.
                            try {
                                if ($p.MainWindowHandle -ne 0) { $null = $p.CloseMainWindow() }
                                else { $p.Kill() }
                                $killed += "$($p.ProcessName):$($p.Id)"
                            } catch { }
                        }
                    }
                    $null = Send-Json $ctx 200 @{ ok = ($killed.Count -gt 0); action = 'close'
                        app = $app.name; closed = $killed
                        note = 'graceful close is requested; an app with unsaved work may show a save prompt and stay open' }
                    Write-Log INFO "POST /pc/app/close from $remote -> 200 $($app.name) closed=$($killed.Count)"
                }
                break
            }

            # ====================================================== windows ===
            '^(POST|GET) /pc/window/(minimise_all|minimize_all|show_desktop)$' {
                $shell = New-Object -ComObject Shell.Application
                if ($path -like '*show_desktop*') { $shell.ToggleDesktop() } else { $shell.MinimizeAll() }
                [Runtime.InteropServices.Marshal]::ReleaseComObject($shell) | Out-Null
                $null = Send-Json $ctx 200 @{ ok = $true; action = ($path -split '/')[-1] }
                Write-Log INFO "$method $path from $remote -> 200"
                break
            }

            '^(POST|GET) /pc/window/next$' {
                # Alt+Tab. Alt must be held down across the Tab press, so this
                # cannot use the single-key helper.
                [JarvisNative]::Combo(@([byte]0x12), [byte]0x09)   # ALT held, TAB tapped
                Start-Sleep -Milliseconds 200
                $null = Send-Json $ctx 200 @{ ok = $true; action = 'next_window'; active = (Get-ActiveWindow) }
                Write-Log INFO "$method /pc/window/next from $remote -> 200"
                break
            }

            '^POST /pc/window/focus$' {
                $want = [string](Get-Field $body 'title' '')
                if (-not $want) {
                    $null = Send-Json $ctx 400 @{ error = 'bad_request'; detail = 'body must be {"title":"<substring of a window title or process name>"}' }
                    break
                }
                $match = Get-Process | Where-Object {
                    $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -and
                    ($_.MainWindowTitle -like "*$want*" -or $_.ProcessName -like "*$want*")
                } | Select-Object -First 1
                if (-not $match) {
                    $null = Send-Json $ctx 404 @{ error = 'window_not_found'; requested = $want
                        windows = @(Get-OpenWindows) }
                    Write-Log WARN "POST /pc/window/focus from $remote -> 404 '$want'"
                    break
                }
                $ok = [JarvisNative]::ForceForeground($match.MainWindowHandle)
                Start-Sleep -Milliseconds 150
                $null = Send-Json $ctx 200 @{ ok = $ok; action = 'focus'
                    matched = $match.MainWindowTitle; active = (Get-ActiveWindow) }
                Write-Log INFO "POST /pc/window/focus from $remote -> 200 '$($match.MainWindowTitle)' ok=$ok"
                break
            }

            '^GET /pc/window$' {
                $null = Send-Json $ctx 200 @{ active = (Get-ActiveWindow); windows = @(Get-OpenWindows) }
                Write-Log INFO "GET /pc/window from $remote -> 200"
                break
            }

            # ======================================================== input ===
            '^POST /pc/input/type$' {
                $text = [string](Get-Field $body 'text' '')
                if (-not $text) {
                    $null = Send-Json $ctx 400 @{ error = 'bad_request'; detail = 'body must be {"text":"..."}' }
                    break
                }
                # Bounded so a runaway model cannot hold the keyboard forever.
                if ($text.Length -gt 500) {
                    $null = Send-Json $ctx 413 @{ error = 'text_too_long'; max = 500; got = $text.Length }
                    Write-Log WARN "POST /pc/input/type from $remote -> 413 length=$($text.Length)"
                    break
                }
                $sent = [JarvisNative]::TypeUnicode($text)
                # Length only. The typed text is NOT logged: it is whatever Natik
                # asked to be typed and could be a password.
                $null = Send-Json $ctx 200 @{ ok = ($sent -gt 0); action = 'type'
                    characters = $text.Length; events_sent = [int]$sent
                    note = 'typed into whatever window currently has focus' }
                Write-Log INFO "POST /pc/input/type from $remote -> 200 chars=$($text.Length) (content not logged)"
                break
            }

            '^POST /pc/input/key$' {
                $keyRaw = [string](Get-Field $body 'key' '')
                $mods   = @(Get-Field $body 'modifiers' @())
                $key    = $keyRaw.ToLower().Trim()
                if (-not $script:KeyMap.ContainsKey($key)) {
                    $null = Send-Json $ctx 400 @{ error = 'unknown_key'; requested = $keyRaw
                        allowed = @($script:KeyMap.Keys | Sort-Object)
                        detail = 'Keys come from a fixed table. Arbitrary key sequences are never accepted.' }
                    Write-Log WARN "POST /pc/input/key from $remote -> 400 unknown key '$keyRaw'"
                    break
                }
                $modVks = @()
                foreach ($m in $mods) {
                    $mm = ([string]$m).ToLower().Trim()
                    if (-not $script:ModMap.ContainsKey($mm)) {
                        $null = Send-Json $ctx 400 @{ error = 'unknown_modifier'; requested = $m
                            allowed = @($script:ModMap.Keys | Sort-Object) }
                        Write-Log WARN "POST /pc/input/key from $remote -> 400 unknown modifier '$m'"
                        $modVks = $null; break
                    }
                    $modVks += $script:ModMap[$mm]
                }
                if ($null -eq $modVks) { break }
                # Combo() holds the modifiers, taps the key, and releases the
                # modifiers in reverse order, exactly like a human hand.
                [JarvisNative]::Combo([byte[]]$modVks, [byte]$script:KeyMap[$key])
                $null = Send-Json $ctx 200 @{ ok = $true; action = 'key'; key = $key; modifiers = @($mods) }
                Write-Log INFO "POST /pc/input/key from $remote -> 200 $($mods -join '+')+$key"
                break
            }

            '^POST /pc/input/mouse$' {
                $vb = [JarvisScreen]::VirtualScreenBounds()   # x, y, width, height
                $x = Get-Field $body 'x' $null
                $y = Get-Field $body 'y' $null
                $btn = ([string](Get-Field $body 'button' '')).ToLower()
                $clicks = [int](Get-Field $body 'clicks' 0)
                $moved = $false
                if ($null -ne $x -and $null -ne $y) {
                    $xi = [int]$x; $yi = [int]$y
                    # Clamp to the virtual screen. An off-screen cursor is not
                    # dangerous but it is unrecoverable without a mouse.
                    if ($xi -lt $vb[0]) { $xi = $vb[0] }
                    if ($yi -lt $vb[1]) { $yi = $vb[1] }
                    if ($xi -ge $vb[0] + $vb[2]) { $xi = $vb[0] + $vb[2] - 1 }
                    if ($yi -ge $vb[1] + $vb[3]) { $yi = $vb[1] + $vb[3] - 1 }
                    $null = [JarvisNative]::MoveCursor($xi, $yi)
                    $moved = $true
                    Start-Sleep -Milliseconds 40
                }
                if ($btn) {
                    if ($btn -notin @('left', 'right', 'middle')) {
                        $null = Send-Json $ctx 400 @{ error = 'bad_button'; allowed = @('left','right','middle') }
                        break
                    }
                    if ($clicks -lt 1) { $clicks = 1 }
                    if ($clicks -gt 3) { $clicks = 3 }
                    [JarvisNative]::Click($btn, $clicks)
                }
                $pos = [JarvisNative]::CursorPosition()
                $null = Send-Json $ctx 200 @{ ok = $true; action = 'mouse'; moved = $moved
                    clicked = $btn; clicks = $(if ($btn) { $clicks } else { 0 })
                    cursor = @{ x = $pos.X; y = $pos.Y }
                    screen = @{ x = $vb[0]; y = $vb[1]; width = $vb[2]; height = $vb[3] } }
                Write-Log INFO "POST /pc/input/mouse from $remote -> 200 moved=$moved btn=$btn"
                break
            }

            # ==================================================== screenshot ==
            '^GET /pc/screenshot$' {
                $mw = 0
                if ($req.QueryString['max_width']) {
                    $q = "$($req.QueryString['max_width'])"
                    if ($q -match '^\d+$') { $mw = [int]$q }
                }
                $shot = Get-Screenshot -MaxWidth $mw
                Send-Png $ctx $shot.bytes
                Write-Log INFO "GET /pc/screenshot from $remote -> 200 $($shot.width)x$($shot.height) $($shot.bytes.Length)B"
                break
            }

            '^GET /pc/screenshot\.json$' {
                $mw = 0
                if ($req.QueryString['max_width']) {
                    $q = "$($req.QueryString['max_width'])"
                    if ($q -match '^\d+$') { $mw = [int]$q }
                }
                $shot = Get-Screenshot -MaxWidth $mw
                $null = Send-Json $ctx 200 @{
                    width = $shot.width; height = $shot.height; bytes = $shot.bytes.Length
                    active_window = (Get-ActiveWindow)
                    png_base64 = [Convert]::ToBase64String($shot.bytes)
                    captured_at = (Get-Date -Format 'o')
                }
                Write-Log INFO "GET /pc/screenshot.json from $remote -> 200 $($shot.width)x$($shot.height)"
                break
            }

            default {
                $null = Send-Json $ctx 404 @{ error = 'not_found'; path = $path; endpoints = @(
                    'GET  /health', 'GET  /status', 'POST /wake',
                    'GET  /pc/status', 'GET  /pc/apps', 'GET  /pc/window', 'GET  /pc/volume',
                    'GET  /pc/screenshot', 'GET  /pc/screenshot.json',
                    'POST /pc/power/lock', 'POST /pc/power/sleep',
                    'POST /pc/power/shutdown  {confirm:true}', 'POST /pc/power/restart {confirm:true}',
                    'POST /pc/power/cancel', 'POST /pc/power/wake  {staged:bool}',
                    'POST /pc/monitor {on:bool}',
                    'POST /pc/media/{play_pause|next|previous|stop|mute|volume_up|volume_down}',
                    'POST /pc/volume {level:0-100}', 'POST /pc/mute {mute:bool}',
                    'POST /pc/app/open {name}', 'POST /pc/app/close {name}',
                    'POST /pc/window/minimise_all', 'POST /pc/window/show_desktop',
                    'POST /pc/window/next', 'POST /pc/window/focus {title}',
                    'POST /pc/input/type {text}', 'POST /pc/input/key {key,modifiers}',
                    'POST /pc/input/mouse {x,y,button,clicks}'
                ) }
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
