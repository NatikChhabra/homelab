@echo off
setlocal enabledelayedexpansion

REM ---------------------------------------------------------------------------
REM Open the Glances dashboard fullscreen in Edge kiosk mode at login.
REM
REM Docker's cold-boot time on this host is not predictable, so this polls
REM Glances until it actually answers instead of sleeping a fixed 20s and
REM hoping. Edge only launches once the dashboard is genuinely serving, so it
REM can never land on a connection-error page.
REM
REM Gives up after MAX_TRIES * DELAY seconds (default 60 * 5s = 5 minutes) and
REM writes the outcome to LOGFILE either way, so a silent no-show is auditable
REM after the fact rather than invisible.
REM ---------------------------------------------------------------------------

REM Absolute paths on purpose: if anything ahead of System32 on PATH provides a
REM GNU "timeout" or "curl" (Git Bash does), the shadowed binaries take
REM different arguments and the wait silently degrades into a busy loop.
REM ping is used as the sleep rather than timeout.exe: timeout.exe aborts with
REM "Input redirection is not supported" whenever stdin is not a real console,
REM which would turn the wait into an instant busy loop.
set "CURL=%SystemRoot%\System32\curl.exe"
set "PING=%SystemRoot%\System32\ping.exe"

set "URL=http://localhost:61208"
set "LOGFILE=C:\ServerData\Stacks\logs\glances-kiosk.log"
set "MAX_TRIES=60"
set "DELAY=5"

if not exist "C:\ServerData\Stacks\logs" mkdir "C:\ServerData\Stacks\logs"

echo [%DATE% %TIME%] kiosk start, waiting for %URL% >> "%LOGFILE%"

set /a tries=0
:wait
set /a tries+=1
"%CURL%" -s -o nul --max-time 5 "%URL%"
if not errorlevel 1 goto ready
if !tries! GEQ %MAX_TRIES% goto giveup
set /a pingcount=%DELAY% + 1
"%PING%" -n !pingcount! 127.0.0.1 >nul
goto wait

:ready
set /a waited=!tries! * %DELAY%
echo [%DATE% %TIME%] Glances responded after ~!waited!s (attempt !tries!), launching Edge >> "%LOGFILE%"
start "" msedge --kiosk "%URL%" --edge-kiosk-type=fullscreen --no-first-run
exit /b 0

:giveup
set /a waited=!tries! * %DELAY%
echo [%DATE% %TIME%] GAVE UP: Glances did not respond after ~!waited!s (!tries! attempts). Edge not launched. >> "%LOGFILE%"
exit /b 1
