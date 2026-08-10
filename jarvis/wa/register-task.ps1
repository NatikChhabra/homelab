# Register "JARVIS - WhatsApp", the scheduled task that keeps the bridge alive.
#
# A FILE RATHER THAN AN INLINE -Command, DELIBERATELY
# Registering this needs backtick line continuations and nested quoting, and
# neither survives a bash -> powershell.exe -Command hop: the continuation is
# eaten and PowerShell tries to run "-Argument" as a command. The same problem
# produced a positive control that silently never ran, earlier in this project.
# Scripts on disk do not have this failure mode.
#
# WHY Interactive AS NATIK, NOT SYSTEM AT BOOT
# A network service does not need a desktop, so SYSTEM at boot looks like the
# better choice and would even start earlier. It is the wrong one here for a
# specific reason: wa/auth/ holds the linked-device credentials, it is currently
# owned by Natik, and Baileys rewrites it on every credentials update. Running
# the bridge as SYSTEM would have it writing that store as a different principal,
# and a half-SYSTEM half-user auth directory is how the WhatsApp link breaks in a
# way that can only be fixed by scanning the QR again. Matching the owner of the
# credentials it maintains is worth more than the earlier start.
#
# TWO TRIGGERS
# At logon (with a 30 s delay, so it is not racing Docker Desktop) and every
# 5 minutes thereafter. The repeat is what makes it self-healing: -Ensure exits
# immediately when /health answers, so the steady-state cost is one short
# PowerShell run per 5 minutes and the bridge cannot stay dead for longer.

$ErrorActionPreference = 'Stop'
$me = (whoami).Trim()

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\ServerData\Stacks\jarvis\wa\start-wa.ps1" -Ensure' `
    -WorkingDirectory 'C:\ServerData\Stacks\jarvis\wa'

$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $me
$atLogon.Delay = 'PT30S'

$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 5)

$principal = New-ScheduledTaskPrincipal -UserId $me -LogonType Interactive -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskPath '\JARVIS\' -TaskName 'WhatsApp' `
    -Action $action -Trigger @($atLogon, $repeat) `
    -Principal $principal -Settings $settings -Force | Out-Null

$reg = Get-ScheduledTask -TaskPath '\JARVIS\' -TaskName 'WhatsApp'
"registered as $me"
"logon=$($reg.Principal.LogonType)  triggers=$($reg.Triggers.Count)  state=$($reg.State)"
