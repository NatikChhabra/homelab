# One-shot at boot: uninstall Edge (WebView2 stays). Needs the IN entries added to
# System32\IntegratedServicesRegionPolicySet.json (2026-11-01), which Windows only
# reads at boot. Logs to logs\remove-edge.log, then unregisters itself on success.
$log = 'C:\ServerData\Stacks\logs\remove-edge.log'
function L($m){ Add-Content $log ("{0}  {1}" -f (Get-Date -Format s), $m) }
$s = Get-ChildItem 'C:\Program Files (x86)\Microsoft\Edge\Application\*\Installer\setup.exe' -ea 0 | sort FullName -desc | select -First 1
if (-not $s) { L 'Edge already gone'; Unregister-ScheduledTask 'ServerData - Remove Edge (once)' -Confirm:$false; exit }
Get-Process msedge -ea 0 | Stop-Process -Force
$p = Start-Process $s.FullName -ArgumentList '--uninstall --system-level --force-uninstall --delete-profile' -Wait -PassThru
L "uninstall exit $($p.ExitCode); msedge.exe present: $(Test-Path 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')"
if (-not (Test-Path 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')) {
  Unregister-ScheduledTask 'ServerData - Remove Edge (once)' -Confirm:$false
}
