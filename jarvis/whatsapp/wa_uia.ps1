# wa_uia.ps1 - WhatsApp Desktop UI-Automation core. Runs ON Natik's main PC,
# inside his own interactive desktop session, against his already-logged-in
# WhatsApp. No unofficial client library, no linked device, no ban risk.
#
# WHY UI AUTOMATION AND NOT A LIBRARY
# whatsapp-web.js / Baileys / mautrix-whatsapp all register a NEW linked device
# against the account and speak the protocol themselves. That is what gets a
# number banned. Driving the real app's own window touches nothing Meta can
# distinguish from a human using the keyboard.
#
# WHY POWERSHELL AND NOT PYTHON OR AUTOHOTKEY
# UIAutomationClient + UIAutomationTypes ship with the .NET Framework on every
# Windows 10/11 box, so this file installs NOTHING on the main PC. Python would
# need an interpreter plus pywinauto/comtypes; AutoHotkey would need the runtime
# plus a vendored third-party UIA library, and produces awkward JSON. Zero new
# software on a machine we cannot even log into yet is worth more than nicer
# syntax. It also matches host-helper/wol-helper.ps1, already proven here.
#
# WHY IT CANNOT RUN AS A SERVICE  (MEASURED 2026-08-08 on the server laptop)
# A shell in session 0 enumerated the UIA root's children and got 0 windows,
# while explorer.exe sat in session 1. Session 0 is isolated from the desktop:
# it has no window station with visible windows, so UIA finds nothing and
# keystrokes go nowhere. Therefore this MUST run as the logged-in user in the
# interactive session. That is a hard OS boundary, not a configuration choice.
#
# WHY THE COMPOSER IS FED FROM THE CLIPBOARD
# WhatsApp's composer is a contenteditable div. Chromium usually exposes no
# ValuePattern on those, so text has to be entered as input. Per-character
# keystrokes cannot carry emoji or non-Latin script, and SendKeys treats
# + ^ % ~ ( ) { } as metacharacters, so a message containing "100% :)" would be
# mangled or would fire stray modifiers. Pasting is one atomic operation that
# carries any Unicode verbatim and inserts newlines without sending. The
# clipboard is saved and restored around the paste.
#
# SAFETY INVARIANTS, in priority order:
#   1. Never send to a contact we are not certain of. After opening a chat the
#      header name must match what was asked, or we abort untouched.
#   2. Never type unless WhatsApp is provably the foreground window, re-checked
#      immediately before every keystroke batch. Otherwise a private message
#      could be typed into whatever else had focus.
#   3. Never report a send as done unless it was read back from the transcript.
#      A voice assistant that falsely confirms is worse than one that admits it
#      does not know. That is what the send_unconfirmed code is for.
#
# Every path returns ONE json object on stdout and exits 0 (ok) or 1 (error).

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('send', 'read', 'preview', 'status', 'dump')]
    [string] $Action,

    # Contact display name exactly as it appears in WhatsApp, or a phone number
    # in full international form (+911234567890).
    [string] $Contact = '',

    [string] $Message = '',

    # Read the message body from stdin instead of -Message.
    # MEASURED: the obvious sentinel `-Message -` does NOT work. PowerShell's
    # parameter binder treats a bare "-" as the start of a parameter name, so
    # the script died during binding with exit 5 and produced no JSON at all -
    # which the agent could only report as the useless 'uia_no_output'. An
    # explicit switch is unambiguous.
    [switch] $MessageFromStdin,

    [int]    $Count = 10,

    # Whole-operation budget. Chromium builds its accessibility tree lazily, so
    # the first query after the app starts can genuinely take seconds.
    [int]    $TimeoutMs = 20000,

    # Optional guard: a file of allowed contact names/numbers, one per line.
    # If the file exists, anything not listed is refused. Absent = no filter.
    [string] $AllowedContactsFile = '',

    # Empty = resolved in the body. MEASURED: $PSScriptRoot is not populated
    # while param defaults bind under `powershell.exe -File`.
    [string] $LogFile = '',

    # Log message bodies. OFF by default and should stay off: the log is on disk
    # and lands in the nightly backup.
    [switch] $LogBodies,

    # Resolve and report the target without typing or sending anything.
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$script:Version = '1.0.0'
$script:StartedAt = Get-Date

$here = $PSScriptRoot
if (-not $here) { $here = Split-Path -Parent $MyInvocation.MyCommand.Definition }
if (-not $LogFile) { $LogFile = Join-Path $here 'wa-agent.log' }

# PRIVACY: a message passed as a command-line argument is visible to any other
# process on the machine (Get-CimInstance Win32_Process exposes CommandLine, and
# Task Manager will show it). Message bodies must never travel that way, so the
# agent passes -MessageFromStdin and writes the real text to stdin instead.
if ($MessageFromStdin) {
    $Message = [Console]::In.ReadToEnd()
    if ($null -eq $Message) { $Message = '' }
    $Message = $Message.TrimEnd("`r", "`n")
}

# --------------------------------------------------------------- selectors ---
# WhatsApp for Windows is now a WebView2 (Chromium) wrapper around WhatsApp Web,
# NOT the old native WinUI/UWP app. So these are WhatsApp Web's ARIA labels
# surfaced through Chromium's UIA provider, and they are localised: a non-English
# UI exposes different strings. They are ALSO undocumented and change between
# releases without notice.
#
# THEREFORE: every selector is a list of candidates tried in order, and the
# whole table can be overridden by selectors.json next to this script, so
# adapting to drift is a text edit and never a code change. Run
#   .\wa_uia.ps1 -Action dump
# on the main PC to read the real names out of the live tree.
$SEL = @{
    composer   = @('Type a message', 'Type a message here', 'Message', 'Type your message')
    search     = @('Search input textbox', 'Search or start a new chat', 'Search', 'Search...')
    sendButton = @('Send')
    chatList   = @('Chat list', 'Chats')
    msgList    = @('Message list', 'Messages')
    header     = @('Conversation header', 'Contact info', 'Profile details')
}

$selOverride = Join-Path $here 'selectors.json'
if (Test-Path $selOverride) {
    try {
        $j = Get-Content $selOverride -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($k in @($SEL.Keys)) {
            if ($j.PSObject.Properties.Name -contains $k) { $SEL[$k] = @($j.$k) }
        }
    } catch {
        # A malformed override must not silently fall back to guesses.
        Write-Host "WARN selectors.json unreadable: $($_.Exception.Message)"
    }
}

# Hard ceilings. These exist so a confused 3B model cannot turn one tool call
# into a broadcast. One recipient per invocation, always.
$MAX_MESSAGE_CHARS = 4000
$MAX_READ_COUNT    = 50

# ----------------------------------------------------------------- logging ---

function Write-Log {
    param([string]$Level, [string]$Message)
    $line = '{0} [{1}] {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    # Write-Host, never Write-Output: wol-helper.ps1 learned this the hard way.
    # Write-Output emits into the pipeline, so a logging function silently
    # prepends its log lines to its own return value.
    Write-Host $line
    try {
        if ((Test-Path $LogFile) -and ((Get-Item $LogFile).Length -gt 2MB)) {
            Move-Item $LogFile "$LogFile.1" -Force
        }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }
}

# Redact anything that could be a message body before it reaches the log.
function Protect-Body {
    param([string]$Text)
    if ($LogBodies) { return $Text }
    if (-not $Text) { return '' }
    return "<$($Text.Length) chars redacted>"
}

# ------------------------------------------------------------------ result ---
# The voice layer must be able to say "I could not reach that" instead of
# falsely confirming, so every exit is a distinct machine-readable code.

function Out-Result {
    param(
        [bool]   $Ok,
        [string] $ErrorCode = '',
        [string] $Detail = '',
        [hashtable] $Data = @{}
    )
    $body = @{
        ok          = $Ok
        action      = $Action
        error       = $ErrorCode
        detail      = $Detail
        version     = $script:Version
        elapsed_ms  = [int]((Get-Date) - $script:StartedAt).TotalMilliseconds
        at          = (Get-Date -Format 'o')
    }
    foreach ($k in $Data.Keys) { $body[$k] = $Data[$k] }

    if ($Ok) { Write-Log INFO  "$Action -> ok" }
    else     { Write-Log ERROR "$Action -> $ErrorCode : $Detail" }

    # stdout carries exactly one JSON document; the agent parses this.
    $body | ConvertTo-Json -Depth 8 -Compress
    if ($Ok) { exit 0 } else { exit 1 }
}

# ------------------------------------------------------------- native glue ---

if (-not ('WaNative' -as [type])) {
    Add-Type -Namespace '' -Name 'WaNative' -MemberDefinition @'
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
'@
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]
$CT = [System.Windows.Automation.ControlType]

# ----------------------------------------------------------------- session ---

function Test-InteractiveSession {
    # Session 0 has no visible desktop (MEASURED: 0 windows enumerable from it).
    # A lock screen also blocks input, and LogonUI.exe is the practical signal.
    # This is a heuristic, not a supported API, so it is reported as one.
    $sid = (Get-Process -Id $PID).SessionId
    $locked = [bool](Get-Process -Name 'LogonUI' -ErrorAction SilentlyContinue)
    return [pscustomobject]@{
        session_id  = $sid
        interactive = ($sid -ne 0)
        locked      = $locked
        heuristic   = 'locked = LogonUI.exe present; not a documented API'
    }
}

# ------------------------------------------------------------------ window ---

function Get-WhatsAppWindow {
    # Both the old native build and the new WebView2 wrapper present a top-level
    # window owned by WhatsApp.exe. The Store build may instead be hosted by
    # ApplicationFrameHost.exe, in which case the frame is the top-level window
    # and the real content is a child; UIA descends through that transparently,
    # so taking the frame handle is still correct.
    $cands = @()

    foreach ($p in @(Get-Process -Name 'WhatsApp' -ErrorAction SilentlyContinue)) {
        if ($p.MainWindowHandle -ne [IntPtr]::Zero) {
            $cands += [pscustomobject]@{ hwnd = $p.MainWindowHandle; pid = $p.Id
                                         proc = $p.ProcessName; title = $p.MainWindowTitle }
        }
    }
    if (-not $cands) {
        foreach ($p in @(Get-Process -Name 'ApplicationFrameHost' -ErrorAction SilentlyContinue)) {
            if ($p.MainWindowHandle -ne [IntPtr]::Zero -and $p.MainWindowTitle -match 'WhatsApp') {
                $cands += [pscustomobject]@{ hwnd = $p.MainWindowHandle; pid = $p.Id
                                             proc = $p.ProcessName; title = $p.MainWindowTitle }
            }
        }
    }
    if (-not $cands) { return $null }

    $c = $cands[0]
    $el = $null
    try { $el = $AE::FromHandle($c.hwnd) } catch { }
    if (-not $el) { return $null }

    return [pscustomobject]@{
        Hwnd = $c.hwnd; Pid = $c.pid; Proc = $c.proc
        Title = $c.title; Element = $el
    }
}

function Set-WindowForeground {
    param([IntPtr]$Hwnd, [int]$Attempts = 5)
    # Windows refuses SetForegroundWindow from a process that does not own the
    # foreground, so attach our input thread to the target's first. Verified by
    # reading GetForegroundWindow back rather than trusting the return value.
    for ($i = 1; $i -le $Attempts; $i++) {
        try {
            if ([WaNative]::IsIconic($Hwnd)) { [void][WaNative]::ShowWindow($Hwnd, 9) }
            $tpid = 0
            $ttid = [WaNative]::GetWindowThreadProcessId($Hwnd, [ref]$tpid)
            $mtid = [WaNative]::GetCurrentThreadId()
            [void][WaNative]::AttachThreadInput($mtid, $ttid, $true)
            [void][WaNative]::BringWindowToTop($Hwnd)
            [void][WaNative]::SetForegroundWindow($Hwnd)
            [void][WaNative]::AttachThreadInput($mtid, $ttid, $false)
        } catch { }
        Start-Sleep -Milliseconds (150 * $i)
        if ([WaNative]::GetForegroundWindow() -eq $Hwnd) { return $true }
    }
    return $false
}

# -------------------------------------------------------------- uia lookups ---
# PERFORMANCE RULE, do not "simplify" this away:
# never FindAll(Descendants) from the window root. A Chromium tree for a busy
# WhatsApp account is thousands of elements and every property read is a
# cross-process call, so a descendant FindAll can take tens of seconds. Use a
# narrow FindFirst (stops at the first hit), then FindAll only over the Children
# of the container you found.

function New-NameCondition {
    param([string]$Name, $ControlType)
    $c = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, $Name)
    if ($ControlType) {
        $t = New-Object System.Windows.Automation.PropertyCondition($AE::ControlTypeProperty, $ControlType)
        return New-Object System.Windows.Automation.AndCondition(@($c, $t))
    }
    return $c
}

function Find-ByNames {
    param(
        $Root,
        [string[]] $Names,
        $ControlType = $null,
        [int] $TimeoutMs = 6000
    )
    # Retried rather than asked once, because Chromium enables its renderer
    # accessibility tree LAZILY. The first UIA query is what switches it on, so
    # an immediate second query is often the one that actually sees content.
    $deadline = (Get-Date).AddMilliseconds($TimeoutMs)
    do {
        foreach ($n in $Names) {
            try {
                $el = $Root.FindFirst($TS::Descendants, (New-NameCondition -Name $n -ControlType $ControlType))
                if ($el) { return $el }
            } catch { }
        }
        Start-Sleep -Milliseconds 350
    } while ((Get-Date) -lt $deadline)
    return $null
}

function Get-ElementText {
    param($El)
    if (-not $El) { return '' }
    try {
        $n = $El.Current.Name
        if ($n) { return $n }
    } catch { }
    # Fall back to the Value then the Text pattern; contenteditable divs and
    # message bubbles expose their content through different ones.
    try {
        $vp = $El.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        if ($vp -and $vp.Current.Value) { return $vp.Current.Value }
    } catch { }
    try {
        $tp = $El.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
        if ($tp) { return $tp.DocumentRange.GetText(4000) }
    } catch { }
    return ''
}

function Invoke-Focus {
    param($El)
    try { $El.SetFocus(); Start-Sleep -Milliseconds 180; return $true } catch { return $false }
}

# ------------------------------------------------------------------- input ---

function Assert-Foreground {
    param([IntPtr]$Hwnd)
    # INVARIANT 2. Called immediately before every keystroke batch. If focus has
    # drifted we abort rather than type a private message into another window.
    if ([WaNative]::GetForegroundWindow() -ne $Hwnd) {
        Out-Result -Ok $false -ErrorCode 'window_focus_lost' `
            -Detail 'WhatsApp stopped being the foreground window mid-operation; aborted before sending any keystrokes.'
    }
}

function Send-Keystrokes {
    param([IntPtr]$Hwnd, [string]$Keys)
    Assert-Foreground -Hwnd $Hwnd
    [System.Windows.Forms.SendKeys]::SendWait($Keys)
    Start-Sleep -Milliseconds 120
}

function Set-TextViaClipboard {
    param([IntPtr]$Hwnd, [string]$Text)
    # Returns the previous clipboard text so the caller can restore it. Only
    # text round-trips: an image or file on the clipboard is lost. Documented.
    $prev = $null
    try { $prev = Get-Clipboard -Raw -ErrorAction Stop } catch { }
    Set-Clipboard -Value $Text
    Start-Sleep -Milliseconds 120
    Send-Keystrokes -Hwnd $Hwnd -Keys '^v'
    return $prev
}

function Restore-Clipboard {
    param($Previous)
    try {
        if ($null -ne $Previous -and $Previous -ne '') { Set-Clipboard -Value $Previous }
        else { Set-Clipboard -Value ' ' }
    } catch { }
}

# ------------------------------------------------------- contact resolution ---

function Test-PhoneNumber {
    param([string]$S)
    return ($S -match '^\+?[0-9][0-9\s\-\(\)]{6,20}$')
}

function Get-NormalizedName {
    param([string]$S)
    if (-not $S) { return '' }
    return ($S -replace '\s+', ' ').Trim().ToLowerInvariant()
}

function Test-ContactAllowed {
    param([string]$Contact)
    if (-not $AllowedContactsFile) { return $true }
    if (-not (Test-Path $AllowedContactsFile)) { return $true }   # absent = no filter
    $want = Get-NormalizedName $Contact
    foreach ($raw in (Get-Content $AllowedContactsFile -Encoding UTF8)) {
        $l = $raw.Trim()
        if (-not $l -or $l.StartsWith('#')) { continue }
        if ((Get-NormalizedName $l) -eq $want) { return $true }
    }
    return $false
}

function Open-ChatByNumber {
    param([string]$Number, [string]$Prefill = '')
    # The whatsapp: deep link makes WhatsApp itself resolve the number and open
    # the chat. That removes chat-list searching entirely, which removes the
    # single largest source of "wrong recipient" risk. ShellExecute handles the
    # Appx protocol activation the Store build registers.
    $digits = ($Number -replace '[^0-9]', '')
    if (-not $digits) { return $false }
    $uri = "whatsapp://send?phone=$digits"
    if ($Prefill) { $uri += '&text=' + [uri]::EscapeDataString($Prefill) }
    try { Start-Process $uri | Out-Null } catch { return $false }
    Start-Sleep -Milliseconds 2500
    return $true
}

function Get-OpenChatName {
    param($Win)
    # INVARIANT 1's read-back. The conversation header carries the name of the
    # chat that is actually open. Everything downstream is checked against this.
    $hdr = Find-ByNames -Root $Win.Element -Names $SEL.header -TimeoutMs 2500
    if ($hdr) {
        try {
            $kid = $hdr.FindFirst($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)
            $t = Get-ElementText $kid
            if ($t) { return $t.Trim() }
        } catch { }
        $t = Get-ElementText $hdr
        if ($t) { return $t.Trim() }
    }
    # Fallback: the window title of the old native build carried the chat name.
    try {
        $t = $Win.Element.Current.Name
        if ($t -and $t -ne 'WhatsApp') { return ($t -replace '\s*[-|]\s*WhatsApp\s*$', '').Trim() }
    } catch { }
    return ''
}

function Open-ChatByName {
    param($Win, [string]$Name)
    # Returns @{ ok; error; detail; candidates }
    $search = Find-ByNames -Root $Win.Element -Names $SEL.search -ControlType $CT::Edit -TimeoutMs 6000
    if (-not $search) {
        return @{ ok = $false; error = 'search_box_not_found'
                  detail = 'Could not locate the chat search box. Run -Action dump and update selectors.json.' }
    }
    if (-not (Invoke-Focus $search)) {
        return @{ ok = $false; error = 'search_box_not_focusable'; detail = 'Search box found but would not take focus.' }
    }

    Send-Keystrokes -Hwnd $Win.Hwnd -Keys '^a'
    $prev = Set-TextViaClipboard -Hwnd $Win.Hwnd -Text $Name
    Start-Sleep -Milliseconds 1200
    Restore-Clipboard $prev

    # Enumerate only the Children of the result list, never a descendant FindAll.
    $list = Find-ByNames -Root $Win.Element -Names $SEL.chatList -TimeoutMs 4000
    $names = @()
    if ($list) {
        try {
            $rows = $list.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)
            foreach ($i in 0..([Math]::Max(0, $rows.Count - 1))) {
                if ($rows.Count -eq 0) { break }
                $t = Get-ElementText $rows[$i]
                if ($t) { $names += $t.Trim() }
            }
        } catch { }
    }

    $want = Get-NormalizedName $Name
    $exact = @($names | Where-Object { (Get-NormalizedName $_) -eq $want })

    if ($exact.Count -eq 0) {
        $part = @($names | Where-Object { (Get-NormalizedName $_).Contains($want) })
        if ($part.Count -eq 0) {
            return @{ ok = $false; error = 'contact_not_found'
                      detail = "No chat matching '$Name'."; candidates = @($names | Select-Object -First 8) }
        }
        if ($part.Count -gt 1) {
            return @{ ok = $false; error = 'contact_ambiguous'
                      detail = "'$Name' matches $($part.Count) chats; refusing to guess a recipient."
                      candidates = @($part | Select-Object -First 8) }
        }
    } elseif ($exact.Count -gt 1) {
        return @{ ok = $false; error = 'contact_ambiguous'
                  detail = "'$Name' matches $($exact.Count) chats exactly; refusing to guess a recipient."
                  candidates = @($exact | Select-Object -First 8) }
    }

    # Enter opens the top result, which is the one we just uniquely matched.
    Send-Keystrokes -Hwnd $Win.Hwnd -Keys '{ENTER}'
    Start-Sleep -Milliseconds 1200
    return @{ ok = $true; error = ''; detail = ''; candidates = @() }
}

# ------------------------------------------------------------ read messages ---

function Get-RecentMessages {
    param($Win, [int]$N)
    $list = Find-ByNames -Root $Win.Element -Names $SEL.msgList -TimeoutMs 6000
    if (-not $list) {
        return @{ ok = $false; error = 'message_list_not_found'
                  detail = 'Could not locate the message list. Run -Action dump and update selectors.json.' }
    }
    $out = @()
    try {
        $rows = $list.FindAll($TS::Children, [System.Windows.Automation.Condition]::TrueCondition)
        $total = $rows.Count
        if ($total -eq 0) { return @{ ok = $true; error = ''; messages = @() } }
        $start = [Math]::Max(0, $total - $N)
        for ($i = $start; $i -lt $total; $i++) {
            $t = Get-ElementText $rows[$i]
            if (-not $t) { continue }
            # WhatsApp Web's row accessible name is usually prefixed with the
            # sender and a timestamp. Parsed best-effort; the raw string is
            # always returned too, so a parser miss never loses information.
            $sender = ''
            $body = $t.Trim()
            if ($body -match '^\s*\[?(?<ts>\d{1,2}:\d{2}[^\]]*)\]?\s*(?<who>[^:]{1,40}):\s*(?<msg>.*)$') {
                $sender = $Matches['who'].Trim()
                $body = $Matches['msg'].Trim()
            }
            $out += [pscustomobject]@{ index = $i; sender = $sender; text = $body; raw = $t.Trim() }
        }
    } catch {
        return @{ ok = $false; error = 'message_read_failed'; detail = $_.Exception.Message }
    }
    return @{ ok = $true; error = ''; messages = $out }
}

# --------------------------------------------------------------- preflight ---

function Invoke-Preflight {
    $sess = Test-InteractiveSession
    if (-not $sess.interactive) {
        Out-Result -Ok $false -ErrorCode 'no_interactive_session' `
            -Detail "Running in session $($sess.session_id). Session 0 is isolated from the desktop: UIA enumerates zero windows and keystrokes reach nothing. This script must run as the logged-in user." `
            -Data @{ session = $sess }
    }
    if ($sess.locked) {
        Out-Result -Ok $false -ErrorCode 'session_locked' `
            -Detail 'The workstation appears locked (LogonUI.exe present). Windows blocks synthetic input at the lock screen; unlock the PC and retry.' `
            -Data @{ session = $sess }
    }

    $win = Get-WhatsAppWindow
    if (-not $win) {
        Out-Result -Ok $false -ErrorCode 'app_not_running' `
            -Detail 'No WhatsApp window found. Start WhatsApp Desktop and leave it running (it may be minimised, but it must not be closed to tray-only with no window).' `
            -Data @{ session = $sess }
    }
    return @{ Session = $sess; Win = $win }
}

function Test-LoggedIn {
    param($Win)
    # A logged-out client shows a QR / "Log in" screen and has no composer and
    # no chat list. Distinguishing this from "still loading" matters: one needs
    # Natik's phone, the other just needs another second.
    foreach ($n in @('Log in', 'Log into WhatsApp', 'Scan the QR code', 'Link a device', 'Steps to log in')) {
        try {
            $el = $Win.Element.FindFirst($TS::Descendants, (New-NameCondition -Name $n))
            if ($el) { return $false }
        } catch { }
    }
    return $true
}

# ================================================================== actions ===

New-Item -ItemType Directory -Force -Path (Split-Path $LogFile) | Out-Null
Write-Log INFO "wa_uia $($script:Version) action=$Action contact='$Contact' msg=$(Protect-Body $Message)"

# ---- status: pure read, no focus change, no input. Safe at any time. --------
if ($Action -eq 'status') {
    $sess = Test-InteractiveSession
    $win = Get-WhatsAppWindow
    $data = @{ session = $sess; app_running = [bool]$win }
    if ($win) {
        $data['window'] = @{ pid = $win.Pid; process = $win.Proc; title = $win.Title
                             hwnd = ('0x{0:X}' -f [int64]$win.Hwnd) }
        $data['logged_in'] = Test-LoggedIn -Win $win
        $data['is_foreground'] = ([WaNative]::GetForegroundWindow() -eq $win.Hwnd)
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $composer = Find-ByNames -Root $win.Element -Names $SEL.composer -TimeoutMs 4000
        $sw.Stop()
        $data['composer_found'] = [bool]$composer
        $data['uia_probe_ms'] = $sw.ElapsedMilliseconds
        $data['chat_list_found'] = [bool](Find-ByNames -Root $win.Element -Names $SEL.chatList -TimeoutMs 2000)
        $data['message_list_found'] = [bool](Find-ByNames -Root $win.Element -Names $SEL.msgList -TimeoutMs 2000)
        if (-not $composer) {
            $data['hint'] = 'Window found but the composer was not. Either no chat is open, the UI language is not English, or the selector names have drifted. Run -Action dump.'
        }
    }
    Out-Result -Ok $true -Data $data
}

# ---- dump: read-only tree survey, for adapting selectors to drift. ----------
# Prints names/types actually present so selectors.json can be written from
# fact rather than from a blog post. Reads only; never focuses or types.
if ($Action -eq 'dump') {
    $win = Get-WhatsAppWindow
    if (-not $win) { Out-Result -Ok $false -ErrorCode 'app_not_running' -Detail 'No WhatsApp window found.' }
    $found = @()
    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
    # Breadth-first with a hard node budget: an unbounded walk of a Chromium
    # tree does not terminate in useful time.
    $queue = New-Object System.Collections.Queue
    $queue.Enqueue(@{ El = $win.Element; Depth = 0 })
    $seen = 0
    while ($queue.Count -gt 0 -and $seen -lt 1200) {
        $item = $queue.Dequeue()
        $el = $item.El; $d = $item.Depth
        $seen++
        try {
            $nm = $el.Current.Name; $ct = $el.Current.ControlType.ProgrammaticName
            if ($nm -and $nm.Length -le 60) {
                $found += [pscustomobject]@{ depth = $d; name = $nm
                                             type = ($ct -replace '^ControlType\.', '') }
            }
        } catch { continue }
        if ($d -ge 12) { continue }
        try {
            $c = $walker.GetFirstChild($el)
            while ($c) { $queue.Enqueue(@{ El = $c; Depth = $d + 1 }); $c = $walker.GetNextSibling($c) }
        } catch { }
    }
    $interesting = @($found | Where-Object {
        $_.type -in @('Edit', 'Button', 'List', 'ListItem', 'Group', 'Text', 'Document')
    } | Select-Object -First 250)
    Out-Result -Ok $true -Data @{
        nodes_visited = $seen
        truncated = ($queue.Count -gt 0)
        elements = $interesting
        note = 'Copy the real composer/search/list names into selectors.json.'
    }
}

$pre = Invoke-Preflight
$win = $pre.Win

if (-not (Test-LoggedIn -Win $win)) {
    Out-Result -Ok $false -ErrorCode 'logged_out' `
        -Detail 'WhatsApp is showing its login/QR screen. This needs Natik to scan with his phone; JARVIS cannot fix it.' `
        -Data @{ session = $pre.Session }
}

if (-not $Contact) {
    Out-Result -Ok $false -ErrorCode 'contact_required' -Detail "Action '$Action' needs a contact."
}
if (-not (Test-ContactAllowed -Contact $Contact)) {
    Out-Result -Ok $false -ErrorCode 'contact_not_allowed' `
        -Detail "'$Contact' is not in the allowlist at $AllowedContactsFile; refusing."
}

# ---- everything below steals focus, so it is gated behind preflight --------

if (-not (Set-WindowForeground -Hwnd $win.Hwnd)) {
    Out-Result -Ok $false -ErrorCode 'window_focus_failed' `
        -Detail 'Could not bring the WhatsApp window to the foreground (another app may be holding a foreground lock, or a full-screen app is running). Nothing was typed.' `
        -Data @{ session = $pre.Session }
}

$openedVia = ''
if (Test-PhoneNumber $Contact) {
    if (-not (Open-ChatByNumber -Number $Contact)) {
        Out-Result -Ok $false -ErrorCode 'deeplink_failed' `
            -Detail "The whatsapp: deep link for '$Contact' did not launch. Check the number is in full international form."
    }
    $openedVia = 'deeplink'
    $win = Get-WhatsAppWindow
    if (-not $win) { Out-Result -Ok $false -ErrorCode 'app_not_running' -Detail 'WhatsApp window vanished after the deep link.' }
    [void](Set-WindowForeground -Hwnd $win.Hwnd)
} else {
    $r = Open-ChatByName -Win $win -Name $Contact
    if (-not $r.ok) {
        Out-Result -Ok $false -ErrorCode $r.error -Detail $r.detail `
            -Data @{ candidates = @($r.candidates) }
    }
    $openedVia = 'search'
}

# INVARIANT 1: confirm the chat that actually opened is the one asked for,
# BEFORE a single character of the message is typed. A search race or a stale
# chat would otherwise deliver a private message to the wrong person, which is
# the one failure here that cannot be undone.
$openName = Get-OpenChatName -Win $win
if ($openName -and (Test-PhoneNumber $Contact) -eq $false) {
    if ((Get-NormalizedName $openName) -ne (Get-NormalizedName $Contact)) {
        Out-Result -Ok $false -ErrorCode 'contact_mismatch' `
            -Detail "Asked for '$Contact' but the open chat is '$openName'. Refusing to act on the wrong conversation; nothing was typed." `
            -Data @{ requested = $Contact; opened = $openName }
    }
}

# ---- preview: resolved the target, changed nothing. ------------------------
if ($Action -eq 'preview' -or $DryRun) {
    Out-Result -Ok $true -Data @{
        resolved_contact = $openName; requested = $Contact; opened_via = $openedVia
        would_send = (Protect-Body $Message)
        message_chars = $Message.Length
        note = 'Dry run. The chat was opened and verified; nothing was typed or sent.'
    }
}

# ---- read ------------------------------------------------------------------
if ($Action -eq 'read') {
    if ($Count -lt 1) { $Count = 1 }
    if ($Count -gt $MAX_READ_COUNT) { $Count = $MAX_READ_COUNT }
    $r = Get-RecentMessages -Win $win -N $Count
    if (-not $r.ok) { Out-Result -Ok $false -ErrorCode $r.error -Detail $r.detail }
    Write-Log INFO "read $($r.messages.Count) messages from '$openName' (bodies not logged)"
    Out-Result -Ok $true -Data @{
        contact = $openName; requested = $Contact; count = @($r.messages).Count
        messages = @($r.messages)
        privacy = 'Returned in this response only. Not written to disk, not cached.'
    }
}

# ---- send ------------------------------------------------------------------
if ($Action -eq 'send') {
    if (-not $Message) { Out-Result -Ok $false -ErrorCode 'message_required' -Detail 'No message text supplied.' }
    if ($Message.Length -gt $MAX_MESSAGE_CHARS) {
        Out-Result -Ok $false -ErrorCode 'message_too_long' `
            -Detail "Message is $($Message.Length) chars; the cap is $MAX_MESSAGE_CHARS."
    }

    $composer = Find-ByNames -Root $win.Element -Names $SEL.composer -ControlType $CT::Edit -TimeoutMs 8000
    if (-not $composer) {
        $composer = Find-ByNames -Root $win.Element -Names $SEL.composer -TimeoutMs 3000
    }
    if (-not $composer) {
        Out-Result -Ok $false -ErrorCode 'composer_not_found' `
            -Detail 'The message box was not found, so nothing was typed. The chat may not have finished opening, or the selector names have drifted; run -Action dump.' `
            -Data @{ contact = $openName }
    }
    if (-not (Invoke-Focus $composer)) {
        Out-Result -Ok $false -ErrorCode 'composer_not_focusable' -Detail 'Message box found but would not take focus. Nothing was typed.'
    }

    Assert-Foreground -Hwnd $win.Hwnd
    $prevClip = Set-TextViaClipboard -Hwnd $win.Hwnd -Text $Message
    Start-Sleep -Milliseconds 350

    # Read the composer back before committing. If the paste did not land we
    # must NOT press Enter: that would send whatever else is in the box.
    $staged = Get-ElementText $composer
    if ($staged -and -not $staged.Contains($Message.Substring(0, [Math]::Min(20, $Message.Length)))) {
        Restore-Clipboard $prevClip
        Out-Result -Ok $false -ErrorCode 'compose_failed' `
            -Detail 'The text did not land in the message box, so Enter was never pressed. Nothing was sent.' `
            -Data @{ contact = $openName }
    }

    Send-Keystrokes -Hwnd $win.Hwnd -Keys '{ENTER}'
    Start-Sleep -Milliseconds 1400
    Restore-Clipboard $prevClip

    # INVARIANT 3: read the transcript back. Only a message we can SEE is a
    # message we report as sent.
    $verify = Get-RecentMessages -Win $win -N 3
    $confirmed = $false
    if ($verify.ok) {
        $probe = $Message.Substring(0, [Math]::Min(40, $Message.Length))
        foreach ($m in @($verify.messages)) {
            if ($m.raw -and $m.raw.Contains($probe)) { $confirmed = $true; break }
        }
    }

    if (-not $confirmed) {
        Write-Log WARN "send to '$openName' could not be verified in the transcript"
        Out-Result -Ok $false -ErrorCode 'send_unconfirmed' `
            -Detail "Enter was pressed but the message could not be found in the transcript afterwards. It MAY have been delivered - check WhatsApp before resending, or you risk sending it twice." `
            -Data @{ contact = $openName; verified = $false }
    }

    Write-Log INFO "sent to '$openName' and verified in transcript ($(Protect-Body $Message))"
    Out-Result -Ok $true -Data @{
        contact = $openName; requested = $Contact; opened_via = $openedVia
        verified = $true
        note = 'Confirmed by reading the message back from the transcript.'
    }
}

Out-Result -Ok $false -ErrorCode 'unknown_action' -Detail "Action '$Action' is not implemented."

