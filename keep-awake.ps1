# Hold this machine awake and its display on, permanently.
#
# WHY THIS EXISTS
# Incident 2026-08-09: the server entered Modern Standby at 00:24:41 and did not
# leave until 07:34:52 -- a 7 h 10 m outage. During Modern Standby this machine
# reports "Standby (S0 Low Power Idle) Network Disconnected", so the network
# stack goes down and every container becomes unreachable while the laptop still
# looks powered on. From the outside it is indistinguishable from "the server
# turned itself off", which is exactly how Natik described it.
#
# The subtle part, and the reason powercfg alone is not enough: when that outage
# happened the sleep timeouts were ALREADY set to 0 (never). Idle timeout was
# never the trigger. The triggers that ignore the timeout are:
#   - lid close                (was LIDACTION=1 "Sleep"; now set to 0 "do nothing")
#   - Start menu > Sleep       (a human choice, deliberately left working)
#   - any process calling SetSuspendState / the vendor power app switching schemes
#
# The last one is why this script exists. The laptop ships four vendor schemes
# (Silent / Performance / Turbo / Balanced) and the vendor control app rewrites
# scheme values when it switches profiles, which silently undoes powercfg. An
# execution-state assertion is held by the OS at a level above the power scheme,
# so it survives a scheme being rewritten underneath it.
#
# It also satisfies the display half of the request directly: ES_DISPLAY_REQUIRED
# keeps the panel powered even if something re-applies a display timeout.
#
# WHAT THIS DOES NOT DO
# It does not block a deliberate sleep from the Start menu or a lid switch set to
# sleep. That is intentional -- a machine nobody can ever put to sleep is a
# different kind of problem, and the lid path is already closed in the schemes.

$ErrorActionPreference = 'Continue'
$log = 'C:\ServerData\Stacks\keep-awake.log'

# The flag arithmetic lives in C#, not PowerShell, and that is deliberate.
# Windows PowerShell 5.1 parses the literal 0x80000000 as Int32 -2147483648, so
# `[uint32]0x80000000` throws "Value was either too large or too small". With
# $ErrorActionPreference='Continue' that failure is near-silent: the constant
# becomes $null, `$null -bor 1 -bor 2` evaluates to 3, and the call degrades from
# a HELD assertion (ES_CONTINUOUS) to a one-shot idle-timer nudge. It still
# returns a success code, still logs "assertion held", and `powercfg /requests`
# still reports None -- a fix that looks applied and does nothing. Caught here
# 2026-08-09 only because the requests table was checked instead of trusted.
Add-Type -Namespace Win32 -Name Power -MemberDefinition @'
[DllImport("kernel32.dll", SetLastError = true)]
private static extern uint SetThreadExecutionState(uint esFlags);

// ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x1) | ES_DISPLAY_REQUIRED (0x2)
public static uint KeepAwake() { return SetThreadExecutionState(0x80000000 | 0x00000001 | 0x00000002); }
public static uint Release()   { return SetThreadExecutionState(0x80000000); }
'@

function Write-Log([string]$m) {
    $line = "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    try { Add-Content -Path $log -Value $line -Encoding utf8 } catch {}
}

Write-Log "keep-awake starting (pid $PID, user $env:USERNAME)"

$prev = [Win32.Power]::KeepAwake()
if ($prev -eq 0) {
    Write-Log "FAILED: SetThreadExecutionState returned 0 (win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
} else {
    Write-Log ("assertion held: SYSTEM + DISPLAY (previous state 0x{0:X})" -f $prev)
}

# Re-assert on a slow loop. The assertion is per-thread and is dropped if the
# thread ever exits; re-asserting is cheap insurance against a session change or
# a fast-user-switch clearing it. 4 minutes is well inside any plausible idle
# timeout while costing nothing measurable.
$tick = 0
while ($true) {
    Start-Sleep -Seconds 240
    $r = [Win32.Power]::KeepAwake()
    $tick++
    if ($r -eq 0) {
        Write-Log "re-assert FAILED (win32 error $([Runtime.InteropServices.Marshal]::GetLastWin32Error()))"
    } elseif ($tick % 15 -eq 0) {
        # One heartbeat an hour. Enough to prove it was alive across an outage
        # without turning the log into a firehose.
        Write-Log "still holding (uptime $([int]((Get-Date) - (Get-Process -Id $PID).StartTime).TotalMinutes) min)"
    }
}
