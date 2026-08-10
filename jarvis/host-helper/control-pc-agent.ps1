# Positive control for server-audit.sh section 23.
#
# Stops the PC agent, so the audit check must FAIL, then restarts it. Exists as a
# file rather than an inline -Command because the nested quoting needed to filter
# processes by command line inside a bash -> powershell -> scriptblock chain is a
# reliable source of parse errors -- and a control that silently fails to run
# reports the same green as a healthy system, which is the exact class of bug
# section 23 was written to catch.

param([ValidateSet('stop','start')][string]$Action)

$ErrorActionPreference = 'Continue'
$me = $PID

if ($Action -eq 'stop') {
    Stop-ScheduledTask -TaskName 'JARVIS - PC Agent' -TaskPath '\JARVIS\' -ErrorAction SilentlyContinue
    $pat = 'pc' + '-agent'
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.ProcessId -ne $me -and $_.CommandLine -match $pat } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force; "killed pid $($_.ProcessId)" }
    Start-Sleep -Seconds 3
    $c = Get-NetTCPConnection -LocalPort 8099 -State Listen -ErrorAction SilentlyContinue
    if ($c) { "WARNING: 8099 still bound by pid $($c.OwningProcess)" } else { "8099 free - agent is down" }
}
else {
    Start-ScheduledTask -TaskName 'JARVIS - PC Agent' -TaskPath '\JARVIS\'
    Start-Sleep -Seconds 9
    $c = Get-NetTCPConnection -LocalPort 8099 -State Listen -ErrorAction SilentlyContinue
    if ($c) { "8099 listening again (pid $($c.OwningProcess))" } else { "WARNING: agent did NOT come back" }
}
