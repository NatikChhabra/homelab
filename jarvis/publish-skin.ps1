# Package the live Rainmeter skin so the main PC can install it.
#
# WHAT THIS IS FOR
# ui/install-hud.ps1 runs on Natik's main PC and fetches the skin over Tailscale
# from http://<server>:3100/static/jarvis-skin.zip. That URL is served by
# voice_server.py's static route, whose root is the ui/ directory -- so the zip
# simply has to land in ui/. This builds it.
#
# RUN THIS WHENEVER THE SKIN CHANGES. The zip is a snapshot, not a live view;
# editing JARVIS.ini on the server does not update what the main PC downloads
# until this is run again and the image rebuilt (ui/ is COPYed into the image,
# so the container serves the copy that was baked in, not the one on disk).
#
# The source is the INSTALLED skin under Documents\Rainmeter\Skins, because that
# is the copy that has actually been rendered and verified on screen. Packaging
# some other copy would risk shipping a version nobody has ever looked at.

$ErrorActionPreference = 'Stop'

$src = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Rainmeter\Skins\JARVIS'
$out = 'C:\ServerData\Stacks\jarvis\ui\jarvis-skin.zip'

if (-not (Test-Path $src)) {
    Write-Host "no skin at $src - nothing to publish" -ForegroundColor Red
    exit 1
}

$files = Get-ChildItem $src -Recurse -File
if ($files.Count -eq 0) {
    Write-Host "skin directory is empty - refusing to publish an empty zip" -ForegroundColor Red
    exit 1
}

if (Test-Path $out) { Remove-Item $out -Force }
$parent = Split-Path $src -Parent
Push-Location $parent
try { Compress-Archive -Path 'JARVIS' -DestinationPath $out -CompressionLevel Optimal } finally { Pop-Location }

$zip = Get-Item $out
"published: $($zip.FullName)"
"  $($files.Count) file(s), $([math]::Round($zip.Length/1KB,1)) KB"

# Prove the archive contains the skin at the path the installer expects. It
# extracts to Skins\ and relies on a top-level "JARVIS\" folder inside the zip;
# a flat archive would scatter .ini files loose into the Skins directory.
#
# Entry separators: Compress-Archive on Windows writes '\' separators, not '/',
# so a split on '/' alone never yields a bare 'JARVIS' root and every valid zip
# tripped the warning below. Split on both separators.
Add-Type -AssemblyName System.IO.Compression.FileSystem
$a = [System.IO.Compression.ZipFile]::OpenRead($out)
$roots = $a.Entries | ForEach-Object { (($_.FullName -split '[/\\]')[0]) } | Sort-Object -Unique
$inis  = ($a.Entries | Where-Object { $_.FullName -like '*.ini' }).Count
$a.Dispose()
"  top-level entries: $($roots -join ', ')"
"  .ini files inside: $inis"
if ($roots -notcontains 'JARVIS') {
    Write-Host "  WARNING: no top-level JARVIS folder - the installer would unpack this wrong" -ForegroundColor Yellow
}

