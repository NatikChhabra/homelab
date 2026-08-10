# ==========================================================================
#  JARVIS HUD -- installer for Natik's main PC
# ==========================================================================
#  Run this ON THE MAIN PC (not on the server), in PowerShell:
#
#      irm http://100.80.120.33:3100/static/install-hud.ps1 | iex
#
#  That is the whole procedure. It installs Rainmeter if missing, fetches the
#  current skin from the server, points it at the server instead of localhost,
#  places it correctly for THIS monitor, and starts it.
#
#  WHY AN INSTALLER RATHER THAN "COPY THE FOLDER OVER"
#  Three things differ between the server and the main PC, and every one of
#  them silently produces a broken-looking HUD:
#
#   1. THE URL. The skin on the server reads http://localhost:3100. On the main
#      PC localhost is the main PC, so every meter renders empty and it looks
#      like a styling bug. The server is reached over Tailscale instead, which
#      is already up between these two machines.
#
#   2. THE SCREEN. The server's desktop is 1536x864 logical (1920x1080 at 125%
#      DPI). Hard-coded coordinates from there can put the panel partly or
#      wholly off the edge of a different monitor -- that exact bug has already
#      happened once on the server itself. This reads the real working area of
#      the machine it is running on and positions against that.
#
#   3. RAINMETER MAY ALREADY BE IN USE. Rainmeter.ini is a single shared config
#      for every skin the user has. Overwriting it would silently unload
#      anything else installed. This edits only the JARVIS section.
#
#  SAFE TO RUN TWICE. It upgrades in place and keeps the existing position if
#  the skin is already installed.
# ==========================================================================

$ErrorActionPreference = 'Stop'

$Server   = 'http://100.80.120.33:3100'
$SkinName = 'JARVIS'
$ConfigId = 'JARVIS\HUD'

function Say($m) { Write-Host "  $m" }
Write-Host ""
Write-Host "JARVIS HUD installer" -ForegroundColor Cyan
Write-Host "--------------------"

# --- 1. can we actually reach the server? --------------------------------
# Checked FIRST and treated as fatal. Installing a HUD that cannot reach its
# data source produces a panel of empty fields, which reads as "the install is
# broken" rather than "the network is down" and sends you looking in the wrong
# place entirely.
Say "checking the server on Tailscale..."
try {
    $null = Invoke-RestMethod -Uri "$Server/api/hud/vitals" -TimeoutSec 8
    Say "server is reachable at $Server"
} catch {
    Write-Host ""
    Write-Host "  CANNOT REACH THE SERVER at $Server" -ForegroundColor Red
    Write-Host "  The HUD would install but every panel would be blank, so stopping here."
    Write-Host ""
    Write-Host "  Check, in this order:"
    Write-Host "    1. Tailscale is running and signed in on THIS pc   (tailscale status)"
    Write-Host "    2. the server 'arjun-rajput' shows as online in that list"
    Write-Host "    3. Docker Desktop is running on the server - JARVIS starts only"
    Write-Host "       after Natik logs in there, so a rebooted server sitting at the"
    Write-Host "       lock screen has no JARVIS running at all"
    Write-Host ""
    return
}

# --- 2. Rainmeter ---------------------------------------------------------
$rmExe = "$env:ProgramFiles\Rainmeter\Rainmeter.exe"
if (-not (Test-Path $rmExe)) {
    Say "Rainmeter is not installed - installing via winget..."
    winget install --id Rainmeter.Rainmeter --silent --accept-package-agreements --accept-source-agreements | Out-Null
    Start-Sleep -Seconds 5
    if (-not (Test-Path $rmExe)) {
        Write-Host "  Rainmeter install failed. Install it from rainmeter.net and re-run this." -ForegroundColor Red
        return
    }
    Say "Rainmeter installed"
} else {
    Say "Rainmeter already present"
}

# --- 3. fetch the skin ----------------------------------------------------
$skinsRoot = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Rainmeter\Skins'
$target    = Join-Path $skinsRoot $SkinName
$tmpZip    = Join-Path $env:TEMP 'jarvis-skin.zip'

Say "downloading the current skin from the server..."
Invoke-WebRequest -Uri "$Server/static/jarvis-skin.zip" -OutFile $tmpZip -TimeoutSec 60

# Rainmeter holds file handles on a loaded skin, so unload before replacing.
$running = Get-Process Rainmeter -ErrorAction SilentlyContinue
if ($running) {
    Say "closing Rainmeter to replace the skin files..."
    & $rmExe '!Quit' 2>$null
    Start-Sleep -Seconds 2
    Get-Process Rainmeter -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 1
}

if (Test-Path $target) { Remove-Item $target -Recurse -Force -ErrorAction SilentlyContinue }
New-Item -ItemType Directory -Path $skinsRoot -Force | Out-Null
Expand-Archive -Path $tmpZip -DestinationPath $skinsRoot -Force
Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
Say "skin installed to $target"

# --- 4. point it at the server, not at this machine -----------------------
# Every localhost reference in every .ini becomes the Tailscale address. Done
# by rewriting the files rather than by asking the skin to read a variable,
# because a variable the skin author forgets to use fails silently.
$rewritten = 0
Get-ChildItem $target -Recurse -Filter *.ini | ForEach-Object {
    $raw = Get-Content $_.FullName -Raw
    $new = $raw -replace 'http://(localhost|127\.0\.0\.1):3100', $Server
    if ($new -ne $raw) {
        Set-Content -Path $_.FullName -Value $new -Encoding UTF8
        $rewritten++
    }
}
Say "pointed $rewritten skin file(s) at $Server"

# --- 5. position for THIS monitor ----------------------------------------
Add-Type -AssemblyName System.Windows.Forms
$wa = [System.Windows.Forms.Screen]::PrimaryScreen.WorkingArea
Say "this screen: $($wa.Width)x$($wa.Height) working area"

# Right-aligned with a margin, below the top edge. Width is read from the skin
# itself where it declares one, so a redesigned skin does not need this script
# changed; 380 is only the fallback.
$skinW = 380
$main = Get-ChildItem $target -Recurse -Filter *.ini | Select-Object -First 1
if ($main) {
    # SkinWidth= is the formal Rainmeter key, but the JARVIS skin uses W=460
    # inside [Variables] (the variable expansion [W] is what every meter reads).
    # Both patterns are matched so a skin redesign does not silently fall back
    # to the 380 default and end up wrong-sized on screen.
    $m = Select-String -Path $main.FullName -Pattern '^\s*SkinWidth\s*=\s*(\d+)' -ErrorAction SilentlyContinue
    if ($m) { $skinW = [int]$m.Matches.Groups[1].Value }
    if ($skinW -eq 380) {
        $m = Select-String -Path $main.FullName -Pattern '^\s*W\s*=\s*(\d+)' -ErrorAction SilentlyContinue
        if ($m) { $skinW = [int]$m.Matches.Groups[1].Value }
    }
}
$x = [Math]::Max(0, $wa.Width - $skinW - 40)
$y = 60

# Rainmeter.ini is shared with every other skin, so only the JARVIS section is
# touched. Rainmeter rewrites this file itself (as UTF-16) whenever it exits, so
# it is edited while Rainmeter is stopped and then nudged with bangs afterwards.
$rmIni = Join-Path $env:APPDATA 'Rainmeter\Rainmeter.ini'
if (-not (Test-Path $rmIni)) {
    New-Item -ItemType Directory -Path (Split-Path $rmIni) -Force | Out-Null
    @"
[Rainmeter]
SkinPath=$skinsRoot\
"@ | Set-Content -Path $rmIni -Encoding UTF8
}

$ini = Get-Content $rmIni -Raw -ErrorAction SilentlyContinue
if ($ini -notmatch [regex]::Escape("[$ConfigId]")) {
    Add-Content -Path $rmIni -Value @"

[$ConfigId]
Active=1
WindowX=$x
WindowY=$y
AlwaysOnTop=-2
Draggable=1
SavePosition=1
"@
    Say "registered the skin in Rainmeter.ini at ${x},${y}"
} else {
    Say "skin already registered in Rainmeter.ini - keeping your position"
}

# --- 6. start it ----------------------------------------------------------
Start-Process $rmExe
Start-Sleep -Seconds 4
& $rmExe '!ActivateConfig' $ConfigId 2>$null
Start-Sleep -Seconds 2
& $rmExe '!ZPos' '-2' $ConfigId 2>$null
& $rmExe '!Refresh' $ConfigId 2>$null

# --- 7. autostart ---------------------------------------------------------
$startup = [Environment]::GetFolderPath('Startup')
$lnk     = Join-Path $startup 'Rainmeter.lnk'
if (-not (Test-Path $lnk)) {
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $rmExe
    $sc.Save()
    Say "added Rainmeter to startup"
} else {
    Say "Rainmeter already starts with Windows"
}

Write-Host ""
Write-Host "  Done. The HUD is on your desktop, on the wallpaper layer." -ForegroundColor Green
Write-Host "  It reads live data from the server over Tailscale, so it keeps"
Write-Host "  working from anywhere, not only at home."
Write-Host ""
Write-Host "  If a panel is blank, the server is unreachable - check Tailscale"
Write-Host "  and that Docker Desktop is running on the server."
Write-Host ""
