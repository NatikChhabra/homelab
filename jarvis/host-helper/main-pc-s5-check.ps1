# main-pc-s5-check.ps1
#
# RUN THIS ON NATIK'S MAIN PC (192.168.29.167), NOT on the server.
# Copy it over, open PowerShell AS ADMINISTRATOR, then:
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\main-pc-s5-check.ps1
#
# WHAT IT IS
# Strictly READ-ONLY. It reads settings and prints them. It changes nothing,
# installs nothing, and never shuts the machine down. Every finding is printed
# with the raw value so you can judge it yourself.
#
# WHY S5 IS A DIFFERENT PROBLEM FROM S3
# Waking from sleep (S3) and waking from a full shutdown (S5) use different
# arming paths. A machine that wakes reliably from sleep and does nothing from
# shutdown is the classic signature of one of the three things below:
#   1. the NIC has "Wake on Magic Packet" but not the separate shutdown-state
#      wake property,
#   2. Windows Fast Startup turned "shutdown" into a hybrid shutdown, during
#      which Microsoft documents that NICs are explicitly NOT armed,
#   3. the BIOS/UEFI cuts standby power to the PCIe slot (ErP / Deep Sleep).
#
# This script tells you which of those apply. It cannot read the BIOS, so that
# part is a manual checklist printed at the end.

$ErrorActionPreference = 'SilentlyContinue'

function Section($t) {
    Write-Host ""
    Write-Host ("=" * 74) -ForegroundColor Cyan
    Write-Host "  $t" -ForegroundColor Cyan
    Write-Host ("=" * 74) -ForegroundColor Cyan
}

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
$elevated = $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Section "0. CONTEXT"
Write-Host "host      : $env:COMPUTERNAME"
Write-Host "elevated  : $elevated"
if (-not $elevated) {
    Write-Host "  -> BitLocker and some power queries will be incomplete. Re-run as Admin." -ForegroundColor Yellow
}
$cs = Get-CimInstance Win32_ComputerSystem
Write-Host "make/model: $($cs.Manufacturer) / $($cs.Model)   (determines BIOS menu wording)"
Write-Host "BIOS      : $((Get-CimInstance Win32_BIOS).SMBIOSBIOSVersion)"

Section "1. WHICH NIC HOLDS THE TARGET MAC (expect D8:43:AE:17:63:25)"
Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } |
    Select-Object Name, InterfaceDescription, MacAddress, LinkSpeed |
    Format-Table -AutoSize -Wrap
Write-Host "Confirm the adapter whose MacAddress is D8-43-AE-17-63-25. Everything"
Write-Host "below must be checked on THAT adapter. Wi-Fi WOL is a different and"
Write-Host "much less reliable feature - if that MAC is a Wi-Fi adapter, say so,"
Write-Host "because wake-from-shutdown over Wi-Fi generally does not work."

Section "2. NIC ADVANCED PROPERTIES (the S3-vs-S5 split lives here)"
Write-Host "Dumping ALL wake-related properties. The exact names differ by driver"
Write-Host "version and vendor, so do not assume a property exists - read the list."
Write-Host ""
foreach ($a in (Get-NetAdapter | Where-Object { $_.Status -eq 'Up' })) {
    Write-Host "--- $($a.Name)  [$($a.InterfaceDescription)] ---" -ForegroundColor White
    Get-NetAdapterAdvancedProperty -Name $a.Name |
        Where-Object { $_.DisplayName -match 'Wake|WOL|Shutdown|Green|Energy|EEE|Power|PME|Speed' } |
        Select-Object DisplayName, RegistryKeyword, DisplayValue |
        Format-Table -AutoSize -Wrap
}
Write-Host "WHAT YOU WANT TO SEE:"
Write-Host "  'Wake on Magic Packet'        = Enabled   <- covers S3. Usually already on."
Write-Host "  'Shutdown Wake-On-Lan'        = Enabled   <- THIS IS THE S4/S5 ONE."
Write-Host "                                              Realtek-specific. Often absent"
Write-Host "                                              or OFF by default. If you have"
Write-Host "                                              it and it is Disabled, that is"
Write-Host "                                              almost certainly your bug."
Write-Host "  'Enable PME'                  = Enabled   <- some drivers use this INSTEAD"
Write-Host "                                              of 'Shutdown Wake-On-Lan'."
Write-Host "  'WOL & Shutdown Link Speed'   = '10 Mbps First'  <- lets the PHY stay up on"
Write-Host "                                              standby power after shutdown."
Write-Host "                                              'Not Speed Down' can drop link."
Write-Host "  'Energy-Efficient Ethernet'   = Disabled  <- EEE/Green Ethernet can park the"
Write-Host "  'Green Ethernet'              = Disabled     PHY and swallow magic packets."
Write-Host ""
Write-Host "NOTE: on the SERVER's Realtek there is no 'Shutdown Wake-On-Lan' property at"
Write-Host "all, only 'WOL & Shutdown Link Speed'. So its presence on YOUR PC is UNKNOWN"
Write-Host "until you read the list above."

Section "3. NIC POWER MANAGEMENT TAB"
foreach ($a in (Get-NetAdapter | Where-Object { $_.Status -eq 'Up' })) {
    $pm = Get-NetAdapterPowerManagement -Name $a.Name
    if ($pm) {
        Write-Host "--- $($a.Name) ---" -ForegroundColor White
        $pm | Select-Object WakeOnMagicPacket, WakeOnPattern, DeviceSleepOnDisconnect,
                            ArpOffload, NSOffload | Format-List
    }
}
Write-Host "WakeOnMagicPacket must be Enabled."
Write-Host "In Device Manager this is: adapter > Properties > Power Management >"
Write-Host "  [x] Allow this device to wake the computer"
Write-Host "  [x] Only allow a magic packet to wake the computer"
Write-Host "(the second is greyed out until the first is ticked)."

Section "4. IS THE NIC CURRENTLY ARMED?"
Write-Host "powercfg /devicequery wake_armed :"
powercfg /devicequery wake_armed
Write-Host ""
Write-Host "The target NIC should appear in that list. If the list is empty or says"
Write-Host "NONE, nothing on this machine can wake it."
Write-Host ""
Write-Host "powercfg /lastwake (what woke it last time) :"
powercfg /lastwake

Section "5. FAST STARTUP  <-- THE BIG S5 TRAP"
$hb = Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled
if ($null -eq $hb.HiberbootEnabled) {
    Write-Host "HiberbootEnabled : NOT SET (Fast Startup treated as enabled on most builds)" -ForegroundColor Yellow
} elseif ($hb.HiberbootEnabled -eq 1) {
    Write-Host "HiberbootEnabled : 1  -> FAST STARTUP IS ON" -ForegroundColor Red
    Write-Host "  This is very likely why wake-from-shutdown fails." -ForegroundColor Red
} else {
    Write-Host "HiberbootEnabled : 0  -> Fast Startup is OFF (good for S5 WOL)" -ForegroundColor Green
}
Write-Host ""
Write-Host "WHY IT MATTERS. With Fast Startup on, 'Shut down' is not a real S5. Windows"
Write-Host "closes your session but hibernates the kernel into a hybrid shutdown (S4),"
Write-Host "putting devices in D3. Microsoft's own documentation states that in this"
Write-Host "state WOL from S4/S5 is unsupported and network adapters are deliberately"
Write-Host "NOT armed, because users expect a shut-down machine to draw no power."
Write-Host ""
Write-Host "THE TRADEOFF, honestly: disabling Fast Startup costs a few seconds of boot"
Write-Host "time and nothing else. It does not affect sleep, hibernate or performance"
Write-Host "once running. It also makes shutdown-time Group Policy actually apply. For"
Write-Host "a machine you intend to wake remotely, disabling it is the right call."
Write-Host ""
Write-Host "TO DISABLE (only if you approve - this script does NOT do it):"
Write-Host '  Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power" -Name HiberbootEnabled -Value 0'
Write-Host "  or: Control Panel > Power Options > Choose what the power buttons do >"
Write-Host "      Change settings that are currently unavailable > untick Turn on fast startup"
Write-Host "CAUTION: Windows feature updates have been observed turning it back ON."
Write-Host "Re-check this value after big updates."

Section "6. SLEEP STATES AVAILABLE"
powercfg /a
Write-Host "If S3 is absent and only 'Modern Standby (S0 Low Power Idle)' is listed,"
Write-Host "this is a Modern Standby machine and its wake behaviour differs; say so."

Section "7. PCI EXPRESS LINK STATE POWER MANAGEMENT"
$scheme = (powercfg /getactivescheme)
Write-Host $scheme
powercfg /query SCHEME_CURRENT SUB_PCIEXPRESS
Write-Host "ASPM should be Off for reliable WOL. Setting index 0 = Off."

Section "8. BITLOCKER  (decides whether unattended boot is possible at all)"
if ($elevated) {
    manage-bde -status
    Write-Host ""
    Write-Host "Key protectors on C: :"
    manage-bde -protectors -get C:
} else {
    Write-Host "SKIPPED - needs Administrator. Re-run elevated and check manually:" -ForegroundColor Yellow
    Write-Host "  manage-bde -status"
    Write-Host "  manage-bde -protectors -get C:"
}
Write-Host ""
Write-Host "HOW TO READ IT:"
Write-Host "  'Protection Status: Protection Off'      -> not encrypted, no impact."
Write-Host "  Protectors = 'TPM' only                  -> unattended boot WORKS. The TPM"
Write-Host "                                              releases the key with no typing."
Write-Host "  Protectors = 'TPM And PIN' / 'Password'  -> a human MUST type something at"
Write-Host "                                              a pre-boot screen. Unattended"
Write-Host "                                              cold boot is IMPOSSIBLE, and no"
Write-Host "                                              auto-logon scheme can fix it."
Write-Host "                                              WOL would only get you to the"
Write-Host "                                              PIN prompt."

Section "9. BIOS / UEFI - MANUAL, THIS SCRIPT CANNOT READ IT"
Write-Host "Reboot into firmware setup and look for these. Names vary by vendor;"
Write-Host "your board is '$($cs.Manufacturer) $($cs.Model)'."
Write-Host ""
Write-Host "  ENABLE one of:"
Write-Host "    'Wake on LAN' / 'Wake on LAN from S4/S5'      (Dell, Lenovo)"
Write-Host "    'Power On By PCI-E'  / 'Resume By PCI-E Device' (Gigabyte, MSI)"
Write-Host "    'PCIe Wakeup' / 'Power On By Onboard LAN'      (ASUS, ASRock)"
Write-Host ""
Write-Host "  DISABLE these - they cut standby power to the NIC and silently kill S5 WOL:"
Write-Host "    'ErP Ready' / 'ErP' / 'EuP 2013'   (Gigabyte, MSI)  -> set Disabled"
Write-Host "    'Deep Sleep' / 'Deep Sleep Control' (ASRock, Dell)  -> set Disabled"
Write-Host "                                                          (or 'Disabled in S4/S5')"
Write-Host ""
Write-Host "ErP/EuP is the single most common BIOS-side cause. It is an EU standby-power"
Write-Host "compliance mode: it drops the machine below ~0.5W, which means the NIC gets"
Write-Host "no standby power and cannot listen for anything."

Section "10. PHYSICAL CHECK"
Write-Host "After you shut the PC down fully, look at the Ethernet port."
Write-Host "  Link LED still lit  -> NIC has standby power and is probably armed. Good."
Write-Host "  All LEDs dark       -> NIC is unpowered. Fix BIOS (ErP/Deep Sleep) first;"
Write-Host "                         no Windows setting can help from there."

Write-Host ""
Write-Host ("=" * 74) -ForegroundColor Green
Write-Host "  Done. Nothing was changed. Send this whole output back." -ForegroundColor Green
Write-Host ("=" * 74) -ForegroundColor Green
