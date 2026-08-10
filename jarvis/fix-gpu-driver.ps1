# fix-gpu-driver.ps1 - reinstall the NVIDIA display driver for the RTX 3050.
#
# WHY THIS EXISTS
# The RTX 3050 (PCI\VEN_10DE&DEV_25A2) is physically present but reports
# "This device cannot start. (Code 10)" and Windows fell back to the
# Microsoft Basic Display Adapter. Root cause: no Display-class NVIDIA driver
# in the driver store (`pnputil /enum-drivers` shows NVIDIA USB/MEDIA/
# SoftwareDevice entries, but no display .inf, and nvidia-smi.exe is absent).
#
# Consequence for JARVIS: nvidia-container-cli reports "WSL environment
# detected but no adapters were found", so Ollama runs 100% on CPU. Measured
# warm LLM latency is ~4.8s against a 3s end-to-end voice target. Fixing this
# driver is the single largest performance win available on this host.
#
# The screen is driven by the AMD Radeon iGPU (1920x1080 live), so installing
# the NVIDIA driver should NOT blank your display.
#
# RUN THIS FROM AN ADMIN POWERSHELL. It does not reboot on its own.

$ErrorActionPreference = 'Stop'

$installer = "C:\Users\Naiti\.claude\jobs\76fc5acc\tmp\nvidia\nvidia-610.88.exe"
$expectedSize = 979651304
$expectedHash = "576A90C6F6EEA47748DB1DEFCBCA1746C81FE4EB972F3132D30878778CEE0B09"

Write-Host "=== preflight ===" -ForegroundColor Cyan

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "NOT ELEVATED. Re-open PowerShell as Administrator and run this again." -ForegroundColor Red
    exit 1
}
Write-Host "elevated: yes"

if (-not (Test-Path $installer)) {
    Write-Host "Installer missing at $installer" -ForegroundColor Red
    Write-Host "Re-download: https://us.download.nvidia.com/Windows/610.88/610.88-notebook-win10-win11-64bit-international-dch-whql.exe"
    exit 1
}

# Verify integrity before executing - this file came off the network.
$size = (Get-Item $installer).Length
if ($size -ne $expectedSize) {
    Write-Host "SIZE MISMATCH: got $size, expected $expectedSize. Do not run." -ForegroundColor Red
    exit 1
}
Write-Host "size: $size (matches)"

$hash = (Get-FileHash $installer -Algorithm SHA256).Hash
if ($hash -ne $expectedHash) {
    Write-Host "HASH MISMATCH. Do not run." -ForegroundColor Red
    Write-Host "  got:      $hash"
    Write-Host "  expected: $expectedHash"
    exit 1
}
Write-Host "sha256: matches"

$sig = Get-AuthenticodeSignature $installer
if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'NVIDIA') {
    Write-Host "SIGNATURE INVALID ($($sig.Status)). Do not run." -ForegroundColor Red
    exit 1
}
Write-Host "signature: Valid, $($sig.SignerCertificate.Subject -replace '.*CN=([^,]+).*','$1')"

Write-Host ""
Write-Host "=== installing NVIDIA 610.88 (silent, clean, no auto-reboot) ===" -ForegroundColor Cyan
Write-Host "This takes 5-15 minutes and the screen may flicker. Do not interrupt it."
Write-Host "Starting at $(Get-Date -Format 'HH:mm:ss')..."

# -s        silent
# -clean    wipe existing NVIDIA driver state first (this is a repair, not an upgrade)
# -noreboot we control the reboot; 29 containers are running
$proc = Start-Process -FilePath $installer `
                      -ArgumentList '-s','-clean','-noreboot' `
                      -Wait -PassThru -NoNewWindow

Write-Host "installer exit code: $($proc.ExitCode)  (finished $(Get-Date -Format 'HH:mm:ss'))"

Write-Host ""
Write-Host "=== post-install check (before reboot) ===" -ForegroundColor Cyan

$dev = Get-PnpDevice | Where-Object { $_.InstanceId -match 'VEN_10DE&DEV_25A2' }
Write-Host "GPU device : $($dev.FriendlyName)"
Write-Host "status     : $($dev.Status)  problem: $($dev.Problem)"

$smi = "C:\Windows\System32\nvidia-smi.exe"
if (Test-Path $smi) {
    Write-Host "nvidia-smi : present"
    & $smi --query-gpu=name,memory.total,driver_version --format=csv
} else {
    Write-Host "nvidia-smi : still absent - a reboot is required to finish binding" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "=== NEXT STEP ===" -ForegroundColor Green
Write-Host "Reboot to finish. That stops all 29 containers; they have restart:always"
Write-Host "so they come back on their own. After the reboot, tell Claude and it will"
Write-Host "verify passthrough and switch Ollama onto the GPU."
