# Turns the Wi-Fi radio on (software switch) and connects to the home 5GHZ
# network. Must run in the user's session: Windows.Devices.Radios refuses
# session-0 callers. Result -> logs\wifi-radio.txt
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$await = [System.WindowsRuntimeSystemExtensions].GetMethods() | ? { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } | select -First 1
function Await($op, [Type]$t){ $task = $await.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$out = foreach($r in $radios | ? { $_.Kind -eq 'WiFi' }){
  $before = $r.State
  $res = Await ($r.SetStateAsync('On')) ([Windows.Devices.Radios.RadioAccessStatus])
  "$($r.Name): $before -> $($r.State) ($res)"
}
Start-Sleep 5
netsh wlan connect name=5GHZ | Out-Null
$out | Set-Content 'C:\ServerData\Stacks\logs\wifi-radio.txt'
