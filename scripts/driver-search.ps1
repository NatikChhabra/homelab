# Lists (and with -Install, installs) Windows Update driver updates. Runs as
# SYSTEM via a one-off task because the WU API refuses remote sessions.
param([switch]$Install, [string]$Match = 'Realtek')
$log = 'C:\ServerData\Stacks\logs\driver-search.log'
$s = New-Object -ComObject Microsoft.Update.Session
$sm = New-Object -ComObject Microsoft.Update.ServiceManager
$sm.ClientApplicationID = 'ServerData'
$searcher = $s.CreateUpdateSearcher()
$searcher.ServiceID = '7971f918-a847-4430-9279-4a52d1efe18d'   # Microsoft Update (includes optional drivers)
$searcher.SearchScope = 1
$searcher.ServerSelection = 3
$r = $searcher.Search("IsInstalled=0 and Type='Driver'")
$lines = @("found $($r.Updates.Count) driver update(s)")
$c = New-Object -ComObject Microsoft.Update.UpdateColl
foreach($u in $r.Updates){ $lines += " - $($u.Title) [$($u.DriverClass)] $($u.DriverVerDate)"; if($u.Title -match $Match){ [void]$c.Add($u) } }
if($Install -and $c.Count){
  $d=$s.CreateUpdateDownloader(); $d.Updates=$c; [void]$d.Download()
  $i=$s.CreateUpdateInstaller(); $i.Updates=$c; $res=$i.Install()
  $lines += "installed $($c.Count): result $($res.ResultCode) (2=ok) reboot=$($res.RebootRequired)"
}
$lines | Set-Content $log
