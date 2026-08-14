$ErrorActionPreference = 'SilentlyContinue'
Write-Output ("# SESSION PROBE  " + (Get-Date -Format 'yyyy-MM-dd HH:mm'))
Write-Output ("jarvis HEAD: " + (git -C C:\ServerData\Stacks\jarvis log -1 --oneline))
Write-Output ("jarvis unpushed: " + (git -C C:\ServerData\Stacks\jarvis rev-list --count '@{u}..HEAD'))
Write-Output ("aether HEAD: " + (git -C C:\ServerData\Stacks\aether log -1 --oneline))
$b1 = 0
$b2 = 0
if (Test-Path "C:\ServerData\Stacks\jarvis\secrets.env") {
    $t1 = Select-String -Path "C:\ServerData\Stacks\jarvis\secrets.env" -Pattern "JARVIS_BRIDGE_TOKEN="
    if ($t1) { $b1 = $t1.Count }
}
if (Test-Path "C:\ServerData\Stacks\secrets.env") {
    $t2 = Select-String -Path "C:\ServerData\Stacks\secrets.env" -Pattern "JARVIS_BRIDGE_TOKEN="
    if ($t2) { $b2 = $t2.Count }
}
Write-Output ("BRIDGE_TOKEN counts jarvis/stacks: $b1 $b2   (G0 OPEN while both = 0)")
docker ps --format "{{.Names}} | {{.Status}}" 2>$null | Select-String "jarvis-" | ForEach-Object { Write-Output $_.Line }
