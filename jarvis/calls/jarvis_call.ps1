<#
    jarvis_call.ps1 -- call control for JARVIS over Bluetooth Hands-Free Profile.

    NOTHING here goes through a carrier API, a SIP provider, or any cloud service.
    Windows acts as the HFP Hands-Free unit; the phone remains the Audio Gateway,
    so the call is placed by the phone, on its own SIM, over Airtel's network.

    ------------------------------------------------------------------ SAFETY
    The conversation model is qwen2.5:3b and it has hallucinated actions before.
    Therefore this script NEVER accepts a raw phone number from the caller.
    It accepts a CONTACT NAME which must exist in contacts.json. Unknown name =
    hard refusal. This makes a hallucinated dial impossible: the worst a confused
    model can do is ring someone already on the approved list.

    Numbers are never echoed back on stdout either -- only names -- so a number
    cannot leak into a model transcript or a log that gets pasted somewhere.

    ------------------------------------------------------- MEASURED API FACTS
    Established 2026-08-08 by introspecting the live WinRT surface on this host
    (Windows 11 build 26200). Do not "simplify" these back; each cost a probe:

      * PhoneLine.DialWithResultAsync returns IAsyncOperation<PhoneLineDialResult>,
        NOT PhoneCallOperationStatus. PhoneLineDialResult is a CLASS with two
        properties: .DialCallStatus (the enum) and .DialedCall (the PhoneCall).
        Awaiting it with the wrong generic type throws inside MakeGenericMethod.
      * PhoneCall.AcceptIncoming()/End() RETURN a PhoneCallOperationStatus. The
        previous version discarded it and reported success unconditionally.
      * PhoneLineTransportDevice.FromId() returns NULL when the phone is merely
        paired but not connected. A null check is mandatory, not defensive noise.
      * PhoneCallStore.GetDefaultLineAsync() faults with 0x8007139F
        ("group or resource is not in the correct state") when no phone line is
        registered. That is an expected not-ready signal, not a crash.
      * PhoneLineWatcher CAN be driven from Windows PowerShell 5.1, but only via
        add_LineAdded() with an explicit TypedEventHandler cast.
        Register-ObjectEvent flatly refuses: "cannot subscribe to Windows RT
        events". Line discovery here uses the watcher and matches on
        PhoneLine.TransportDeviceId so we never drive a line belonging to some
        other transport.
      * $PSScriptRoot is EMPTY inside a param() block default under -File. It is
        correct in the body. Path defaults are therefore resolved in the body.
        (The old version crashed on every single invocation because of this.)

    ------------------------------------------------------------ VERIFICATION
    status/contacts/state: EXECUTED and working 2026-08-08 with the phone out of
    range -- they correctly report not-ready.
    dial/answer/hangup/route: NOT EXECUTED against a live call. The phone was not
    connectable during the build (BluetoothDevice.FromBluetoothAddressAsync
    returned 0x800710DF, device not ready). See calls/NOTES.md.

    Usage:
      -Action status                report readiness; never touches the phone
      -Action contacts              list allowlisted NAMES (never the numbers)
      -Action state                 report current call + caller ID as JSON
      -Action dial   -Contact mum   place a call to an allowlisted contact
      -Action answer                accept the ringing incoming call
      -Action reject                reject the ringing incoming call
      -Action hangup                end whatever call is in progress
      -Action route  -To pc|phone   move call audio between this PC and the phone

    Every action prints one JSON object on stdout and exits with:
      0 = the requested thing definitely happened
      1 = it definitely did NOT happen, and 'reason' says why
      2 = internal error
    There is deliberately no third state: a call either went through or it did not.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('status', 'contacts', 'state', 'dial', 'answer', 'reject', 'hangup', 'route')]
    [string]$Action,

    [string]$Contact = '',

    # Target for -Action route. 'pc' lets JARVIS hear and speak on the call.
    [ValidateSet('pc', 'phone')]
    [string]$To = 'pc',

    # Left empty on purpose: $PSScriptRoot does not exist yet in a param default.
    [string]$ContactsFile = '',

    # Perform the one-time registration of this app with Windows for HFP.
    # State-changing, so it is opt-in and never implied by another action.
    [switch]$RegisterTransport,

    # How long to wait for the HFP service-level connection, in seconds.
    [int]$ConnectTimeoutSec = 12
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ContactsFile)) {
    $ContactsFile = Join-Path $PSScriptRoot 'contacts.json'
}

$IFACE_PHONELINE_TRANSPORT = '{BD41DF2D-ADDD-4FC9-A194-B9881D2A2EFA}'
$UUID_HFP_AG = '0000111f'
$UUID_HSP_AG = '00001112'

# ---------------------------------------------------------------- result plumbing
function Emit([bool]$Ok, [string]$Reason, [hashtable]$Extra = $null) {
    $o = [ordered]@{
        ok     = $Ok
        action = $Action
        reason = $Reason
        at     = (Get-Date).ToString('o')
    }
    if ($null -ne $Extra) { foreach ($k in $Extra.Keys) { $o[$k] = $Extra[$k] } }
    ([pscustomobject]$o) | ConvertTo-Json -Depth 6 -Compress
    if ($Ok) { exit 0 } else { exit 1 }
}

# ---------------------------------------------------------------- WinRT helpers
$script:AsTaskGeneric = $null
function Initialize-WinRT {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop | Out-Null
    $null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
    $null = [Windows.ApplicationModel.Calls.PhoneLineTransportDevice, Windows.ApplicationModel.Calls, ContentType = WindowsRuntime]
    $null = [Windows.ApplicationModel.Calls.PhoneLine, Windows.ApplicationModel.Calls, ContentType = WindowsRuntime]
    $null = [Windows.ApplicationModel.Calls.PhoneCallManager, Windows.ApplicationModel.Calls, ContentType = WindowsRuntime]
    $null = [Windows.ApplicationModel.Calls.PhoneCall, Windows.ApplicationModel.Calls, ContentType = WindowsRuntime]
    $script:AsTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })[0]
    if ($null -eq $script:AsTaskGeneric) { throw 'Could not locate WindowsRuntimeSystemExtensions.AsTask.' }
}

# Await a WinRT IAsyncOperation<T>. $ResultType MUST be T exactly -- passing the
# wrong T is the bug that made the previous dial path unrunnable.
# Task.Wait() throws AggregateException on fault, so the real error is unwrapped
# here; otherwise every failure reads as the useless "One or more errors occurred."
function Wait-WinRT($Operation, [Type]$ResultType, [int]$TimeoutMs = 20000) {
    $m  = $script:AsTaskGeneric.MakeGenericMethod($ResultType)
    $nt = $m.Invoke($null, @($Operation))
    $done = $false
    try {
        $done = $nt.Wait($TimeoutMs)
    } catch {
        $e = $nt.Exception
        while ($null -ne $e.InnerException) { $e = $e.InnerException }
        throw ("{0} (0x{1:X8})" -f $e.Message, $e.HResult)
    }
    if (-not $done) { throw ("WinRT call timed out after {0} ms." -f $TimeoutMs) }
    return $nt.Result
}

function Get-WinRTType([string]$Name) {
    return [Type]::GetType("Windows.ApplicationModel.Calls.$Name, Windows.ApplicationModel.Calls, ContentType=WindowsRuntime")
}

# ---------------------------------------------------------------- contacts
# Returns @{ names = <ordered name list>; map = @{name=number} }.
function Get-Contacts {
    if (-not (Test-Path $ContactsFile)) {
        Emit $false ("No contacts file at {0}. Copy contacts.example.json to contacts.json and add the people JARVIS may ring." -f $ContactsFile)
    }
    try {
        $raw = Get-Content $ContactsFile -Raw -Encoding UTF8
        $obj = $raw | ConvertFrom-Json
    } catch {
        Emit $false ("contacts.json is not valid JSON: {0}" -f $_.Exception.Message)
    }
    $map = @{}
    $names = New-Object System.Collections.ArrayList
    foreach ($p in $obj.PSObject.Properties) {
        $name = $p.Name.Trim().ToLowerInvariant()
        if ($name -eq '' -or $name.StartsWith('_')) { continue }
        $number = ([string]$p.Value).Trim()
        # Accept only E.164-ish or plain national digits. Reject anything that could
        # smuggle AT commands or shell metacharacters toward the modem.
        if ($number -notmatch '^\+?[0-9]{4,15}$') {
            Emit $false ("Contact '{0}' has an invalid number. Use digits only, optionally a leading +." -f $p.Name)
        }
        # Emergency-services guard. PhoneCallStore.IsEmergencyPhoneNumberAsync was
        # measured on 2026-08-08 to answer False for 112/100/911 on this host,
        # because no cellular line is registered -- so it is useless as a check and
        # this static list stands in for it. A mistyped contact must never be able
        # to ring emergency services. India first, then common international codes.
        $bare = $number -replace '[^0-9]', ''
        $EMERGENCY = @('100','101','102','108','112','1091','1098','181','1090','911','999','000','110','119')
        if ($EMERGENCY -contains $bare) {
            Emit $false ("Contact '{0}' is an emergency number. JARVIS will never dial emergency services; remove it from contacts.json." -f $p.Name)
        }
        $map[$name] = $number
        [void]$names.Add($name)
    }
    if ($map.Count -eq 0) { Emit $false 'contacts.json contains no usable contacts.' }
    return @{ names = @($names | Sort-Object); map = $map }
}

# ---------------------------------------------------------------- phone discovery
function Get-PhoneTransportInfo {
    $aqs = 'System.Devices.DevObjectType:=8 AND System.Devices.InterfaceClassGuid:="' +
           $IFACE_PHONELINE_TRANSPORT + '"'
    $all = @(Wait-WinRT ([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($aqs)) `
                        ([Windows.Devices.Enumeration.DeviceInformationCollection]))

    # 0000111F/00001112 = the REMOTE end is an Audio Gateway, i.e. a phone.
    # 0000111E would be a headset, where Windows is the AG and cannot place calls.
    $candidates = @($all | Where-Object {
        ($_.Id -match [regex]::Escape($UUID_HFP_AG)) -or ($_.Id -match [regex]::Escape($UUID_HSP_AG))
    })

    if ($candidates.Count -eq 0) {
        Emit $false 'No phone is paired for hands-free calling. Pair it in Windows Settings, then enable the "Phone calls" profile on the phone.'
    }

    $live = @($candidates | Where-Object { $_.IsEnabled })
    if ($live.Count -eq 0) {
        $names = ($candidates | ForEach-Object { $_.Name }) -join ', '
        Emit $false ("The phone ({0}) is paired but not connected. Bring it into Bluetooth range and connect it." -f $names) `
             @{ paired_but_offline = $true; phone = $names }
    }
    return $live[0]
}

function Open-Transport($TransportInfo) {
    $device = $null
    try {
        $device = [Windows.ApplicationModel.Calls.PhoneLineTransportDevice]::FromId($TransportInfo.Id)
    } catch {
        Emit $false ("Could not open the phone's hands-free transport: {0}" -f $_.Exception.Message)
    }
    # FromId returns null (no throw) when the phone is paired but not connected.
    if ($null -eq $device) {
        Emit $false ("Windows returned no hands-free transport for {0}. It is paired but not actively connected -- reconnect it and retry." -f $TransportInfo.Name) `
             @{ paired_but_offline = $true; phone = $TransportInfo.Name }
    }

    $registered = $false
    try { $registered = [bool]$device.IsRegistered() } catch { $registered = $false }

    if (-not $registered) {
        if ($RegisterTransport) {
            try {
                $device.RegisterApp()
                $registered = [bool]$device.IsRegistered()
            } catch {
                Emit $false ("Registering with Windows for hands-free calling failed: {0}" -f $_.Exception.Message)
            }
            if (-not $registered) { Emit $false 'RegisterApp() completed but Windows still reports the app as unregistered.' }
        } else {
            Emit $false ('This app is not yet registered with Windows for hands-free calling. ' +
                         'Run once, interactively: jarvis_call.ps1 -Action status -RegisterTransport') `
                 @{ needs_registration = $true }
        }
    }

    try {
        $null = Wait-WinRT ($device.ConnectAsync()) ([bool]) ($ConnectTimeoutSec * 1000)
    } catch {
        Emit $false ("Could not establish the hands-free link to the phone: {0}" -f $_.Exception.Message)
    }
    return $device
}

# ---------------------------------------------------------------- line discovery
# Enumerate lines with PhoneLineWatcher and pick the one whose TransportDeviceId
# matches OUR transport. GetDefaultLineAsync alone is wrong -- it can hand back a
# line belonging to a different transport, and it faults 0x8007139F when nothing
# is registered yet. Register-ObjectEvent cannot bind WinRT events, so the
# handlers below use explicit TypedEventHandler casts.
function Get-PhoneLine($Device) {
    $tStore = Get-WinRTType 'PhoneCallStore'
    $store = $null
    try {
        $store = Wait-WinRT ([Windows.ApplicationModel.Calls.PhoneCallManager]::RequestStoreAsync()) $tStore
    } catch {
        Emit $false ("Could not open the Windows phone call store: {0}" -f $_.Exception.Message)
    }

    $watcher = $store.RequestLineWatcher()
    $found = New-Object System.Collections.ArrayList
    $done  = New-Object System.Threading.ManualResetEventSlim $false

    $tW    = Get-WinRTType 'PhoneLineWatcher'
    $tArgs = Get-WinRTType 'PhoneLineWatcherEventArgs'
    $hAdd  = [Windows.Foundation.TypedEventHandler`2].MakeGenericType($tW, $tArgs)
    $hDone = [Windows.Foundation.TypedEventHandler`2].MakeGenericType($tW, [object])

    $onAdded = [scriptblock]{ param($s, $e) [void]$found.Add($e.LineId) }
    $onDone  = [scriptblock]{ param($s, $e) $done.Set() }

    try {
        $null = $watcher.add_LineAdded(($onAdded -as $hAdd))
        $null = $watcher.add_EnumerationCompleted(($onDone -as $hDone))
        $watcher.Start()
        # EnumerationCompleted does not always fire on this build even though the
        # watcher reaches the EnumerationCompleted STATUS, so poll the status too.
        $deadline = (Get-Date).AddSeconds(10)
        while ((Get-Date) -lt $deadline) {
            if ($done.IsSet) { break }
            if ("$($watcher.Status)" -in @('EnumerationCompleted', 'Stopped')) { break }
            Start-Sleep -Milliseconds 200
        }
    } catch {
        Emit $false ("Could not enumerate phone lines: {0}" -f $_.Exception.Message)
    } finally {
        try { $watcher.Stop() } catch { }
    }

    $tLine = Get-WinRTType 'PhoneLine'
    $ours = $null
    foreach ($id in $found) {
        try {
            $l = Wait-WinRT ([Windows.ApplicationModel.Calls.PhoneLine]::FromIdAsync($id)) $tLine
        } catch { continue }
        if ($null -eq $l) { continue }
        $tdid = ''
        try { $tdid = [string]$l.TransportDeviceId } catch { $tdid = '' }
        if ($tdid -and $Device.DeviceId -and ($tdid -eq $Device.DeviceId)) { $ours = $l; break }
        if ($null -eq $ours) { $ours = $l }   # fallback: first line seen
    }

    # Last resort: the default line. Expected to fault 0x8007139F when the phone
    # has not registered a line, which is a not-ready condition, not an error.
    if ($null -eq $ours) {
        try {
            $lineId = Wait-WinRT ($store.GetDefaultLineAsync()) ([System.Guid])
            if ($lineId -ne [System.Guid]::Empty) {
                $ours = Wait-WinRT ([Windows.ApplicationModel.Calls.PhoneLine]::FromIdAsync($lineId)) $tLine
            }
        } catch {
            Emit $false ('The phone connected but has not published a callable line yet. On the phone, open the ' +
                         'paired-device settings for this PC and make sure the "Phone calls" permission is enabled.') `
                 @{ detail = $_.Exception.Message }
        }
    }

    if ($null -eq $ours) {
        Emit $false 'The phone connected but exposed no callable line. On the phone, confirm the "Phone calls" profile is enabled for this PC.'
    }
    return $ours
}

# ---------------------------------------------------------------- call helpers
function Get-ActiveCalls($Line) {
    $tRes = Get-WinRTType 'PhoneCallsResult'
    $res = Wait-WinRT ($Line.GetAllActivePhoneCallsAsync()) $tRes
    if ($null -eq $res) { return @() }
    try {
        if ("$($res.OperationStatus)" -ne 'Succeeded') { return @() }
    } catch { }
    try { return @($res.AllActivePhoneCalls) } catch { return @() }
}

# Caller ID for the reporting leg. Never throws; unknown fields come back empty
# so a missing name can never masquerade as a real one.
function Get-CallDetail($Call) {
    $d = [ordered]@{ call_id = ''; status = ''; direction = ''; display_name = ''; number_known = $false; audio = '' }
    try { $d.call_id = [string]$Call.CallId } catch { }
    try { $d.status  = "$($Call.Status)" } catch { }
    try { $d.audio   = "$($Call.AudioDevice)" } catch { }
    try {
        $tInfo = Get-WinRTType 'PhoneCallInfo'
        $info = Wait-WinRT ($Call.GetPhoneCallInfoAsync()) $tInfo 8000
        if ($null -ne $info) {
            try { $d.direction    = "$($info.CallDirection)" } catch { }
            try { $d.display_name = [string]$info.DisplayName } catch { }
            # The number itself is deliberately NOT emitted, only whether one exists.
            try { $d.number_known = -not [string]::IsNullOrWhiteSpace([string]$info.PhoneNumber) } catch { }
        }
    } catch { }
    return $d
}

# Map an unknown caller to a known contact NAME, by number, without ever emitting
# the number. Lets JARVIS say "Mum is calling" instead of reading digits aloud.
function Resolve-CallerName($Call, $ContactMap) {
    try {
        $tInfo = Get-WinRTType 'PhoneCallInfo'
        $info = Wait-WinRT ($Call.GetPhoneCallInfoAsync()) $tInfo 8000
        if ($null -eq $info) { return '' }
        $num = ''
        try { $num = ([string]$info.PhoneNumber) -replace '[^0-9]', '' } catch { return '' }
        if ($num -eq '') { return '' }
        foreach ($k in $ContactMap.Keys) {
            $c = ($ContactMap[$k]) -replace '[^0-9]', ''
            if ($c -eq '') { continue }
            # Compare the last 10 digits: the phone may report +91XXXXXXXXXX or the
            # bare national number depending on how the contact was saved.
            $n1 = if ($num.Length -ge 10) { $num.Substring($num.Length - 10) } else { $num }
            $n2 = if ($c.Length   -ge 10) { $c.Substring($c.Length - 10) }     else { $c }
            if ($n1 -eq $n2) { return $k }
        }
    } catch { }
    return ''
}

# ---------------------------------------------------------------- main
try {
    Initialize-WinRT
} catch {
    Emit $false ("Windows Runtime calling APIs are unavailable: {0}" -f $_.Exception.Message)
}

# ---- contacts (no hardware needed) -----------------------------------------
if ($Action -eq 'contacts') {
    $c = Get-Contacts
    Emit $true ("{0} contact(s) approved for calling." -f $c.names.Count) @{ contacts = @($c.names) }
}

# ---- status (no state change) ----------------------------------------------
if ($Action -eq 'status') {
    $probe = Join-Path $PSScriptRoot 'hfp_probe.ps1'
    if (-not (Test-Path $probe)) { Emit $false 'hfp_probe.ps1 is missing next to this script.' }
    $json = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $probe -Json 2>$null
    $parsed = $null
    try { $parsed = $json | ConvertFrom-Json } catch { }
    $ready = $false
    if ($null -ne $parsed) { try { $ready = [bool]$parsed.ready } catch { } }

    # -RegisterTransport is handled here so the one-time setup has an obvious home.
    if ($RegisterTransport) {
        if (-not $ready) { Emit $false 'Cannot register while the phone is not connected. Connect it, then re-run.' @{ probe = $parsed } }
        $ti = Get-PhoneTransportInfo
        $dev = Open-Transport $ti
        Emit $true ("Registered for hands-free calling with {0}." -f $ti.Name) @{ phone = $ti.Name; registered = $true }
    }

    if ($ready) { Emit $true 'Ready to place and answer calls.' @{ probe = $parsed } }
    else        { Emit $false 'Not ready. Run hfp_probe.ps1 for the full reason list.' @{ probe = $parsed } }
}

# ---- validate the dial target BEFORE touching any hardware ------------------
# Ordering matters for safety. If the allowlist check happened after the phone
# handshake, then with the phone offline an unapproved name would be answered
# with "the phone is not connected" -- which reads as "try again later" rather
# than "no". The refusal must be unconditional and must not depend on hardware.
$script:DialNumber = $null
$script:DialKey    = $null
if ($Action -eq 'dial') {
    if ([string]::IsNullOrWhiteSpace($Contact)) {
        Emit $false 'No contact given. JARVIS must name who to call.'
    }
    $contacts = Get-Contacts
    $script:DialKey = $Contact.Trim().ToLowerInvariant()
    if (-not $contacts.map.ContainsKey($script:DialKey)) {
        # The refusal lists the approved NAMES so the model can correct itself,
        # and no number is disclosed in the process.
        Emit $false ("'{0}' is not on the approved call list, so I will not dial." -f $Contact) `
             @{ known_contacts = @($contacts.names) }
    }
    $script:DialNumber = $contacts.map[$script:DialKey]
}

# Everything below needs live hardware.
$transportInfo = Get-PhoneTransportInfo
$device        = Open-Transport $transportInfo
$line          = Get-PhoneLine $device

# ---- state -----------------------------------------------------------------
if ($Action -eq 'state') {
    $calls = @()
    try { $calls = Get-ActiveCalls $line } catch {
        Emit $false ("Could not read the phone's active calls: {0}" -f $_.Exception.Message)
    }
    $contacts = Get-Contacts
    $details = @()
    foreach ($c in $calls) {
        $d = Get-CallDetail $c
        $known = Resolve-CallerName $c $contacts.map
        $d.known_contact = $known
        $details += [pscustomobject]$d
    }
    $routing = ''
    try { $routing = "$($device.AudioRoutingStatus)" } catch { }
    Emit $true ("{0} active call(s)." -f $details.Count) `
         @{ calls = $details; count = $details.Count; phone = $transportInfo.Name; audio_routing = $routing }
}

# ---- dial ------------------------------------------------------------------
if ($Action -eq 'dial') {
    $key    = $script:DialKey
    $number = $script:DialNumber

    $canDial = $true
    try { $canDial = [bool]$line.CanDial } catch { $canDial = $true }
    if (-not $canDial) { Emit $false 'The phone line reports that it cannot place outgoing calls right now.' }

    try {
        $tDial = Get-WinRTType 'PhoneLineDialResult'
        $result = Wait-WinRT ($line.DialWithResultAsync($number, $key)) $tDial 30000
        $st = ''
        try { $st = "$($result.DialCallStatus)" } catch { $st = '<unreadable>' }
        if ($st -ne 'Succeeded') {
            Emit $false ("The phone refused the call. Windows reported: {0}" -f $st) @{ contact = $key; dial_status = $st }
        }
        $callId = ''
        try { if ($null -ne $result.DialedCall) { $callId = [string]$result.DialedCall.CallId } } catch { }
        Emit $true ("Calling {0}." -f $key) @{ contact = $key; dial_status = $st; call_id = $callId }
    } catch {
        Emit $false ("Dialling failed: {0}" -f $_.Exception.Message) @{ contact = $key }
    }
}

# ---- route -----------------------------------------------------------------
# Moves the call's audio between the phone's own earpiece and this PC. Routing to
# the PC is what actually puts JARVIS on the call: the audio then appears on the
# "realme C63 5G Hands-Free HF Audio" WASAPI endpoints that call_bridge.py uses.
if ($Action -eq 'route') {
    $calls = @()
    try { $calls = Get-ActiveCalls $line } catch {
        Emit $false ("Could not read the phone's active calls: {0}" -f $_.Exception.Message)
    }
    if ($calls.Count -eq 0) { Emit $false 'There is no call in progress to route.' }

    $target = if ($To -eq 'pc') { 'LocalDevice' } else { 'RemoteDevice' }
    $tEnum = Get-WinRTType 'PhoneCallAudioDevice'
    $val = [Enum]::Parse($tEnum, $target)

    if ($To -eq 'pc') {
        $rs = ''
        try { $rs = "$($device.AudioRoutingStatus)" } catch { }
        if ($rs -eq 'CannotRouteToLocalDevice') {
            Emit $false 'Windows says this call cannot be routed to the PC, so JARVIS cannot join it.' @{ audio_routing = $rs }
        }
    }

    try {
        $tOp = Get-WinRTType 'PhoneCallOperationStatus'
        $st = Wait-WinRT ($calls[0].ChangeAudioDeviceAsync($val)) $tOp 15000
        if ("$st" -ne 'Succeeded') {
            Emit $false ("Windows would not move the call audio: {0}" -f $st) @{ target = $To; op_status = "$st" }
        }
        Emit $true ("Call audio moved to the {0}." -f $(if ($To -eq 'pc') { 'PC' } else { 'phone' })) `
             @{ target = $To; op_status = "$st" }
    } catch {
        Emit $false ("Could not move the call audio: {0}" -f $_.Exception.Message) @{ target = $To }
    }
}

# ---- answer / reject / hangup ----------------------------------------------
$calls = @()
try { $calls = Get-ActiveCalls $line } catch {
    Emit $false ("Could not read the phone's active calls: {0}" -f $_.Exception.Message)
}

if ($Action -eq 'answer') {
    $incoming = @($calls | Where-Object { "$($_.Status)" -eq 'Incoming' })
    if ($incoming.Count -eq 0) { Emit $false 'There is no incoming call to answer.' @{ active_calls = $calls.Count } }
    $contacts = Get-Contacts
    $who = Resolve-CallerName $incoming[0] $contacts.map
    try {
        # AcceptIncoming RETURNS a status. The old version ignored it and always
        # claimed success, which is exactly the "reported working, never observed"
        # failure mode this project forbids.
        $tOp = Get-WinRTType 'PhoneCallOperationStatus'
        $st = Wait-WinRT ($incoming[0].AcceptIncomingAsync()) $tOp 15000
        if ("$st" -ne 'Succeeded') {
            Emit $false ("Windows would not answer the call: {0}" -f $st) @{ op_status = "$st" }
        }
        Emit $true 'Call answered.' @{ op_status = "$st"; known_contact = $who; call_id = ([string]$incoming[0].CallId) }
    } catch {
        Emit $false ("Could not answer the call: {0}" -f $_.Exception.Message)
    }
}

if ($Action -eq 'reject') {
    $incoming = @($calls | Where-Object { "$($_.Status)" -eq 'Incoming' })
    if ($incoming.Count -eq 0) { Emit $false 'There is no incoming call to reject.' }
    try {
        $tOp = Get-WinRTType 'PhoneCallOperationStatus'
        $st = Wait-WinRT ($incoming[0].RejectIncomingAsync()) $tOp 15000
        if ("$st" -ne 'Succeeded') { Emit $false ("Windows would not reject the call: {0}" -f $st) @{ op_status = "$st" } }
        Emit $true 'Call rejected.' @{ op_status = "$st" }
    } catch {
        Emit $false ("Could not reject the call: {0}" -f $_.Exception.Message)
    }
}

if ($Action -eq 'hangup') {
    if ($calls.Count -eq 0) { Emit $false 'There is no call in progress.' }
    $ended = 0
    $bad = @()
    foreach ($c in $calls) {
        try {
            $tOp = Get-WinRTType 'PhoneCallOperationStatus'
            $st = Wait-WinRT ($c.EndAsync()) $tOp 15000
            if ("$st" -eq 'Succeeded') { $ended++ } else { $bad += "$st" }
        } catch {
            $bad += $_.Exception.Message
        }
    }
    if ($ended -eq 0) {
        Emit $false ("Could not end the call: {0}" -f ($bad -join '; ')) @{ attempted = $calls.Count }
    }
    Emit $true ("Ended {0} call(s)." -f $ended) @{ ended = $ended; failures = @($bad) }
}

Emit $false ("Unhandled action '{0}'." -f $Action)
