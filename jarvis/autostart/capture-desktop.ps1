# Capture the interactive desktop (session 1) to a PNG.
#
# WHY THIS EXISTS
# Work on this box happens over SSH, which lands in session 0. Session 0 has no
# display: Win32_VideoController reports the real 1920x1080 panel, but
# System.Windows.Forms.Screen inside an SSH session reports a fake 1024x768
# "WinDisc" adapter, and windows belonging to session 1 cannot be enumerated
# from session 0 at all (separate window stations).
#
# So "is the HUD actually on screen?" is not answerable from the SSH session by
# any amount of process- or config-inspection. Running this as a scheduled task
# with LogonType=Interactive puts it in session 1, where it can see the real
# desktop, and it writes a file session 0 can then read. That file is the only
# honest evidence that a desktop skin rendered.

param([string]$Out = 'C:\ServerData\Stacks\jarvis\desktop-capture.png')

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.X, $b.Y, 0, 0, $bmp.Size)
$g.Dispose()
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()

"captured $($b.Width)x$($b.Height) -> $Out" |
    Out-File -FilePath 'C:\ServerData\Stacks\jarvis\desktop-capture.log' -Encoding utf8
