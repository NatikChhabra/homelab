# JARVIS HFP Interactive Registration
# Run this script from an interactive PowerShell window (not as a service)

Write-Host "=== JARVIS HFP Registration ===" -ForegroundColor Cyan
Write-Host ""
Write-Host "This will register JARVIS with Windows for hands-free calling." -ForegroundColor Yellow
Write-Host "Your phone must be connected via Bluetooth." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to continue..."

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.ApplicationModel.Calls.PhoneLineTransportDevice, Windows.ApplicationModel.Calls, ContentType = WindowsRuntime]
$null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]

$IFACE_PHONELINE_TRANSPORT = '{BD41DF2D-ADDD-4FC9-A194-B9881D2A2EFA}'
$UUID_HFP_AG = '0000111f'

$aqs = "System.Devices.DevObjectType:=8 AND System.Devices.InterfaceClassGuid:=" + '"' + $IFACE_PHONELINE_TRANSPORT + '"'

Write-Host "Searching for Bluetooth HFP devices..." -ForegroundColor Green
$task = [Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($aqs)
$nt = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$nt = $nt.MakeGenericMethod([Windows.Devices.Enumeration.DeviceInformationCollection])
$netTask = $nt.Invoke($null, @($task))
$netTask.Wait(20000)
$devices = $netTask.Result

$found = $false
foreach ($dev in $devices) {
    if ($dev.Id -match $UUID_HFP_AG) {
        $found = $true
        Write-Host "Found: $($dev.Name)" -ForegroundColor Green
        $transport = [Windows.ApplicationModel.Calls.PhoneLineTransportDevice]::FromId($dev.Id)
        if ($null -ne $transport) {
            Write-Host "Transport opened. Registered: $([bool]$transport.IsRegistered())" -ForegroundColor Yellow
            Write-Host "Registering with Windows..." -ForegroundColor Green
            try {
                $transport.RegisterApp()
                Start-Sleep -Seconds 2
                if ($transport.IsRegistered()) {
                    Write-Host "SUCCESS: Registration complete!" -ForegroundColor Green
                } else {
                    Write-Host "FAILED: RegisterApp() completed but Windows still reports unregistered." -ForegroundColor Red
                }
            } catch {
                Write-Host "FAILED: $($_.Exception.Message)" -ForegroundColor Red
            }
        }
    }
}

if (-not $found) {
    Write-Host "No HFP devices found. Make sure your phone is connected." -ForegroundColor Red
}

Write-Host ""
Write-Host "Registration attempt complete." -ForegroundColor Cyan
Read-Host "Press Enter to exit..."
