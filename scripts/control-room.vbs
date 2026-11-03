' Launches control-room.ps1 with no console window at all (a .lnk to
' powershell -WindowStyle Hidden still leaves a minimised console on the taskbar).
CreateObject("WScript.Shell").Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\ServerData\Stacks\scripts\control-room.ps1", 0, False
