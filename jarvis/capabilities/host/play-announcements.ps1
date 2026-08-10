# JARVIS announcement player (host side).
#
# WHY THIS EXISTS: the capability service runs in a Linux container on Docker
# Desktop for Windows. That container has no audio device and cannot be given
# one -- there is no /dev/snd to pass through. So the container renders speech
# with Piper and writes a finished .wav; this script is the part that actually
# reaches a speaker.
#
# The browser sink (/ws/announce, the page at http://localhost:3102/) is the
# other half. Both always receive every announcement, because either one can be
# absent: no browser tab open, or nobody logged in at the console. An
# announcement that is silently swallowed is the worst failure this feature has.
#
# Run in the foreground to watch it:
#     powershell -ExecutionPolicy Bypass -File play-announcements.ps1 -Verbose
#
# Install as a logon task (asks first -- do not run unattended without reading):
#     see the README in this folder, section "Host player".

[CmdletBinding()]
param(
    [string] $BaseUrl = "http://localhost:3102",
    [int]    $PollSeconds = 3,
    # Bounded run for testing. 0 = run forever.
    [int]    $MaxSeconds = 0
)

$ErrorActionPreference = 'Continue'
Add-Type -AssemblyName System.Windows.Forms | Out-Null

$started = Get-Date
Write-Verbose "polling $BaseUrl every ${PollSeconds}s"

while ($true) {
    if ($MaxSeconds -gt 0 -and ((Get-Date) - $started).TotalSeconds -ge $MaxSeconds) {
        Write-Verbose "max runtime reached, exiting"
        break
    }

    $pending = $null
    try {
        $pending = Invoke-RestMethod -Uri "$BaseUrl/api/announce/pending" -TimeoutSec 5
    } catch {
        # The service being down is normal during a rebuild. Keep polling
        # quietly rather than dying and needing a manual restart.
        Write-Verbose "service unreachable: $($_.Exception.Message)"
        Start-Sleep -Seconds $PollSeconds
        continue
    }

    foreach ($name in @($pending.pending)) {
        if (-not $name) { continue }
        $tmp = Join-Path $env:TEMP "jarvis-$name"
        try {
            Invoke-WebRequest -Uri "$BaseUrl/api/announce/file/$name" -OutFile $tmp -TimeoutSec 20 | Out-Null

            # PlaySync, not Play: the next announcement must not start on top of
            # this one. Two overlapping voices are less intelligible than one
            # announcement arriving a few seconds late.
            $player = New-Object System.Media.SoundPlayer $tmp
            Write-Host ("{0}  playing {1}" -f (Get-Date -Format 'HH:mm:ss'), $name)
            $player.PlaySync()
            $player.Dispose()

            # Only delete server-side AFTER it has finished playing, so a crash
            # mid-playback replays rather than loses the announcement.
            Invoke-RestMethod -Uri "$BaseUrl/api/announce/file/$name" -Method Delete -TimeoutSec 5 | Out-Null
        } catch {
            Write-Warning "could not play ${name}: $($_.Exception.Message)"
        } finally {
            if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
        }
    }

    Start-Sleep -Seconds $PollSeconds
}
