$ErrorActionPreference = 'SilentlyContinue'
Write-Output ("# SESSION PROBE  " + (Get-Date -Format 'yyyy-MM-dd HH:mm'))
Write-Output ("jarvis HEAD: " + (git -C C:\ServerData\Stacks\jarvis log -1 --oneline))
Write-Output ("jarvis unpushed: " + (git -C C:\ServerData\Stacks\jarvis rev-list --count '@{u}..HEAD'))
Write-Output ("aether HEAD: " + (git -C C:\ServerData\Stacks\aether log -1 --oneline))
$b1 = (findstr /c:"JARVIS_BRIDGE_TOKEN=" C:\ServerData\Stacks\jarvis\secrets.env | find /c /v "")
$b2 = (findstr /c:"JARVIS_BRIDGE_TOKEN=" C:\ServerData\Stacks\secrets.env | find /c /v "")
Write-Output ("BRIDGE_TOKEN counts jarvis/stacks: $b1 $b2   (G0 OPEN while both = 0)")
docker ps --format "{{.Names}} | {{.Status}}" 2>$null | Select-String "jarvis-" | ForEach-Object { Write-Output $_.Line }
