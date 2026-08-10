# test-pc-agent-interactive.ps1 - end-to-end desktop tests.
#
# These cannot be verified by reading an HTTP response, because "200 OK, I sent
# the keystrokes" is exactly what a broken input path also returns. So this
# script closes the loop: it types into Notepad, copies with a real Ctrl+A /
# Ctrl+C, reads the clipboard back FROM SESSION 1, and compares strings.
#
# The test string deliberately contains  + ^ % ~ ( ) { } [ ]  because those are
# SendKeys control characters. If the agent were using SendKeys they would be
# swallowed or reinterpreted as modifiers; surviving them round-trip proves the
# KEYEVENTF_UNICODE path.
#
# Cleanup kills Notepad with Stop-Process rather than the close endpoint, so an
# unsaved-changes dialog is never left sitting on Natik's screen.

param([int]$Port = 8199)

$ErrorActionPreference = 'Continue'
$base = "http://127.0.0.1:$Port"

$tok = $null
foreach ($raw in (Get-Content 'C:\ServerData\Stacks\jarvis\secrets.env' -Encoding UTF8)) {
    $kv = $raw.Trim() -split '=', 2
    if ($kv.Length -eq 2 -and $kv[0].Trim() -eq 'JARVIS_WOL_TOKEN') { $tok = $kv[1].Trim() }
}
if (-not $tok) { Write-Host 'no token in secrets.env'; exit 1 }
$H = @{ Authorization = "Bearer $tok" }

function Call {
    param([string]$Method, [string]$Path, $Body = $null)
    $p = @{ Uri = "$base$Path"; Method = $Method; Headers = $H; TimeoutSec = 30 }
    if ($null -ne $Body) { $p.Body = ($Body | ConvertTo-Json -Compress); $p.ContentType = 'application/json' }
    try { return Invoke-RestMethod @p } catch { return [pscustomobject]@{ error = $_.Exception.Message } }
}
function Say($ok, $msg) {
    $tag = if ($ok) { 'PASS' } else { 'FAIL' }
    Write-Host ("{0}  {1}" -f $tag, $msg)
}

$MARKER = 'JARVIS pc-agent test 12345 +^%~(){}[] end'

Write-Host "`n=== APP LAUNCH (allowlist) ===" -ForegroundColor Cyan
$r = Call POST '/pc/app/open' @{ name = 'calc' }        # 'calc' is an aka, not the name
Say ($r.ok -eq $true) "open calculator by alias 'calc' -> ok=$($r.ok) app=$($r.app)"
Start-Sleep -Seconds 4
$apps = Call GET '/pc/apps'
Say ($apps.running -contains 'calculator') "calculator now listed as running: $($apps.running -join ', ')"

Write-Host "`n=== GRACEFUL CLOSE ===" -ForegroundColor Cyan
$r = Call POST '/pc/app/close' @{ name = 'calculator' }
Say ($r.ok -eq $true) "close calculator -> closed=$($r.closed -join ',')"
Start-Sleep -Seconds 3
$apps = Call GET '/pc/apps'
Say (-not ($apps.running -contains 'calculator')) "calculator gone from running list: $($apps.running -join ', ')"

Write-Host "`n=== TYPING + KEY COMBOS (clipboard round-trip) ===" -ForegroundColor Cyan
$null = Call POST '/pc/app/open' @{ name = 'notepad' }
Start-Sleep -Seconds 4
$f = Call POST '/pc/window/focus' @{ title = 'Notepad' }
$focused = "$($f.active.process)" -match 'notepad'
Say $focused "focus Notepad -> active='$($f.active.title)' process=$($f.active.process)"

if (-not $focused) {
    Write-Host 'ABORTING typing test: Notepad is not focused, and typing into an unknown window is not acceptable.' -ForegroundColor Red
} else {
    $t = Call POST '/pc/input/type' @{ text = $MARKER }
    Say ($t.ok -eq $true) "type $($t.characters) chars -> events_sent=$($t.events_sent)"
    Start-Sleep -Milliseconds 800
    $k1 = Call POST '/pc/input/key' @{ key = 'a'; modifiers = @('ctrl') }
    Say ($k1.ok -eq $true) "ctrl+a"
    Start-Sleep -Milliseconds 400
    $k2 = Call POST '/pc/input/key' @{ key = 'c'; modifiers = @('ctrl') }
    Say ($k2.ok -eq $true) "ctrl+c"
    Start-Sleep -Milliseconds 900

    # Clipboard must be read from THIS session (1). STA is required or
    # Clipboard::GetText throws "Current thread must be set to STA mode".
    $clip = ''
    try {
        $ps = [PowerShell]::Create()
        $null = $ps.AddScript('Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.Clipboard]::GetText()')
        $ps.Runspace = [RunspaceFactory]::CreateRunspace()
        $ps.Runspace.ApartmentState = 'STA'
        $ps.Runspace.Open()
        $clip = ($ps.Invoke() | Select-Object -First 1)
        $ps.Runspace.Close(); $ps.Dispose()
    } catch { $clip = "CLIPBOARD ERROR: $($_.Exception.Message)" }

    $clean = ("$clip").Trim()
    Say ($clean -eq $MARKER) "clipboard round-trip"
    Write-Host "     sent: $MARKER"
    Write-Host "     read: $clean"
}

Write-Host "`n=== MOUSE ===" -ForegroundColor Cyan
$m = Call POST '/pc/input/mouse' @{ x = 400; y = 300 }
Say ($m.cursor.x -eq 400 -and $m.cursor.y -eq 300) "move cursor to 400,300 -> cursor=$($m.cursor.x),$($m.cursor.y) screen=$($m.screen.width)x$($m.screen.height)"
$m2 = Call POST '/pc/input/mouse' @{ x = 99999; y = 99999 }
Say ($m2.cursor.x -lt $m2.screen.width -and $m2.cursor.y -lt $m2.screen.height) "off-screen coords clamped -> cursor=$($m2.cursor.x),$($m2.cursor.y)"

Write-Host "`n=== WINDOWS ===" -ForegroundColor Cyan
$w = Call POST '/pc/window/next'
Say ($w.ok -eq $true) "alt-tab -> now active '$($w.active.title)'"
$mm = Call POST '/pc/window/minimise_all'
Say ($mm.ok -eq $true) "minimise all -> ok=$($mm.ok)"
Start-Sleep -Seconds 2

Write-Host "`n=== CLEANUP ===" -ForegroundColor Cyan
$n = @(Get-Process notepad -ErrorAction SilentlyContinue)
foreach ($p in $n) { try { $p.Kill() } catch { } }
Say $true "killed $($n.Count) notepad process(es) without leaving a save dialog on screen"
