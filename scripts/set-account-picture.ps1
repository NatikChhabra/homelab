# Sets the Windows account picture for Naiti from C:\ServerData\Branding\Image*.png.
# Runs as SYSTEM (the AccountPicture key is not writable by admins). 2026-11-01.
$sid = (New-Object System.Security.Principal.NTAccount('Naiti')).Translate([System.Security.Principal.SecurityIdentifier]).Value
$dir = "C:\Users\Public\AccountPictures\$sid"
New-Item -ItemType Directory -Force $dir | Out-Null
$key = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AccountPicture\Users\$sid"
New-Item -Force $key | Out-Null
foreach ($s in 32,40,48,64,96,192,208,240,424,448,1080) {
  Copy-Item "C:\ServerData\Branding\Image$s.png" "$dir\Image$s.png" -Force
  Set-ItemProperty $key "Image$s" "$dir\Image$s.png"
}
"done $sid $((Get-Item $key).ValueCount) values" | Set-Content C:\ServerData\Stacks\logs\account-picture.txt
