# _test-transport.ps1 - transport-layer test suite for wa-agent.ps1.
#
# Tests everything that does NOT need a desktop session: routing, auth, the
# source allowlist, parameter parsing, the http.sys 411 trap, and the failure
# taxonomy. UI actions are expected to fail here with no_interactive_session,
# and that failing correctly is itself one of the assertions.
#
# Safe to run at any time: it never sends a WhatsApp message and never needs
# WhatsApp to be installed.

param(
    [string] $BaseUrl = 'http://127.0.0.1:8098',
    [string] $Token   = $env:JARVIS_WHATSAPP_TOKEN
)

$pass = 0; $fail = 0

function Invoke-Case {
    param([string]$Name, [string]$Method, [string]$Url, $Headers, $Body, [string]$ContentType,
          [int]$ExpectStatus, [string]$ExpectError = '',
          [string]$ExpectJsonPath = '', [string]$ExpectJsonValue = '')
    $sc = 0; $content = ''
    try {
        $p = @{ Uri = $Url; Method = $Method; TimeoutSec = 90; UseBasicParsing = $true }
        if ($Headers) { $p.Headers = $Headers }
        if ($null -ne $Body) { $p.Body = $Body }
        if ($ContentType) { $p.ContentType = $ContentType }
        $r = Invoke-WebRequest @p
        $sc = [int]$r.StatusCode; $content = $r.Content
    } catch {
        try { $sc = [int]$_.Exception.Response.StatusCode } catch { }
        try {
            $sr = New-Object IO.StreamReader($_.Exception.Response.GetResponseStream())
            $content = $sr.ReadToEnd()
        } catch { }
        if ($sc -eq 0) { $content = $_.Exception.Message }
    }

    $gotErr = ''
    $obj = $null
    try { $obj = $content | ConvertFrom-Json; $gotErr = $obj.error } catch { }

    $ok = ($sc -eq $ExpectStatus)
    $why = ''
    if ($ok -and $ExpectError -and $gotErr -ne $ExpectError) {
        $ok = $false; $why = "error='$gotErr' expected '$ExpectError'"
    }
    if ($ok -and $ExpectJsonPath) {
        $v = $obj
        foreach ($seg in ($ExpectJsonPath -split '\.')) {
            try { $v = $v.$seg } catch { $v = $null }
        }
        if ("$v" -ne $ExpectJsonValue) {
            $ok = $false; $why = "$ExpectJsonPath='$v' expected '$ExpectJsonValue'"
        }
    }
    if (-not $ok -and -not $why) { $why = "HTTP $sc expected $ExpectStatus" }

    if ($ok) {
        $script:pass++
        Write-Host ("  PASS  {0,-26} HTTP {1} error='{2}'" -f $Name, $sc, $gotErr) -ForegroundColor Green
    } else {
        $script:fail++
        Write-Host ("  FAIL  {0,-26} {1}" -f $Name, $why) -ForegroundColor Red
        Write-Host ("        body: " + ($content -replace '\s+', ' ')) -ForegroundColor DarkGray
    }
}

if (-not $Token) { Write-Host 'set $env:JARVIS_WHATSAPP_TOKEN first' -ForegroundColor Red; exit 1 }
$auth = @{ Authorization = "Bearer $Token" }

Write-Host "`n--- unauthenticated surface ---"
Invoke-Case 'health-no-auth'      'GET'  "$BaseUrl/health"                      $null $null $null 200
Invoke-Case 'no-token'            'GET'  "$BaseUrl/whatsapp?action=status"      $null $null $null 401 'unauthorized'
Invoke-Case 'wrong-token'         'GET'  "$BaseUrl/whatsapp?action=status" `
                                  @{ Authorization = 'Bearer wrong_token_aaaaaaaaaaaaaaaaaaaa' } $null $null 401 'unauthorized'
Invoke-Case 'malformed-auth'      'GET'  "$BaseUrl/whatsapp?action=status" `
                                  @{ Authorization = "Basic $Token" } $null $null 401 'unauthorized'

Write-Host "`n--- routing and parameters ---"
Invoke-Case 'unknown-path'        'GET'  "$BaseUrl/nope"                        $auth $null $null 404 'not_found'
Invoke-Case 'bad-action'          'GET'  "$BaseUrl/whatsapp?action=destroy"     $auth $null $null 400 'bad_action'
Invoke-Case 'missing-action'      'GET'  "$BaseUrl/whatsapp"                    $auth $null $null 400 'bad_action'
Invoke-Case 'bad-json-body'       'POST' "$BaseUrl/whatsapp"                    $auth '{not json' 'application/json' 400 'bad_json'

Write-Host "`n--- the http.sys 411 trap (a body-less POST) ---"
# http.sys answers a POST carrying neither body nor Content-Length with
# "411 Length Required" BEFORE the script ever runs, which is how the WOL
# helper got bitten. The GET alias plus query-string parsing is the mitigation.
# The assertion that matters is simply: this is NOT a 411.
Invoke-Case 'POST-bodyless'       'POST' "$BaseUrl/whatsapp?action=status"      $auth $null $null 200
Invoke-Case 'POST-json-status'    'POST' "$BaseUrl/whatsapp"                    $auth '{"action":"status"}' 'application/json' 200
Invoke-Case 'GET-alias-status'    'GET'  "$BaseUrl/whatsapp?action=status"      $auth $null $null 200

Write-Host "`n--- status is diagnostic and must work even when nothing else can ---"
# status deliberately runs BEFORE preflight, so it still answers from session 0
# and reports interactive=false. That is what makes it useful for diagnosis.
Invoke-Case 'status-reports-state' 'GET' "$BaseUrl/whatsapp?action=status"      $auth $null $null 200 '' `
    -ExpectJsonPath 'session.interactive' -ExpectJsonValue 'False'

Write-Host "`n--- UI actions must FAIL CLOSED with a distinct code, never silently ---"
Invoke-Case 'read-no-session'      'GET' "$BaseUrl/whatsapp?action=read&contact=Anyone&count=5" $auth $null $null 502 'no_interactive_session'
Invoke-Case 'preview-no-session'   'GET' "$BaseUrl/whatsapp?action=preview&contact=Anyone"      $auth $null $null 502 'no_interactive_session'
# The critical one: a fully-formed send must still refuse, and must say why.
Invoke-Case 'send-no-session'      'GET' "$BaseUrl/whatsapp?action=send&contact=Anyone&message=hello" $auth $null $null 502 'no_interactive_session'

Write-Host "`n--- send guards ---"
Invoke-Case 'send-without-message' 'GET' "$BaseUrl/whatsapp?action=send&contact=NoSuchContact" $auth $null $null 400 'message_required'

Write-Host "`n--- source allowlist (second instance that excludes loopback) ---"
# Security-relevant, so proven rather than assumed: an agent whose allowlist
# does not contain the caller must answer 403 BEFORE authentication, so a
# correct token from a disallowed address is still refused.
$altPort = 8097
$altLog = Join-Path $PSScriptRoot '_test-agent-alt.log'
$agentPath = Join-Path $PSScriptRoot 'wa-agent.ps1'
$alt = Start-Process powershell.exe -PassThru -WindowStyle Hidden -ArgumentList @(
    '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$agentPath`"",
    '-Port', $altPort, '-BindAddress', '127.0.0.1', '-SkipSessionCheck',
    '-AllowFrom', '10.99.99.99/32', '-LogFile', "`"$altLog`""
)
Start-Sleep -Seconds 6
Invoke-Case 'blocked-source-health' 'GET' "http://127.0.0.1:$altPort/health"  $null $null $null 403 'forbidden_source'
Invoke-Case 'blocked-source-valid-token' 'GET' "http://127.0.0.1:$altPort/whatsapp?action=status" $auth $null $null 403 'forbidden_source'
try { Stop-Process -Id $alt.Id -Force -ErrorAction SilentlyContinue } catch { }
Remove-Item $altLog -ErrorAction SilentlyContinue

Write-Host "`n================ $pass passed, $fail failed ================`n"
if ($fail -gt 0) { exit 1 }
exit 0
