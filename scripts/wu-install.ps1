# Installs pending Windows Updates (no reboot). Runs as SYSTEM because the WU
# API refuses remote sessions. Result -> logs\wu-install.log
$log = 'C:\ServerData\Stacks\logs\wu-install.log'
$s = New-Object -ComObject Microsoft.Update.Session
$r = $s.CreateUpdateSearcher().Search('IsInstalled=0 and IsHidden=0')
$c = New-Object -ComObject Microsoft.Update.UpdateColl
$r.Updates | % { $_.AcceptEula(); [void]$c.Add($_) }
if ($c.Count) {
  $d = $s.CreateUpdateDownloader(); $d.Updates = $c; [void]$d.Download()
  $i = $s.CreateUpdateInstaller(); $i.Updates = $c; $res = $i.Install()
  $msg = "installed $($c.Count): result $($res.ResultCode) (2=ok), reboot required $($res.RebootRequired); " + (($r.Updates | % Title) -join '; ')
} else { $msg = 'nothing pending' }
Add-Content $log ("{0}  {1}" -f (Get-Date -Format s), $msg)
