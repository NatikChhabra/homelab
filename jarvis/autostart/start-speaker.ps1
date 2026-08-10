# Start the host speaker service (HTTP -> Kokoro -> Echo Dot).
#
# Separate from the ears task on purpose. The listener and the speaker fail
# independently: the Echo can drop off Bluetooth without the microphone caring,
# and a wedged wake-word connection should not take away JARVIS's ability to
# speak when something else (a timer firing, an announcement, the HUD) asks it to.
#
# Loopback-bound and token-authed. This process can make noise in a bedroom at
# any hour, so it must not be reachable from the LAN.

$ErrorActionPreference = 'Continue'
$Stack = 'C:\ServerData\Stacks\jarvis'
$Log   = Join-Path $Stack 'logs\autostart-speaker.log'
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null

function Log($m) {
    Add-Content -Path $Log -Encoding utf8 -Value ("{0}  {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m)
}

Log "=== autostart: speaker ==="

# Kokoro must be answering before this is useful; without it every /say would
# fall through to an error rather than speech.
$deadline = (Get-Date).AddMinutes(10)
$ready = $false
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest 'http://localhost:8880/v1/audio/voices' -TimeoutSec 5 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Seconds 10 }
}

if (-not $ready) {
    Log "FAIL kokoro (8880) never became ready"
    exit 1
}
Log "kokoro ready"

$secrets = 'C:\ServerData\Stacks\secrets.env'
if (Test-Path $secrets) {
    Get-Content $secrets | ForEach-Object {
        if ($_ -match '^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$') {
            $v = $Matches[2].Trim().Trim('"').Trim("'")
            [Environment]::SetEnvironmentVariable($Matches[1], $v, 'Process')
        }
    }
    Log "secrets loaded"
}

# Report which endpoint won, so a silent fallback from the Echo to the laptop
# speakers is visible in the log instead of being a mystery in the morning.
Set-Location $Stack
$dev = python "$Stack\host-speaker\speaker.py" --list 2>&1 | Select-String 'selected:'
Log "$dev"

Log "starting speaker.py"
python "$Stack\host-speaker\speaker.py" 2>&1 | ForEach-Object { Log $_ }
Log "speaker.py exited"
