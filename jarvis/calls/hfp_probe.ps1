<#
    hfp_probe.ps1 -- READ-ONLY diagnostic for the JARVIS Bluetooth HFP call path.

    Changes nothing. Pairs nothing. Registers nothing. Dials nothing.
    Run it any time to find out exactly why calling is or is not available.

    Background, established 2026-08-08 by measurement on this machine:
      Windows 11 25H2 implements the HFP *Hands-Free unit* role, not only the
      Audio Gateway role that most public write-ups describe. When an Android
      phone is paired, Windows instantiates a BTHHFENUM\BTHHFPAUDIO node and a
      pair of ordinary WASAPI endpoints carrying the call audio. That is what
      makes a local, provider-free calling path possible at all.

    IMPORTANT DISCOVERY (cost one bug in the first version of this script):
      The phone's HFP-AG UUID 0000111F is NOT exposed as a PnP node in the
      Bluetooth class. The Bluetooth class only shows 00001112 (Headset Audio
      Gateway, the older HSP profile). 0000111F appears ONLY on the device
      INTERFACE under class {BD41DF2D-...}. Detect phones via the interface
      enumeration, never via Get-PnpDevice -Class Bluetooth.

    Exit codes:
      0  every precondition met, calling should work
      1  a precondition failed (details printed, remediation printed)
      2  the probe itself could not run (unexpected error)
#>

[CmdletBinding()]
param(
    # Optional: restrict the report to one phone by Bluetooth MAC, e.g. AC3971D7AE92
    [string]$PhoneMac = '',

    # Emit a single JSON object instead of the human report (used by the bridge).
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# HFP/HSP service UUIDs. Which one a device advertises tells us which ROLE it plays.
#   0000111F = Handsfree Audio Gateway -> remote device is the AG (a PHONE).
#              Windows connects as the HF unit. This is the one we need.
#   00001112 = Headset Audio Gateway   -> same idea, older HSP profile.
#   0000111E = Handsfree (HF)          -> remote device is a HEADSET; Windows is
#              the AG. Useless for placing calls. Kept only to avoid confusion.
$UUID_HFP_AG = '0000111f'
$UUID_HSP_AG = '00001112'
$UUID_HFP_HF = '0000111e'

# Interface class queried by Windows.ApplicationModel.Calls.PhoneLineTransportDevice.
# Confirmed by calling GetDeviceSelector() on this machine, which returned:
#   System.Devices.InterfaceClassGuid:="{BD41DF2D-ADDD-4FC9-A194-B9881D2A2EFA}"
#   AND System.Devices.DeviceInstanceId:~~"{0000111f-0000-1000-8000-00805f9b34fb}"
$IFACE_PHONELINE_TRANSPORT = '{BD41DF2D-ADDD-4FC9-A194-B9881D2A2EFA}'

# MMDevices DeviceState bit values (mmdeviceapi.h)
$DEVSTATE = @{ 1 = 'ACTIVE'; 2 = 'DISABLED'; 4 = 'NOTPRESENT'; 8 = 'UNPLUGGED' }

$script:Failures = @()
$script:Warnings = @()
$script:Passes   = @()

function Write-Head([string]$Text) {
    if ($Json) { return }
    Write-Host ''
    Write-Host $Text -ForegroundColor Cyan
    Write-Host ('-' * $Text.Length) -ForegroundColor Cyan
}
function Add-Fail([string]$What, [string]$Fix) {
    $script:Failures += [pscustomobject]@{ What = $What; Fix = $Fix }
    if (-not $Json) { Write-Host "  FAIL  $What" -ForegroundColor Red }
}
function Add-Warn([string]$What) {
    $script:Warnings += $What
    if (-not $Json) { Write-Host "  WARN  $What" -ForegroundColor Yellow }
}
function Add-Pass([string]$What) {
    $script:Passes += $What
    if (-not $Json) { Write-Host "  PASS  $What" -ForegroundColor Green }
}
function Write-Note([string]$What) {
    if (-not $Json) { Write-Host "  note  $What" -ForegroundColor DarkGray }
}

# Enumerate device interfaces via WinRT. Returns @() on any failure rather than
# throwing, so a broken WinRT stack degrades to a clear FAIL instead of a crash.
function Get-DeviceInterfaces([string]$Aqs) {
    try {
        Add-Type -AssemblyName System.Runtime.WindowsRuntime -ErrorAction Stop | Out-Null
        $null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]
        $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
            Where-Object {
                $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
            })[0]
        $m  = $asTaskGeneric.MakeGenericMethod([Windows.Devices.Enumeration.DeviceInformationCollection])
        $nt = $m.Invoke($null, @([Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($Aqs)))
        if (-not $nt.Wait(20000)) { return @() }
        if ($nt.IsFaulted) { return @() }
        return @($nt.Result)
    } catch {
        return @()
    }
}

try {
    if (-not $Json) {
        Write-Host ''
        Write-Host '=== JARVIS HFP call-path probe (read-only) ===' -ForegroundColor White
        Write-Host ("Host: {0}   Windows: {1}" -f $env:COMPUTERNAME, [System.Environment]::OSVersion.Version)
    }

    # ---------------------------------------------------------------- 1. radio
    Write-Head '1. Bluetooth radio'
    $allBth = @(Get-PnpDevice -Class Bluetooth -ErrorAction SilentlyContinue)
    $radios = @($allBth | Where-Object {
        $_.InstanceId -notlike 'BTHENUM*' -and $_.InstanceId -notlike 'BTHLE*'
    })
    $radioOk = @($radios | Where-Object { $_.Status -eq 'OK' })
    if ($radioOk.Count -eq 0) {
        Add-Fail 'No Bluetooth radio in a working state.' `
                 'Check Settings > Bluetooth & devices, and Device Manager for a disabled or errored adapter.'
    } else {
        foreach ($r in $radioOk) { Add-Pass ("Radio present: {0}" -f $r.FriendlyName) }
    }

    $bthserv = Get-Service -Name 'bthserv' -ErrorAction SilentlyContinue
    if ($null -eq $bthserv) {
        Add-Fail 'Bluetooth Support Service (bthserv) not installed.' 'Reinstall the Bluetooth drivers.'
    } elseif ($bthserv.Status -ne 'Running') {
        Add-Fail ("bthserv is {0}, expected Running." -f $bthserv.Status) 'Run: Start-Service bthserv'
    } else {
        Add-Pass 'bthserv (Bluetooth Support Service) is Running.'
    }

    # ------------------------------------------------------- 2. paired phone(s)
    # Authoritative source is the DEVICE INTERFACE enumeration, not the PnP class.
    Write-Head '2. Paired device offering Handsfree Audio Gateway (i.e. a phone)'
    $ifaceAqs = 'System.Devices.DevObjectType:=8 AND System.Devices.InterfaceClassGuid:="' +
                $IFACE_PHONELINE_TRANSPORT + '"'
    $ifaces = Get-DeviceInterfaces $ifaceAqs

    $phones = @()
    foreach ($d in $ifaces) {
        $isAg = ($d.Id -match [regex]::Escape($UUID_HFP_AG)) -or ($d.Id -match [regex]::Escape($UUID_HSP_AG))
        if (-not $isAg) { continue }
        if ($PhoneMac -and ($d.Id -notmatch [regex]::Escape($PhoneMac))) { continue }
        $mac = if ($d.Id -match '&([0-9A-Fa-f]{12})_C') { $Matches[1].ToUpper() } else { '<unknown>' }
        $phones += [pscustomobject]@{
            Name       = $d.Name
            Mac        = $mac
            Id         = $d.Id
            IsEnabled  = [bool]$d.IsEnabled
        }
    }

    if ($ifaces.Count -eq 0) {
        Add-Fail 'Could not enumerate PhoneLineTransportDevice interfaces at all.' `
                 'The WinRT Calls API is unavailable. Confirm this is Windows 10 1903 or newer.'
    } elseif ($phones.Count -eq 0) {
        Add-Fail 'No paired device offers Handsfree Audio Gateway (0000111F / 00001112).' `
                 ('Pair the phone in Windows Settings > Bluetooth, then on the PHONE open the paired-device ' +
                  'settings for this PC and enable the "Phone calls" / "Phone audio" profile.')
    } else {
        foreach ($p in $phones) {
            Add-Pass ("Phone offers HFP Audio Gateway: {0}  (MAC {1})" -f $p.Name, $p.Mac)
        }
    }

    # Headsets reported only so the two roles are never confused during debugging.
    foreach ($d in $ifaces) {
        if ($d.Id -match [regex]::Escape($UUID_HFP_HF)) {
            Write-Note ("Headset, not a phone (Windows is the AG here, cannot place calls): {0}" -f $d.Name)
        }
    }

    # ------------------------------------- 3. is the phone actually CONNECTED?
    Write-Head '3. Live connection state'
    $connectedAny = $false
    foreach ($p in $phones) {
        if ($p.IsEnabled) {
            $connectedAny = $true
            Add-Pass ("{0} is CONNECTED and its transport interface is enabled." -f $p.Name)
        } else {
            Add-Warn ("{0} is paired but NOT currently connected." -f $p.Name)
        }
    }
    if ($phones.Count -gt 0 -and -not $connectedAny) {
        Add-Fail 'No paired phone is connected right now.' `
                 ('Bring the phone into Bluetooth range (roughly 10 m, same room), turn its Bluetooth on, ' +
                  'and connect it to this PC. Calls cannot be placed or answered otherwise.')
    }

    # -------------------------------- 4. PhoneLineTransportDevice reachability
    Write-Head '4. Call-control handle (PhoneLineTransportDevice)'
    if ($phones.Count -eq 0) {
        Add-Warn 'Skipped: no phone found in step 2.'
    } else {
        foreach ($p in $phones) {
            if (-not $p.IsEnabled) {
                Add-Warn ("Transport present but disabled while the phone is offline: {0}" -f $p.Name)
                continue
            }
            try {
                $null = [Windows.ApplicationModel.Calls.PhoneLineTransportDevice, Windows.ApplicationModel.Calls, ContentType = WindowsRuntime]
                $dev = [Windows.ApplicationModel.Calls.PhoneLineTransportDevice]::FromId($p.Id)
                if ($null -eq $dev) {
                    Add-Fail ("FromId returned null for {0} even though it is connected." -f $p.Name) `
                             'Disconnect and reconnect the phone, then re-run this probe.'
                } else {
                    Add-Pass ("Transport handle obtained for {0}." -f $p.Name)
                    try   { Add-Pass ("  registered with Windows: {0}" -f $dev.IsRegistered()) }
                    catch { Add-Warn ("  IsRegistered() failed: {0}" -f $_.Exception.Message) }
                    try   { Add-Pass ("  audio routing status: {0}" -f $dev.AudioRoutingStatus) }
                    catch { Add-Warn ("  AudioRoutingStatus failed: {0}" -f $_.Exception.Message) }
                }
            } catch {
                Add-Fail ("Could not obtain a transport handle: {0}" -f $_.Exception.Message) `
                         'Re-run with the phone connected. If it persists, the Calls API is blocked on this build.'
            }
        }
    }

    # ------------------------------------------------- 5. call-audio endpoints
    Write-Head '5. Call audio endpoints (JARVIS needs these to hear and speak)'
    if ($phones.Count -eq 0) {
        Add-Warn 'Skipped: no phone found in step 2.'
    } else {
        foreach ($dir in @('Render', 'Capture')) {
            $base = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\$dir"
            $hits = @()
            Get-ChildItem $base -ErrorAction SilentlyContinue | ForEach-Object {
                $props = Get-ItemProperty (Join-Path $_.PSPath 'Properties') -ErrorAction SilentlyContinue
                if ($null -eq $props) { return }
                $desc  = $props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},6'
                $iface = $props.'{b3f8fa53-0004-438e-9003-51a46e139bfc},2'
                if ($null -eq $desc -or $null -eq $iface) { return }
                if ($iface -notlike '*BTHHFENUM\BTHHFPAUDIO*') { return }
                # Only endpoints belonging to a detected PHONE, never a headset.
                $belongs = $false
                foreach ($p in $phones) { if ($desc -like ("*{0}*" -f $p.Name)) { $belongs = $true } }
                if (-not $belongs) { return }
                $stateRaw = (Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue).DeviceState
                $stateTxt = if ($DEVSTATE.ContainsKey([int]$stateRaw)) { $DEVSTATE[[int]$stateRaw] } else { "0x{0:X}" -f $stateRaw }
                $hits += [pscustomobject]@{ Desc = $desc; State = $stateTxt }
            }
            if ($hits.Count -eq 0) {
                Add-Fail ("No HFP {0} endpoint exists for the phone." -f $dir) `
                         ('Connect the phone once with the "Phone audio" profile enabled so Windows creates ' +
                          'the endpoint, then re-run.')
            } else {
                foreach ($h in $hits) {
                    if ($h.State -eq 'ACTIVE') {
                        Add-Pass ("{0}: {1} [{2}]" -f $dir, $h.Desc, $h.State)
                    } else {
                        Add-Warn ("{0}: {1} [{2}] -- becomes ACTIVE when the phone connects." -f $dir, $h.Desc, $h.State)
                    }
                }
            }
        }
    }

    # ------------------------------------------------------- 6. contention check
    Write-Head '6. Contention (only one app may hold the HFP registration)'
    $yourPhone = @(Get-AppxPackage -Name 'Microsoft.YourPhone' -ErrorAction SilentlyContinue)
    if ($yourPhone.Count -gt 0) {
        Add-Warn ('Phone Link is installed. It uses the same HFP APIs and may hold the registration. ' +
                  'If dialling fails with a state error, close Phone Link completely and retry.')
    } else {
        Add-Pass 'Phone Link is not installed; nothing competes for the HFP registration.'
    }

    # ------------------------------------------------------------- 7. verdict
    $ready = ($script:Failures.Count -eq 0)

    if ($Json) {
        [pscustomobject]@{
            ready    = $ready
            phones   = @($phones | ForEach-Object { @{ name = $_.Name; mac = $_.Mac; connected = $_.IsEnabled } })
            passes   = $script:Passes
            warnings = $script:Warnings
            failures = @($script:Failures | ForEach-Object { @{ what = $_.What; fix = $_.Fix } })
        } | ConvertTo-Json -Depth 6
        if ($ready) { exit 0 } else { exit 1 }
    }

    Write-Head 'Verdict'
    if ($ready) {
        Write-Host '  READY -- every precondition for placing a call is met.' -ForegroundColor Green
        if ($script:Warnings.Count -gt 0) {
            Write-Host ("  ({0} warning(s) above are informational.)" -f $script:Warnings.Count) -ForegroundColor Yellow
        }
        exit 0
    }

    Write-Host ("  NOT READY -- {0} blocking problem(s):" -f $script:Failures.Count) -ForegroundColor Red
    $i = 1
    foreach ($f in $script:Failures) {
        Write-Host ("   {0}. {1}" -f $i, $f.What) -ForegroundColor Red
        Write-Host ("      fix: {0}" -f $f.Fix) -ForegroundColor DarkYellow
        $i++
    }
    exit 1

} catch {
    if ($Json) {
        [pscustomobject]@{ ready = $false; error = $_.Exception.Message } | ConvertTo-Json
    } else {
        Write-Host ''
        Write-Host ("PROBE ERROR: {0}" -f $_.Exception.Message) -ForegroundColor Magenta
        Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    }
    exit 2
}
