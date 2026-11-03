# Captures every monitor of the signed-in desktop to logs\screen.png.
# Runs via the "Desktop - Screenshot" task (Interactive), because SSH sessions
# live in session 0 and cannot see the desktop.
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
Add-Type -Namespace W -Name D -MemberDefinition '[DllImport("user32.dll")] public static extern bool SetProcessDPIAware();'
[W.D]::SetProcessDPIAware() | Out-Null
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save('C:\ServerData\Stacks\logs\screen.png', [System.Drawing.Imaging.ImageFormat]::Png)
"$($b.Width)x$($b.Height) $(Get-Date -Format s)" | Set-Content 'C:\ServerData\Stacks\logs\screen.txt'
