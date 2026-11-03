# Runs IN the user's desktop session (via the "Desktop - Set Wallpaper" task):
# static fallback wallpaper + Lively live wallpaper.
$still = 'C:\Users\Naiti\Pictures\Wallpaper\aurora-lock.jpg'
$live  = 'C:\Users\Naiti\Pictures\Wallpaper\aurora\index.html'
Add-Type -Namespace W -Name P -MemberDefinition '[DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int SystemParametersInfo(int a,int b,string c,int d);'
Set-ItemProperty 'HKCU:\Control Panel\Desktop' WallpaperStyle '10'   # fill
[W.P]::SystemParametersInfo(20, 0, $still, 3) | Out-Null
$lively = 'C:\Program Files\Lively Wallpaper\Lively.exe'
if (-not (Get-Process Lively -ea 0)) { Start-Process $lively; Start-Sleep 12 }
& $lively setwp --file $live
