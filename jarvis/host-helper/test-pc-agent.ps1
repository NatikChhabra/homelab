# test-pc-agent.ps1 - exercises every pc-agent endpoint against a running instance.
#
# Reads the token from secrets.env and NEVER prints it. Run against the test
# instance (-Port 8199), not the live one, unless you mean it.
#
# Destructive endpoints (shutdown, restart) are tested ONLY for their refusal
# path: that an unconfirmed request is rejected with 428. Confirming them would
# power off the machine, which is the whole point of the confirm gate.

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

$script:pass = 0; $script:fail = 0

function T {
    param([string]$Name, [string]$Method, [string]$Path, $Body = $null,
          [int[]]$Expect = @(200), [hashtable]$Headers = $null, [switch]$Raw)
    $h = if ($null -ne $Headers) { $Headers } else { $H }
    $p = @{ Uri = "$base$Path"; Method = $Method; Headers = $h; TimeoutSec = 25 }
    if ($null -ne $Body) { $p.Body = ($Body | ConvertTo-Json -Compress); $p.ContentType = 'application/json' }
    try {
        $r = Invoke-WebRequest @p -UseBasicParsing
        $code = [int]$r.StatusCode
        $out  = if ($Raw) { "$($r.RawContentLength) bytes, $($r.Headers['Content-Type'])" } else { $r.Content }
    } catch {
        $code = -1
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        $out = $_.Exception.Message
        try {
            $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
            $out = $sr.ReadToEnd(); $sr.Close()
        } catch { }
    }
    $ok = $Expect -contains $code
    if ($ok) { $script:pass++ } else { $script:fail++ }
    $tag = if ($ok) { 'PASS' } else { 'FAIL' }
    if ($out.Length -gt 240) { $out = $out.Substring(0, 240) + '...' }
    Write-Host ("{0}  {1,-34} {2,-4} {3,-30} {4}" -f $tag, $Name, $code, $Path, $out)
}

Write-Host "`n=== SECURITY CONTROLS ===" -ForegroundColor Cyan
T 'health needs no auth'      GET  '/health'      -Headers @{}                       -Expect 200
T 'no token -> 401'           GET  '/pc/status'   -Headers @{}                       -Expect 401
T 'wrong token -> 401'        GET  '/pc/status'   -Headers @{Authorization='Bearer wrongtokenwrongtoken'} -Expect 401
T 'token in query -> 400'     GET  "/pc/status?token=$tok" -Headers @{}              -Expect 400
T 'no arbitrary command'      POST '/run'         @{ cmd = 'calc' }                  -Expect 404
T 'no arbitrary powershell'   POST '/exec'        @{ command = 'Get-Process' }       -Expect 404
T 'app not on allowlist'      POST '/pc/app/open' @{ name = 'cmd.exe' }              -Expect 404
T 'app by path refused'       POST '/pc/app/open' @{ name = 'C:\Windows\System32\cmd.exe' } -Expect 404
T 'shutdown unconfirmed -> 428' POST '/pc/power/shutdown' @{}                        -Expect 428
T 'restart unconfirmed -> 428'  POST '/pc/power/restart'  @{ confirm = $false }      -Expect 428
T 'unknown key refused'       POST '/pc/input/key' @{ key = 'delete_all_files' }     -Expect 400
T 'unknown modifier refused'  POST '/pc/input/key' @{ key='a'; modifiers=@('hyper') } -Expect 400
T 'oversized text refused'    POST '/pc/input/type' @{ text = ('x' * 900) }          -Expect 413

Write-Host "`n=== READ-ONLY ===" -ForegroundColor Cyan
T 'pc status'                 GET  '/pc/status'
T 'wake-target status'        GET  '/status'
T 'app list + running'        GET  '/pc/apps'
T 'window list'               GET  '/pc/window'
T 'volume read'               GET  '/pc/volume'
T 'screenshot png'            GET  '/pc/screenshot?max_width=640' -Raw
T 'screenshot json'           GET  '/pc/screenshot.json?max_width=200'

Write-Host "`n=== VOLUME (restored afterwards) ===" -ForegroundColor Cyan
$before = (Invoke-RestMethod "$base/pc/volume" -Headers $H)
Write-Host "  volume before: $($before.volume_percent)% muted=$($before.muted)"
T 'set volume 37'             POST '/pc/volume' @{ level = 37 }
T 'set volume clamp >100'     POST '/pc/volume' @{ level = 5000 }
T 'bad volume rejected'       POST '/pc/volume' @{ level = 'loud' }                  -Expect 400
T 'mute toggle'               POST '/pc/mute'   @{ mute = $true }
T 'unmute'                    POST '/pc/mute'   @{ mute = $false }
T 'volume_down key'           POST '/pc/media/volume_down'
T 'volume_up key'             POST '/pc/media/volume_up'
T 'play_pause key'            POST '/pc/media/play_pause'
T 'next key'                  POST '/pc/media/next'
T 'previous key'              POST '/pc/media/previous'
$null = Invoke-RestMethod "$base/pc/volume" -Method POST -Headers $H -Body (@{level=$before.volume_percent}|ConvertTo-Json) -ContentType 'application/json'
$null = Invoke-RestMethod "$base/pc/mute" -Method POST -Headers $H -Body (@{mute=$before.muted}|ConvertTo-Json) -ContentType 'application/json'
$after = (Invoke-RestMethod "$base/pc/volume" -Headers $H)
Write-Host "  volume restored to: $($after.volume_percent)% muted=$($after.muted)"

Write-Host "`n=== POWER (non-destructive only) ===" -ForegroundColor Cyan
T 'cancel with none pending'  POST '/pc/power/cancel'
T 'wake magic packet'         POST '/pc/power/wake' @{}

Write-Host "`n=== SUMMARY ===" -ForegroundColor Cyan
Write-Host "passed: $script:pass   failed: $script:fail"
