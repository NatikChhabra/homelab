# One-off 03:30 updates for packages that can't update while someone is
# connected: Tailscale (restarts the tunnel this machine is reached through)
# and Git (in use by any open Git Bash). Logs to logs\night-updates.log.
$log = 'C:\ServerData\Stacks\logs\night-updates.log'
function L($m){ Add-Content $log ("{0}  {1}" -f (Get-Date -Format s), $m) }
foreach ($id in 'Git.Git','Microsoft.WindowsTerminal','Tailscale.Tailscale') {
  $r = winget upgrade --id $id -e --silent --force --accept-package-agreements --accept-source-agreements --disable-interactivity 2>&1 | Select-Object -Last 1
  L "$id -> $r"
}
Start-Sleep 20
L ("tailscale status: " + ((& 'C:\Program Files\Tailscale\tailscale.exe' status --self=true --peers=false 2>&1) -join ' '))
Unregister-ScheduledTask -TaskName 'ServerData - Night Updates (once)' -Confirm:$false
