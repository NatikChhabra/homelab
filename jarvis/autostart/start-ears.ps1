# Start the always-on listener (host mic -> wake word -> Whisper -> Echo Dot).
#
# Runs on the HOST, not in Docker, because Docker Desktop on Windows exposes
# neither a microphone nor a speaker. See host-ears/ears.py for the full reasoning.
#
# Waits for the containers it depends on rather than assuming the stack task
# already finished -- task delays are a hint, not a guarantee, and a cold boot
# reorders things.

$ErrorActionPreference = 'Continue'
$Stack = 'C:\ServerData\Stacks\jarvis'
$Log   = Join-Path $Stack 'logs\autostart-ears.log'
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null

function Log($m) {
    Add-Content -Path $Log -Encoding utf8 -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m)
}

Log "=== autostart: ears ==="

# Wake word (10400) and Whisper (10300) must both answer before the loop can work.
$deadline = (Get-Date).AddMinutes(10)
$ready = $false
while ((Get-Date) -lt $deadline) {
    $wake = Test-NetConnection -ComputerName localhost -Port 10400 -InformationLevel Quiet -WarningAction SilentlyContinue
    $stt  = Test-NetConnection -ComputerName localhost -Port 10300 -InformationLevel Quiet -WarningAction SilentlyContinue
    if ($wake -and $stt) { $ready = $true; break }
    Start-Sleep -Seconds 10
}

if (-not $ready) {
    Log "FAIL wake word (10400) or whisper (10300) never came up"
    exit 1
}
Log "wake word and whisper reachable"

# Load secrets into the environment. They are never hardcoded and never passed
# on the command line, where they would be visible in the process list.
$secrets = 'C:\ServerData\Stacks\secrets.env'
if (Test-Path $secrets) {
    Get-Content $secrets | ForEach-Object {
        if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $v = $Matches[2].Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($Matches[1], $v, 'Process')
        }
    }
    Log "secrets loaded"
} else {
    Log "WARN secrets.env not found; HA calls will fail"
}

Set-Location $Stack
Log "starting ears.py"
# 2>&1 merges stderr so a traceback lands in the log rather than vanishing.
python "$Stack\host-ears\ears.py" 2>&1 | ForEach-Object { Log $_ }
Log "ears.py exited"
