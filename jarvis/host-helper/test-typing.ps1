# test-typing.ps1 - proves the input path end to end against a throwaway target.
#
# The earlier round-trip used Notepad and hit two problems worth recording:
#   1. Windows 11 Notepad is TABBED. Launching it does not create a new window,
#      it adds a tab to the existing one, so "focus the window titled Notepad"
#      landed on a file Natik already had open. Never aim automated typing at a
#      window that might contain someone's work.
#   2. The clipboard read was nested in a child runspace and returned empty.
#
# This version owns its target: a WinForms TextBox this script creates, types
# into via the agent, and reads back directly. Nothing else on the desktop is
# touched, and the answer is unambiguous.
#
# MUST be run with -STA (WinForms and the clipboard both require it) and in the
# INTERACTIVE session, or the window is created on a desktop nobody can focus.

param([int]$Port = 8199)

$ErrorActionPreference = 'Continue'
$base = "http://127.0.0.1:$Port"

$tok = $null
foreach ($raw in (Get-Content 'C:\ServerData\Stacks\jarvis\secrets.env' -Encoding UTF8)) {
    $kv = $raw.Trim() -split '=', 2
    if ($kv.Length -eq 2 -and $kv[0].Trim() -eq 'JARVIS_WOL_TOKEN') { $tok = $kv[1].Trim() }
}
$H = @{ Authorization = "Bearer $tok" }
function Call {
    param([string]$Method, [string]$Path, $Body = $null)
    $p = @{ Uri = "$base$Path"; Method = $Method; Headers = $H; TimeoutSec = 30 }
    if ($null -ne $Body) { $p.Body = ($Body | ConvertTo-Json -Compress); $p.ContentType = 'application/json' }
    try { return Invoke-RestMethod @p } catch { return [pscustomobject]@{ error = $_.Exception.Message } }
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

# The marker carries every SendKeys control character. If the agent used
# SendKeys these would be eaten or turned into modifiers; surviving intact is
# the proof that KEYEVENTF_UNICODE is doing the work.
$MARKER = 'JARVIS 12345 +^%~(){}[] done'

$form = New-Object Windows.Forms.Form
$form.Text = 'JARVIS INPUT TEST - do not type here'
$form.Size = New-Object Drawing.Size(600, 200)
$form.TopMost = $true
$form.StartPosition = 'CenterScreen'
$box = New-Object Windows.Forms.TextBox
$box.Multiline = $true
$box.Dock = 'Fill'
$box.Font = New-Object Drawing.Font('Consolas', 12)
$form.Controls.Add($box)

# Capture the ACTUAL key events the control receives, with modifiers. This is
# the honest test of a key combo: a multiline edit control does not act on
# Ctrl+A as select-all (a classic Win32 quirk), so checking SelectedText would
# test the control, not the agent. KeyData contains e.g. "A, Control" and proves
# the agent held Ctrl down across the A press.
$script:keylog = New-Object System.Collections.ArrayList
$box.Add_KeyDown({ [void]$script:keylog.Add("$($_.KeyData)") })

$script:result = $null

$timer = New-Object Windows.Forms.Timer
$timer.Interval = 1200
$timer.Add_Tick({
    $timer.Stop()
    $box.Focus() | Out-Null
    [Windows.Forms.Application]::DoEvents()

    $t = Call POST '/pc/input/type' @{ text = $MARKER }
    Start-Sleep -Milliseconds 700
    [Windows.Forms.Application]::DoEvents()
    $typed = $box.Text

    # Key combos, verified by the events the control actually received. ctrl+a
    # then ctrl+c: a real editor would select and copy; here we only need proof
    # the modifier+key arrived, which KeyData gives us regardless of the control.
    $script:keylog.Clear()
    $null = Call POST '/pc/input/key' @{ key = 'a'; modifiers = @('ctrl') }
    Start-Sleep -Milliseconds 400
    [Windows.Forms.Application]::DoEvents()
    $ctrlA = @($script:keylog) -join ' | '

    $script:keylog.Clear()
    $null = Call POST '/pc/input/key' @{ key = 'c'; modifiers = @('ctrl', 'shift') }
    Start-Sleep -Milliseconds 400
    [Windows.Forms.Application]::DoEvents()
    $ctrlShiftC = @($script:keylog) -join ' | '
    # And that a plain key with no modifier reaches the app: End then more text.
    $null = Call POST '/pc/input/key' @{ key = 'end' }
    Start-Sleep -Milliseconds 300
    $null = Call POST '/pc/input/type' @{ text = '!' }
    Start-Sleep -Milliseconds 500
    [Windows.Forms.Application]::DoEvents()
    $afterEnd = $box.Text

    $script:result = [pscustomobject]@{
        expected      = $MARKER
        typed         = $typed
        typed_ok      = ($typed -eq $MARKER)
        events_sent   = $t.events_sent
        ctrl_a_events = $ctrlA
        ctrl_a_ok     = ($ctrlA -match 'A, Control' -or $ctrlA -match 'Control, A')
        ctrl_shift_c_events = $ctrlShiftC
        ctrl_shift_c_ok = ($ctrlShiftC -match 'C' -and $ctrlShiftC -match 'Control' -and $ctrlShiftC -match 'Shift')
        after_end_key = $afterEnd
        end_key_ok    = ($afterEnd -eq ($MARKER + '!'))
    }
    $form.Close()
})
$timer.Start()
[Windows.Forms.Application]::Run($form)

$r = $script:result
if (-not $r) { Write-Host 'FAIL  test never ran'; exit 1 }
function Say($ok, $m) { Write-Host ("{0}  {1}" -f $(if ($ok) {'PASS'} else {'FAIL'}), $m) }
Write-Host ''
Say $r.typed_ok  "type: $($r.events_sent) input events"
Write-Host "        expected: $($r.expected)"
Write-Host "        received: $($r.typed)"
Say $r.ctrl_a_ok "ctrl+a delivered as key event -> [$($r.ctrl_a_events)]"
Say $r.ctrl_shift_c_ok "ctrl+shift+c delivered as key event -> [$($r.ctrl_shift_c_events)]"
Say $r.end_key_ok "unmodified 'end' key then '!' -> '$($r.after_end_key)'"
