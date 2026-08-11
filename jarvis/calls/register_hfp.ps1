# HFP Registration Script for JARVIS
# This script must be run interactively (not as a service)

Write-Output "=== JARVIS HFP Registration ==="
Write-Output ""
Write-Output "This script will register JARVIS with Windows for hands-free calling."
Write-Output "Your phone will need to be connected via Bluetooth."
Write-Output ""
Read-Host "Press Enter to continue..."

# Load WinRT types
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.ApplicationModel.Calls.PhoneLineTransportDevice, Windows.ApplicationModel.Calls, ContentType = WindowsRuntime]
$null = [Windows.Devices.Enumeration.DeviceInformation, Windows.Devices.Enumeration, ContentType = WindowsRuntime]

# Find HFP device
$IFACE_PHONELINE_TRANSPORT = '{BD41DF2D-ADDD-4FC9-A194-B9881D2A2EFA}'
$UUID_HFP_AG = '0000111f'

$aqs = "System.Devices.DevObjectType:=8 AND System.Devices.InterfaceClassGuid:=" + '"' + $IFACE_PHONELINE_TRANSPORT + '"'

Write-Output "Searching for Bluetooth HFP devices..."
$task = [Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($aqs)
$nt = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$nt = $nt.MakeGenericMethod([Windows.Devices.Enumeration.DeviceInformationCollection])
$netTask = $nt.Invoke($null, @($task))
$netTask.Wait(20000)
$devices = $netTask.Result

foreach ($dev in $devices) {
    if ($dev.Id -match $UUID_HFP_AG) {
        Write-Output "Found: $($dev.Name)"
        $transport = [Windows.ApplicationModel.Calls.PhoneLineTransportDevice]::FromId($dev.Id)
        if ($null -ne $transport) {
            Write-Output "Opening transport..."
            Write-Output "Registering with Windows..."
            try {
                $transport.RegisterApp()
                Start-Sleep -Seconds 2
                if ($transport.IsRegistered()) {
                    Write-Output "SUCCESS: Registration complete!"
                } else {
                    Write-Output "FAILED: RegisterApp() completed but Windows still reports unregistered."
                }
            } catch {
                Write-Output "FAILED: $($_.Exception.Message)"
            }
        }
    }
}

Write-Output ""
Write-Output "Registration attempt complete."
Read-Host "Press Enter to exit..."
