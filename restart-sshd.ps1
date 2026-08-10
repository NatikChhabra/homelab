# Restart the sshd LISTENER without dropping established sessions.
#
# WHY THIS IS SAFE, AND WHY IT IS VERIFIED RATHER THAN ASSUMED
# On Win32-OpenSSH the service process (sshd.exe) is only the listener. Each live
# connection is a separate sshd-session.exe that has already forked and owns its
# own socket. Measured 2026-08-09: sshd.exe pid 6600 -> sshd-session 15328 ->
# sshd-session 11528, the last being the session Claude Code runs under. Stopping
# and starting the listener therefore leaves existing sessions attached; only new
# connection ATTEMPTS are refused, for the ~2 s the service is down.
#
# The service cannot be reloaded in place -- CanPauseAndContinue is False and
# Win32-OpenSSH has no SIGHUP equivalent -- so a stop/start is the only way to
# make sshd_config take effect without waiting for a reboot.
#
# This script runs DETACHED from the SSH session on purpose. If the stop were run
# as a child of the session being tested and the assumption above were wrong, the
# script would die halfway through, leaving sshd stopped and the box unreachable
# over SSH until someone walked to it. Detached, the restart completes even in
# that case.
#
# Config is validated BEFORE the stop. Starting sshd with a bad config leaves the
# service dead and remote access gone.

$ErrorActionPreference = 'Continue'
$log = 'C:\ServerData\Stacks\sshd-restart.log'
function W([string]$m) {
    "{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m | Add-Content -Path $log -Encoding utf8
}

W "=== sshd restart requested ==="

# 1. Validate. Never stop a working sshd on the strength of an unvalidated file.
$sshdExe = (Get-CimInstance Win32_Service -Filter "Name='sshd'").PathName -replace '^"|"$', ''
$test = & $sshdExe -T 2>&1
if ($LASTEXITCODE -ne 0) {
    W "ABORT: sshd -T failed (exit $LASTEXITCODE). Config is invalid, service left running."
    ($test | Select-Object -First 5) | ForEach-Object { W "  $_" }
    exit 1
}
$eff = ($test | Select-String '^(clientaliveinterval|clientalivecountmax|tcpkeepalive)') -join ' | '
W "config valid. effective: $eff"

# 2. Record what is connected, so the claim "sessions survived" is checked
#    against reality rather than asserted.
$before = @(Get-CimInstance Win32_Process -Filter "Name='sshd-session.exe'" | ForEach-Object { $_.ProcessId })
W "sshd-session pids before: $($before -join ', ')"

# 3. Restart the listener.
Stop-Service sshd -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-Service sshd -ErrorAction SilentlyContinue
Start-Sleep -Seconds 3

$svc = (Get-Service sshd).Status
W "service status after start: $svc"

$after = @(Get-CimInstance Win32_Process -Filter "Name='sshd-session.exe'" | ForEach-Object { $_.ProcessId })
W "sshd-session pids after: $($after -join ', ')"

$survived = @($before | Where-Object { $after -contains $_ })
W "sessions that survived: $($survived.Count) of $($before.Count)"

# 4. Confirm the listener is actually accepting again.
$listen = Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue
if ($listen) { W "port 22 listening again (pid $($listen[0].OwningProcess))" }
else { W "WARNING: nothing is listening on port 22" }

W "=== done ==="
