# Runs 10 min after logon: full server audit, result copied to the Sync folder
# so a reboot can be verified from the phone/browser (http://<server>:8095).
& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File 'C:\ServerData\Stacks\run-job.ps1' -Job server-audit
$src = 'C:\ServerData\Stacks\server-audit-report.txt'
$dst = 'C:\ServerData\Sync\Claude code\BOOT-CHECK.txt'
$head = "Boot check $(Get-Date -Format 'yyyy-MM-dd HH:mm')  (booted $((Get-CimInstance Win32_OperatingSystem).LastBootUpTime))`r`n"
Set-Content $dst ($head + (Get-Content $src -Raw -ErrorAction SilentlyContinue))
