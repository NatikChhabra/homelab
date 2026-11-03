# udp-snapshot.ps1 -- runs the moment Windows logs Tcpip 4266 ("all UDP ports
# in use"). Added 2026-11-01: the host ran out of UDP ephemeral ports 51 times
# since August (once 19 min before an unclean shutdown) and nothing recorded
# which process held them, so the culprit stayed unknown. This captures it
# while the burst is happening. Triggered by the task "ServerData - UDP Exhaustion
# Snapshot" (event trigger, runs as SYSTEM). Read-only: it only records.
$log = 'C:\ServerData\Stacks\logs\udp-ports.log'
$u = Get-NetUDPEndpoint -ErrorAction SilentlyContinue
$svc = @{}
Get-CimInstance Win32_Service | Where-Object ProcessId | ForEach-Object { $svc[[int]$_.ProcessId] += @($_.Name) }
$top = $u | Group-Object OwningProcess | Sort-Object Count -Descending | Select-Object -First 5 | ForEach-Object {
    $procId = [int]$_.Name
    $name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
    $s = if ($svc[$procId]) { ' [' + ($svc[$procId] -join ',') + ']' } else { '' }
    "$($_.Count) x $name($procId)$s"
}
$worst = ($u | Group-Object OwningProcess | Sort-Object Count -Descending | Select-Object -First 1).Name
$ports = $u | Where-Object OwningProcess -eq $worst | Group-Object LocalAddress | Sort-Object Count -Descending | Select-Object -First 3 | ForEach-Object { "$($_.Count)@$($_.Name)" }
"$(Get-Date -Format s) total=$($u.Count) | top: $($top -join '; ') | worst-owner bind addrs: $($ports -join ', ')" | Add-Content $log
