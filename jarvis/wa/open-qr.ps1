# Open authenticated WhatsApp QR page without exposing token in chat or browser history.
$ErrorActionPreference = 'Stop'
$envFile = 'C:\ServerData\Stacks\jarvis\secrets.env'
$line = Get-Content $envFile | Where-Object { $_ -match '^JARVIS_WA_TOKEN=(.+)$' } | Select-Object -First 1
if (-not $line) { throw 'JARVIS_WA_TOKEN missing from secrets.env' }
$token = $line -replace '^JARVIS_WA_TOKEN=', ''
if ([string]::IsNullOrWhiteSpace($token)) { throw 'JARVIS_WA_TOKEN empty' }
$url = 'http://127.0.0.1:8097/qr?token=' + [uri]::EscapeDataString($token)
Start-Process $url
Write-Output 'Opened authenticated WhatsApp QR page. Scan it from WhatsApp > Settings > Linked devices.'
