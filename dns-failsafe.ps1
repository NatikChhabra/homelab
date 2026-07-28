# ============================================================================
# dns-failsafe.ps1 -- flip this machine's DNS between AdGuard and automatic.
#
# WHY THIS EXISTS: pointing a machine's adapter at AdGuard (100.80.120.33) is
# what makes ad blocking work, but it also makes that machine depend on the
# server being up. If the server is off, DNS stops answering and the machine
# has no internet at all -- while looking like a broken connection rather than
# a DNS problem, which is the part that wastes an hour.
#
# A secondary DNS entry does NOT solve this. It was tried on 2026-07-28:
# with 127.0.0.1 primary and 1.1.1.1 secondary, Windows used the secondary
# freely and doubleclick.net resolved normally. Any fallback resolver defeats
# the filtering it is supposed to back up. It is one or the other.
#
# So this script makes the switch a single command instead of a trip through
# adapter settings.
#
#   powershell -ExecutionPolicy Bypass -File dns-failsafe.ps1 -Mode adguard
#   powershell -ExecutionPolicy Bypass -File dns-failsafe.ps1 -Mode auto
#   powershell -ExecutionPolicy Bypass -File dns-failsafe.ps1 -Mode status
#
# NOTE: IPv6 matters as much as IPv4 here. The router hands out an IPv6 DNS
# server over router advertisements, and Windows PREFERS it -- on 2026-07-28
# every IPv4 setting was being silently ignored because of exactly that, and
# nslookup kept reporting the ISP resolver no matter what was configured.
# Both families are always set together below.
# ============================================================================

[CmdletBinding()]
param(
    [ValidateSet('adguard', 'auto', 'status')]
    [string]$Mode = 'status'
)

$ErrorActionPreference = 'Stop'

$AdGuardV4 = '100.80.120.33'   # AdGuard over Tailscale
$AdGuardV6 = '::1'             # AdGuard on this host, when it runs here

# Only adapters that are actually up and carry a default route matter. Setting
# DNS on every adapter Windows has ever seen is how you end up with a stale
# entry on a disconnected NIC quietly winning the resolution race.
function Get-TargetInterfaces {
    $routed = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
              Select-Object -ExpandProperty ifIndex -Unique
    Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and $routed -contains $_.ifIndex }
}

function Show-Status {
    Write-Host "`nCurrent DNS:" -ForegroundColor Cyan
    Get-DnsClientServerAddress |
        Where-Object { $_.ServerAddresses } |
        Select-Object InterfaceAlias, AddressFamily, ServerAddresses |
        Format-Table -AutoSize

    Write-Host "Resolution test:" -ForegroundColor Cyan
    foreach ($probe in @(@('github.com', 'should RESOLVE'), @('doubleclick.net', 'should be BLOCKED'))) {
        try {
            $r = Resolve-DnsName -Name $probe[0] -DnsOnly -QuickTimeout -ErrorAction Stop
            $ip = ($r | Where-Object { $_.IPAddress } | Select-Object -First 1).IPAddress
            if ($ip -in @('0.0.0.0', '::')) { $verdict = 'BLOCKED' } else { $verdict = $ip }
        } catch { $verdict = 'FAILED - no answer' }
        "  {0,-20} {1,-22} {2}" -f $probe[0], $probe[1], $verdict
    }
}

switch ($Mode) {
    'status' { Show-Status; break }

    'adguard' {
        foreach ($a in Get-TargetInterfaces) {
            Set-DnsClientServerAddress -InterfaceIndex $a.ifIndex -ServerAddresses @($AdGuardV4)
            try { Set-DnsClientServerAddress -InterfaceIndex $a.ifIndex -ServerAddresses @($AdGuardV6) -ErrorAction Stop } catch {}
            Write-Host "  $($a.Name): DNS -> AdGuard" -ForegroundColor Green
        }
        ipconfig /flushdns | Out-Null
        Show-Status
        break
    }

    'auto' {
        foreach ($a in Get-TargetInterfaces) {
            # ResetServerAddresses drops the static entries and goes back to
            # whatever DHCP / router advertisement provides, for both families.
            Set-DnsClientServerAddress -InterfaceIndex $a.ifIndex -ResetServerAddresses
            Write-Host "  $($a.Name): DNS -> automatic (router/ISP)" -ForegroundColor Yellow
        }
        ipconfig /flushdns | Out-Null
        Write-Host "`nAd blocking is now OFF on this machine. Re-enable with -Mode adguard." -ForegroundColor Yellow
        Show-Status
        break
    }
}
