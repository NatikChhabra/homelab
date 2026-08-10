# Restart the PC agent for real.
#
# Restart-ScheduledTask is NOT sufficient here and that is a measured fact, not a
# precaution. Observed 2026-08-09: after editing pc-agent.ps1 and calling
# Restart-ScheduledTask, /health still answered and still reported the OLD build
# -- the new file routes 404'd and the startup log still showed the previous
# "starting" timestamp with no "folder allowlist" line. The task reports success
# while the original powershell.exe keeps running and keeps port 8099 bound, so a
# newly launched instance cannot bind and exits immediately. The net effect is a
# restart that looks clean and changes nothing, which is the same class of
# false-confirmation bug that hid hud.py and verify/ in this project.
#
# So: stop the task, kill the process by its command line, WAIT for the port to
# actually be free, then start and confirm the new build is what answers.
#
# The -notmatch $PID guard is not decoration either. Twice this session a filter
# matching 'pc-agent' killed the very shell running the filter, because the
# shell's own command line contained the search string.

$ErrorActionPreference = 'Continue'
$mine = $PID

Stop-ScheduledTask -TaskName 'JARVIS - PC Agent' -TaskPath '\JARVIS\' -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 400

$killed = 0
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | ForEach-Object {
    if ($_.ProcessId -ne $mine -and $_.CommandLine -and $_.CommandLine -match 'pc-agent\.ps1') {
        try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop; $killed++ } catch {}
    }
}
"killed $killed old agent process(es)"

# Wait for the listener to release 8099. Starting before this is what produced
# the silent no-op above.
$free = $false
foreach ($i in 1..25) {
    $b = Get-NetTCPConnection -LocalPort 8099 -State Listen -ErrorAction SilentlyContinue
    if (-not $b) { $free = $true; break }
    Start-Sleep -Milliseconds 200
}
if ($free) { "port 8099 released" } else { "WARNING: 8099 still bound - the new instance will fail to bind" }

Start-ScheduledTask -TaskName 'JARVIS - PC Agent' -TaskPath '\JARVIS\'

foreach ($i in 1..30) {
    try {
        $h = Invoke-RestMethod -Uri 'http://localhost:8099/health' -TimeoutSec 2 -ErrorAction Stop
        "up: $($h.service) v$($h.version) interactive=$($h.interactive)"
        break
    } catch { Start-Sleep -Milliseconds 500 }
}
